"""Tests for the CLI interface."""

import json
import subprocess
import sys
import os
import pytest

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
