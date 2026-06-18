"""Tests for agent resolution and run diagnostics."""

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from orchestration import agents as agents_module
from orchestration.agents import _classify_run_outcome, _compact_learnings_artifact_if_needed, _configured_agent_sound_name, _parse_agent_md, _play_agent_sound, _resolve_agent_file, _resolve_agent_sound_file, _runtime_reported_timeout, _strip_ansi_escape_codes, count_active_agent_runs, get_agent_run, get_agent_run_context, get_global_sound_mute, list_agent_runs, read_agent_run_context_file, retry_agent_run, run_agent, set_global_sound_mute
from orchestration.locks import acquire_lock, lock_status
from orchestration.artifacts import create_artifact, list_artifacts, read_artifact
from orchestration.tasks import create_task, pause_task, read_task, _save_task
from orchestration.workstreams import create_workstream, save_workstream, set_workstream_env_key, unset_workstream_env_key
from orchestration.models import RetryConfig


@pytest.fixture(autouse=True)
def _clear_runtime_model_env(monkeypatch):
    monkeypatch.delenv("ORCHESTRATION_AGENT_RUNTIME", raising=False)
    monkeypatch.delenv("ORCHESTRATION_CLINE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ORCHESTRATION_CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("ORCHESTRATION_COPILOT_CONTEXT_DIRS", raising=False)
    monkeypatch.delenv("DEFAULT_EFFORT", raising=False)
    monkeypatch.delenv("HIGH_EFFORT", raising=False)
    monkeypatch.delenv("MEDIUM_EFFORT", raising=False)
    monkeypatch.delenv("LOW_EFFORT", raising=False)
    monkeypatch.delenv("CODING_EFFORT", raising=False)
    monkeypatch.delenv("CLINE_DEFAULT_EFFORT", raising=False)
    monkeypatch.delenv("CLINE_HIGH_EFFORT", raising=False)
    monkeypatch.delenv("CLINE_MEDIUM_EFFORT", raising=False)
    monkeypatch.delenv("CLINE_LOW_EFFORT", raising=False)
    monkeypatch.delenv("CLINE_CODING_EFFORT", raising=False)
    monkeypatch.delenv("COPILOT_DEFAULT_EFFORT", raising=False)
    monkeypatch.delenv("COPILOT_HIGH_EFFORT", raising=False)
    monkeypatch.delenv("COPILOT_MEDIUM_EFFORT", raising=False)
    monkeypatch.delenv("COPILOT_LOW_EFFORT", raising=False)
    monkeypatch.delenv("COPILOT_CODING_EFFORT", raising=False)
    monkeypatch.delenv("CLINE_DEFAULT_LLM", raising=False)
    monkeypatch.delenv("CLINE_HIGH_LLM", raising=False)
    monkeypatch.delenv("CLINE_MEDIUM_LLM", raising=False)
    monkeypatch.delenv("CLINE_LOW_LLM", raising=False)
    monkeypatch.delenv("CLINE_CODING_LLM", raising=False)
    monkeypatch.delenv("ORCHESTRATION_CLINE_VERBOSE", raising=False)
    monkeypatch.delenv("CODING_LLM", raising=False)
    monkeypatch.delenv("COPILOT_MODEL", raising=False)
    monkeypatch.delenv("COPILOT_HIGH_LLM", raising=False)
    monkeypatch.delenv("COPILOT_MEDIUM_LLM", raising=False)
    monkeypatch.delenv("COPILOT_LOW_LLM", raising=False)
    monkeypatch.delenv("COPILOT_CODING_LLM", raising=False)
    monkeypatch.delenv("BEANS_PROXY", raising=False)
    monkeypatch.delenv("BEANS_PROXY_HOST", raising=False)
    monkeypatch.delenv("BEANS_PROXY_PORT", raising=False)
    monkeypatch.delenv("COPILOT_PROVIDER_BASE_URL", raising=False)
    monkeypatch.delenv("COPILOT_PROVIDER_TYPE", raising=False)
    monkeypatch.delenv("COPILOT_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("CLINE_DATA_DIR", raising=False)
    monkeypatch.setenv("WORKSTREAM_ROOT", "")
    monkeypatch.setenv("ARTIFACT_ROOT", "")
    monkeypatch.setenv("ARTICACT_ROOT", "")
    monkeypatch.setenv("ORCHESTRATION_BASE_ENV_PATH", "")
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


def test_list_agent_runs_reads_bounded_recent_metadata(workspace):
    for index in range(80):
        run_id = f"old-run-{index:02d}"
        meta = _base_run(run_id, "ws-1", [])
        meta["started_at"] = f"2026-01-01T00:{index:02d}:00+00:00"
        _write_run_meta(workspace, run_id, meta)
        path = agents_module._agent_run_meta_path(workspace, run_id)
        os.utime(path, (index + 1, index + 1))

    newest_ids = []
    for index in range(10):
        run_id = f"new-run-{index:02d}"
        newest_ids.append(run_id)
        meta = _base_run(run_id, "ws-1", [])
        meta["started_at"] = f"2026-01-02T00:{index:02d}:00+00:00"
        _write_run_meta(workspace, run_id, meta)
        path = agents_module._agent_run_meta_path(workspace, run_id)
        os.utime(path, (1_000 + index, 1_000 + index))

    original_read_run_meta = agents_module._read_run_meta
    read_paths = []

    def tracking_read_run_meta(path):
        read_paths.append(path)
        return original_read_run_meta(path)

    with patch("orchestration.agents._read_run_meta", side_effect=tracking_read_run_meta):
        runs = list_agent_runs(limit=5, base_dir=workspace)

    assert [run["run_id"] for run in runs] == list(reversed(newest_ids[-5:]))
    assert len(read_paths) < 80


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


class _FakeCopilotFatalProc(_FakeProc):
    def __init__(self, stdout_handle, message=None):
        super().__init__()
        self._stdout_handle = stdout_handle
        self._message = message or (
            "Execution failed: CAPIError: 400 Duplicate item found with id "
            "fc_call_duplicate"
        )

    def communicate(self, timeout=None):
        self.communicate_timeouts.append(timeout)
        self._stdout_handle.write(f"{self._message}\n")
        self._stdout_handle.flush()
        return ("", "")


def test_capture_provider_context_copies_cline_task_files_to_central_store(workspace, tmp_path, monkeypatch):
    cline_home = tmp_path / ".cline"
    tasks_dir = cline_home / "data" / "tasks"
    tasks_dir.mkdir(parents=True)
    monkeypatch.setenv("ORCHESTRATION_CLINE_CONFIG_DIR", str(cline_home))

    snapshot = agents_module._snapshot_provider_context("cline")

    task_dir = tasks_dir / "1778000000000"
    task_dir.mkdir()
    (task_dir / "api_conversation_history.json").write_text(
        json.dumps([
            {"role": "user", "content": "build the thing", "ts": 1},
            {"role": "assistant", "content": [{"type": "text", "text": "working on it"}], "ts": 2},
        ]),
        encoding="utf-8",
    )
    (task_dir / "ui_messages.json").write_text(
        json.dumps([
            {"type": "say", "say": "thinking", "text": "checking files", "ts": 3},
            {"type": "ask", "text": "need input?", "ts": 4},
        ]),
        encoding="utf-8",
    )
    (task_dir / "context_history.json").write_text(
        json.dumps([{"type": "context", "text": "file context snapshot"}]),
        encoding="utf-8",
    )
    (task_dir / "focus_chain_taskid_1778000000000.md").write_text("visible reasoning notes\n", encoding="utf-8")

    capture = agents_module._capture_provider_context(workspace, "run-context", "cline", snapshot)

    assert capture["file_count"] == 4
    context_dir = agents_module._agent_run_context_dir(workspace, "run-context")
    manifest_path = agents_module._agent_run_context_manifest_path(workspace, "run-context")
    assert os.path.exists(manifest_path)
    assert os.path.commonpath([context_dir, manifest_path]) == context_dir

    details = get_agent_run_context("run-context", base_dir=workspace)
    copied_paths = [f["copied_path"] for f in details["files"]]
    assert any(path.endswith("api_conversation_history.json") for path in copied_paths)
    assert all(not os.path.isabs(path) for path in copied_paths)
    assert any(m["role"] == "user" and "build the thing" in m["text"] for m in details["messages"])
    assert any("checking files" in e["text"] for e in details["events"])


def test_capture_provider_context_missing_roots_warns_without_failure(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_CLAUDE_CONFIG_DIR", str(tmp_path / "missing-claude"))

    snapshot = agents_module._snapshot_provider_context("claude-code")
    capture = agents_module._capture_provider_context(workspace, "run-missing-context", "claude-code", snapshot)

    assert capture["file_count"] == 0
    assert capture["warnings"]
    details = get_agent_run_context("run-missing-context", base_dir=workspace)
    assert details["manifest"]["storage"]["central"] is True


def test_read_agent_run_context_file_blocks_path_traversal(workspace):
    context_dir = agents_module._agent_run_context_dir(workspace, "run-traversal")
    os.makedirs(context_dir, exist_ok=True)
    with open(os.path.join(context_dir, "safe.txt"), "w", encoding="utf-8") as f:
        f.write("safe")

    assert read_agent_run_context_file("run-traversal", "safe.txt", base_dir=workspace)["content"] == "safe"
    with pytest.raises(ValueError):
        read_agent_run_context_file("run-traversal", "../safe.txt", base_dir=workspace)


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


def test_parse_agent_md_reads_x_role_header(workspace):
    agents_dir = os.path.join(workspace, "Agents")
    role_agent = os.path.join(agents_dir, "role_agent.md")
    with open(role_agent, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Role Agent\n"
            "x-role: exec\n"
            "---\n"
            "You are role aware.\n"
        )

    parsed = _parse_agent_md(role_agent)

    assert parsed["agent_type"] == "executive"


def test_parse_agent_md_reads_x_own_worktree_header(workspace):
    agents_dir = os.path.join(workspace, "Agents")
    own_worktree_agent = os.path.join(agents_dir, "own_worktree_agent.md")
    with open(own_worktree_agent, "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Own Worktree Agent\n"
            "x-own-worktree: true\n"
            "---\n"
            "You are worktree aware.\n"
        )

    parsed = _parse_agent_md(own_worktree_agent)

    assert parsed["own_worktree"] is True


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


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeFailedProc())
def test_run_agent_uses_and_cleans_own_worktree(mock_popen, mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "own_worktree_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Own Worktree Agent\n"
            "description: Runs in an isolated worktree\n"
            "x-own-worktree: true\n"
            "---\n"
            "You are worktree aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)
    isolated_root = os.path.join(workspace, ".orchestration", "run_worktrees", "run-123")
    worktree = {
        "repo_root": os.path.abspath(workspace),
        "worktree_root": isolated_root,
        "workspace_root": isolated_root,
    }

    with patch("orchestration.agents._provision_run_worktree", return_value=worktree) as mock_provision, \
         patch("orchestration.agents._deprovision_run_worktree") as mock_deprovision:
        with pytest.raises(RuntimeError, match="failed"):
            run_agent("Own Worktree Agent", workstream_id=ws.id, base_dir=workspace, _run_id="run-123")

    prompt = mock_popen.call_args.args[0][mock_popen.call_args.args[0].index("-p") + 1]
    popen_env = mock_popen.call_args.kwargs["env"]

    mock_provision.assert_called_once_with(os.path.abspath(workspace), "run-123", base_dir=workspace)
    mock_deprovision.assert_called_once_with(worktree, base_dir=workspace)
    assert f"WORKSPACE_ROOT: {isolated_root}" in prompt
    assert popen_env["WORKSPACE_ROOT"] == isolated_root
    assert popen_env["ORCHESTRATION_AGENT_WORKTREE_ROOT"] == isolated_root
    assert mock_popen.call_args.kwargs["cwd"] == isolated_root


def test_provision_run_worktree_uses_orphan_branch_for_unborn_head(workspace):
    repo_root = os.path.join(workspace, "empty_repo")
    os.makedirs(repo_root, exist_ok=True)
    subprocess.run(["git", "init", repo_root], check=True, capture_output=True, text=True)

    run_id = "run-unborn"
    worktree = agents_module._provision_run_worktree(repo_root, run_id, base_dir=workspace)

    try:
        assert worktree["repo_root"] == os.path.abspath(repo_root)
        assert worktree["worktree_root"].endswith(os.path.join("run_worktrees", run_id))
        assert worktree["workspace_root"] == worktree["worktree_root"]
        assert worktree["branch_name"] == run_id
        assert os.path.isdir(worktree["worktree_root"])
    finally:
        agents_module._deprovision_run_worktree(worktree, base_dir=workspace)

    branch_check = subprocess.run(
        ["git", "-C", repo_root, "show-ref", "--verify", f"refs/heads/{run_id}"],
        capture_output=True,
        text=True,
    )
    assert branch_check.returncode != 0


def test_provision_run_worktree_roots_under_workspace_root_not_state_root(workspace):
    repo_root = os.path.join(workspace, "code_repo")
    state_root = os.path.join(workspace, "state_repo")
    os.makedirs(repo_root, exist_ok=True)
    os.makedirs(state_root, exist_ok=True)

    subprocess.run(["git", "init", repo_root], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", repo_root, "config", "user.email", "tests@example.com"], check=True)
    subprocess.run(["git", "-C", repo_root, "config", "user.name", "Test User"], check=True)

    tracked = os.path.join(repo_root, "tracked.txt")
    with open(tracked, "w", encoding="utf-8") as f:
        f.write("base\n")
    subprocess.run(["git", "-C", repo_root, "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", repo_root, "commit", "-m", "base"], check=True, capture_output=True, text=True)

    run_id = "run-workspace-root"
    worktree = agents_module._provision_run_worktree(repo_root, run_id, base_dir=state_root)
    expected_managed_root = os.path.join(repo_root, ".orchestration", "run_worktrees")
    legacy_managed_root = os.path.join(state_root, ".orchestration", "run_worktrees")

    try:
        assert worktree["managed_root"] == expected_managed_root
        assert worktree["worktree_root"].startswith(expected_managed_root + os.sep)
        assert not worktree["worktree_root"].startswith(legacy_managed_root + os.sep)
        assert os.path.isdir(worktree["worktree_root"])
    finally:
        agents_module._deprovision_run_worktree(worktree, base_dir=state_root)


def test_provision_run_worktree_prefers_local_main_over_detached_head(workspace):
    repo_root = os.path.join(workspace, "repo_with_main")
    os.makedirs(repo_root, exist_ok=True)
    subprocess.run(["git", "init", repo_root], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", repo_root, "config", "user.email", "tests@example.com"], check=True)
    subprocess.run(["git", "-C", repo_root, "config", "user.name", "Test User"], check=True)

    tracked = os.path.join(repo_root, "tracked.txt")
    with open(tracked, "w", encoding="utf-8") as f:
        f.write("base\n")
    subprocess.run(["git", "-C", repo_root, "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", repo_root, "commit", "-m", "base"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", repo_root, "checkout", "-B", "main"], check=True, capture_output=True, text=True)

    main_commit = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    subprocess.run(["git", "-C", repo_root, "checkout", "-b", "feature"], check=True, capture_output=True, text=True)
    with open(tracked, "a", encoding="utf-8") as f:
        f.write("feature\n")
    subprocess.run(["git", "-C", repo_root, "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", repo_root, "commit", "-m", "feature"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", repo_root, "checkout", "--detach"], check=True, capture_output=True, text=True)

    run_id = "run-prefers-main"
    worktree = agents_module._provision_run_worktree(repo_root, run_id, base_dir=workspace)
    try:
        worktree_head = subprocess.run(
            ["git", "-C", worktree["worktree_root"], "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert worktree_head == main_commit
    finally:
        agents_module._deprovision_run_worktree(worktree, base_dir=workspace)


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
def test_run_agent_applies_workstream_env_overrides_to_child_process(mock_popen, mock_which, workspace, monkeypatch):
    ws = create_workstream(name="Env WS", base_dir=workspace)
    set_workstream_env_key(ws.id, "WS_ONLY", "from-workstream", base_dir=workspace)

    monkeypatch.setenv("MASK_ME", "from-system")
    unset_workstream_env_key(ws.id, "MASK_ME", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    child_env = mock_popen.call_args.kwargs["env"]
    assert child_env["WS_ONLY"] == "from-workstream"
    assert "MASK_ME" not in child_env


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_applies_mounted_working_directory_env_to_child_process(mock_popen, mock_which, workspace, monkeypatch):
    working_root = os.path.join(workspace, "mounted_repo")
    os.makedirs(working_root, exist_ok=True)
    with open(os.path.join(working_root, ".env"), "w", encoding="utf-8") as f:
        f.write("QA_BYPASS_SECRET_STAGING=from-mounted-root\n")

    monkeypatch.setenv("QA_BYPASS_SECRET_STAGING", "from-system")

    parent = create_workstream(name="Mounted Parent", base_dir=workspace)
    parent.working_directory = working_root
    save_workstream(parent, workspace)

    child = create_workstream(name="Mounted Child", parent_id=parent.id, base_dir=workspace)

    run_agent("test_agent", workstream_id=child.id, base_dir=workspace)

    child_env = mock_popen.call_args.kwargs["env"]
    assert child_env["QA_BYPASS_SECRET_STAGING"] == "from-mounted-root"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_applies_base_env_before_workstream_overrides(mock_popen, mock_which, workspace, tmp_path, monkeypatch):
    base_env = tmp_path / "foundation.env"
    base_env.write_text("FOUNDATION_ONLY=from-foundation\nOVERRIDE_ME=from-foundation\nMASK_ME=from-foundation\n")
    monkeypatch.setenv("ORCHESTRATION_BASE_ENV_PATH", str(base_env))

    ws = create_workstream(name="Env WS", base_dir=workspace)
    set_workstream_env_key(ws.id, "OVERRIDE_ME", "from-workstream", base_dir=workspace)
    unset_workstream_env_key(ws.id, "MASK_ME", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    child_env = mock_popen.call_args.kwargs["env"]
    assert child_env["FOUNDATION_ONLY"] == "from-foundation"
    assert child_env["OVERRIDE_ME"] == "from-workstream"
    assert "MASK_ME" not in child_env


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


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/gh")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_coding_model_level_for_copilot(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("COPILOT_CODING_LLM", "gpt-5.3-codex")

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "coding_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Coding Agent\n"
            "description: Uses coding level\n"
            "x-runtime: copilot\n"
            "x-model-level: coding\n"
            "---\n"
            "You are coding aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Coding Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "gpt-5.3-codex"


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
def test_run_agent_preserves_task_pause_set_after_task_load(mock_popen, mock_which, workspace, monkeypatch):
    ws = create_workstream(name="Pause Race WS", base_dir=workspace)
    task = create_task(ws.id, title="T", base_dir=workspace)

    original_build_system_prompt = agents_module._build_system_prompt
    pause_triggered = {"value": False}

    def _pause_during_startup(agent_def, base_dir):
        if not pause_triggered["value"]:
            pause_task(task.id, base_dir=workspace)
            pause_triggered["value"] = True
        return original_build_system_prompt(agent_def, base_dir)

    monkeypatch.setattr(agents_module, "_build_system_prompt", _pause_during_startup)

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    task_fresh = read_task(task.id, workspace)

    assert task_fresh.paused is True
    assert any(entry.type == "task_paused" for entry in task_fresh.audit)
    assert not any(entry.type == "task_resumed" for entry in task_fresh.audit)


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


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_level_specific_effort_env_when_x_effort_missing(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("HIGH_LLM", "anthropic/claude-opus-4-6")
    monkeypatch.setenv("HIGH_EFFORT", "xhigh")

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "env_level_effort_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Env Level Effort Agent\n"
            "description: Uses env level effort\n"
            "x-model-level: high\n"
            "---\n"
            "You are env-level-effort aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Env Level Effort Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "xhigh"

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    assert latest["effort"] == "xhigh"
    assert " --effort xhigh" in latest.get("command_line", "")


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_runtime_specific_effort_env_for_cline(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("CLINE_DEFAULT_EFFORT", "high")

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_env_effort_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Env Effort Agent\n"
            "description: Uses runtime env effort\n"
            "x-runtime: cline\n"
            "---\n"
            "You are runtime-env-effort aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Env Effort Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--thinking" in cmd
    assert cmd[cmd.index("--thinking") + 1] == "high"

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    assert latest["runtime"] == "cline"
    assert latest["effort"] == "high"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/copilot")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_runtime_level_specific_effort_env_for_copilot(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("COPILOT_CODING_LLM", "gpt-5.3-codex")
    monkeypatch.setenv("COPILOT_CODING_EFFORT", "medium")

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "copilot_env_level_effort_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Copilot Env Level Effort Agent\n"
            "description: Uses copilot level env effort\n"
            "x-runtime: copilot\n"
            "x-model-level: coding\n"
            "---\n"
            "You are copilot-level-env-effort aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Copilot Env Level Effort Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "medium"

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    assert latest["runtime"] == "copilot"
    assert latest["effort"] == "medium"


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
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == workspace
    assert "-m" in cmd
    assert cmd[cmd.index("-m") + 1] == "claude-opus-4-6"
    assert "--timeout" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "1800"
    assert "--auto-approve" in cmd
    assert cmd[cmd.index("--auto-approve") + 1] == "true"
    assert "--thinking" in cmd
    assert cmd[cmd.index("--thinking") + 1] == "high"
    assert "--verbose" not in cmd
    assert "--append-system-prompt" not in cmd
    assert "--dangerously-skip-permissions" not in cmd
    effective_prompt = cmd[-1]
    assert "=== SYSTEM INSTRUCTIONS ===" in effective_prompt
    assert "=== TASK ===" in effective_prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_omits_cline_thinking_for_medium_effort(mock_popen, mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_medium_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Medium Agent\n"
            "description: Runs with Cline medium effort\n"
            "x-runtime: cline\n"
            "x-effort: medium\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Cline Medium Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--thinking" not in cmd

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    assert latest["runtime"] == "cline"
    assert latest["command_line"].startswith("/usr/bin/cline ")


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/copilot")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_can_use_copilot_runtime(mock_popen, mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "copilot_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Copilot Agent\n"
            "description: Runs with Copilot\n"
            "x-runtime: copilot\n"
            "x-effort: max\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("Copilot Agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert cmd[0] == "/usr/bin/copilot"
    assert "-p" in cmd
    effective_prompt = cmd[cmd.index("-p") + 1]
    assert "=== SYSTEM INSTRUCTIONS ===" in effective_prompt
    assert "=== TASK ===" in effective_prompt
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == "auto"
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "text"
    assert "--silent" in cmd
    assert "--allow-all" in cmd
    assert "--no-ask-user" in cmd
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert "--append-system-prompt" not in cmd
    assert "--dangerously-skip-permissions" not in cmd

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    assert latest["runtime"] == "copilot"
    assert latest["command_line"].startswith("/usr/bin/copilot ")


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/copilot")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_routes_copilot_through_beans_proxy(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("BEANS_PROXY", "true")
    monkeypatch.setenv("BEANS_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("BEANS_PROXY_PORT", "8123")
    monkeypatch.setenv("ORCHESTRATION_AGENT_RUNTIME", "copilot")
    monkeypatch.setenv("COPILOT_MODEL", "gpt-5.4")
    monkeypatch.setattr("orchestration.agents._fetch_beans_proxy_usage_records", lambda pseudo_key: [])

    task = create_task(create_workstream(name="Proxy WS", base_dir=workspace).id, title="Track Me", base_dir=workspace)

    run_agent("test_agent", task_ids=[task.id], base_dir=workspace)

    child_env = mock_popen.call_args.kwargs["env"]
    assert child_env["COPILOT_PROVIDER_BASE_URL"] == "http://127.0.0.1:8123"
    assert child_env["COPILOT_PROVIDER_TYPE"] == "openai"
    assert child_env["COPILOT_PROVIDER_API_KEY"] == f"task-{task.id}"

    cmd = mock_popen.call_args.args[0]
    assert cmd[cmd.index("--model") + 1] == "gpt-5.4"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/cline")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
@patch("orchestration.agents.subprocess.run")
def test_run_agent_routes_cline_through_beans_proxy(mock_run, mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("BEANS_PROXY", "true")
    monkeypatch.setenv("BEANS_PROXY_HOST", "127.0.0.1")
    monkeypatch.setenv("BEANS_PROXY_PORT", "8123")
    monkeypatch.setenv("CLINE_DEFAULT_LLM", "anthropic/claude-sonnet-4-20250514")
    monkeypatch.setattr("orchestration.agents._fetch_beans_proxy_usage_records", lambda pseudo_key: [])

    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "cline_proxy_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Cline Proxy Agent\n"
            "description: Runs with Cline via Beans\n"
            "x-runtime: cline\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Proxy WS", base_dir=workspace)
    task = create_task(ws.id, title="Track Me", base_dir=workspace)

    run_agent("Cline Proxy Agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    auth_cmd = mock_run.call_args.args[0]
    assert auth_cmd[:4] == ["/usr/bin/cline", "auth", "--provider", "openai-compatible"]
    assert "--baseurl" in auth_cmd
    assert auth_cmd[auth_cmd.index("--baseurl") + 1] == "http://127.0.0.1:8123"
    assert "--apikey" in auth_cmd
    assert auth_cmd[auth_cmd.index("--apikey") + 1] == f"task-{task.id}"
    assert auth_cmd[auth_cmd.index("--modelid") + 1] == "claude-sonnet-4-20250514"

    cmd = mock_popen.call_args.args[0]
    assert "-P" in cmd
    assert cmd[cmd.index("-P") + 1] == "openai-compatible"
    assert cmd[cmd.index("-m") + 1] == "claude-sonnet-4-20250514"

    child_env = mock_popen.call_args.kwargs["env"]
    assert "CLINE_DATA_DIR" in child_env
    assert not os.path.exists(child_env["CLINE_DATA_DIR"])


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/copilot")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_copilot_runtime_strips_openai_key_from_child_env(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_AGENT_RUNTIME", "copilot")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("GH_TOKEN", "test-github-token")

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    child_env = mock_popen.call_args.kwargs["env"]
    assert "OPENAI_API_KEY" not in child_env
    assert child_env["GH_TOKEN"] == "test-github-token"


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/copilot")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_syncs_task_token_usage_from_beans_proxy(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("BEANS_PROXY", "true")
    monkeypatch.setenv("ORCHESTRATION_AGENT_RUNTIME", "copilot")
    monkeypatch.setattr(
        "orchestration.agents._fetch_beans_proxy_usage_records",
        lambda pseudo_key: [
            {"input_tokens": 100, "output_tokens": 25},
            {"input_tokens": 50, "output_tokens": 10},
            {"input_tokens": 0, "output_tokens": 0, "error": "upstream_timeout"},
        ],
    )

    ws = create_workstream(name="Proxy WS", base_dir=workspace)
    task = create_task(ws.id, title="Track Me", base_dir=workspace)

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    task_obj = read_task(task.id, workspace)
    assert task_obj.token_usage["pseudo_key"] == f"task-{task.id}"
    assert task_obj.token_usage["input_tokens"] == 150
    assert task_obj.token_usage["output_tokens"] == 35
    assert task_obj.token_usage["request_count"] == 2


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/copilot")
def test_run_agent_marks_copilot_runtime_reported_error_as_failed(mock_which, workspace):
    agents_dir = os.path.join(workspace, "Agents")
    with open(os.path.join(agents_dir, "copilot_error_agent.md"), "w", encoding="utf-8") as f:
        f.write(
            "---\n"
            "name: Copilot Error Agent\n"
            "description: Runs with Copilot\n"
            "x-runtime: copilot\n"
            "---\n"
            "You are runtime-aware.\n"
        )

    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    def _fake_popen(*args, **kwargs):
        return _FakeCopilotFatalProc(kwargs["stdout"])

    with patch("orchestration.agents.subprocess.Popen", side_effect=_fake_popen):
        with pytest.raises(RuntimeError, match=r"failed \(exit 0\)"):
            run_agent("Copilot Error Agent", workstream_id=ws.id, base_dir=workspace)

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    details = get_agent_run(latest["run_id"], base_dir=workspace)

    assert latest["runtime"] == "copilot"
    assert latest["status"] == "failed"
    assert latest["exit_code"] == 0
    assert latest["output_bytes"] > 0
    assert "Execution failed: CAPIError" in details["output"]


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/copilot")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_can_use_copilot_runtime_from_env_default(mock_popen, mock_which, workspace, monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_AGENT_RUNTIME", "copilot")
    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert cmd[0] == "/usr/bin/copilot"

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    assert latest["runtime"] == "copilot"


def test_resolve_runtime_executable_falls_back_to_gh_for_copilot():
    with patch("orchestration.agents.shutil.which") as mock_which:
        mock_which.side_effect = lambda command: {
            "copilot": None,
            "gh": "/usr/bin/gh",
        }.get(command)

        resolved = agents_module._resolve_runtime_executable("copilot")

    assert resolved == "/usr/bin/gh"


def test_progress_prompt_pins_commands_to_current_run(workspace):
    section = agents_module._agent_progress_prompt_section(workspace, run_id="run-abc")

    assert "progress init --run run-abc --item" in section
    assert "aim for roughly 8-10 items unless the task is genuinely simple" in section
    assert "Avoid generic items like 'read the docs', 'do the coding', or 'run tests and handoff'" in section
    assert "progress add --run run-abc --item" in section
    assert "progress set-active <item_id> --run run-abc" in section
    assert "progress complete <item_id> --run run-abc" in section
    assert "progress block <item_id> --run run-abc --message" in section
    assert "Before declaring the run done, make one final checklist pass" in section


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


def test_run_agent_timeout_preserves_partial_claude_output(workspace):
    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    cmd = [
        sys.executable,
        "-c",
        "import sys, time; print('partial timeout output', flush=True); time.sleep(5)",
    ]

    with patch("orchestration.agents._resolve_runtime_executable", return_value="/usr/bin/claude"), \
         patch("orchestration.agents._build_runtime_command", return_value=(cmd, "prompt")):
        with pytest.raises(RuntimeError, match="timed out"):
            run_agent("test_agent", workstream_id=ws.id, timeout=1, base_dir=workspace)

    runs = list_agent_runs(limit=5, base_dir=workspace)
    latest = runs[0]
    details = get_agent_run(latest["run_id"], base_dir=workspace)

    assert latest["status"] == "timeout"
    assert latest["output_bytes"] > 0
    assert latest["first_output_at"] is not None
    assert latest["last_output_at"] is not None
    assert "partial timeout output" in details["output"]


def test_runtime_reported_timeout_detects_cline_timeout():
    assert _runtime_reported_timeout("cline", "Error: Timeout\n", 1) is True
    assert _runtime_reported_timeout("cline", '{"type": "error", "message": "Timeout"}', 1) is True
    assert _runtime_reported_timeout("claude-code", "Error: Timeout\n", 1) is False
    assert _runtime_reported_timeout("cline", "done", 0) is False


# ── ANSI escape code stripping ────────────────────────────────────────

class TestStripAnsiEscapeCodes:
    """Tests for `_strip_ansi_escape_codes`."""

    # ── No-op on clean text ───────────────────────────────────────────

    def test_noop_on_empty(self):
        assert _strip_ansi_escape_codes("") == ""

    def test_noop_on_plain_text(self):
        text = "Hello world.\nThis is clean output."
        assert _strip_ansi_escape_codes(text) == text

    def test_noop_on_markdown(self):
        text = "# Heading\n- bullet\n```python\nprint('hi')\n```"
        assert _strip_ansi_escape_codes(text) == text

    # ── Complete CSI (SGR) sequences ──────────────────────────────────

    def test_strips_reset(self):
        assert _strip_ansi_escape_codes("\x1b[0mtext") == "text"

    def test_strips_dim(self):
        assert _strip_ansi_escape_codes("\x1b[2m- \x1b[0m") == "- "

    def test_strips_color_codes(self):
        assert _strip_ansi_escape_codes("\x1b[31mred\x1b[0m") == "red"
        assert _strip_ansi_escape_codes("\x1b[1;32mbold green\x1b[0m") == "bold green"
        assert _strip_ansi_escape_codes("\x1b[38;5;208morange\x1b[0m") == "orange"

    def test_strips_complex_csi(self):
        assert _strip_ansi_escape_codes("\x1b[1;4;33mtext\x1b[0m") == "text"
        assert _strip_ansi_escape_codes("\x1b[48;2;255;128;0mbg\x1b[0m") == "bg"


    # ── Bare SGR fragments (no ESC) ───────────────────────────────────

    def test_strips_bare_sgr_fragments(self):
        text = "[0m[2m- [0m[2msurvey complete[0m[2m"
        expected = "- survey complete"
        assert _strip_ansi_escape_codes(text) == expected

    def test_strips_bare_sgr_in_mixed_content(self):
        text = "[0m[2mAnd[0m[2m the[0m[2m valid[0m[2m next[0m[2m states"
        expected = "And the valid next states"
        assert _strip_ansi_escape_codes(text) == expected

    def test_strips_bare_dim_around_survey(self):
        text = "[0m[2m- [0m[2m`[0m[2msurvey[0m[2m complete[0m[2m`\n"
        expected = "- `survey complete`\n"
        assert _strip_ansi_escape_codes(text) == expected

    # ── Preserves legitimate text ─────────────────────────────────────

    def test_preserves_bracketed_sections(self):
        text = "[Section 1]\nContent here.\n[Section 2]\nMore content."
        assert _strip_ansi_escape_codes(text) == text

    def test_preserves_array_literals(self):
        text = "['planned', 'reject']"
        assert _strip_ansi_escape_codes(text) == text

    def test_preserves_numeric_references(self):
        text = "See reference [1] for details. In section [2.3] we cover..."
        assert _strip_ansi_escape_codes(text) == text

    def test_preserves_edge_brackets(self):
        text = "[m] [k] [h]"
        assert _strip_ansi_escape_codes(text) == text

    def test_preserves_markdown_links(self):
        text = "[click here](https://example.com)"
        assert _strip_ansi_escape_codes(text) == text

    def test_preserves_task_lists(self):
        text = "- [ ] todo\n- [x] done"
        assert _strip_ansi_escape_codes(text) == text

    def test_preserves_code_blocks(self):
        text = "```\n[0] -> first\n[1] -> second\n```"
        assert _strip_ansi_escape_codes(text) == text


    # ── Agent-like output (integration samples) ───────────────────────

    def test_cleans_survey_complete_output(self):
        text = (
            "\x1b[0m\x1b[2m- \x1b[0m\x1b[2m`\x1b[0m\x1b[2msurvey\x1b[0m\x1b[2m "
            "complete\x1b[0m\x1b[2m`\n\n"
            "\x1b[0m\x1b[2mAnd\x1b[0m\x1b[2m the\x1b[0m\x1b[2m valid\x1b[0m\x1b[2m "
            "next\x1b[0m\x1b[2m states\x1b[0m\x1b[2m for\x1b[0m\x1b[2m "
            "`\x1b[0m\x1b[2mnew\x1b[0m\x1b[2m`\x1b[0m\x1b[2m are\x1b[0m\x1b[2m "
            "`\x1b[0m\x1b[2m['\x1b[0m\x1b[2mplanned\x1b[0m\x1b[2m',\x1b[0m\x1b[2m "
            "'\x1b[0m\x1b[2mre\x1b[0m\x1b[2mject\x1b[0m\x1b[2m']\x1b[0m\x1b[2m`.\n"
        )
        expected = (
            "- `survey complete`\n\n"
            "And the valid next states for `new` are `['planned', 'reject']`.\n"
        )
        assert _strip_ansi_escape_codes(text) == expected

    def test_cleans_mixed_complete_and_bare(self):
        text = "\x1b[2mPart 1\x1b[0m\n[2mPart 2[0m\n\x1b[2mPart 3\x1b[0m"
        expected = "Part 1\nPart 2\nPart 3"
        assert _strip_ansi_escape_codes(text) == expected

    # ── Other ANSI sequence types ─────────────────────────────────────

    def test_strips_character_set_sequences(self):
        text = "\x1b(0line draw\x1b(B"
        assert _strip_ansi_escape_codes(text) == "line draw"

    def test_strips_osc_sequences_bel_terminated(self):
        text = "Before \x1b]0;My Title\x07 after"
        assert _strip_ansi_escape_codes(text) == "Before  after"

    # ── Edge cases ────────────────────────────────────────────────────

    def test_multiple_sequences_consecutive(self):
        assert _strip_ansi_escape_codes("\x1b[0m\x1b[2m\x1b[33mhello") == "hello"

    def test_sequence_at_start(self):
        assert _strip_ansi_escape_codes("\x1b[0mstart") == "start"

    def test_sequence_at_end(self):
        assert _strip_ansi_escape_codes("end\x1b[0m") == "end"

    def test_only_sequences(self):
        assert _strip_ansi_escape_codes("\x1b[0m\x1b[2m") == ""

    def test_unclosed_sequence(self):
        assert _strip_ansi_escape_codes("text\x1b[32") == "text\x1b[32"

