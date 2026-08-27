"""Tests for trigger operations."""

import os
import pytest
import yaml
from datetime import datetime, timezone, timedelta
from orchestration.workstreams import create_workstream, read_workstream, resolve_workstream_state_root, save_workstream
from orchestration.tasks import create_task, list_tasks, read_task, update_task
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

    def test_create_email_trigger(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_email={"recipient": "Build@Mail.Example.com", "event": "new_thread"},
            action="run_agent",
            agent="email_triage_agent",
            base_dir=workspace,
        )
        assert trigger.on_email == {
            "recipient": "build@mail.example.com",
            "event": "new_thread",
        }
        assert trigger.agent == "email_triage_agent"

    def test_trigger_persisted_in_workstream(self, workspace, ws):
        create_trigger(ws.id, on_state="Done", action="run_command", command="echo x", base_dir=workspace)
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert len(reloaded.triggers) == 1
        assert reloaded.triggers[0].on_state == "Done"

    def test_state_trigger_can_store_task_selection(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_state="To Do",
            action="run_agent",
            agent="test_agent",
            task_selection="all_unlocked",
            base_dir=workspace,
        )
        assert trigger.task_selection == "all_unlocked"

    def test_schedule_trigger_rejects_task_selection(self, workspace, ws):
        with pytest.raises(ValueError, match="only supported for --on-state"):
            create_trigger(
                ws.id,
                on_schedule="*/5 * * * *",
                action="run_command",
                command="echo x",
                task_selection="all_unlocked",
                base_dir=workspace,
            )

    def test_trigger_requires_exactly_one_condition(self, workspace, ws):
        with pytest.raises(ValueError, match="Exactly one trigger condition"):
            create_trigger(
                ws.id,
                on_state="To Do",
                on_email={"recipient": "build@mail.example.com", "event": "new_thread"},
                action="run_command",
                command="echo x",
                base_dir=workspace,
            )


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


