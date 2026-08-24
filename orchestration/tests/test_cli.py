"""Tests for the CLI interface."""

import csv
import importlib
import json
import shutil
import subprocess
import sys
import os
import pytest
import yaml
import types
from pathlib import Path

from orchestration.cli import build_parser, main


@pytest.fixture(autouse=True)
def _clear_persistence_root_env(monkeypatch):
    monkeypatch.setenv("WORKSTREAM_ROOT", "")
    monkeypatch.setenv("ARTIFACT_ROOT", "")
    monkeypatch.setenv("ARTICACT_ROOT", "")


def run_cli(*args, base_dir=None, cwd=None):
    """Run the CLI and capture output."""
    cmd_args = list(args)
    if base_dir:
        cmd_args = ["--base-dir", base_dir] + cmd_args

    env = os.environ.copy()
    # Keep CLI subprocesses pinned to the test workspace instead of reloading
    # local developer persistence roots from .env during module import.
    env["WORKSTREAM_ROOT"] = ""
    env["ARTIFACT_ROOT"] = ""
    env["ARTICACT_ROOT"] = ""
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "orchestration.cli"] + cmd_args,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd or repo_root,
    )
    return result


def test_cli_loads_dotenv_on_import(monkeypatch):
    calls = []

    fake_dotenv = types.SimpleNamespace(load_dotenv=lambda: calls.append(True))
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    import orchestration.cli as cli_module
    importlib.reload(cli_module)

    assert calls


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

    def test_create_with_explicit_routing_fields(self, workspace):
        working_root = Path(workspace) / "code_root"
        artifact_root = Path(workspace) / "artifact_root"
        child_root = Path(workspace) / "child_root"
        for path in (working_root, artifact_root, child_root):
            path.mkdir()

        result = run_cli(
            "workstream", "create",
            "--name", "Routed",
            "--working-directory", str(working_root),
            "--artifact-root", str(artifact_root),
            "--child-workstream-root", str(child_root),
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["working_directory"] == str(working_root)
        assert data["data"]["artifact_root"] == str(artifact_root)
        assert data["data"]["child_workstream_root"] == str(child_root)

    def test_context_read_and_update(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", "--context", "brief one", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("workstream", "context", ws_id, base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["context"] == "brief one"

        result = run_cli(
            "workstream", "update-context", ws_id,
            "--content", "brief two",
            "--updated-by", "cli-test",
            base_dir=workspace,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["context"] == "brief two"

        audit_path = Path(workspace) / "workspace_audit.yaml"
        audit = yaml.safe_load(audit_path.read_text())
        assert any(e["type"] == "workstream_context_updated" and e["description"] == "Workstream context updated by cli-test" for e in audit)

    def test_agent_concurrency_read_and_update(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("workstream", "agent-concurrency", ws_id, base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["agent_concurrency"] == {}

        policy = json.dumps({
            "default": 1,
            "state_overrides": {
                "Staging Deploy": {"overrides": {"DevOps Engineer": 1}},
                "Production Deploy": {"overrides": {"DevOps Engineer": 1}},
            },
        })
        result = run_cli(
            "workstream", "update-agent-concurrency", ws_id,
            "--policy", policy,
            "--updated-by", "cli-test",
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["agent_concurrency"]["state_overrides"]["Staging Deploy"]["overrides"]["DevOps Engineer"] == 1

        audit_path = Path(workspace) / "workspace_audit.yaml"
        audit = yaml.safe_load(audit_path.read_text())
        assert any(
            e["type"] == "workstream_agent_concurrency_updated"
            and e["description"] == "Workstream agent concurrency updated by cli-test"
            for e in audit
        )

    def test_gettags_and_upsert_tag(self, workspace):
        result = run_cli("workstream", "create", "--name", "Tags", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli(
            "workstream", "upsert-tag", ws_id,
            "--name", "Urgent",
            "--color", "#eb5a46",
            base_dir=workspace,
        )
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"] == {"name": "Urgent", "color": "#eb5a46"}

        run_cli("task", "create", ws_id, "--title", "Card", "--tags", "Urgent, Review", base_dir=workspace)

        result = run_cli("workstream", "gettags", ws_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert {tag["name"] for tag in data} == {"Urgent", "Review"}
        assert next(tag for tag in data if tag["name"] == "Urgent")["color"] == "#eb5a46"

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

    def test_token_usage_writes_csv_to_explicit_output(self, workspace, tmp_path):
        from orchestration.tasks import create_task, _save_task

        result = run_cli("workstream", "create", "--name", "Token Board", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]
        first = create_task(ws_id, title="First, task", base_dir=workspace)
        first.token_usage = {"input_tokens": 120, "output_tokens": 45}
        _save_task(first, base_dir=workspace)
        create_task(ws_id, title="No Tokens", base_dir=workspace)

        output_path = tmp_path / "custom_tokens.csv"
        result = run_cli(
            "workstream", "token-usage", ws_id,
            "--output", str(output_path),
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["path"] == str(output_path)
        assert data["task_count"] == 2

        with output_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))

        assert rows[0] == ["task ID", "task title", "input tokens", "output tokens"]
        by_title = {row[1]: row for row in rows[1:]}
        assert by_title["First, task"] == [first.id, "First, task", "120", "45"]
        assert by_title["No Tokens"][1:] == ["No Tokens", "0", "0"]

    def test_token_usage_default_output_filename(self, workspace, tmp_path):
        result = run_cli("workstream", "create", "--name", "Token Board", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]
        run_cli("task", "create", ws_id, "--title", "Card", base_dir=workspace)

        result = run_cli("workstream", "token-usage", ws_id, base_dir=workspace, cwd=str(tmp_path))
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["path"] == f"tokens_{ws_id}.csv"
        assert data["task_count"] == 1
        assert (tmp_path / f"tokens_{ws_id}.csv").exists()

    def test_token_usage_recovers_placeholder_task_totals_from_agent_runs(self, workspace, tmp_path):
        result = run_cli("workstream", "create", "--name", "Token Board", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]
        task_id = "task-placeholder"
        tasks_dir = Path(workspace) / "workstreams" / ws_id / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / f"{task_id}.yaml").write_text(
            "\n".join([
                f"id: {task_id}",
                f"workstream_id: {ws_id}",
                "title: '[CORRUPT] Original task title'",
                "status: _error",
                "tags:",
                "- _error",
                "comments: []",
                "audit: []",
                "attachments: []",
                "rank: '1024.0'",
            ]) + "\n",
            encoding="utf-8",
        )
        runs_dir = Path(workspace) / ".orchestration" / "agent_runs"
        runs_dir.mkdir(parents=True)
        (runs_dir / "run.json").write_text(
            json.dumps({
                "beans_proxy": {
                    "task_totals": {
                        "pseudo_key": f"task-{task_id}",
                        "input_tokens": 321,
                        "output_tokens": 45,
                        "updated_at": "2026-06-25T00:00:00+00:00",
                    }
                }
            }),
            encoding="utf-8",
        )

        output_path = tmp_path / "tokens.csv"
        result = run_cli(
            "workstream", "token-usage", ws_id,
            "--output", str(output_path),
            base_dir=workspace,
        )
        assert result.returncode == 0

        with output_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows == [
            ["task ID", "task title", "input tokens", "output tokens"],
            [task_id, "Original task title", "321", "45"],
        ]

    def test_migrate_artifact_root_home(self, workspace, tmp_path):
        mount_root = tmp_path / "mounted_repo"
        mount_root.mkdir()

        result = run_cli("workstream", "create", "--name", "Product", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        root_yaml = Path(workspace) / "workstreams" / f"{ws_id}.yaml"
        data = yaml.safe_load(root_yaml.read_text())
        data["working_directory"] = str(mount_root)
        data["artifact_root"] = str(mount_root)
        root_yaml.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

        result = run_cli("workstream", "create", "--name", "Child", "--parent", ws_id, base_dir=workspace)
        child_id = json.loads(result.stdout)["data"]["id"]
        result = run_cli(
            "artifact", "create",
            "--path", "reports/one.md",
            "--content", "hello",
            "--workstream", ws_id,
            base_dir=workspace,
        )
        assert result.returncode == 0

        result = run_cli("workstream", "migrate-artifact-root-home", ws_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["planned_artifact_file_count"] == 1

        result = run_cli("workstream", "migrate-artifact-root-home", ws_id, "--apply", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["applied"] is True

        reloaded = yaml.safe_load(root_yaml.read_text())
        assert reloaded.get("artifact_root") is None
        assert Path(workspace, "artifacts", "reports", "one.md").exists()

    def test_migrate_artifact_root_home_can_archive_conflicts(self, workspace, tmp_path):
        mount_root = tmp_path / "mounted_repo"
        mount_root.mkdir()

        result = run_cli("workstream", "create", "--name", "Product", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        root_yaml = Path(workspace) / "workstreams" / f"{ws_id}.yaml"
        data = yaml.safe_load(root_yaml.read_text())
        data["working_directory"] = str(mount_root)
        data["artifact_root"] = str(mount_root)
        root_yaml.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

        result = run_cli(
            "artifact", "create",
            "--path", "reports/one.md",
            "--content", "source",
            "--workstream", ws_id,
            base_dir=workspace,
        )
        assert result.returncode == 0

        result = run_cli(
            "artifact", "create",
            "--path", "reports/one.md",
            "--content", "dest",
            base_dir=workspace,
        )
        assert result.returncode == 0

        result = run_cli(
            "workstream", "migrate-artifact-root-home", ws_id,
            "--apply", "--archive-conflicts",
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["applied"] is True
        assert Path(workspace, "artifacts", "_migration_conflicts", ws_id, "reports", "one.md").exists()

    def test_migrate_child_layout(self, workspace):
        result = run_cli("workstream", "create", "--name", "Root", base_dir=workspace)
        root_id = json.loads(result.stdout)["data"]["id"]
        result = run_cli("workstream", "create", "--name", "Child", "--parent", root_id, base_dir=workspace)
        child_id = json.loads(result.stdout)["data"]["id"]

        flat_yaml = Path(workspace) / "workstreams" / f"{child_id}.yaml"
        target_yaml = Path(workspace) / "workstreams" / root_id / "workstreams" / f"{child_id}.yaml"
        target_dir = Path(workspace) / "workstreams" / root_id / "workstreams" / child_id
        flat_yaml.write_text(target_yaml.read_text())
        shutil.copytree(target_dir, Path(workspace) / "workstreams" / child_id)
        target_yaml.unlink()
        shutil.rmtree(target_dir)

        result = run_cli("workstream", "migrate-child-layout", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert child_id in data["data"]["moved_workstream_ids"]

        result = run_cli("workstream", "migrate-child-layout", "--apply", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["data"]["applied"] is True
        assert target_yaml.exists()
        assert not flat_yaml.exists()


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

        result = run_cli("artifact", "create", "--path", "Theses/attach.md", "--content", "hello", base_dir=workspace)
        assert result.returncode == 0

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

    def test_reorder_commands_before_after_and_index(self, workspace):
        result = run_cli(
            "workstream", "create", "--name", "WS",
            "--states", json.dumps({"To Do": ["Done"], "Done": []}),
            base_dir=workspace,
        )
        ws_id = json.loads(result.stdout)["data"]["id"]

        t1 = json.loads(run_cli("task", "create", ws_id, "--title", "T1", base_dir=workspace).stdout)["data"]["id"]
        t2 = json.loads(run_cli("task", "create", ws_id, "--title", "T2", base_dir=workspace).stdout)["data"]["id"]
        t3 = json.loads(run_cli("task", "create", ws_id, "--title", "T3", base_dir=workspace).stdout)["data"]["id"]

        # New tasks are inserted at the top, so the initial order is [T3, T2, T1].
        # Move T3 immediately before T1 -> [T2, T3, T1]
        result = run_cli("task", "move-before", t3, t1, base_dir=workspace)
        assert result.returncode == 0

        # Move T1 immediately after T2 -> [T2, T1, T3]
        result = run_cli("task", "move-after", t1, t2, base_dir=workspace)
        assert result.returncode == 0

        # T2 is already at index 0, so this is a no-op.
        result = run_cli("task", "move-to-index", t2, "0", base_dir=workspace)
        assert result.returncode == 0

        listed = json.loads(run_cli("task", "list", ws_id, "--status", "To Do", base_dir=workspace).stdout)["data"]
        ordered_ids = [t["id"] for t in listed]
        assert ordered_ids == [t2, t1, t3]


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

    def test_lock_list_returns_active_workstream_locks(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("task", "create", ws_id, "--title", "T1", base_dir=workspace)
        task_one = json.loads(result.stdout)["data"]["id"]
        result = run_cli("task", "create", ws_id, "--title", "T2", base_dir=workspace)
        task_two = json.loads(result.stdout)["data"]["id"]

        assert run_cli("lock", "acquire", task_one, "--agent", "agent-1", base_dir=workspace).returncode == 0
        assert run_cli("lock", "acquire", task_two, "--agent", "agent-2", base_dir=workspace).returncode == 0

        result = run_cli("lock", "list", ws_id, base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data[task_one]["locked"] is True
        assert data[task_two]["agent_id"] == "agent-2"


class TestCLIArtifact:
    def test_artifact_lifecycle(self, workspace):
        # Create
        result = run_cli("artifact", "create", "--path", "test.md", "--content", "# Hello", base_dir=workspace)
        assert result.returncode == 0

        # Read
        result = run_cli("artifact", "read", "test.md", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["mode"] == "text"
        assert data["content"] == "# Hello"
        assert data["resolved_path"].endswith("/artifacts/test.md")

        # List
        result = run_cli("artifact", "list", base_dir=workspace)
        assert result.returncode == 0
        assert "test.md" in json.loads(result.stdout)["data"]

    def test_artifact_create_binary_from_base64(self, workspace):
        payload_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6p8n8AAAAASUVORK5CYII="

        result = run_cli(
            "artifact", "create",
            "--path", "assets/pixel.png",
            "--content-base64", payload_base64,
            base_dir=workspace,
        )

        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["mode"] == "binary"
        assert data["bytes_written"] > 0
        assert (Path(workspace) / "artifacts" / "assets" / "pixel.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    def test_artifact_read_returns_metadata_for_binary_files(self, workspace):
        payload_base64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6p8n8AAAAASUVORK5CYII="

        create_result = run_cli(
            "artifact", "create",
            "--path", "assets/pixel.png",
            "--content-base64", payload_base64,
            base_dir=workspace,
        )
        assert create_result.returncode == 0

        result = run_cli("artifact", "read", "assets/pixel.png", base_dir=workspace)

        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["path"] == "assets/pixel.png"
        assert data["mode"] == "binary"
        assert data["content_type"] == "image/png"
        assert data["bytes"] > 0
        assert data["resolved_path"].endswith("/artifacts/assets/pixel.png")
        assert "content" not in data

    def test_artifact_create_rejects_text_mode_for_raster_images(self, workspace):
        result = run_cli(
            "artifact", "create",
            "--path", "assets/pixel.png",
            "--content", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB",
            base_dir=workspace,
        )

        assert result.returncode != 0
        assert "Raster image artifacts cannot be written with --content" in result.stderr

    def test_artifact_copytree_can_target_local_artifact_store(self, workspace):
        ws_result = run_cli("workstream", "create", "--name", "Local WS", base_dir=workspace)
        ws_id = json.loads(ws_result.stdout)["data"]["id"]
        run_cli(
            "artifact", "create",
            "--path", "Stage 2 Research/example/readme.md",
            "--content", "hello",
            base_dir=workspace,
        )

        result = run_cli(
            "artifact", "copytree", "Stage 2 Research/example",
            "--workstream", ws_id,
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["skipped_same_count"] == 1

    def test_artifact_copytree_copies_to_workspace_artifacts_for_working_directory_context(self, workspace):
        working_root = Path(workspace) / "mounted_repo"
        working_root.mkdir()

        parent_result = run_cli(
            "workstream", "create",
            "--name", "Startup",
            "--working-directory", str(working_root),
            base_dir=workspace,
        )
        parent_id = json.loads(parent_result.stdout)["data"]["id"]

        child_result = run_cli(
            "workstream", "create",
            "--name", "Product Development",
            "--parent", parent_id,
            base_dir=workspace,
        )
        child_id = json.loads(child_result.stdout)["data"]["id"]

        run_cli(
            "artifact", "create",
            "--path", "Stage 2 Research/example/readme.md",
            "--content", "hello",
            base_dir=workspace,
        )

        result = run_cli(
            "artifact", "copytree", "Stage 2 Research/example",
            "--workstream", child_id,
            "--source-base", workspace,
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["copy_count"] == 0
        assert data["skipped_same_count"] == 1
        assert (Path(workspace) / "artifacts" / "Stage 2 Research" / "example" / "readme.md").read_text() == "hello"

    def test_artifact_read_logs_audit_when_run_context_present(self, workspace, monkeypatch):
        run_cli("artifact", "create", "--path", "x.md", "--content", "hello", base_dir=workspace)
        monkeypatch.setenv("ORCHESTRATION_AGENT_RUN_ID", "run-123")
        monkeypatch.setenv("ORCHESTRATION_AGENT_NAME", "JTBD Analyst")
        monkeypatch.setenv("ORCHESTRATION_AGENT_TASK_IDS", '["task-1","task-2"]')

        result = run_cli("artifact", "read", "x.md", base_dir=workspace)
        assert result.returncode == 0

        from orchestration.workspace_audit import get_audit_log
        entries = get_audit_log(base_dir=workspace, event_type="artifact_read")
        assert len(entries) == 1
        e = entries[0]
        assert e["run_id"] == "run-123"
        assert e["agent"] == "JTBD Analyst"
        assert e["artifact_path"] == "x.md"
        assert e["task_ids"] == ["task-1", "task-2"]

    def test_artifact_read_no_agent_context_does_not_log(self, workspace):
        run_cli("artifact", "create", "--path", "y.md", "--content", "hello", base_dir=workspace)
        result = run_cli("artifact", "read", "y.md", base_dir=workspace)
        assert result.returncode == 0

        from orchestration.workspace_audit import get_audit_log
        entries = get_audit_log(base_dir=workspace, event_type="artifact_read")
        assert entries == []

    def test_artifact_commands_infer_workstream_from_agent_context(self, workspace, monkeypatch):
        working_root = Path(workspace) / "career_pivot_repo"
        working_root.mkdir()

        result = run_cli(
            "workstream", "create",
            "--name", "Career Pivot",
            "--working-directory", str(working_root),
            base_dir=workspace,
        )
        parent_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli(
            "workstream", "create",
            "--name", "Go-to-Market",
            "--parent", parent_id,
            base_dir=workspace,
        )
        ws_id = json.loads(result.stdout)["data"]["id"]

        monkeypatch.setenv("ORCHESTRATION_AGENT_WORKSTREAM_ID", ws_id)

        result = run_cli(
            "artifact", "create",
            "--path", "reports/hello.md",
            "--content", "hello",
            base_dir=workspace,
        )
        assert result.returncode == 0

        created_path = Path(workspace) / "artifacts" / "reports" / "hello.md"
        assert created_path.exists()

        result = run_cli("artifact", "read", "reports/hello.md", base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"]["content"] == "hello"

        result = run_cli("artifact", "list", "--prefix", "reports/", base_dir=workspace)
        assert result.returncode == 0
        assert json.loads(result.stdout)["data"] == ["reports/hello.md"]


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
        assert data["filter"] == {"state": "To Do"}

    def test_create_schedule_trigger_with_state_and_tag_filter(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli("trigger", "create", ws_id,
                         "--on-schedule", "0 * * * *",
                         "--filter", '{"state": "Live", "tag": "requestor_notify"}',
                         "--action", "run_agent",
                         "--agent", "product_feedback_loopback",
                         base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["on_schedule"] == "0 * * * *"
        assert data["filter"] == {"state": "Live", "tag": "requestor_notify"}
        assert data["agent"] == "product_feedback_loopback"

    def test_create_state_trigger_with_task_selection(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli(
            "trigger",
            "create",
            ws_id,
            "--on-state",
            "To Do",
            "--task-selection",
            "all_unlocked",
            "--action",
            "run_agent",
            "--agent",
            "test_agent",
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["task_selection"] == "all_unlocked"

    def test_create_email_trigger(self, workspace):
        result = run_cli("workstream", "create", "--name", "WS", base_dir=workspace)
        ws_id = json.loads(result.stdout)["data"]["id"]

        result = run_cli(
            "trigger",
            "create",
            ws_id,
            "--on-email-recipient",
            "build@guild.platformstud.io",
            "--on-email-event",
            "new_thread",
            "--action",
            "run_agent",
            "--agent",
            "startup_vendor",
            base_dir=workspace,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert data["on_email"] == {
            "recipient": "build@guild.platformstud.io",
            "event": "new_thread",
        }

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

    def test_agent_active_accepts_legacy_list_format(self, workspace):
        state_dir = Path(workspace) / ".orchestration"
        state_dir.mkdir(parents=True)
        active_yaml = state_dir / "active_agents.yaml"
        active_yaml.write_text(yaml.safe_dump([
            {
                "run_id": "run-legacy-001",
                "agent": "seo_indexer",
                "started_at": "2026-04-22T10:00:00+00:00",
                "log_path": ".orchestration/agent_runs/run-legacy-001.log",
            }
        ], sort_keys=False), encoding="utf-8")

        result = run_cli("agent", "active", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert data["status"] == "ok"
        assert isinstance(data["data"], list)
        assert data["data"][0]["run_id"] == "run-legacy-001"

    def test_agent_kill_marks_run_killed(self, workspace):
        state_dir = Path(workspace) / ".orchestration"
        runs_dir = state_dir / "agent_runs"
        runs_dir.mkdir(parents=True)

        run_id = "run-kill-001"
        run_meta = runs_dir / f"{run_id}.json"
        run_meta.write_text(json.dumps({
            "run_id": run_id,
            "agent": "LinkedIn SDR",
            "status": "running",
            "started_at": "2026-04-22T10:00:00+00:00",
            "ended_at": None,
            "exit_code": None,
        }, indent=2), encoding="utf-8")

        active_yaml = state_dir / "active_agents.yaml"
        active_yaml.write_text(yaml.safe_dump({
            "runs": [{
                "run_id": run_id,
                "agent": "LinkedIn SDR",
                "started_at": "2026-04-22T10:00:00+00:00",
                "log_path": ".orchestration/agent_runs/run-kill-001.log",
            }]
        }, sort_keys=False), encoding="utf-8")

        result = run_cli("agent", "kill", run_id, base_dir=workspace)
        assert result.returncode == 0
        payload = json.loads(result.stdout)["data"]
        assert payload["status"] == "killed"

        # Reload metadata and ensure terminal status is recorded by the command.
        reloaded = json.loads(run_meta.read_text(encoding="utf-8"))
        assert reloaded["status"] == "killed"


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

    def test_scheduler_tick_runs_when_scheduler_not_running(self, workspace):
        result = run_cli("scheduler", "tick", base_dir=workspace)
        assert result.returncode == 0
        data = json.loads(result.stdout)["data"]
        assert "last_tick_at" in data

    def test_scheduler_tick_fails_when_scheduler_running(self, workspace):
        state_path = Path(workspace) / "scheduler_state.yaml"
        state_path.write_text(yaml.safe_dump({"pid": os.getpid()}), encoding="utf-8")

        result = run_cli("scheduler", "tick", base_dir=workspace)
        assert result.returncode != 0
        error = json.loads(result.stderr)
        assert error["code"] == "RUNTIME_ERROR"
        assert "disabled while scheduler is running" in error["message"]


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
