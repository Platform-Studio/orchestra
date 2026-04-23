"""Tests for trigger operations."""

import os
import pytest
from datetime import datetime, timezone, timedelta
from orchestration.workstreams import create_workstream, read_workstream
from orchestration.tasks import create_task, read_task, update_task
from orchestration.triggers import create_trigger, list_triggers, delete_trigger, execute_trigger, run_trigger_now, get_active_triggers
from orchestration.scheduler import tick, _save_state


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


class TestExecuteTrigger:
    def test_run_command(self, workspace, ws):
        trigger = create_trigger(ws.id, on_state="Done", action="run_command", command="echo hello", base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)
        result = execute_trigger(trigger, [task.id], ws.id, base_dir=workspace)
        assert result["status"] == "ok"
        assert "hello" in result["stdout"]

    def test_run_command_without_task(self, workspace, ws):
        """run_command with empty task_ids list."""
        trigger = create_trigger(ws.id, on_state="Done", action="run_command", command="echo hello", base_dir=workspace)
        result = execute_trigger(trigger, [], ws.id, base_dir=workspace)
        assert result["status"] == "ok"
        assert "hello" in result["stdout"]

    def test_template_variables_in_command(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_state="Done",
            action="run_command",
            command="echo task={task_id} ws={workstream_id}",
            base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)
        result = execute_trigger(trigger, [task.id], ws.id, base_dir=workspace)
        assert task.id in result["stdout"]
        assert ws.id in result["stdout"]

    def test_execute_trigger_skips_when_workstream_paused(self, workspace, ws):
        from orchestration.workstreams import save_workstream

        ws.paused = True
        save_workstream(ws, workspace)

        trigger = create_trigger(
            ws.id,
            on_state="Done",
            action="run_command",
            command="echo should_not_run",
            base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)

        result = execute_trigger(trigger, [task.id], ws.id, base_dir=workspace)
        assert result["status"] == "skipped"
        assert "paused" in result.get("message", "").lower()


class TestStateTriggerViaTick:
    def test_tick_fires_state_trigger(self, workspace, ws):
        """State triggers fire during tick() for tasks in the target state."""
        marker_file = os.path.join(workspace, "state_trigger_fired.txt")
        create_trigger(
            ws.id, on_state="In Progress", action="run_command",
            command=f"echo fired > {marker_file}", base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)
        update_task(task.id, status="In Progress", base_dir=workspace)
        # Trigger fires on next tick, not on state change
        assert not os.path.exists(marker_file)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        assert len(result["state_triggers_fired"]) == 1
        # State triggers now run on daemon threads — wait briefly for the
        # background thread to finish the run_command.
        import time
        for _ in range(30):
            if os.path.exists(marker_file):
                break
            time.sleep(0.1)
        assert os.path.exists(marker_file)

    def test_tick_does_not_fire_for_wrong_state(self, workspace, ws):
        """State trigger only fires for tasks in the matching state."""
        create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo nope", base_dir=workspace,
        )
        create_task(ws.id, title="T", base_dir=workspace)  # In "To Do"
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        assert len(result["state_triggers_fired"]) == 0
    def test_locked_task_is_skipped(self, workspace, ws):
        """Tasks with active locks are not re-triggered."""
        from orchestration.locks import acquire_lock
        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo x", max_concurrent=5, base_dir=workspace,
        )
        task = create_task(ws.id, title="Locked", base_dir=workspace)
        acquire_lock(task.id, "some_agent", base_dir=workspace)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        assert len(result["state_triggers_fired"]) == 0


