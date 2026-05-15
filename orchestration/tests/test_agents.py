"""Tests for agent resolution and run diagnostics."""

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from orchestration import agents as agents_module
from orchestration.agents import _classify_run_outcome, _compact_learnings_artifact_if_needed, _configured_agent_sound_name, _parse_agent_md, _play_agent_sound, _resolve_agent_file, _resolve_agent_sound_file, _runtime_reported_timeout, count_active_agent_runs, get_agent_run, get_global_sound_mute, list_agent_runs, retry_agent_run, run_agent, set_global_sound_mute
from orchestration.locks import acquire_lock, lock_status
from orchestration.artifacts import create_artifact, list_artifacts, read_artifact
from orchestration.tasks import create_task, read_task, _save_task
from orchestration.workstreams import create_workstream, save_workstream
from orchestration.models import RetryConfig


@pytest.fixture(autouse=True)
def _clear_runtime_model_env(monkeypatch):
    monkeypatch.delenv("ORCHESTRATION_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("ORCHESTRATION_CLINE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLINE_DEFAULT_LLM", raising=False)
    monkeypatch.delenv("CLINE_HIGH_LLM", raising=False)
    monkeypatch.delenv("CLINE_MEDIUM_LLM", raising=False)
    monkeypatch.delenv("CLINE_LOW_LLM", raising=False)
    monkeypatch.delenv("ORCHESTRATION_CLINE_VERBOSE", raising=False)
    monkeypatch.setenv("WORKSTREAM_ROOT", "")
    monkeypatch.setenv("ARTIFACT_ROOT", "")
    monkeypatch.setenv("ARTICACT_ROOT", "")
    monkeypatch.setenv("AUDIO_FILE_PATH", "")
    monkeypatch.setenv("DEFAULT_AGENT_START_SOUND", "")
    monkeypatch.setenv("DEFAULT_AGENT_FINISHED_SOUND", "")
    monkeypatch.setenv("DEFAULT_AGENT_ERROR_SOUND", "")


def _write_run_meta(workspace: str, run_id: str, payload: dict) -> None:
    agents_module._write_run_meta(workspace, run_id, payload)


def _active_agents_path(workspace: str) -> str:
    return agents_module._active_agents_path(workspace)


def _process_locks_dir(workspace: str) -> str:
    return os.path.join(agents_module._state_dir(workspace), "process_locks")


def _run_log_path(workspace: str, run_id: str) -> str:
    return os.path.join(agents_module._agent_runs_dir(workspace), f"{run_id}.log")


def _base_run(run_id: str, workstream_id: str, task_ids: list) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "run_id": run_id,
        "agent": "SEO Indexer",
        "agent_ref": "seo_indexer",
        "workstream_id": workstream_id,
        "workstream_path": "A / B",
        "task_ids": task_ids,
        "tasks": [],
        "prompt": "p",
        "system_prompt": "s",
        "log_path": None,
        "started_at": now,
        "ended_at": now,
        "status": "failed",
        "exit_code": 1,
    }


def test_get_agent_run_retry_info_standalone_not_eligible(workspace):
    ws = create_workstream(name="Standalone WS", base_dir=workspace)
    run_id = "run-standalone"
    _write_run_meta(workspace, run_id, _base_run(run_id, ws.id, []))

    details = get_agent_run(run_id, base_dir=workspace)
    retry = details["retry"]

    assert retry["eligible"] is False
    assert retry["reason"] == "Standalone run (no task binding)"
    assert retry["retries_remaining"] is None
    assert retry["next_retry_at"] is None


def test_get_agent_run_retry_info_task_bound_includes_remaining_and_next_retry(workspace):
    ws = create_workstream(name="Retry WS", base_dir=workspace)
    ws.retry = RetryConfig(max_retries=4, backoff="linear", base_seconds=30)
    save_workstream(ws, workspace)

    task = create_task(ws.id, title="T", base_dir=workspace)
    task_obj = read_task(task.id, workspace)
    task_obj.retry_count = 2
    task_obj.last_failure_at = datetime.now(timezone.utc).isoformat()
    task_obj.scheduled_at = datetime.now(timezone.utc).isoformat()
    task_obj.scheduled_action = {"type": "run_agent", "agent": "seo_indexer"}
    _save_task(task_obj, workspace)

    run_id = "run-task"
    _write_run_meta(workspace, run_id, _base_run(run_id, ws.id, [task.id]))

    details = get_agent_run(run_id, base_dir=workspace)
    retry = details["retry"]

    assert retry["eligible"] is True
    assert retry["task_id"] == task.id
    assert retry["max_retries"] == 4
    assert retry["retries_remaining"] == 2
    assert retry["next_retry_at"] == task_obj.scheduled_at