class TestRunTriggerNow:
    def test_schedule_trigger_run_now_allows_paused_workstream(self, workspace, ws):
        ws.paused = True
        save_workstream(ws, workspace)
        trigger = create_trigger(
            ws.id,
            on_schedule="*/5 * * * *",
            action="run_command",
            command="echo x",
            base_dir=workspace,
        )

        result = run_trigger_now(trigger.id, base_dir=workspace)

        assert result["status"] == "started"
        assert result["trigger_id"] == trigger.id

    def test_state_trigger_run_now_uses_first_matching_task_while_paused(self, workspace, ws, monkeypatch):
        ws.paused = True
        save_workstream(ws, workspace)
        trigger = create_trigger(
            ws.id,
            on_state="To Do",
            action="run_command",
            command="echo forced",
            base_dir=workspace,
        )
        task = create_task(ws.id, title="T1", base_dir=workspace)

        captured = {}

        class _InlineThread:
            def __init__(self, target=None, args=None, kwargs=None, daemon=None):
                self._target = target
                self._args = args or ()
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        def _fake_lock_invoke_unlock(_trigger, task_ids, _ws, _base_dir, background=False, ignore_paused=False):
            captured["task_ids"] = list(task_ids)
            captured["background"] = background
            captured["ignore_paused"] = ignore_paused
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.triggers.threading.Thread", _InlineThread)
        monkeypatch.setattr("orchestration.triggers._audit_trigger", lambda *_a, **_k: None, raising=False)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        result = run_trigger_now(trigger.id, base_dir=workspace)

        assert result["status"] == "started"
        assert captured["task_ids"] == [task.id]
        assert captured["background"] is True
        assert captured["ignore_paused"] is True

    def test_state_trigger_run_now_can_batch_all_matching_tasks(self, workspace, ws, monkeypatch):
        ws.paused = True
        save_workstream(ws, workspace)
        trigger = create_trigger(
            ws.id,
            on_state="To Do",
            action="run_agent",
            agent="test_agent",
            task_selection="all_unlocked",
            base_dir=workspace,
        )
        task1 = create_task(ws.id, title="T1", base_dir=workspace)
        task2 = create_task(ws.id, title="T2", base_dir=workspace)

        captured = {}

        class _InlineThread:
            def __init__(self, target=None, args=None, kwargs=None, daemon=None):
                self._target = target
                self._args = args or ()
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        def _fake_lock_invoke_unlock(_trigger, task_ids, _ws, _base_dir, background=False, ignore_paused=False):
            captured["task_ids"] = list(task_ids)
            captured["background"] = background
            captured["ignore_paused"] = ignore_paused
            return {"trigger_id": _trigger.id, "status": "dispatched"}

        monkeypatch.setattr("orchestration.triggers.threading.Thread", _InlineThread)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        result = run_trigger_now(trigger.id, base_dir=workspace)

        assert result["status"] == "started"
        assert captured["task_ids"] == [task2.id, task1.id]
        assert captured["background"] is True
        assert captured["ignore_paused"] is True

    def test_state_trigger_run_now_skips_when_no_matching_task(self, workspace, ws, monkeypatch):
        ws.paused = True
        save_workstream(ws, workspace)
        trigger = create_trigger(
            ws.id,
            on_state="Done",
            action="run_command",
            command="echo forced",
            base_dir=workspace,
        )
        create_task(ws.id, title="T1", base_dir=workspace)

        captured = {"calls": 0}

        class _InlineThread:
            def __init__(self, target=None, args=None, kwargs=None, daemon=None):
                self._target = target
                self._args = args or ()
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        def _fake_lock_invoke_unlock(*_args, **_kwargs):
            captured["calls"] += 1
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.triggers.threading.Thread", _InlineThread)
        monkeypatch.setattr("orchestration.triggers._audit_trigger", lambda *_a, **_k: None, raising=False)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        result = run_trigger_now(trigger.id, base_dir=workspace)

        assert result["status"] == "started"
        assert captured["calls"] == 0

    def test_schedule_trigger_run_now_filters_by_state_and_tag(self, workspace, ws, monkeypatch):
        trigger = create_trigger(
            ws.id,
            on_schedule="*/5 * * * *",
            filter={"state": "Done", "tag": "requestor_notify"},
            action="run_agent",
            agent="test_agent",
            base_dir=workspace,
        )
        matching = create_task(ws.id, title="Matching", tags=["requestor_notify"], base_dir=workspace)
        update_task(matching.id, status="In Progress", base_dir=workspace)
        update_task(matching.id, status="Done", base_dir=workspace)

        missing_tag = create_task(ws.id, title="Missing Tag", base_dir=workspace)
        update_task(missing_tag.id, status="In Progress", base_dir=workspace)
        update_task(missing_tag.id, status="Done", base_dir=workspace)
        create_task(ws.id, title="Wrong State", tags=["requestor_notify"], base_dir=workspace)

        captured = {}

        class _InlineThread:
            def __init__(self, target=None, args=None, kwargs=None, daemon=None):
                self._target = target
                self._args = args or ()
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        def _fake_lock_invoke_unlock(_trigger, task_ids, _ws, _base_dir, background=False, ignore_paused=False):
            captured["task_ids"] = list(task_ids)
            captured["ignore_paused"] = ignore_paused
            return {"trigger_id": _trigger.id, "status": "dispatched"}

        monkeypatch.setattr("orchestration.triggers.threading.Thread", _InlineThread)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        result = run_trigger_now(trigger.id, base_dir=workspace)

        assert result["status"] == "started"
        assert captured == {"task_ids": [matching.id], "ignore_paused": True}

    def test_schedule_trigger_run_now_skips_when_filter_matches_no_tasks(self, workspace, ws, monkeypatch):
        trigger = create_trigger(
            ws.id,
            on_schedule="*/5 * * * *",
            filter={"state": "Done", "tag": "requestor_notify"},
            action="run_agent",
            agent="test_agent",
            base_dir=workspace,
        )
        create_task(ws.id, title="Wrong State", tags=["requestor_notify"], base_dir=workspace)

        captured = {"calls": 0, "audit": None}

        class _InlineThread:
            def __init__(self, target=None, args=None, kwargs=None, daemon=None):
                self._target = target
                self._args = args or ()
                self._kwargs = kwargs or {}

            def start(self):
                self._target(*self._args, **self._kwargs)

        def _fake_lock_invoke_unlock(*_args, **_kwargs):
            captured["calls"] += 1
            return {"status": "dispatched"}

        def _fake_audit(_trigger, result, _ws, _base_dir, task_ids=None):
            captured["audit"] = {"result": result, "task_ids": list(task_ids or [])}

        monkeypatch.setattr("orchestration.triggers.threading.Thread", _InlineThread)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)
        monkeypatch.setattr("orchestration.scheduler._audit_trigger", _fake_audit)

        result = run_trigger_now(trigger.id, base_dir=workspace)

        assert result["status"] == "started"
        assert captured["calls"] == 0
        assert captured["audit"] == {
            "result": {
                "trigger_id": trigger.id,
                "status": "skipped",
                "message": "No unlocked tasks currently match trigger filter",
            },
            "task_ids": [],
        }

    def test_run_now_rejects_paused_trigger(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_schedule="*/5 * * * *",
            action="run_command",
            command="echo x",
            base_dir=workspace,
        )
        trigger.paused = True
        ws.triggers = [trigger]
        save_workstream(ws, workspace)

        with pytest.raises(RuntimeError, match="paused"):
            run_trigger_now(trigger.id, base_dir=workspace)

    def test_run_now_rejects_trigger_in_paused_column(self, workspace, ws):
        from orchestration.workstreams import pause_workstream_states

        pause_workstream_states(ws.id, ["To Do"], base_dir=workspace)
        trigger = create_trigger(
            ws.id,
            on_state="To Do",
            action="run_command",
            command="echo x",
            base_dir=workspace,
        )

        with pytest.raises(RuntimeError, match="Column 'To Do' is paused"):
            run_trigger_now(trigger.id, base_dir=workspace)


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

    def test_execute_trigger_can_ignore_paused_for_manual_force_run(self, workspace, ws):
        from orchestration.workstreams import save_workstream

        ws.paused = True
        save_workstream(ws, workspace)

        trigger = create_trigger(
            ws.id,
            on_state="Done",
            action="run_command",
            command="echo forced_run",
            base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)

        result = execute_trigger(trigger, [task.id], ws.id, base_dir=workspace, ignore_paused=True)
        assert result["status"] == "ok"
        assert "forced_run" in result["stdout"]

    def test_execute_trigger_skips_when_trigger_paused(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_state="Done",
            action="run_command",
            command="echo should_not_run",
            base_dir=workspace,
        )
        trigger.paused = True
        ws.triggers = [trigger]
        save_workstream(ws, workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)

        result = execute_trigger(trigger, [task.id], ws.id, base_dir=workspace, ignore_paused=True)

        assert result["status"] == "skipped"
        assert "paused" in result.get("message", "")

    def test_execute_trigger_skips_when_column_paused(self, workspace, ws):
        from orchestration.workstreams import pause_workstream_states

        pause_workstream_states(ws.id, ["Done"], base_dir=workspace)
        trigger = create_trigger(
            ws.id,
            on_state="Done",
            action="run_command",
            command="echo should_not_run",
            base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)

        result = execute_trigger(trigger, [task.id], ws.id, base_dir=workspace, ignore_paused=True)

        assert result["status"] == "skipped"
        assert "Column 'Done' is paused" == result.get("message")

    def test_execute_run_agent_can_ignore_paused_for_manual_force_run(self, workspace, ws, monkeypatch):
        from orchestration.workstreams import save_workstream

        ws.paused = True
        save_workstream(ws, workspace)

        trigger = create_trigger(
            ws.id,
            on_state="Done",
            action="run_agent",
            agent="test_agent",
            base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)
        captured = {}

        def _fake_run_agent(agent_name, **kwargs):
            captured["agent_name"] = agent_name
            captured.update(kwargs)
            return {"run_id": "run-123", "status": "started"}

        monkeypatch.setattr("orchestration.agents.run_agent", _fake_run_agent)

        result = execute_trigger(trigger, [task.id], ws.id, base_dir=workspace, ignore_paused=True)

        assert result["status"] == "ok"
        assert captured["agent_name"] == "test_agent"
        assert captured["task_ids"] == [task.id]
        assert captured["workstream_id"] == ws.id
        assert captured["prompt_source"] == "state_trigger"
        assert captured["allow_paused_workstream"] is True
        assert captured["concurrency_state"] == "Done"

    @pytest.mark.parametrize(
        ("trigger_kwargs", "expected_source"),
        [
            ({"on_state": "To Do"}, "state_trigger"),
            ({"on_schedule": "0 9 * * *"}, "schedule_trigger"),
            ({"on_email": {"recipient": "build@example.com", "event": "new_thread"}}, "email_trigger"),
        ],
    )
    def test_execute_run_agent_passes_trigger_prompt_source(
        self, workspace, ws, monkeypatch, trigger_kwargs, expected_source
    ):
        trigger = create_trigger(
            ws.id,
            action="run_agent",
            agent="test_agent",
            prompt="Do the triggered work.",
            base_dir=workspace,
            **trigger_kwargs,
        )
        captured = {}

        def _fake_run_agent(_agent_name, **kwargs):
            captured.update(kwargs)
            return {"run_id": "run-123"}

        monkeypatch.setattr("orchestration.agents.run_agent", _fake_run_agent)

        result = execute_trigger(trigger, [], ws.id, base_dir=workspace)

        assert result["status"] == "ok"
        assert captured["prompt"] == "Do the triggered work."
        assert captured["prompt_source"] == expected_source


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

    def test_tick_skips_state_trigger_for_paused_column(self, workspace, ws, monkeypatch):
        from orchestration.workstreams import pause_workstream_states

        pause_workstream_states(ws.id, ["To Do"], base_dir=workspace)
        trigger = create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo x", base_dir=workspace,
        )
        create_task(ws.id, title="Paused", base_dir=workspace)
        calls = []

        def _fake_lock_invoke_unlock(*_args, **_kwargs):
            calls.append(True)
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        result = tick(workspace)

        assert calls == []
        assert result["state_triggers_fired"] == [{
            "trigger_id": trigger.id,
            "task_ids": [],
            "result": {"status": "skipped", "reason": "Column 'To Do' is paused"},
        }]

    def test_tick_skips_paused_state_trigger(self, workspace, ws, monkeypatch):
        trigger = create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo x", base_dir=workspace,
        )
        trigger.paused = True
        ws.triggers = [trigger]
        save_workstream(ws, workspace)
        create_task(ws.id, title="Paused trigger", base_dir=workspace)
        calls = []

        def _fake_lock_invoke_unlock(*_args, **_kwargs):
            calls.append(True)
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        result = tick(workspace)

        assert calls == []
        assert result["state_triggers_fired"] == [{
            "trigger_id": trigger.id,
            "task_ids": [],
            "result": {"status": "skipped", "reason": f"Trigger '{trigger.id}' is paused"},
        }]

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
            command="echo x", base_dir=workspace,
        )
        task = create_task(ws.id, title="Locked", base_dir=workspace)
        acquire_lock(task.id, "some_agent", base_dir=workspace)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)
        assert len(result["state_triggers_fired"]) == 0

    def test_tick_dispatches_first_unlocked_by_order(self, workspace, ws, monkeypatch):
        from orchestration.locks import acquire_lock
        from orchestration.tasks import move_task_up

        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo x", base_dir=workspace,
        )

        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        # Make order T1, T3, T2 and then lock T1 so scheduler should pick T3.
        move_task_up(t3.id, base_dir=workspace)
        acquire_lock(t1.id, "busy-agent", base_dir=workspace)

        calls = []

        def _fake_lock_invoke_unlock(_trigger, task_ids, *_args, **_kwargs):
            calls.append(list(task_ids))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        assert len(calls) == 1
        assert calls[0] == [t3.id]
        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["task_ids"] == [t3.id]

    def test_tick_dispatches_all_unlocked_tasks_when_configured(self, workspace, ws, monkeypatch):
        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", task_selection="all_unlocked", base_dir=workspace,
        )

        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        calls = []

        def _fake_lock_invoke_unlock(_trigger, task_ids, *_args, **_kwargs):
            calls.append(list(task_ids))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_a, **_k: 0)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        assert calls == [[t3.id, t2.id, t1.id]]
        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["task_ids"] == [t3.id, t2.id, t1.id]

    def test_tick_scans_workstream_locks_once_for_multiple_state_triggers(self, workspace, ws, monkeypatch):
        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo first", base_dir=workspace,
        )
        create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo second", base_dir=workspace,
        )

        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)

        calls = []
        lock_scan_count = {"count": 0}

        def _fake_lock_list(*_args, **_kwargs):
            lock_scan_count["count"] += 1
            return {}

        def _fake_lock_invoke_unlock(_trigger, task_ids, *_args, **_kwargs):
            calls.append(list(task_ids))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.locks.list_workstream_locks_for_workstream", _fake_lock_list)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        assert lock_scan_count["count"] == 1
        # New tasks are inserted at the top, so the latest created unlocked task
        # is selected first for same-state triggers.
        assert calls == [[t2.id], [t1.id]]
        assert len(result["state_triggers_fired"]) == 2