class TestMaxConcurrent:
    def test_create_trigger_with_max_concurrent(self, workspace, ws):
        trigger = create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", max_concurrent=3, base_dir=workspace,
        )
        assert trigger.max_concurrent == 3
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.triggers[0].max_concurrent == 3

    def test_default_max_concurrent_is_1(self, workspace, ws):
        trigger = create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", base_dir=workspace,
        )
        assert trigger.max_concurrent == 1

    def test_trigger_fires_when_no_locks(self, workspace, ws):
        """Trigger should fire normally when no locks are held."""
        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo ok", base_dir=workspace,
        )
        create_task(ws.id, title="T", base_dir=workspace)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["result"]["status"] == "dispatched"

    def test_trigger_skipped_when_at_max_concurrent(self, workspace, ws):
        """Trigger should be skipped when active locks >= max_concurrent."""
        from orchestration.locks import acquire_lock
        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo x", max_concurrent=1, base_dir=workspace,
        )
        # Create a task and lock it to simulate an active agent
        locked_task = create_task(ws.id, title="Locked", base_dir=workspace)
        acquire_lock(locked_task.id, "some_agent", base_dir=workspace)

        # Create another task that should be skipped (at max_concurrent)
        create_task(ws.id, title="Trigger", base_dir=workspace)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        # Trigger fires but returns skipped status due to max_concurrent
        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["result"]["status"] == "skipped"

    def test_trigger_fires_when_below_max_concurrent(self, workspace, ws):
        """Trigger should fire when active locks < max_concurrent."""
        from orchestration.locks import acquire_lock
        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo ok", max_concurrent=2, base_dir=workspace,
        )
        # Create one lock — still below max_concurrent=2
        locked_task = create_task(ws.id, title="Locked", base_dir=workspace)
        acquire_lock(locked_task.id, "some_agent", base_dir=workspace)

        # Unlocked task in same state should fire
        create_task(ws.id, title="Trigger", base_dir=workspace)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["result"]["status"] == "dispatched"

    def test_trigger_skipped_when_at_max_concurrent_2(self, workspace, ws):
        """Trigger with max_concurrent=2 should skip when 2 locks active."""
        from orchestration.locks import acquire_lock
        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo x", max_concurrent=2, base_dir=workspace,
        )
        t1 = create_task(ws.id, title="L1", base_dir=workspace)
        t2 = create_task(ws.id, title="L2", base_dir=workspace)
        acquire_lock(t1.id, "agent1", base_dir=workspace)
        acquire_lock(t2.id, "agent2", base_dir=workspace)

        # Another unlocked task — but at max_concurrent=2 with 2 locks
        create_task(ws.id, title="Trigger", base_dir=workspace)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        # Trigger fires but returns skipped status due to max_concurrent
        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["result"]["status"] == "skipped"

    def test_max_concurrent_serialization_roundtrip(self, workspace, ws):
        """max_concurrent should survive YAML serialization."""
        create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", max_concurrent=5, base_dir=workspace,
        )
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.triggers[0].max_concurrent == 5

    def test_max_concurrent_default_not_serialized(self, workspace, ws):
        """Default max_concurrent=1 should not appear in serialized YAML."""
        import yaml, os
        create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", base_dir=workspace,
        )
        # Read the raw workstream YAML
        ws_file = os.path.join(workspace, "workstreams", f"{ws.id}.yaml")
        with open(ws_file) as f:
            data = yaml.safe_load(f)
        trigger_data = data["triggers"][0]
        assert "max_concurrent" not in trigger_data


class TestTriggerPrompt:
    def test_create_trigger_with_prompt(self, workspace, ws):
        trigger = create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", prompt="skip the survey step",
            base_dir=workspace,
        )
        assert trigger.prompt == "skip the survey step"

    def test_prompt_persisted_in_workstream(self, workspace, ws):
        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", prompt="custom instructions",
            base_dir=workspace,
        )
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.triggers[0].prompt == "custom instructions"

    def test_prompt_none_not_serialized(self, workspace, ws):
        import yaml
        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )
        ws_file = os.path.join(workspace, "workstreams", f"{ws.id}.yaml")
        with open(ws_file) as f:
            data = yaml.safe_load(f)
        trigger_data = data["triggers"][0]
        assert "prompt" not in trigger_data

    def test_prompt_serialization_roundtrip(self, workspace, ws):
        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", prompt="do something special",
            base_dir=workspace,
        )
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.triggers[0].prompt == "do something special"
        assert reloaded.triggers[0].to_dict()["prompt"] == "do something special"


class TestTriggerTimeout:
    def test_create_trigger_with_timeout(self, workspace, ws):
        trigger = create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", timeout=3600, base_dir=workspace,
        )
        assert trigger.timeout == 3600

    def test_timeout_persisted_in_workstream(self, workspace, ws):
        create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", timeout=900, base_dir=workspace,
        )
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.triggers[0].timeout == 900

    def test_default_timeout_is_none(self, workspace, ws):
        trigger = create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", base_dir=workspace,
        )
        assert trigger.timeout is None

    def test_timeout_none_not_serialized(self, workspace, ws):
        import yaml
        create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", base_dir=workspace,
        )
        ws_file = os.path.join(workspace, "workstreams", f"{ws.id}.yaml")
        with open(ws_file) as f:
            data = yaml.safe_load(f)
        trigger_data = data["triggers"][0]
        assert "timeout" not in trigger_data

    def test_timeout_serialization_roundtrip(self, workspace, ws):
        create_trigger(
            ws.id, on_state="Done", action="run_command",
            command="echo x", timeout=1200, base_dir=workspace,
        )
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.triggers[0].timeout == 1200
        assert reloaded.triggers[0].to_dict()["timeout"] == 1200