def test_get_agent_run_retry_info_multi_task_not_eligible(workspace):
    ws = create_workstream(name="Multi WS", base_dir=workspace)
    t1 = create_task(ws.id, title="T1", base_dir=workspace)
    t2 = create_task(ws.id, title="T2", base_dir=workspace)

    run_id = "run-multi"
    _write_run_meta(workspace, run_id, _base_run(run_id, ws.id, [t1.id, t2.id]))

    details = get_agent_run(run_id, base_dir=workspace)
    retry = details["retry"]

    assert retry["eligible"] is False
    assert "Multi-task run" in retry["reason"]
    assert retry["next_retry_at"] is None


def test_retry_agent_run_reuses_recorded_context(workspace):
    ws = create_workstream(name="Retry WS", base_dir=workspace)
    task = create_task(ws.id, title="T", base_dir=workspace)

    run_id = "run-retry"
    meta = _base_run(run_id, ws.id, [task.id])
    meta["agent"] = "SEO Indexer"
    meta["agent_ref"] = "seo_indexer"
    _write_run_meta(workspace, run_id, meta)

    with patch("orchestration.agents.run_agent") as mock_run_agent:
        mock_run_agent.return_value = {"run_id": "new-run", "agent": "SEO Indexer"}

        result = retry_agent_run(run_id, base_dir=workspace)

    mock_run_agent.assert_called_once_with(
        "seo_indexer",
        task_ids=[task.id],
        workstream_id=ws.id,
        base_dir=workspace,
        allow_paused_workstream=False,
        retried_from_run_id=run_id,
    )
    assert result["run_id"] == "new-run"
    assert result["retried_from_run_id"] == run_id


def test_retry_agent_run_can_override_paused_workstream(workspace):
    ws = create_workstream(name="Product Development", base_dir=workspace)
    task = create_task(ws.id, title="T", base_dir=workspace)

    run_id = "run-retry-paused"
    meta = _base_run(run_id, ws.id, [task.id])
    meta["agent"] = "SEO Indexer"
    meta["agent_ref"] = "seo_indexer"
    _write_run_meta(workspace, run_id, meta)

    with patch("orchestration.agents.run_agent") as mock_run_agent:
        mock_run_agent.return_value = {"run_id": "new-run", "agent": "SEO Indexer"}

        result = retry_agent_run(run_id, base_dir=workspace, allow_paused_workstream=True)

    mock_run_agent.assert_called_once_with(
        "seo_indexer",
        task_ids=[task.id],
        workstream_id=ws.id,
        base_dir=workspace,
        allow_paused_workstream=True,
        retried_from_run_id=run_id,
    )
    assert result["run_id"] == "new-run"
    assert result["retried_from_run_id"] == run_id


def test_retry_agent_run_persists_retry_lineage(workspace):
    ws = create_workstream(name="Retry WS", base_dir=workspace)
    task = create_task(ws.id, title="T", base_dir=workspace)

    run_id = "run-parent"
    meta = _base_run(run_id, ws.id, [task.id])
    _write_run_meta(workspace, run_id, meta)

    with patch("orchestration.agents.run_agent") as mock_run_agent:
        mock_run_agent.return_value = {"run_id": "run-child", "agent": "SEO Indexer"}

        retry_agent_run(run_id, base_dir=workspace)

    parent = get_agent_run(run_id, base_dir=workspace)["run"]
    assert parent["retried_to_run_ids"] == ["run-child"]


def test_get_agent_run_includes_retry_lineage(workspace):
    ws = create_workstream(name="Retry WS", base_dir=workspace)
    run_id = "run-lineage"
    meta = _base_run(run_id, ws.id, [])
    meta["retried_from_run_id"] = "run-older"
    meta["retried_to_run_ids"] = ["run-newer-1", "run-newer-2"]
    _write_run_meta(workspace, run_id, meta)

    details = get_agent_run(run_id, base_dir=workspace)

    assert details["run"]["retried_from_run_id"] == "run-older"
    assert details["run"]["retried_to_run_ids"] == ["run-newer-1", "run-newer-2"]


def test_count_active_agent_runs_ignores_dead_pids_and_prunes_state(workspace):
    state_dir = os.path.dirname(_active_agents_path(workspace))
    os.makedirs(state_dir, exist_ok=True)
    active_path = _active_agents_path(workspace)

    with open(active_path, "w", encoding="utf-8") as f:
        f.write(
            "runs:\n"
            "- run_id: dead-run\n"
            "  agent: Coder\n"
            "  agent_ref: coder\n"
            "  workstream_id: ws-1\n"
            "  task_ids: [t-1]\n"
            "  pid: 99999999\n"
            "  started_at: '2026-01-01T00:00:00+00:00'\n"
        )

    count = count_active_agent_runs("ws-1", "coder", base_dir=workspace)
    assert count == 0

    with open(active_path, "r", encoding="utf-8") as f:
        remaining = f.read()
    assert "dead-run" not in remaining


