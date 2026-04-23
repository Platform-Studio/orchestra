"""Tests for the CLI interface."""

import json
import subprocess
import sys
import os
import pytest
import yaml
from pathlib import Path

from orchestration.cli import build_parser, main


def run_cli(*args, base_dir=None):
    """Run the CLI and capture output."""
    cmd_args = list(args)
    if base_dir:
        cmd_args = ["--base-dir", base_dir] + cmd_args

    result = subprocess.run(
        [sys.executable, "-m", "orchestration.cli"] + cmd_args,
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    return result


class TestCLIWorkstream:
    def test_create_and_list(self, workspace):
        # Create
        result = run_cli("workstream", "create", "--name", "Test WS", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"
        ws_id = data["data"]["id"]

        # List
        result = run_cli("workstream", "list", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data["data"]) == 1

        # Read
        result = run_cli("workstream", "read", ws_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["name"] == "Test WS"

    def test_find(self, workspace):
        run_cli("workstream", "create", "--name", "SDR Outreach", base_dir=workspace)
        run_cli("workstream", "create", "--name", "Product Dev", base_dir=workspace)
        result = run_cli("workstream", "find", "--query", "sdr", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert len(data["data"]) == 1

    def test_create_with_states(self, workspace):
        states = json.dumps({"Open": ["Closed"], "Closed": []})
        result = run_cli("workstream", "create", "--name", "Custom", "--states", states, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "Open" in data["data"]["task_states"]

    def test_create_with_mounted_workspace_path(self, workspace):
        mount_root = Path(workspace) / "external_repo"
        mount_root.mkdir()
        result = run_cli(
            "workstream", "create",
            "--name", "Career Pivot",
            "--mounted-workspace-path", str(mount_root),
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["mounted_workspace_path"] == str(mount_root)

    def test_tree(self, workspace):
        # Create parent
        result = run_cli("workstream", "create", "--name", "Root", base_dir=workspace)
        root_id = json.loads(result.stdout)["data"]["id"]

        # Create child
        result = run_cli("workstream", "create", "--name", "Child", "--parent", root_id, base_dir=workspace)
        child_id = json.loads(result.stdout)["data"]["id"]

        # Create grandchild
        run_cli("workstream", "create", "--name", "Grandchild", "--parent", child_id, base_dir=workspace)

        # Tree
        result = run_cli("workstream", "tree", base_dir=workspace)
        assert result.returncode == 0
        assert "Root" in result.stdout
        assert "└── Child" in result.stdout
        assert "└── Grandchild" in result.stdout

    def test_descendants(self, workspace):
        # Create root -> child -> grandchild
        result = run_cli("workstream", "create", "--name", "Root", base_dir=workspace)
        root_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("workstream", "create", "--name", "Child", "--parent", root_id, base_dir=workspace)
        child_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("workstream", "create", "--name", "Grandchild", "--parent", child_id, base_dir=workspace)
        grandchild_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("workstream", "descendants", root_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        rows = data["data"]

        assert len(rows) == 2
        ids = {r["id"] for r in rows}
        assert child_id in ids
        assert grandchild_id in ids
        depths = {r["id"]: r["depth"] for r in rows}
        assert depths[child_id] == 1
        assert depths[grandchild_id] == 2

    def test_descendants_include_self(self, workspace):
        result = run_cli("workstream", "create", "--name", "Root", base_dir=workspace)
        root_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("workstream", "create", "--name", "Child", "--parent", root_id, base_dir=workspace)
        child_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("workstream", "descendants", root_id, "--include-self", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        rows = data["data"]

        assert len(rows) == 2
        depths = {r["id"]: r["depth"] for r in rows}
        assert depths[root_id] == 0
        assert depths[child_id] == 1


class TestCLITask:
    def test_full_lifecycle(self, workspace):
        # Create workstream
        result = run_cli("workstream", "create", "--name", "WS",
                         "--states", json.dumps({"To Do": ["Done"], "Done": []}),
                         base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        # Create task
        result = run_cli("task", "create", ws_id, "--title", "My Task", base_dir=workspace)
        assert result.returncode == 0
        task_id = json.loads(result.stdout)["data"]["id"]

        # Read task
        result = run_cli("task", "read", task_id, base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["title"] == "My Task"

        # Update status
        result = run_cli("task", "update", task_id, "--status", "Done", base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["status"] == "Done"

        # Comment
        result = run_cli("task", "comment", task_id, "--message", "Hello", base_dir=workspace)
        assert result.returncode == 0

        # Audit
        result = run_cli("task", "audit", task_id, base_dir=workspace)
        assert result.returncode == 0
        audit = json.loads(result.stdout)["data"]
        assert len(audit) >= 2

        # Archive
        result = run_cli("task", "archive", task_id, base_dir=workspace)
        assert result.returncode == 0

    def test_create_task_with_attachments(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli(
            "task", "create", ws_id,
            "--title", "My Task",
            "--attachment", "Theses/a.md",
            "--attachment", "Theses/b.md",
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["attachments"] == ["Theses/a.md", "Theses/b.md"]

    def test_attach_and_detach_task_attachment(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("task", "create", ws_id, "--title", "My Task", base_dir=workspace)
        task_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("task", "attach", task_id, "--path", "Theses/attach.md", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["attachments"] == ["Theses/attach.md"]

        result = run_cli("task", "detach", task_id, "--path", "Theses/attach.md", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["attachments"] == []

    def test_invalid_transition_error(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS",
                         "--states", json.dumps({"Open": ["Closed"], "Closed": []}),
                         base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("task", "create", ws_id, "--title", "T", base_dir=workspace)
        task_id = json.loads(result.stdout)["data"]["id"]

        # Try invalid transition: Open -> Open (same state, no-op)
        # Open can only go to Closed
        result = run_cli("task", "update", task_id, "--status", "Nonexistent", base_dir=workspace)
        assert result.returncode != 0
        error = json.loads(result.stderr)
        assert error["code"] == "INVALID_TRANSITION"


class TestCLILock:
    def test_lock_lifecycle(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("task", "create", ws_id, "--title", "T", base_dir=workspace)
        task_id = json.loads(result.stdout)["data"]["id"]

        # Acquire
        result = run_cli("lock", "acquire", task_id, "--agent", "agent-1", base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["agent_id"] == "agent-1"

        # Status
        result = run_cli("lock", "status", task_id, base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["locked"] is True

        # Release
        result = run_cli("lock", "release", task_id, "--agent", "agent-1", base_dir=workspace)
        assert result.returncode == 0

        # Status after release
        result = run_cli("lock", "status", task_id, base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["locked"] is False


class TestCLIArtifact:
    def test_artifact_lifecycle(self, workspace):
        # Create
        result = run_cli("artifact", "create", "--path", "test.md", "--content", "# Hello", base_dir=workspace)
        assert result.returncode == 0

        # Read
        result = run_cli("artifact", "read", "test.md", base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["content"] == "# Hello"

        # List
        result = run_cli("artifact", "list", base_dir=workspace)
        assert result.returncode == 0
        assert "test.md" in json.loads(result.stdout)["data"]

    def test_artifact_read_logs_audit_when_run_context_present(self, workspace, monkeypatch):
        run_cli("artifact", "create", "--path", "x.md", "--content", "hello", base_dir=workspace)
        monkeypatch.setenv("ORCHESTRATION_AGENT_RUN_ID", "run-123")
        monkeypatch.setenv("ORCHESTRATION_AGENT_NAME", "JTBD Analyst")
        monkeypatch.setenv("ORCHESTRATION_AGENT_TASK_IDS", '["task-1","task-2"]')
        monkeypatch.setenv("ORCHESTRATION_AGENT_WORKSTREAM_ID", "ws-9")

        result = run_cli("artifact", "read", "x.md", base_dir=workspace)
        assert result.returncode == 0

        from orchestration.workspace_audit import get_audit_log
        entries = get_audit_log(base_dir=workspace, event_type="artifact_read")
        assert len(entries) == 1
        e = entries[0]
        assert e["run_id"] == "run-123"
        assert e["agent"] == "JTBD Analyst"
        assert e["artifact_path"] == "x.md"
        assert e["workstream_id"] == "ws-9"
        assert e["task_ids"] == ["task-1", "task-2"]

    def test_artifact_read_no_agent_context_does_not_log(self, workspace):
        run_cli("artifact", "create", "--path", "y.md", "--content", "hello", base_dir=workspace)
        result = run_cli("artifact", "read", "y.md", base_dir=workspace)
        assert result.returncode == 0

        from orchestration.workspace_audit import get_audit_log
        entries = get_audit_log(base_dir=workspace, event_type="artifact_read")
        assert entries == []


class TestCLITrigger:
    def test_trigger_lifecycle(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        # Create trigger
        result = run_cli("trigger", "create", ws_id,
                         "--on-state", "pending",
                         "--action", "run_command",
                         "--command", "echo hello",
                         base_dir=workspace)
        assert result.returncode == 0
        trigger_id = json.loads(result.stdout)["data"]["id"]

        # List triggers
        result = run_cli("trigger", "list", ws_id, base_dir=workspace)
        assert result.returncode == 0
        assert len(json.loads(result.stdout)["data"]) == 1

        # Delete trigger
        result = run_cli("trigger", "delete", trigger_id, base_dir=workspace)
        assert result.returncode == 0

        # Verify deleted
        result = run_cli("trigger", "list", ws_id, base_dir=workspace)
        assert len(json.loads(result.stdout)["data"]) == 0


class TestCLIScheduleTrigger:
    def test_create_schedule_trigger(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("trigger", "create", ws_id,
                         "--on-schedule", "0 9 * * 1",
                         "--filter", '{"state": "To Do"}',
                         "--action", "run_command",
                         "--command", "echo weekly",
                         base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["on_schedule"] == "0 9 * * 1"

    def test_create_trigger_requires_state_or_schedule(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("trigger", "create", ws_id,
                         "--action", "run_command",
                         "--command", "echo oops",
                         base_dir=workspace)
        assert result.returncode == 1
        data = json.loads(result.stderr)
        assert data["status"] == "error"


class TestCLITriggerPrompt:
    def test_create_trigger_with_prompt(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("trigger", "create", ws_id,
                         "--on-state", "pending",
                         "--action", "run_agent",
                         "--agent", "test_agent",
                         "--prompt", "skip the survey step",
                         base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["prompt"] == "skip the survey step"

    def test_create_trigger_without_prompt(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("trigger", "create", ws_id,
                         "--on-state", "pending",
                         "--action", "run_command",
                         "--command", "echo hi",
                         base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert "prompt" not in data


class TestCLIAgentRuntime:
    def test_agent_active_empty(self, workspace):
        result = run_cli("agent", "active", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"
        assert data["data"] == []

    def test_agent_tail_reads_recent_lines(self, workspace):
        state_dir = Path(workspace) / ".orchestration"
        runs_dir = state_dir / "agent_runs"
        runs_dir.mkdir(parents=True)

        run_id = "run-test-001"
        log_path = runs_dir / f"{run_id}.log"
        log_path.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")

        active_yaml = state_dir / "active_agents.yaml"
        active_yaml.write_text(yaml.safe_dump({
            "runs": [{
                "run_id": run_id,
                "agent": "seo_indexer",
                "started_at": "2026-04-22T10:00:00+00:00",
                "log_path": str(Path(".orchestration") / "agent_runs" / f"{run_id}.log"),
            }]
        }, sort_keys=False), encoding="utf-8")

        result = run_cli("agent", "tail", run_id, "--lines", "2", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"
        assert data["data"]["tail"] == "line-2\nline-3\n"


class TestCLITaskSchedule:
    def test_create_task_with_schedule(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        future = "2030-01-01T00:00:00+00:00"
        action = '{"type": "run_command", "command": "echo sched"}'

        result = run_cli("task", "create", ws_id,
                         "--title", "Scheduled Task",
                         "--scheduled-at", future,
                         "--scheduled-action", action,
                         base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["scheduled_at"] == future

    def test_clear_schedule(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        future = "2030-01-01T00:00:00+00:00"
        action = '{"type": "run_command", "command": "echo sched"}'

        result = run_cli("task", "create", ws_id,
                         "--title", "T",
                         "--scheduled-at", future,
                         "--scheduled-action", action,
                         base_dir=workspace)
        task_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("task", "clear-schedule", task_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data.get("scheduled_at") is None


class TestCLIWorkstreamPauseResume:
    def test_pause_and_resume(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        # Pause
        result = run_cli("workstream", "pause", ws_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["paused"] is True

        # Resume
        result = run_cli("workstream", "resume", ws_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data.get("paused", False) is False


class TestCLIScheduler:
    def test_scheduler_status(self, workspace):
        result = run_cli("scheduler", "status", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert "running" in data


class TestCLIEnv:
    def test_set_get_list_local(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("env", "set", ws_id, "MODEL", "gpt-5", base_dir=workspace)
        assert result.returncode == 0

        result = run_cli("env", "get", "MODEL", "--workstream", ws_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["value"] == "gpt-5"

        result = run_cli("env", "list", "--workstream", ws_id, "--local", base_dir=workspace)
        assert result.returncode == 0
        env_map = json.loads(result.stdout)["data"]
        assert env_map["MODEL"] == "gpt-5"

    def test_inheritance_and_override(self, workspace):
        result = run_cli("workstream", "create", "--name", "Parent", base_dir=workspace)
        parent_id = json.loads(result.stdout)["data"]["id"]
        result = run_cli("workstream", "create", "--name", "Child", "--parent", parent_id, base_dir=workspace)
        child_id = json.loads(result.stdout)["data"]["id"]

        run_cli("env", "set", parent_id, "API_URL", "https://parent.example", base_dir=workspace)
        result = run_cli("env", "get", "API_URL", "--workstream", child_id, base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["value"] == "https://parent.example"

        run_cli("env", "set", child_id, "API_URL", "https://child.example", base_dir=workspace)
        result = run_cli("env", "get", "API_URL", "--workstream", child_id, base_dir=workspace)
        assert json.loads(result.stdout)["data"]["value"] == "https://child.example"

    def test_unset_masks_parent_and_system(self, workspace, monkeypatch):
        monkeypatch.setenv("SECRET", "from-system")
        result = run_cli("workstream", "create", "--name", "Parent", base_dir=workspace)
        parent_id = json.loads(result.stdout)["data"]["id"]
        result = run_cli("workstream", "create", "--name", "Child", "--parent", parent_id, base_dir=workspace)
        child_id = json.loads(result.stdout)["data"]["id"]

        run_cli("env", "set", parent_id, "SECRET", "from-parent", base_dir=workspace)
        result = run_cli("env", "unset", child_id, "SECRET", base_dir=workspace)
        assert result.returncode == 0

        result = run_cli("env", "get", "SECRET", "--workstream", child_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["value"] is None
        assert data["found"] is False

    def test_get_uses_task_context_and_fallback_system(self, workspace, monkeypatch):
        monkeypatch.setenv("ONLY_SYSTEM", "sys-value")
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]
        result = run_cli("task", "create", ws_id, "--title", "T", base_dir=workspace)
        task_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("env", "get", "ONLY_SYSTEM", "--task", task_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["value"] == "sys-value"
        assert data["found"] is True