class TestEmailTriggerViaTick:
    def test_tick_dispatches_email_trigger_as_standalone_run(self, workspace, ws, monkeypatch):
        trigger = create_trigger(
            ws.id,
            on_email={"recipient": "build@mail.example.com", "event": "new_thread"},
            action="run_agent",
            agent="email_triage_agent",
            base_dir=workspace,
        )

        def _fake_list_new_thread_messages(recipient):
            assert recipient == "build@mail.example.com"
            return [{
                "storage_key": "msg-1",
                "from": "Alex Example <jb@example.com>",
                "to": "build@mail.example.com",
                "subject": "Need a feature",
                "date": "Mon, 26 May 2026 10:00:00 +0000",
                "body": "Please add the new workflow.",
                "attachments": [
                    {
                        "name": "brief.pdf",
                        "content_type": "application/pdf",
                        "size": 12345,
                    }
                ],
            }]

        dispatched = []

        def _fake_run_without_lock(_trigger, _ws, _base_dir, task_ids=None, **_kwargs):
            dispatched.append({
                "task_ids": list(task_ids or []),
                "prompt": _trigger.prompt,
                "event_context": getattr(_trigger, "event_context", None),
            })

        monkeypatch.setattr("orchestration.email_inbox.list_new_thread_messages", _fake_list_new_thread_messages)
        monkeypatch.setattr("orchestration.scheduler._run_without_lock", _fake_run_without_lock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        tasks = list_tasks(ws.id, base_dir=workspace)
        assert len(tasks) == 0
        assert len(dispatched) == 1
        assert dispatched[0]["task_ids"] == []
        assert "Inbound email event:" in dispatched[0]["prompt"]
        assert "Need a feature" in dispatched[0]["prompt"]
        assert "Attachments (1):" in dispatched[0]["prompt"]
        assert "brief.pdf (application/pdf, 12345 bytes)" in dispatched[0]["prompt"]
        assert dispatched[0]["event_context"] == {
            "email_from": "Alex Example <jb@example.com>",
            "email_to": "build@mail.example.com",
            "email_subject": "Need a feature",
            "email_date": "Mon, 26 May 2026 10:00:00 +0000",
            "email_body": "Please add the new workflow.",
            "email_storage_key": "msg-1",
            "email_attachment_count": 1,
            "email_attachments_json": '[{"content_type": "application/pdf", "name": "brief.pdf", "size": 12345}]',
        }
        assert len(result["email_triggers_fired"]) == 1
        assert result["email_triggers_fired"][0]["trigger_id"] == trigger.id

        state_path = os.path.join(
            resolve_workstream_state_root(ws.id, base_dir=workspace),
            ".orchestration",
            "triggers",
            f"{trigger.id}.yaml",
        )
        with open(state_path) as f:
            trigger_state = yaml.safe_load(f) or {}
        assert trigger_state["processed_keys"] == ["msg-1"]

    def test_tick_dedupes_processed_email_threads(self, workspace, ws, monkeypatch):
        trigger = create_trigger(
            ws.id,
            on_email={"recipient": "build@mail.example.com", "event": "new_thread"},
            action="run_command",
            command="echo {email_subject}",
            base_dir=workspace,
        )

        def _fake_list_new_thread_messages(_recipient):
            return [{
                "storage_key": "msg-1",
                "from": "Alex Example <jb@example.com>",
                "to": "build@mail.example.com",
                "subject": "Need a feature",
                "body": "Please add the new workflow.",
                "attachments": [],
            }]

        dispatched = []

        def _fake_run_without_lock(_trigger, _ws, _base_dir, task_ids=None, **_kwargs):
            dispatched.append({
                "task_ids": list(task_ids or []),
                "command": _trigger.command,
                "event_context": getattr(_trigger, "event_context", None),
            })

        monkeypatch.setattr("orchestration.email_inbox.list_new_thread_messages", _fake_list_new_thread_messages)
        monkeypatch.setattr("orchestration.scheduler._run_without_lock", _fake_run_without_lock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        tick(workspace)
        tick(workspace)

        tasks = list_tasks(ws.id, base_dir=workspace)
        assert len(tasks) == 0
        assert len(dispatched) == 1
        assert dispatched[0]["task_ids"] == []
        assert dispatched[0]["event_context"]["email_subject"] == "Need a feature"
        assert dispatched[0]["event_context"]["email_attachment_count"] == 0


class TestAgentConcurrency:
    def test_state_trigger_skipped_when_agent_limit_reached(self, workspace, ws, monkeypatch):
        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )
        create_task(ws.id, title="T", base_dir=workspace)

        called = {"count": 0}

        def _fake_lock_invoke_unlock(*_args, **_kwargs):
            called["count"] += 1
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_a, **_k: 1)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        assert called["count"] == 0
        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["result"]["status"] == "skipped"

    def test_state_trigger_dispatches_single_first_task_when_slots_available(self, workspace, ws, monkeypatch):
        ws.agent_concurrency = {
            "default": 1,
            "overrides": {"test_agent": 2},
        }
        save_workstream(ws, workspace)

        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )

        create_task(ws.id, title="T1", base_dir=workspace)
        create_task(ws.id, title="T2", base_dir=workspace)
        create_task(ws.id, title="T3", base_dir=workspace)

        calls = []

        def _fake_lock_invoke_unlock(_trigger, task_ids, *_args, **_kwargs):
            calls.append(list(task_ids))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_a, **_k: 0)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        assert len(calls) == 1
        assert len(result["state_triggers_fired"]) == 1
        assert all(x["result"]["status"] == "dispatched" for x in result["state_triggers_fired"])

    def test_same_tick_does_not_overdispatch_same_agent_across_triggers(self, workspace, ws, monkeypatch):
        # default agent_concurrency is 1
        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )
        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )

        create_task(ws.id, title="T1", base_dir=workspace)
        create_task(ws.id, title="T2", base_dir=workspace)

        calls = []

        def _fake_lock_invoke_unlock(_trigger, task_ids, *_args, **_kwargs):
            calls.append((str(_trigger.id), list(task_ids)))
            return {"status": "dispatched"}

        # Simulate race where active-runs registry has not updated yet.
        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_a, **_k: 0)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        dispatched = [x for x in result["state_triggers_fired"] if x["result"].get("status") == "dispatched"]
        assert len(calls) == 1
        assert len(dispatched) == 1

    def test_same_agent_can_dispatch_once_per_state_bucket(self, workspace, ws, monkeypatch):
        ws.agent_concurrency = {
            "default": 1,
            "state_overrides": {
                "To Do": {"overrides": {"test_agent": 1}},
                "In Progress": {"overrides": {"test_agent": 1}},
            },
        }
        save_workstream(ws, workspace)

        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )
        create_trigger(
            ws.id, on_state="In Progress", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )

        todo_task = create_task(ws.id, title="Stage", base_dir=workspace)
        prod_task = create_task(ws.id, title="Prod", base_dir=workspace)
        update_task(prod_task.id, status="In Progress", base_dir=workspace)

        calls = []

        def _fake_lock_invoke_unlock(_trigger, task_ids, *_args, **_kwargs):
            calls.append((_trigger.on_state, list(task_ids)))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_a, **_k: 0)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        assert calls == [("In Progress", [prod_task.id]), ("To Do", [todo_task.id])]
        assert all(x["result"]["status"] == "dispatched" for x in result["state_triggers_fired"])

    def test_same_state_bucket_still_limits_same_agent(self, workspace, ws, monkeypatch):
        ws.agent_concurrency = {
            "default": 1,
            "state_overrides": {
                "To Do": {"overrides": {"test_agent": 1}},
            },
        }
        save_workstream(ws, workspace)

        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )
        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )

        create_task(ws.id, title="T1", base_dir=workspace)
        create_task(ws.id, title="T2", base_dir=workspace)

        calls = []

        def _fake_lock_invoke_unlock(_trigger, task_ids, *_args, **_kwargs):
            calls.append((_trigger.on_state, list(task_ids)))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_a, **_k: 0)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        dispatched = [x for x in result["state_triggers_fired"] if x["result"].get("status") == "dispatched"]
        skipped = [x for x in result["state_triggers_fired"] if x["result"].get("reason") == "agent_concurrency reached"]
        assert len(calls) == 1
        assert len(dispatched) == 1
        assert len(skipped) == 1

    def test_background_thread_start_failure_releases_lock(self, workspace, ws, monkeypatch):
        from orchestration.locks import lock_status

        create_trigger(
            ws.id, on_state="To Do", action="run_agent",
            agent="test_agent", base_dir=workspace,
        )
        task = create_task(ws.id, title="T-lock", base_dir=workspace)

        class _BoomThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("thread start failed")

        monkeypatch.setattr("orchestration.scheduler.threading.Thread", _BoomThread)
        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_a, **_k: 0)

        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)
        result = tick(workspace)

        assert len(result["state_triggers_fired"]) == 1
        assert result["state_triggers_fired"][0]["result"]["status"] == "error"
        assert lock_status(task.id, base_dir=workspace) is None


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

    def test_run_now_allows_state_trigger(self, workspace, ws):
        """Run Now should allow state-based triggers."""
        trigger = create_trigger(
            ws.id, on_state="To Do", action="run_command",
            command="echo nope", base_dir=workspace,
        )
        result = run_trigger_now(trigger.id, base_dir=workspace)
        assert result["status"] == "started"
        assert result["trigger_id"] == trigger.id

    def test_run_now_allows_schedule_trigger_on_paused_workstream(self, workspace, ws):
        """Run Now should execute schedule triggers even when the workstream is paused."""
        from orchestration.workstreams import save_workstream

        ws.paused = True
        save_workstream(ws, workspace)

        trigger = create_trigger(
            ws.id, on_schedule="0 9 * * *", action="run_command",
            command="echo nope", base_dir=workspace,
        )

        result = run_trigger_now(trigger.id, base_dir=workspace)

        assert result["status"] == "started"
        assert result["trigger_id"] == trigger.id

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