def test_count_active_agent_runs_kills_expired_process_lock_runs(workspace, monkeypatch):
    state_dir = os.path.dirname(_active_agents_path(workspace))
    os.makedirs(state_dir, exist_ok=True)
    active_path = _active_agents_path(workspace)
    run_id = "expired-run"

    with open(active_path, "w", encoding="utf-8") as f:
        f.write(
            "runs:\n"
            f"- run_id: {run_id}\n"
            "  agent: SEO Indexer\n"
            "  agent_ref: seo_indexer\n"
            "  workstream_id: ws-1\n"
            "  task_ids: []\n"
            "  pid: 424242\n"
            "  started_at: '2026-01-01T00:00:00+00:00'\n"
        )

    _write_run_meta(workspace, run_id, {
        "run_id": run_id,
        "agent": "SEO Indexer",
        "agent_ref": "seo_indexer",
        "workstream_id": "ws-1",
        "workstream_path": "WS",
        "task_ids": [],
        "tasks": [],
        "prompt": "",
        "system_prompt": "",
        "command_line": "",
        "log_path": None,
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": None,
        "status": "running",
        "exit_code": None,
        "retried_from_run_id": None,
        "retried_to_run_ids": [],
    })

    process_locks_dir = _process_locks_dir(workspace)
    os.makedirs(process_locks_dir, exist_ok=True)
    with open(os.path.join(process_locks_dir, f"{run_id}.lock"), "w", encoding="utf-8") as f:
        f.write(
            "agent_id: SEO Indexer\n"
            "acquired_at: '2026-01-01T00:00:00+00:00'\n"
            "expires_at: '2026-01-01T00:10:00+00:00'\n"
            "pid: 424242\n"
        )

    monkeypatch.setattr("orchestration.agents._is_pid_alive", lambda pid: True)
    monkeypatch.setattr("orchestration.agents._find_run_pids", lambda _run_id: [424242])

    killed = []

    def _fake_kill(pid, sig):
        killed.append((pid, sig))

    monkeypatch.setattr("orchestration.agents.os.kill", _fake_kill)

    count = count_active_agent_runs("ws-1", "seo_indexer", base_dir=workspace)
    assert count == 0
    assert killed == [(424242, 9)]

    with open(active_path, "r", encoding="utf-8") as f:
        remaining = f.read()
    assert run_id not in remaining

    meta = get_agent_run(run_id, base_dir=workspace)["run"]
    assert meta["status"] == "timeout"
    assert meta["exit_code"] == -9
    assert not os.path.exists(os.path.join(process_locks_dir, f"{run_id}.lock"))


def test_state_dir_uses_workstream_root_file_uri(workspace, tmp_path, monkeypatch):
    from orchestration.agents import _state_dir

    state_root = tmp_path / "shared_state"
    monkeypatch.setenv("WORKSTREAM_ROOT", f"file:{state_root}")

    assert _state_dir(workspace) == os.path.join(str(state_root.resolve()), ".orchestration")


def test_list_agent_runs_demotes_stale_running_status(workspace):
    run_id = "run-stale"
    meta = _base_run(run_id, "ws-1", ["t-1"])
    meta["status"] = "running"
    meta["ended_at"] = None
    _write_run_meta(workspace, run_id, meta)

    runs = list_agent_runs(limit=20, base_dir=workspace)
    run = next(r for r in runs if r.get("run_id") == run_id)
    assert run["status"] == "stale"


def test_get_agent_run_demotes_stale_running_status(workspace):
    run_id = "run-stale-detail"
    meta = _base_run(run_id, "ws-1", ["t-1"])
    meta["status"] = "running"
    meta["ended_at"] = None
    _write_run_meta(workspace, run_id, meta)

    details = get_agent_run(run_id, base_dir=workspace)

    assert details["run"]["status"] == "stale"


