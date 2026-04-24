"""Tests for agent resolution and run diagnostics."""

import json
import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from orchestration.agents import _resolve_agent_file, get_agent_run, run_agent
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


class _FakeProc:
    def __init__(self):
        self.pid = 12345
        self.returncode = 0

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
