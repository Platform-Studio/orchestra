"""Tests for agent resolution and run diagnostics."""

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from orchestration.agents import _classify_run_outcome, _resolve_agent_file, count_active_agent_runs, get_agent_run, list_agent_runs, run_agent
from orchestration.locks import acquire_lock, lock_status
from orchestration.artifacts import create_artifact
from orchestration.tasks import create_task, read_task, _save_task
from orchestration.workstreams import create_workstream, save_workstream
from orchestration.models import RetryConfig


def _write_run_meta(workspace: str, run_id: str, payload: dict) -> None:
    runs_dir = f"{workspace}/.orchestration/agent_runs"
    import os
    os.makedirs(runs_dir, exist_ok=True)
    with open(f"{runs_dir}/{run_id}.json", "w", encoding="utf-8") as f:
        json.dump(payload, f)


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


def test_count_active_agent_runs_ignores_dead_pids_and_prunes_state(workspace):
    state_dir = os.path.join(workspace, ".orchestration")
    os.makedirs(state_dir, exist_ok=True)
    active_path = os.path.join(state_dir, "active_agents.yaml")

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


def test_list_agent_runs_demotes_stale_running_status(workspace):
    run_id = "run-stale"
    meta = _base_run(run_id, "ws-1", ["t-1"])
    meta["status"] = "running"
    meta["ended_at"] = None
    _write_run_meta(workspace, run_id, meta)

    runs = list_agent_runs(limit=20, base_dir=workspace)
    run = next(r for r in runs if r.get("run_id") == run_id)
    assert run["status"] == "stale"


class _FakeProc:
    def __init__(self):
        self.pid = 12345
        self.returncode = 0

    def communicate(self, timeout=None):
        return ("", "")


class _FakeTerminatedProc(_FakeProc):
    def __init__(self):
        self.pid = 12345
        self.returncode = 143

    def communicate(self, timeout=None):
        return ("", "")


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
def test_run_agent_passes_agent_body_as_system_prompt(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Standalone WS", base_dir=workspace)

    run_agent("test_agent", workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    assert "--append-system-prompt" in cmd
    system_prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "You are a test agent." in system_prompt


@patch("orchestration.agents.shutil.which", return_value="/usr/bin/claude")
@patch("orchestration.agents.subprocess.Popen", return_value=_FakeProc())
def test_run_agent_uses_mounted_workspace_root_for_descendant(mock_popen, mock_which, workspace):
    mount_root = os.path.join(workspace, "mounted_repo")
    os.makedirs(mount_root, exist_ok=True)

    parent = create_workstream(name="Mounted Parent", base_dir=workspace)
    parent.mounted_workspace_path = mount_root
    save_workstream(parent, workspace)

    child = create_workstream(name="Mounted Child", parent_id=parent.id, base_dir=workspace)

    run_agent("test_agent", workstream_id=child.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert f"WORKSPACE_ROOT: {mount_root}" in prompt

    popen_env = mock_popen.call_args.kwargs["env"]
    assert popen_env["WORKSPACE_ROOT"] == mount_root
    assert popen_env["ORCHESTRATION_ROOT"] == os.path.abspath(workspace)


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
def test_run_agent_releases_matching_lock(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Lock WS", base_dir=workspace)
    task = create_task(ws.id, title="T", base_dir=workspace)

    acquire_lock(task.id, agent_id="Test Agent", base_dir=workspace)
    assert lock_status(task.id, base_dir=workspace) is not None

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    assert lock_status(task.id, base_dir=workspace) is None


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
def test_run_agent_inlines_attachments_when_workstream_flag_enabled(mock_popen, mock_which, workspace):
    ws = create_workstream(name="Inline WS", base_dir=workspace)
    ws.inline_attachments = True
    save_workstream(ws, workspace)

    create_artifact("reports/brief.md", "inline me", base_dir=workspace, workstream_id=ws.id)
    task = create_task(ws.id, title="Task", attachments=["reports/brief.md"], base_dir=workspace)

    run_agent("test_agent", task_ids=[task.id], workstream_id=ws.id, base_dir=workspace)

    cmd = mock_popen.call_args.args[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Task attachments (artifact paths + inlined content):" in prompt
    assert "- Path: reports/brief.md" in prompt
    assert "inline me" in prompt


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
    assert cmd[0] == "/usr/bin/claude"
    assert "-p" in cmd
    assert "--append-system-prompt" in cmd
    assert "--model" in cmd
    assert "--output-format" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "text"
    assert "--verbose" in cmd
    assert "--effort" not in cmd


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
    assert "--effort" in cmd
    assert cmd[cmd.index("--effort") + 1] == "high"


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
