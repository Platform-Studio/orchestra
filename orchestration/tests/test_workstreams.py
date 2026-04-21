"""Tests for workstream operations."""

import pytest
from orchestration.workstreams import (
    create_workstream,
    list_workstreams,
    read_workstream,
    find_workstreams,
    save_workstream,
    set_workstream_env_key,
    unset_workstream_env_key,
    resolve_workstream_env_key,
    resolve_env_key,
    list_effective_workstream_env,
)
from orchestration.tasks import create_task


class TestCreateWorkstream:
    def test_create_basic(self, workspace):
        ws = create_workstream(name="Test WS", base_dir=workspace)
        assert ws.name == "Test WS"
        assert ws.id is not None
        assert ws.description is None
        assert "pending" in ws.task_states

    def test_create_with_description(self, workspace):
        ws = create_workstream(
            name="SDR Outreach",
            description="Manage leads",
            base_dir=workspace,
        )
        assert ws.description == "Manage leads"

    def test_create_with_custom_states(self, workspace):
        states = {
            "To Do": ["In Progress"],
            "In Progress": ["Done", "Failed"],
            "Done": [],
            "Failed": ["To Do"],
        }
        ws = create_workstream(name="Custom", task_states=states, base_dir=workspace)
        assert ws.task_states == states
        assert ws.initial_status() == "To Do"

    def test_create_with_retry(self, workspace):
        retry = {"max_retries": 5, "backoff": "linear", "base_seconds": 30}
        ws = create_workstream(name="Retry WS", retry=retry, base_dir=workspace)
        assert ws.retry.max_retries == 5
        assert ws.retry.backoff == "linear"
        assert ws.retry.base_seconds == 30

    def test_create_with_parent(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)
        assert child.parent_id == parent.id

    def test_creates_directories(self, workspace):
        import os
        ws = create_workstream(name="Dir Test", base_dir=workspace)
        assert os.path.exists(os.path.join(workspace, "workstreams", f"{ws.id}.yaml"))
        assert os.path.isdir(os.path.join(workspace, "workstreams", ws.id, "tasks"))


class TestListWorkstreams:
    def test_list_empty(self, workspace):
        result = list_workstreams(base_dir=workspace)
        assert result == []

    def test_list_multiple(self, workspace):
        create_workstream(name="WS 1", base_dir=workspace)
        create_workstream(name="WS 2", base_dir=workspace)
        result = list_workstreams(base_dir=workspace)
        assert len(result) == 2


class TestReadWorkstream:
    def test_read_existing(self, workspace):
        ws = create_workstream(name="Read Test", base_dir=workspace)
        loaded = read_workstream(ws.id, base_dir=workspace)
        assert loaded.name == "Read Test"
        assert loaded.id == ws.id

    def test_read_not_found(self, workspace):
        with pytest.raises(FileNotFoundError):
            read_workstream("nonexistent-id", base_dir=workspace)


class TestFindWorkstreams:
    def test_find_by_name(self, workspace):
        create_workstream(name="SDR Outreach", base_dir=workspace)
        create_workstream(name="Product Dev", base_dir=workspace)
        results = find_workstreams("sdr", base_dir=workspace)
        assert len(results) == 1
        assert results[0].name == "SDR Outreach"

    def test_find_by_description(self, workspace):
        create_workstream(name="WS1", description="sales pipeline", base_dir=workspace)
        results = find_workstreams("pipeline", base_dir=workspace)
        assert len(results) == 1

    def test_find_no_match(self, workspace):
        create_workstream(name="WS1", base_dir=workspace)
        results = find_workstreams("nonexistent", base_dir=workspace)
        assert len(results) == 0


class TestValidateTransition:
    def test_valid_transition(self, workspace):
        ws = create_workstream(name="Trans", base_dir=workspace)
        assert ws.validate_transition("pending", "in_progress") is True

    def test_invalid_transition(self, workspace):
        ws = create_workstream(name="Trans", base_dir=workspace)
        assert ws.validate_transition("pending", "completed") is False

    def test_custom_states_transition(self, workspace):
        states = {"To Do": ["In Progress"], "In Progress": ["Done"], "Done": []}
        ws = create_workstream(name="Custom", task_states=states, base_dir=workspace)
        assert ws.validate_transition("To Do", "In Progress") is True
        assert ws.validate_transition("To Do", "Done") is False
        assert ws.validate_transition("Done", "To Do") is False


class TestWorkstreamEnv:
    def test_env_inherits_from_parent(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        set_workstream_env_key(parent.id, "OPENAI_API_KEY", "parent-key", base_dir=workspace)

        value = resolve_workstream_env_key(child.id, "OPENAI_API_KEY", base_dir=workspace)
        assert value == "parent-key"

    def test_env_child_override(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        set_workstream_env_key(parent.id, "MODEL", "gpt-parent", base_dir=workspace)
        set_workstream_env_key(child.id, "MODEL", "gpt-child", base_dir=workspace)

        value = resolve_workstream_env_key(child.id, "MODEL", base_dir=workspace)
        assert value == "gpt-child"

    def test_env_unset_masks_parent_and_system(self, workspace, monkeypatch):
        monkeypatch.setenv("SECRET_TOKEN", "system-secret")
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        set_workstream_env_key(parent.id, "SECRET_TOKEN", "parent-secret", base_dir=workspace)
        unset_workstream_env_key(child.id, "SECRET_TOKEN", base_dir=workspace)

        value = resolve_workstream_env_key(child.id, "SECRET_TOKEN", base_dir=workspace)
        assert value is None

    def test_env_falls_back_to_system_env(self, workspace, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "system-anthropic")
        ws = create_workstream(name="Standalone", base_dir=workspace)

        value = resolve_workstream_env_key(ws.id, "ANTHROPIC_API_KEY", base_dir=workspace)
        assert value == "system-anthropic"

    def test_resolve_env_key_with_task_context(self, workspace):
        ws = create_workstream(name="WS", base_dir=workspace)
        task = create_task(ws.id, title="Task", base_dir=workspace)
        set_workstream_env_key(ws.id, "MODEL", "gpt-5", base_dir=workspace)

        value = resolve_env_key("MODEL", task_id=task.id, base_dir=workspace)
        assert value == "gpt-5"

    def test_list_effective_workstream_env_excludes_masked(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        set_workstream_env_key(parent.id, "MODEL", "gpt-parent", base_dir=workspace)
        set_workstream_env_key(parent.id, "TEMPERATURE", "0.1", base_dir=workspace)
        unset_workstream_env_key(child.id, "MODEL", base_dir=workspace)

        env_map = list_effective_workstream_env(child.id, base_dir=workspace)
        assert "MODEL" not in env_map
        assert env_map["TEMPERATURE"] == "0.1"