class TestTriggerLocking:
    """Verify that execute_trigger acquires and releases locks for task-bound actions."""

    def test_run_command_no_locking_in_execute_trigger(self, workspace, ws):
        """execute_trigger does NOT lock — callers are responsible."""
        from orchestration.locks import lock_status
        trigger = create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo hello", base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)
        result = execute_trigger(trigger, [task.id], ws.id, base_dir=workspace)
        assert result["status"] == "ok"
        # No lock was acquired by execute_trigger
        assert lock_status(task.id, base_dir=workspace) is None

    def test_state_trigger_tick_acquires_and_releases_lock(self, workspace, ws):
        """Scheduler locks tasks before trigger, unlocks after."""
        from orchestration.locks import lock_status
        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo ok", base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["result"]["status"] == "dispatched"
        # Lock is released by the background thread — wait briefly
        import time
        for _ in range(30):
            if lock_status(task.id, base_dir=workspace) is None:
                break
            time.sleep(0.1)
        assert lock_status(task.id, base_dir=workspace) is None


class TestRunTriggerNow:
    def test_run_now_schedule_trigger_no_filter(self, workspace, ws):
        """Run Now kicks off a schedule trigger and returns immediately."""
        trigger = create_trigger(
            ws.id, on_schedule="0 9 * * *", action="run_command",
            command="echo hello", base_dir=workspace,
        )
        result = run_trigger_now(trigger.id, base_dir=workspace)
        assert result["status"] == "started"
        assert result["trigger_id"] == trigger.id

    def test_run_now_schedule_trigger_with_filter(self, workspace, ws):
        """Run Now kicks off a schedule trigger with filter."""
        trigger = create_trigger(
            ws.id, on_schedule="0 9 * * *", action="run_command",
            command="echo {task_id}", filter={"state": "To Do"},
            base_dir=workspace,
        )
        create_task(ws.id, title="Filterable", base_dir=workspace)
        result = run_trigger_now(trigger.id, base_dir=workspace)
        assert result["status"] == "started"

    def test_run_now_rejects_state_trigger(self, workspace, ws):
        """Run Now should reject state-based triggers."""
        trigger = create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo nope", base_dir=workspace,
        )
        with pytest.raises(ValueError, match="schedule-based"):
            run_trigger_now(trigger.id, base_dir=workspace)

    def test_run_now_rejects_paused_workstream(self, workspace, ws):
        """Run Now should refuse to execute when workstream is paused."""
        from orchestration.workstreams import save_workstream

        ws.paused = True
        save_workstream(ws, workspace)

        trigger = create_trigger(
            ws.id, on_schedule="0 9 * * *", action="run_command",
            command="echo nope", base_dir=workspace,
        )
        with pytest.raises(RuntimeError, match="paused"):
            run_trigger_now(trigger.id, base_dir=workspace)

    def test_run_now_not_found(self, workspace, ws):
        """Run Now raises FileNotFoundError for unknown trigger ID."""
        with pytest.raises(FileNotFoundError):
            run_trigger_now("nonexistent-id", base_dir=workspace)


class TestActiveTriggers:
    def test_active_triggers_empty_by_default(self):
        assert isinstance(get_active_triggers(), list)

    def test_trigger_tracked_during_execution(self, workspace, ws):
        """execute_trigger adds/removes trigger ID from active set."""
        import threading
        from orchestration.triggers import _active_triggers_lock, _active_triggers
        from orchestration.models import Trigger, new_id

        seen_active = []
        trigger = Trigger(
            id=new_id(), action="run_command", command="sleep 0.2",
        )

        def check():
            import time
            time.sleep(0.05)
            seen_active.extend(get_active_triggers())

        checker = threading.Thread(target=check)
        checker.start()
        execute_trigger(trigger, [], ws.id, base_dir=workspace)
        checker.join()
        assert trigger.id in seen_active
        # After execution, no longer active
        assert trigger.id not in get_active_triggers()