def test_get_agent_run_stale_reason_mentions_empty_log_and_missing_completion(workspace):
    ws = create_workstream(name="Stale WS", base_dir=workspace)
    task = create_task(ws.id, title="T", base_dir=workspace)
    run_id = "run-stale-reason"
    meta = _base_run(run_id, ws.id, [task.id])
    meta["status"] = "running"
    meta["ended_at"] = None
    meta["started_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    meta["log_path"] = _run_log_path(workspace, run_id)
    _write_run_meta(workspace, run_id, meta)

    task_obj = read_task(task.id, workspace)
    task_obj.add_audit("agent_started", "Agent 'SEO Indexer' started processing")
    _save_task(task_obj, workspace)

    os.makedirs(agents_module._agent_runs_dir(workspace), exist_ok=True)
    with open(_run_log_path(workspace, run_id), "w", encoding="utf-8") as f:
        f.write("")

    details = get_agent_run(run_id, base_dir=workspace)

    assert details["run"]["status"] == "stale"
    assert "no output was ever written" in details["interruption_reason"].lower()


def test_get_agent_run_stale_reason_mentions_retry_scheduled(workspace):
    ws = create_workstream(name="Stale WS", base_dir=workspace)
    task = create_task(ws.id, title="T", base_dir=workspace)
    run_id = "run-stale-retry"
    meta = _base_run(run_id, ws.id, [task.id])
    meta["status"] = "running"
    meta["ended_at"] = None
    meta["started_at"] = datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat()
    _write_run_meta(workspace, run_id, meta)

    task_obj = read_task(task.id, workspace)
    task_obj.add_audit("agent_started", "Agent 'SEO Indexer' started processing")
    task_obj.add_audit("retry_scheduled", "Retry 1/3 scheduled for later")
    _save_task(task_obj, workspace)

    details = get_agent_run(run_id, base_dir=workspace)

    assert details["run"]["status"] == "stale"
    assert "scheduled for retry" in details["interruption_reason"].lower()


class _FakeProc:
    def __init__(self):
        self.pid = 12345
        self.returncode = 0
        self.communicate_timeouts = []

    def communicate(self, timeout=None):
        self.communicate_timeouts.append(timeout)
        return ("", "")


class _FakeTerminatedProc(_FakeProc):
    def __init__(self):
        self.pid = 12345
        self.returncode = 143
        self.communicate_timeouts = []

    def communicate(self, timeout=None):
        self.communicate_timeouts.append(timeout)
        return ("", "")


class _FakeFailedProc(_FakeProc):
    def __init__(self):
        super().__init__()
        self.returncode = 1


def test_resolve_agent_file_accepts_frontmatter_name(workspace):
    agents_dir = os.path.join(workspace, "Agents")
    dashboard_agent = os.path.join(agents_dir, "dashboard_setup_agent.md")
    with open(dashboard_agent, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Dashboard Setup\n"
            "description: Dashboard metrics planner\n"
            "---\n"
            "You are the dashboard setup agent.\n"
        )

    resolved = _resolve_agent_file("Dashboard Setup", workspace)

    assert resolved == dashboard_agent

def test_parse_agent_md_reads_sound_headers(workspace):
    agents_dir = os.path.join(workspace, "Agents")
    sound_agent = os.path.join(agents_dir, "sound_agent.md")
    with open(sound_agent, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Sound Agent\n"
            "x-sound-start: start.wav\n"
            "x-sound-finish: finish.wav\n"
            "x-sound-error: error.wav\n"
            "---\n"
            "You are sound aware.\n"
        )

    parsed = _parse_agent_md(sound_agent)

    assert parsed["sound_start"] == "start.wav"
    assert parsed["sound_start_defined"] is True
    assert parsed["sound_finish"] == "finish.wav"
    assert parsed["sound_finish_defined"] is True
    assert parsed["sound_error"] == "error.wav"
    assert parsed["sound_error_defined"] is True


def test_configured_agent_sound_name_uses_default_env_when_header_missing(monkeypatch):
    monkeypatch.setenv("DEFAULT_AGENT_START_SOUND", "start.mp3")

    configured = _configured_agent_sound_name({}, "start")

    assert configured == "start.mp3"


def test_configured_agent_sound_name_explicit_header_overrides_default_even_when_blank(monkeypatch):
    monkeypatch.setenv("DEFAULT_AGENT_START_SOUND", "start.mp3")

    configured = _configured_agent_sound_name({
        "sound_start": "",
        "sound_start_defined": True,
    }, "start")

    assert configured is None


def test_resolve_agent_sound_file_uses_audio_file_path_override(workspace, monkeypatch):
    audio_dir = os.path.join(workspace, "custom_audio")
    os.makedirs(audio_dir, exist_ok=True)
    sound_path = os.path.join(audio_dir, "ding.wav")
    with open(sound_path, "wb") as f:
        f.write(b"wave")

    monkeypatch.setenv("AUDIO_FILE_PATH", "custom_audio")

    resolved = _resolve_agent_sound_file("ding.wav", workspace)

    assert resolved == sound_path


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/afplay")
@patch("orchestration.agents.subprocess.Popen")
def test_play_agent_sound_uses_default_audio_dir(mock_popen, mock_which, workspace):
    audio_dir = os.path.join(workspace, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    sound_path = os.path.join(audio_dir, "intro.wav")
    with open(sound_path, "wb") as f:
        f.write(b"wave")

    played = _play_agent_sound({"sound_start": "intro.wav", "sound_start_defined": True}, "start", workspace)

    assert played == sound_path
    assert mock_popen.call_args.args[0] == ["/usr/bin/afplay", sound_path]


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/afplay")
@patch("orchestration.agents.subprocess.Popen")
def test_play_agent_sound_uses_default_env_when_header_missing(mock_popen, mock_which, workspace, monkeypatch):
    audio_dir = os.path.join(workspace, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    sound_path = os.path.join(audio_dir, "start.mp3")
    with open(sound_path, "wb") as f:
        f.write(b"mp3")

    monkeypatch.setenv("DEFAULT_AGENT_START_SOUND", "start.mp3")

    played = _play_agent_sound({}, "start", workspace)

    assert played == sound_path
    assert mock_popen.call_args.args[0] == ["/usr/bin/afplay", sound_path]


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/afplay")
@patch("orchestration.agents.subprocess.Popen")
def test_play_agent_sound_returns_none_when_global_mute_enabled(mock_popen, mock_which, workspace):
    audio_dir = os.path.join(workspace, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    sound_path = os.path.join(audio_dir, "intro.wav")
    with open(sound_path, "wb") as f:
        f.write(b"wave")

    assert set_global_sound_mute(True, workspace) is True
    assert get_global_sound_mute(workspace) is True

    played = _play_agent_sound({"sound_start": "intro.wav", "sound_start_defined": True}, "start", workspace)

    assert played is None
    mock_popen.assert_not_called()


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_plays_start_and_finish_sounds(mock_popen, mock_which, workspace, monkeypatch):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "sound_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Sound Agent\n"
            "x-sound-start: intro.wav\n"
            "x-sound-finish: outro.wav\n"
            "x-sound-error: fail.wav\n"
            "---\n"
            "You are sound aware.\n"
        )

    events = []

    def _fake_play(agent_def, event, base_dir):
        events.append((event, agent_def.get(f"sound_{event}")))
        return None

    monkeypatch.setattr("orchestration.agents._play_agent_sound", _fake_play)

    ws = create_workstream(name="Sound WS", base_dir=workspace)
    run_agent("Sound Agent", workstream_id=ws.id, base_dir=workspace)

    assert events == [("start", "intro.wav"), ("finish", "outro.wav")]


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeFailedProc())
def test_run_agent_plays_error_sound_on_failure(mock_popen, mock_which, workspace, monkeypatch):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "sound_error_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Sound Error Agent\n"
            "x-sound-start: intro.wav\n"
            "x-sound-finish: outro.wav\n"
            "x-sound-error: fail.wav\n"
            "---\n"
            "You are sound aware.\n"
        )

    events = []

    def _fake_play(agent_def, event, base_dir):
        events.append((event, agent_def.get(f"sound_{event}")))
        return None

    monkeypatch.setattr("orchestration.agents._play_agent_sound", _fake_play)

    ws = create_workstream(name="Sound WS", base_dir=workspace)
    with pytest.raises(RuntimeError, match="failed"):
        run_agent("Sound Error Agent", workstream_id=ws.id, base_dir=workspace)

    assert events == [("start", "intro.wav"), ("error", "fail.wav")]


def test_resolve_agent_file_falls_back_to_source_agents_for_mounted_workspace(tmp_path, monkeypatch):
    workspace_dir = tmp_path / "mounted"
    workspace_dir.mkdir()
    source_dir = tmp_path / "source"
    agents_dir = source_dir / "Agents"
    agents_dir.mkdir(parents=True)
    fallback_agent = agents_dir / "fallback_agent.md"
    fallback_agent.write_text(
        "---\n"
        "name: Fallback Agent\n"
        "description: Agent loaded from source tree\n"
        "---\n"
        "You are loaded from the source Agents directory.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(agents_module, "SOURCE_DIR", str(source_dir))

    resolved = agents_module._resolve_agent_file("fallback_agent", str(workspace_dir))

    assert resolved == str(fallback_agent)


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_injects_workstream_context(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Standalone WS", context="Use message variant B", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Workstream Operating Context:" in prompt
    assert "Use message variant B" in prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_workstream_only_prompt_includes_task_locking_contract(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Available states for tasks on this workstream:" in prompt
    assert "You may inspect tasks in this workstream and decide which ones to work on." in prompt
    assert " -m orchestration.cli --base-dir " in prompt
    assert " lock acquire <task_id> --agent \"<agent_name>\"" in prompt
    assert " lock release <task_id> --agent \"<agent_name>\"" in prompt
    assert "Do not modify a task unless you successfully acquired its lock first." in prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_passes_agent_body_as_system_prompt(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--append-system-prompt" in cmd
    system_prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "You are a test agent." in system_prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_prompt_uses_current_python_executable(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Prompt WS", base_dir=workspace)
    task = create_task(ws.id, title="Read me", base_dir=workspace)

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    expected = f"{sys.executable} -m orchestration.cli --base-dir {workspace}"

    prompt = cmd[cmd.index("-p") + 1]
    assert f"Run: {expected} task read {task.id}" in prompt
    assert "Run: python -m orchestration.cli" not in prompt

    system_prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert f"Use `{expected} <command>`" in system_prompt
    assert "Use `python -m orchestration.cli <command>`" not in system_prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_does_not_inline_default_learning_prompt(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Learning WS", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "AGENT LEARNING (DEFAULT)" not in prompt
    assert "test_agent_learnings.md" not in prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_omits_learning_prompt_when_disabled(mock_popen, mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "no_learning_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: No Learning Agent\n"
            "description: Turns default learning behavior off\n"
            "x-learning: false\n"
            "---\n"
            "You are a test agent.\n"
        )

    ws = create_workstream(name="Learning WS", base_dir=workspace)
    run_agent("No Learning Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "AGENT LEARNING (DEFAULT)" not in prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_working_directory_for_descendant(mock_popen, mock_which, workspace):
    working_root = os.path.join(workspace, "mounted_repo")
    os.makedirs(working_root, exist_ok=True)

    parent = create_workstream(name="Mounted Parent", base_dir=workspace)
    parent.working_directory = working_root
    save_workstream(parent, workspace)

    child = create_workstream(name="Mounted Child", parent_id=parent.id, base_dir=workspace)

    run_agent("test_agent", workstream_id=child.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert f"WORKSPACE_ROOT: {working_root}" in prompt

    popen_env = mock_popen.call_args.kwargs["env"]
    assert popen_env["WORKSPACE_ROOT"] == working_root
    assert popen_env["ORCHESTRATION_ROOT"] == os.path.abspath(workspace)


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_persists_concurrency_state(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Deploy WS", base_dir=workspace)
    task = create_task(ws.id, title="Deploy", base_dir=workspace)

    result = run_agent(
        "test_agent",
        task_ids=[task.id],
        workstream_id=ws.id,
        base_dir=workspace,
        concurrency_state="Staging Deploy",
    )

    details = get_agent_run(result["run_id"], base_dir=workspace)
    assert details["run"]["concurrency_state"] == "Staging Deploy"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_accepts_frontmatter_name(mock_popen, mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "dashboard_setup_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Dashboard Setup\n"
            "description: Dashboard metrics planner\n"
            "---\n"
            "You are the dashboard setup agent.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    result = run_agent("Dashboard Setup", workstream_id=ws.id, base_dir=workspace)

    assert result["agent"] == "Dashboard Setup"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_x_model_and_x_effort(mock_popen, mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "model_effort_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Model Effort Agent\n"
            "description: Uses explicit model and effort\n"
            "x-model: anthropic/claude-opus-4-6\n"
            "x-effort: low\n"
            "---\n"
            "You are model-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Model Effort Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-6"
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "low"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_x_model_level_env_mapping(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("HIGH_LLM", "anthropic/claude-opus-4-6")

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "model_level_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Model Level Agent\n"
            "description: Uses model level\n"
            "x-model-level: high\n"
            "---\n"
            "You are level-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Model Level Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-opus-4-6"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_rejects_paused_workstream(mock_popen, mock_which, workspace):
    from orchestration.workstreams import save_workstream

    ws = create_workstream(name="Paused WS", base_dir=workspace)
    ws.paused = True
    save_workstream(ws, workspace)

    with pytest.raises(RuntimeError, match="paused"):
        run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    mock_popen.assert_not_called()


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_allows_paused_workstream_when_explicitly_overridden(mock_popen, mock_which, workspace):
    from orchestration.workstreams import save_workstream

    ws = create_workstream(name="Paused WS", base_dir=workspace)
    ws.paused = True
    save_workstream(ws, workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace, allow_paused_workstream=True)

    mock_popen.assert_called_once()


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_releases_matching_lock(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Lock WS", base_dir=workspace)
    task = create_task(ws.id, title="T", base_dir=workspace)

    acquire_lock(task.id, agent_id="Test Agent", base_dir=workspace)
    assert lock_status(task.id, base_dir=workspace) is not None

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    assert lock_status(task.id, base_dir=workspace) is None


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_task_prompt_does_not_include_workstream_discovery_locking_contract(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Task WS", base_dir=workspace)
    task = create_task(ws.id, title="Task", base_dir=workspace)

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "You may inspect tasks in this workstream and decide which ones to work on." not in prompt
    assert "Do not modify a task unless you successfully acquired its lock first." not in prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_does_not_inline_attachments_by_default(mock_popen, mock_which, workspace):
    ws = create_workstream(name="No Inline WS", base_dir=workspace)
    create_artifact("reports/brief.md", "inline me", base_dir=workspace, workstream_id=ws.id)
    task = create_task(ws.id, title="Task", attachments=["reports/brief.md"], base_dir=workspace)

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Task attachments (artifact paths + inlined content):" not in prompt
    assert "inline me" not in prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_does_not_inline_attachments_when_workstream_flag_enabled(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Inline WS", base_dir=workspace)
    ws.inline_attachments = True
    save_workstream(ws, workspace)

    create_artifact("reports/brief.md", "inline me", base_dir=workspace, workstream_id=ws.id)
    task = create_task(ws.id, title="Task", attachments=["reports/brief.md"], base_dir=workspace)

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Task attachments (artifact paths + inlined content):" not in prompt
    assert "- Path: reports/brief.md" not in prompt
    assert "inline me" not in prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_rejects_invalid_image_attachments_preflight(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Image WS", base_dir=workspace)
    artifacts_dir = os.path.join(workspace, "artifacts", "assets")
    os.makedirs(artifacts_dir, exist_ok=True)
    with open(os.path.join(artifacts_dir, "logo.png"), "wb") as f:
        f.write(b"not a real image")
    task = create_task(ws.id, title="Task", base_dir=workspace)
    task.attachments = ["assets/logo.png"]
    _save_task(task, workspace)

    with pytest.raises(RuntimeError, match="Invalid image attachments"):
        run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    mock_popen.assert_not_called()


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_accepts_valid_image_attachments_preflight(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Image WS", base_dir=workspace)
    artifacts_dir = os.path.join(workspace, "artifacts", "assets")
    os.makedirs(artifacts_dir, exist_ok=True)
    logo_path = os.path.join(artifacts_dir, "logo.png")
    with open(logo_path, "wb") as f:
        # Valid PNG signature + padded bytes to satisfy minimum size guard.
        f.write(b"\x89PNG\r\n\x1a\n" + (b"\x00" * 128))

    task = create_task(ws.id, title="Task", attachments=["assets/logo.png"], base_dir=workspace)
    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    mock_popen.assert_called_once()


def test_compact_learnings_artifact_if_needed_respects_env_threshold(workspace, monkeypatch):
    ws = create_workstream(name="Learning WS", base_dir=workspace)
    learning_path = "test_agent_learnings.md"
    initial_content = (
        "# Learnings\n\n"
        + "\n".join(
            [
                "- Prefer narrow prompts tied to explicit task IDs.",
                "- Prefer narrow prompts tied to explicit task IDs.",
                "- Always verify artifacts in the workstream root before creating new files.",
                "- Keep outputs compact and avoid repeating previous conclusions.",
            ]
            * 40
        )
        + "\n"
    )
    create_artifact(learning_path, initial_content, base_dir=workspace, workstream_id=ws.id)

    monkeypatch.setenv("ORCHESTRATION_LEARNINGS_COMPACTION_THRESHOLD_BYTES", "200")

    result = _compact_learnings_artifact_if_needed(
        {"file": os.path.join(workspace, "Agents", "test_agent.md"), "learning_enabled": True},
        ws.id,
        workspace,
    )

    assert result["checked"] is True
    assert result["compacted"] is True
    compacted = read_artifact(learning_path, base_dir=workspace, workstream_id=ws.id)
    assert "Agent Learnings (Compacted)" in compacted

    artifacts = list_artifacts(base_dir=workspace, workstream_id=ws.id)
    assert any(path.startswith("test_agent_learnings_archive_") for path in artifacts)


@pytest.mark.parametrize(
    "returncode,timeout_expired,expected",
    [
        (0, False, "completed"),
        (1, False, "failed"),
        (143, False, "killed"),
        (-15, False, "killed"),
        (137, False, "killed"),
        (-9, False, "killed"),
        (1, True, "timeout"),
    ],
)
def test_classify_run_outcome(returncode, timeout_expired, expected):
    assert _classify_run_outcome(returncode, timeout_expired) == expected


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_cli_parameters_include_required_flags(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    popen_kwargs = mock_popen.call_args.kwargs
    assert cmd[0] == "/usr/bin/claude"
    assert "-p" in cmd
    assert "--append-system-prompt" in cmd
    assert "--model" in cmd
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "text"
    assert "--verbose" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--effort" not in cmd
    assert popen_kwargs.get("start_new_session") is True

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    command_line = latest.get("command_line", "")
    assert command_line.startswith("/usr/bin/claude ")
    assert " --append-system-prompt " in command_line
    assert " --dangerously-skip-permissions" in command_line


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_cli_parameters_include_effort_when_configured(mock_popen, mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "effort_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Effort Agent\n"
            "description: Uses effort\n"
            "x-effort: high\n"
            "---\n"
            "You are effort-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Effort Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--dangerously-skip-permissions" in cmd
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "high"

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    command_line = latest.get("command_line", "")
    assert " --effort high" in command_line


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_can_use_cline_runtime(mock_popen, mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Agent\n"
            "description: Runs with Cline\n"
            "x-runtime: cline\n"
            "x-model: anthropic/claude-opus-4-6\n"
            "x-effort: high\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert cmd[0] == "/usr/bin/cline"
    assert "-y" in cmd
    assert "-a" in cmd
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == workspace
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "claude-opus-4-6"
    assert "--timeout" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "1800"
    assert "--thinking" in cmd
    assert "--verbose" not in cmd
    assert "--append-system-prompt" not in cmd
    assert "--dangerously-skip-permissions" not in cmd
    effective_prompt = cmd[-1]
    assert "=== SYSTEM INSTRUCTIONS ===" in effective_prompt
    assert "=== TASK ===" in effective_prompt

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    assert latest["runtime"] == "cline"
    assert latest["command_line"].startswith("/usr/bin/cline ")


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
def test_run_agent_gives_cline_timeout_cleanup_grace(mock_which, workspace):
    fake_proc = _FakeProc()
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_timeout_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Timeout Agent\n"
            "description: Runs with Cline\n"
            "x-runtime: cline\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    with patch("orchestration.agents.subprocess.Popen", return_value=fake_proc):
        run_agent("Cline Timeout Agent", workstream_id=ws.id, timeout=100, base_dir=workspace)

    assert fake_proc.communicate_timeouts == [130]


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_can_enable_cline_verbose_output(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_CLINE_VERBOSE", "true")
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_verbose_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Verbose Agent\n"
            "description: Runs with Cline\n"
            "x-runtime: cline\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Verbose Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--verbose" in cmd


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_cline_runtime_default_model(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.delenv("CLINE_DEFAULT_LLM", raising=False)

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_default_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Default Agent\n"
            "description: Uses Cline defaults\n"
            "x-runtime: cline\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Default Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert cmd[cmd.index("-m") + 1] == "deepseek/deepseek-v4-flash"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_cline_high_level_default_model(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.delenv("CLINE_HIGH_LLM", raising=False)

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_high_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline High Agent\n"
            "description: Uses Cline high level\n"
            "x-runtime: cline\n"
            "x-model-level: high\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline High Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert cmd[cmd.index("-m") + 1] == "deepseek/deepseek-v4-pro"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_cline_level_default_model(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.delenv("CLINE_LOW_LLM", raising=False)

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_low_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Low Agent\n"
            "description: Uses Cline low level\n"
            "x-runtime: cline\n"
            "x-model-level: low\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Low Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert cmd[cmd.index("-m") + 1] == "deepseek/deepseek-v4-flash"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_passes_valid_cline_config_dir(mock_popen, mock_which, workspace, monkeypatch):
    config_dir = os.path.join(workspace, ".cline-test")
    os.makedirs(os.path.join(config_dir, "data"), exist_ok=True)
    monkeypatch.setenv("ORCHESTRATION_CLINE_CONFIG_DIR", config_dir)

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_config_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Config Agent\n"
            "description: Uses configured Cline auth\n"
            "x-runtime: cline\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Config Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--config" in cmd
    assert cmd[cmd.index("--config") + 1] == os.path.abspath(config_dir)


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_normalizes_cline_data_config_dir(mock_popen, mock_which, workspace, monkeypatch):
    config_root = os.path.join(workspace, ".cline-test")
    data_dir = os.path.join(config_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "globalState.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    monkeypatch.setenv("ORCHESTRATION_CLINE_CONFIG_DIR", data_dir)

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_data_config_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Data Config Agent\n"
            "description: Uses configured Cline auth data dir\n"
            "x-runtime: cline\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Data Config Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--config" in cmd
    assert cmd[cmd.index("--config") + 1] == os.path.abspath(config_root)


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_ignores_invalid_cline_config_dir(mock_popen, mock_which, workspace, monkeypatch):
    invalid_dir = os.path.join(workspace, "bin")
    os.makedirs(os.path.join(invalid_dir, "data"), exist_ok=True)
    for name in ["node", "npm", "npx", "corepack"]:
        with open(os.path.join(invalid_dir, name), "w", encoding="utf-8") as f:
            f.write("")
    monkeypatch.setenv("ORCHESTRATION_CLINE_CONFIG_DIR", invalid_dir)

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_invalid_config_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Invalid Config Agent\n"
            "description: Ignores invalid configured Cline auth dir\n"
            "x-runtime: cline\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Invalid Config Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--config" not in cmd


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeTerminatedProc())
def test_run_agent_marks_sigterm_exit_as_killed(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    with pytest.raises(RuntimeError, match="was killed"):
        run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    assert latest["status"] == "killed"
    assert latest["exit_code"] == 143


def test_runtime_reported_timeout_detects_cline_timeout():
    assert _runtime_reported_timeout("cline", "Error: Timeout\n", 1) is True
    assert _runtime_reported_timeout("cline", '{"type": "error", "message": "Timeout"}', 1) is True
    assert _runtime_reported_timeout("claude-code", "Error: Timeout\n", 1) is False
    assert _runtime_reported_timeout("cline", "done", 0) is False
