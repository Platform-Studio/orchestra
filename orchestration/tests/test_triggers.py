"""Tests for trigger operations."""

import pytest
from orchestration.workstreams import create_workstream, read_workstream
from orchestration.tasks import create_task, read_task, update_task
from orchestration.triggers import create_trigger, list_triggers, delete_trigger, evaluate_triggers


@pytest.fixture
def ws(workspace):
    states = {
        "To Do": ["In Progress"],
        "In Progress": ["Done"],
        "Done": [],
    }
    return create_workstream(name="Trigger WS", task_states=states, base_dir=workspace)


class TestCreateTrigger:
    def test_create_run_command(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_state="Done",
            action="run_command",
            command="echo done",
            base_dir=workspace,
        )
        assert trigger.on_state == "Done"
        assert trigger.action == "run_command"
        assert trigger.command == "echo done"
        assert trigger.id is not None

    def test_create_run_agent(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_state="To Do",
            action="run_agent",
            agent="test_agent",
            base_dir=workspace,
        )
        assert trigger.action == "run_agent"
        assert trigger.agent == "test_agent"

    def test_trigger_persisted_in_workstream(self, workspace, ws):
        create_trigger(ws.id, on_state="Done", action="run_command", command="echo x", base_dir=workspace)
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert len(reloaded.triggers) == 1
        assert reloaded.triggers[0].on_state == "Done"


class TestListTriggers:
    def test_list_empty(self, workspace, ws):
        triggers = list_triggers(ws.id, base_dir=workspace)
        assert triggers == []

    def test_list_multiple(self, workspace, ws):
        create_trigger(ws.id, on_state="Done", action="run_command", command="echo 1", base_dir=workspace)
        create_trigger(ws.id, on_state="To Do", action="run_command", command="echo 2", base_dir=workspace)
        triggers = list_triggers(ws.id, base_dir=workspace)
        assert len(triggers) == 2


class TestDeleteTrigger:
    def test_delete_existing(self, workspace, ws):
        trigger = create_trigger(ws.id, on_state="Done", action="run_command", command="echo x", base_dir=workspace)
        result = delete_trigger(trigger.id, base_dir=workspace)
        assert result is True
        triggers = list_triggers(ws.id, base_dir=workspace)
        assert len(triggers) == 0

    def test_delete_not_found(self, workspace, ws):
        with pytest.raises(FileNotFoundError):
            delete_trigger("nonexistent", base_dir=workspace)


class TestEvaluateTriggers:
    def test_run_command_trigger_fires(self, workspace, ws):
        create_trigger(ws.id, on_state="Done", action="run_command", command="echo hello", base_dir=workspace)
        ws_reloaded = read_workstream(ws.id, base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)
        results = evaluate_triggers(ws_reloaded, "Done", task.id, base_dir=workspace)
        assert len(results) == 1
        assert results[0]["status"] == "ok"
        assert "hello" in results[0]["stdout"]

    def test_trigger_does_not_fire_for_wrong_state(self, workspace, ws):
        create_trigger(ws.id, on_state="Done", action="run_command", command="echo hello", base_dir=workspace)
        ws_reloaded = read_workstream(ws.id, base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)
        results = evaluate_triggers(ws_reloaded, "To Do", task.id, base_dir=workspace)
        assert len(results) == 0

    def test_template_variables_in_command(self, workspace, ws):
        create_trigger(
            ws.id,
            on_state="Done",
            action="run_command",
            command="echo task={task_id} ws={workstream_id}",
            base_dir=workspace,
        )
        ws_reloaded = read_workstream(ws.id, base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)
        results = evaluate_triggers(ws_reloaded, "Done", task.id, base_dir=workspace)
        assert task.id in results[0]["stdout"]
        assert ws.id in results[0]["stdout"]

    def test_trigger_fires_on_status_update(self, workspace, ws):
        """Triggers should fire when task status changes via update_task."""
        import os
        marker_file = os.path.join(workspace, "trigger_fired.txt")
        create_trigger(
            ws.id,
            on_state="In Progress",
            action="run_command",
            command=f"echo fired > {marker_file}",
            base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)
        update_task(task.id, status="In Progress", base_dir=workspace)
        assert os.path.exists(marker_file)
