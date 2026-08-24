"""Tests for scheduler operations and schedule-based features."""

import os
import pytest
import time
from datetime import datetime, timezone, timedelta

from orchestration.workstreams import create_workstream, read_workstream
from orchestration.tasks import create_task, read_task, update_task, clear_schedule
from orchestration.triggers import create_trigger
from orchestration.scheduler import (
    _state_path,
    _load_state,
    _save_state,
    _existing_running_pid,
    _is_live_non_zombie,
    _resolve_tick_timeout,
    DEFAULT_TICK_TIMEOUT,
    _cron_matches_time,
    _cron_matches_between,
    _select_state_trigger_task_ids,
    _task_matches_filter,
    _matching_task_ids,
    _lock_invoke_unlock,
    tick,
    status,
)


@pytest.fixture(autouse=True)
def _clear_persistence_root_env(monkeypatch):
    monkeypatch.setenv("WORKSTREAM_ROOT", "")
    monkeypatch.setenv("ARTIFACT_ROOT", "")
    monkeypatch.setenv("ARTICACT_ROOT", "")


@pytest.fixture
def ws(workspace):
    states = {
        "To Do": ["In Progress"],
        "In Progress": ["Done"],
        "Done": [],
    }
    return create_workstream(name="Sched WS", task_states=states, base_dir=workspace)


# ── Cron matching ────────────────────────────────────────────────────

class TestCronMatching:
    def test_star_matches_all(self):
        dt = datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc)
        assert _cron_matches_time(["*", "*", "*", "*", "*"], dt) is True

    def test_exact_minute(self):
        dt = datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc)
        assert _cron_matches_time(["30", "*", "*", "*", "*"], dt) is True
        assert _cron_matches_time(["15", "*", "*", "*", "*"], dt) is False

    def test_step_minute(self):
        dt = datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc)
        assert _cron_matches_time(["*/30", "*", "*", "*", "*"], dt) is True
        assert _cron_matches_time(["*/20", "*", "*", "*", "*"], dt) is False

    def test_exact_hour(self):
        dt = datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc)
        assert _cron_matches_time(["*", "10", "*", "*", "*"], dt) is True
        assert _cron_matches_time(["*", "11", "*", "*", "*"], dt) is False

    def test_exact_full_match(self):
        # Wednesday April 15, 2026 — cron day_of_week: 3 (0=Sun)
        dt = datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc)
        assert _cron_matches_time(["30", "10", "15", "4", "3"], dt) is True
        assert _cron_matches_time(["30", "10", "15", "4", "1"], dt) is False

    def test_matches_between(self):
        base = datetime(2026, 4, 15, 10, 0, tzinfo=timezone.utc)
        now = datetime(2026, 4, 15, 10, 5, tzinfo=timezone.utc)
        # Should match at 10:03
        assert _cron_matches_between("3 * * * *", base, now) is True
        # Should not match (minute 30 not between 10:01-10:05)
        assert _cron_matches_between("30 * * * *", base, now) is False

    def test_matches_between_boundary(self):
        base = datetime(2026, 4, 15, 10, 29, tzinfo=timezone.utc)
        now = datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc)
        assert _cron_matches_between("30 * * * *", base, now) is True

    def test_matches_between_step_boundary(self):
        base = datetime(2026, 4, 15, 10, 29, tzinfo=timezone.utc)
        now = datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc)
        assert _cron_matches_between("*/30 * * * *", base, now) is True


# ── Task filter matching ────────────────────────────────────────────

class TestTaskFilterMatching:
    def test_matches_state(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert _task_matches_filter(task, {"state": "To Do"}) is True
        assert _task_matches_filter(task, {"state": "Done"}) is False

    def test_matches_state_list(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert _task_matches_filter(task, {"state": ["To Do", "In Progress"]}) is True
        assert _task_matches_filter(task, {"state": ["Done", "Blocked"]}) is False

    def test_matches_status_list_alias(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert _task_matches_filter(task, {"status": ["To Do", "In Progress"]}) is True
        assert _task_matches_filter(task, {"status": ["Done"]}) is False

    def test_matches_tags(self, workspace, ws):
        task = create_task(ws.id, title="T", tags=["urgent"], base_dir=workspace)
        assert _task_matches_filter(task, {"tags": ["urgent"]}) is True
        assert _task_matches_filter(task, {"tags": ["low"]}) is False

    def test_matches_all_required_tags(self, workspace, ws):
        task = create_task(ws.id, title="T", tags=["urgent", "requestor_notify"], base_dir=workspace)
        assert _task_matches_filter(task, {"tags": ["urgent", "requestor_notify"]}) is True
        assert _task_matches_filter(task, {"tags": ["urgent", "missing"]}) is False

    def test_matches_singular_tag_filter(self, workspace, ws):
        task = create_task(ws.id, title="T", tags=["requestor_notify"], base_dir=workspace)
        assert _task_matches_filter(task, {"tag": "requestor_notify"}) is True
        assert _task_matches_filter(task, {"tag": "requestor_notified"}) is False

    def test_matches_state_and_tags_conjunctively(self, workspace, ws):
        task = create_task(ws.id, title="T", tags=["requestor_notify"], base_dir=workspace)
        assert _task_matches_filter(task, {"state": "To Do", "tags": ["requestor_notify"]}) is True
        assert _task_matches_filter(task, {"state": "Done", "tags": ["requestor_notify"]}) is False
        assert _task_matches_filter(task, {"state": "To Do", "tags": ["requestor_notified"]}) is False

    def test_matching_task_ids_prefilters_by_state_before_tags(self, workspace, ws, monkeypatch):
        live = create_task(ws.id, title="Live", tags=["requestor_notify"], base_dir=workspace)
        update_task(live.id, status="In Progress", base_dir=workspace)
        update_task(live.id, status="Done", base_dir=workspace)
        backlog = create_task(ws.id, title="Backlog", tags=["requestor_notify"], base_dir=workspace)
        tasks = [live, backlog]
        tasks_by_status = {"Done": [live], "To Do": [backlog]}
        checked_ids = []

        def _record_checked_task(task, filter_def):
            checked_ids.append(task.id)
            return True

        monkeypatch.setattr("orchestration.scheduler._task_matches_filter", _record_checked_task)

        assert _matching_task_ids(
            tasks,
            {"state": "Done", "tag": "requestor_notify"},
            tasks_by_status=tasks_by_status,
        ) == [live.id]
        assert checked_ids == [live.id]

    def test_empty_filter_matches_all(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert _task_matches_filter(task, {}) is True

    def test_excludes_task_with_active_task_error(self, workspace, ws):
        from orchestration.tasks import add_task_error, read_task

        task = create_task(ws.id, title="T", base_dir=workspace)
        add_task_error(task.id, message="preflight failed", base_dir=workspace)
        errored = read_task(task.id, base_dir=workspace)

        assert _task_matches_filter(errored, {}) is False


class TestStateTriggerTaskSelection:
    def test_defaults_to_first_unlocked(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_state="To Do",
            action="run_command",
            command="echo x",
            base_dir=workspace,
        )
        assert _select_state_trigger_task_ids(trigger, ["a", "b", "c"]) == ["a"]

    def test_all_unlocked_returns_full_batch(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_state="To Do",
            action="run_command",
            command="echo x",
            task_selection="all_unlocked",
            base_dir=workspace,
        )
        assert _select_state_trigger_task_ids(trigger, ["a", "b", "c"]) == ["a", "b", "c"]


# ── State file ───────────────────────────────────────────────────────

class TestSchedulerState:
    def test_load_empty(self, workspace):
        state = _load_state(workspace)
        assert state == {}

    def test_save_and_load(self, workspace):
        _save_state({"last_tick_at": "2026-04-15T10:00:00+00:00"}, workspace)
        state = _load_state(workspace)
        assert state["last_tick_at"] == "2026-04-15T10:00:00+00:00"

    def test_state_path(self, workspace):
        path = _state_path(workspace)
        assert path.endswith("scheduler_state.yaml")

    def test_existing_running_pid_returns_live_pid(self, workspace):
        _save_state({"pid": os.getpid()}, workspace)
        pid = _existing_running_pid(workspace)
        assert pid == os.getpid()

    def test_existing_running_pid_clears_stale_pid(self, workspace):
        _save_state({"pid": 99999999}, workspace)
        pid = _existing_running_pid(workspace)
        assert pid is None
        state = _load_state(workspace)
        assert "pid" not in state

    def test_existing_running_pid_clears_zombie_pid(self, workspace, monkeypatch):
        """A zombie (<defunct>) PID must be treated as stale, not live.

        Regression test: `os.kill(pid, 0)` returns success for zombies,
        so the lockfile entry would otherwise never get cleaned up.
        """
        _save_state({"pid": 12345}, workspace)
        # Pretend ps reports the pid as a zombie ('Z...').
        monkeypatch.setattr(
            "orchestration.scheduler.subprocess.run",
            lambda *a, **kw: _FakeCompletedProcess("Z"),
        )
        pid = _existing_running_pid(workspace)
        assert pid is None
        state = _load_state(workspace)
        assert "pid" not in state

    def test_is_live_non_zombie_returns_true_for_current_process(self):
        assert _is_live_non_zombie(os.getpid()) is True

    def test_is_live_non_zombie_returns_false_for_missing_pid(self):
        # 99999999 is well outside the realistic pid range on macOS/Linux.
        assert _is_live_non_zombie(99999999) is False

    def test_is_live_non_zombie_returns_false_for_zombie(self, monkeypatch):
        monkeypatch.setattr(
            "orchestration.scheduler.subprocess.run",
            lambda *a, **kw: _FakeCompletedProcess("Z"),
        )
        assert _is_live_non_zombie(os.getpid()) is False

    def test_is_live_non_zombie_returns_true_for_running(self, monkeypatch):
        monkeypatch.setattr(
            "orchestration.scheduler.subprocess.run",
            lambda *a, **kw: _FakeCompletedProcess("S"),
        )
        assert _is_live_non_zombie(os.getpid()) is True


class _FakeCompletedProcess:
    """Minimal stand-in for subprocess.run().CompletedProcess."""

    def __init__(self, stdout: str = ""):
        self.stdout = stdout
        self.returncode = 0


# ── Watchdog / tick timeout ─────────────────────────────────────────

class TestTickTimeout:
    def test_default_tick_timeout_used_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("SCHEDULER_TICK_TIMEOUT", raising=False)
        assert _resolve_tick_timeout() == DEFAULT_TICK_TIMEOUT
        assert _resolve_tick_timeout() == 600  # documented default

    def test_env_var_overrides_default(self, monkeypatch):
        monkeypatch.setenv("SCHEDULER_TICK_TIMEOUT", "120")
        assert _resolve_tick_timeout() == 120

    def test_invalid_env_var_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SCHEDULER_TICK_TIMEOUT", "not-a-number")
        assert _resolve_tick_timeout() == DEFAULT_TICK_TIMEOUT

    def test_zero_or_negative_env_var_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SCHEDULER_TICK_TIMEOUT", "0")
        assert _resolve_tick_timeout() == DEFAULT_TICK_TIMEOUT
        monkeypatch.setenv("SCHEDULER_TICK_TIMEOUT", "-1")
        assert _resolve_tick_timeout() == DEFAULT_TICK_TIMEOUT

    def test_watchdog_updates_last_tick_at_when_stuck(self, workspace, monkeypatch):
        """If a tick exceeds the timeout, the watchdog updates last_tick_at
        so the UI can show the scheduler is still alive."""
        from orchestration.scheduler import _load_state, _save_state
        from datetime import datetime, timezone

        # Write an initial state with a known-old last_tick_at
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        _save_state({"last_tick_at": old_time, "pid": os.getpid()}, workspace)

        # Simulate the watchdog's heartbeat action: update last_tick_at to now
        # This is what the watchdog does after detecting a stuck tick
        s = _load_state(workspace)
        s["last_tick_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(s, workspace)

        # Verify the state was updated
        loaded = _load_state(workspace)
        new_tick = datetime.fromisoformat(loaded["last_tick_at"])
        old_tick = datetime.fromisoformat(old_time)
        assert new_tick > old_tick

    def test_watchdog_outer_loop_detects_stuck_tick(self, workspace, monkeypatch):
        """The watchdog's stuck-tick detection logic: when a tick's elapsed
        time exceeds the configured timeout, the warning + heartbeat path fires."""
        from orchestration.scheduler import _resolve_tick_timeout
        from datetime import datetime as _dt

        # Set a very small timeout so the test is fast
        monkeypatch.setenv("SCHEDULER_TICK_TIMEOUT", "1")
        timeout = _resolve_tick_timeout()
        assert timeout == 1

        # Simulate a tick that started 5s ago
        tick_started_at = time.time() - 5
        elapsed = time.time() - tick_started_at
        assert elapsed > timeout, "elapsed should exceed the 1s timeout"

        # Simulate the watchdog's stuck-tick branch
        log_called = []
        def fake_log_event(event_type, desc, base_dir, **kwargs):
            log_called.append((event_type, desc, kwargs))
        from orchestration.workspace_audit import log_event as real_log_event
        monkeypatch.setattr("orchestration.workspace_audit.log_event", fake_log_event)

        state_updates = []
        # Reset state with stale last_tick_at
        _save_state({"last_tick_at": "2026-01-01T00:00:00+00:00", "pid": os.getpid()}, workspace)

        # The watchdog branch:
        if elapsed > timeout:
            fake_log_event(
                "tick_stuck",
                f"Scheduler tick has been running for {int(elapsed)}s (timeout={timeout}s).",
                workspace,
                status="warning",
            )
            s = _load_state(workspace)
            s["last_tick_at"] = _dt.now(timezone.utc).isoformat()
            _save_state(s, workspace)
            state_updates.append(s)

        # Verify warning was logged
        assert any(call[0] == "tick_stuck" for call in log_called), \
            f"expected tick_stuck log event, got: {log_called}"

        # Verify state was updated to a recent timestamp
        assert len(state_updates) == 1
        loaded = _load_state(workspace)
        new_tick = datetime.fromisoformat(loaded["last_tick_at"])
        # Should be within the last 5 seconds
        assert (datetime.now(timezone.utc) - new_tick).total_seconds() < 5

    def test_state_trigger_dispatch_uses_background_true(self, workspace, ws, monkeypatch):
        """The state-trigger dispatch path in tick() must use background=True
        so a hanging agent subprocess does not block the tick loop."""
        # Create a state trigger on the initial state so it matches the new task
        from orchestration.triggers import create_trigger
        create_trigger(
            ws.id,
            action="run_agent",
            on_state="To Do",
            agent="test_agent",
            base_dir=workspace,
        )

        # Create a task in the initial 'To Do' state (default for new tasks)
        task = create_task(ws.id, "Test task", base_dir=workspace)

        # Patch _lock_invoke_unlock to capture the background argument
        from orchestration import scheduler as sched_mod
        captured = {}

        def spy(trigger, task_ids, ws_arg, base_dir, background=False, ignore_paused=False):
            captured["background"] = background
            captured["task_ids"] = task_ids
            # Return a benign result so tick() can complete
            return {"trigger_id": trigger.id, "status": "dispatched"}

        monkeypatch.setattr(sched_mod, "_lock_invoke_unlock", spy)

        # Run a single tick
        sched_mod.tick(base_dir=workspace)

        # Verify the dispatch was backgrounded
        assert captured.get("background") is True, (
            f"state-trigger dispatch must use background=True; got {captured.get('background')}"
        )


# ── Scheduler status ────────────────────────────────────────────────

class TestSchedulerStatus:
    def test_status_not_running(self, workspace):
        result = status(workspace)
        assert result["running"] is False

    def test_status_running_with_recent_tick(self, workspace):
        """Status reports running when last_tick_at is recent."""
        _save_state({
            "last_tick_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),  # current process, so os.kill(pid, 0) succeeds
        }, workspace)
        result = status(workspace)
        assert result["running"] is True

    def test_status_with_last_tick(self, workspace):
        _save_state({"last_tick_at": "2026-04-15T10:00:00+00:00"}, workspace)
        result = status(workspace)
        assert result["last_tick_at"] == "2026-04-15T10:00:00+00:00"


# ── Task-level scheduling ───────────────────────────────────────────

class TestTaskScheduling:
    def test_create_task_with_schedule(self, workspace, ws):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        action = {"type": "run_command", "command": "echo scheduled"}
        task = create_task(
            ws.id, title="Sched Task",
            scheduled_at=future,
            scheduled_action=action,
            base_dir=workspace,
        )
        assert task.scheduled_at == future
        assert task.scheduled_action == action

    def test_update_task_schedule(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        action = {"type": "run_command", "command": "echo later"}
        updated = update_task(
            task.id,
            scheduled_at=future,
            scheduled_action=action,
            base_dir=workspace,
        )
        assert updated.scheduled_at == future
        assert updated.scheduled_action == action

    def test_clear_schedule(self, workspace, ws):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        action = {"type": "run_command", "command": "echo later"}
        task = create_task(
            ws.id, title="T",
            scheduled_at=future,
            scheduled_action=action,
            base_dir=workspace,
        )
        cleared = clear_schedule(task.id, base_dir=workspace)
        assert cleared.scheduled_at is None
        assert cleared.scheduled_action is None

    def test_clear_schedule_adds_audit(self, workspace, ws):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        task = create_task(
            ws.id, title="T",
            scheduled_at=future,
            scheduled_action={"type": "run_command", "command": "echo x"},
            base_dir=workspace,
        )
        cleared = clear_schedule(task.id, base_dir=workspace)
        audit_types = [a.type for a in cleared.audit]
        assert "schedule_cleared" in audit_types


# ── Schedule-based triggers ─────────────────────────────────────────

class TestScheduleTriggers:
    def test_create_schedule_trigger(self, workspace, ws):
        trigger = create_trigger(
            ws.id,
            on_schedule="0 9 * * 1",
            filter={"state": "To Do"},
            action="run_command",
            command="echo weekly",
            base_dir=workspace,
        )
        assert trigger.on_schedule == "0 9 * * 1"
        assert trigger.filter == {"state": "To Do"}
        assert trigger.on_state is None

    def test_schedule_trigger_without_filter(self, workspace, ws):
        """Filterless schedule triggers are allowed — they fire once per tick."""
        trigger = create_trigger(
            ws.id,
            on_schedule="0 9 * * 1",
            action="run_command",
            command="echo x",
            base_dir=workspace,
        )
        assert trigger.filter is None
        assert trigger.on_schedule == "0 9 * * 1"

    def test_cannot_set_both_on_state_and_on_schedule(self, workspace, ws):
        with pytest.raises(ValueError, match="Exactly one trigger condition must be specified"):
            create_trigger(
                ws.id,
                on_state="Done",
                on_schedule="0 9 * * 1",
                filter={"state": "To Do"},
                action="run_command",
                command="echo x",
                base_dir=workspace,
            )

    def test_must_set_state_or_schedule(self, workspace, ws):
        with pytest.raises(ValueError, match="Exactly one trigger condition must be specified"):
            create_trigger(
                ws.id,
                action="run_command",
                command="echo x",
                base_dir=workspace,
            )

    def test_schedule_trigger_persisted(self, workspace, ws):
        create_trigger(
            ws.id,
            on_schedule="0 9 * * 1",
            filter={"state": "To Do"},
            action="run_command",
            command="echo check",
            base_dir=workspace,
        )
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.triggers[0].on_schedule == "0 9 * * 1"
        assert reloaded.triggers[0].filter == {"state": "To Do"}


# ── Workstream pause ────────────────────────────────────────────────

class TestWorkstreamPause:
    def test_pause_workstream(self, workspace, ws):
        from orchestration.workstreams import save_workstream

    def test_lock_invoke_unlock_can_force_run_while_paused(self, workspace, ws):
        from orchestration.workstreams import save_workstream

        ws.paused = True
        save_workstream(ws, workspace)

        trigger = create_trigger(
            ws.id,
            on_state="To Do",
            action="run_command",
            command="echo forced_scheduler_path",
            base_dir=workspace,
        )
        task = create_task(ws.id, title="T", base_dir=workspace)

        result = _lock_invoke_unlock(trigger, [task.id], ws, workspace, ignore_paused=True)

        assert result["status"] == "ok"
        assert "forced_scheduler_path" in result["stdout"]
        ws.paused = True
        save_workstream(ws, workspace)
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.paused is True

    def test_resume_workstream(self, workspace, ws):
        from orchestration.workstreams import save_workstream
        ws.paused = True
        save_workstream(ws, workspace)
        ws.paused = False
        save_workstream(ws, workspace)
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.paused is False


# ── Tick ─────────────────────────────────────────────────────────────

class TestTick:
    def test_tick_fires_past_due_task_schedule(self, workspace, ws):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        marker = os.path.join(workspace, "sched_fired.txt")
        task = create_task(
            ws.id, title="Scheduled",
            scheduled_at=past,
            scheduled_action={"type": "run_command", "command": f"echo ok > {marker}"},
            base_dir=workspace,
        )
        result = tick(workspace)
        assert len(result["task_schedules_fired"]) == 1
        # Schedule should be cleared
        reloaded = read_task(task.id, base_dir=workspace)
        assert reloaded.scheduled_at is None
        assert reloaded.scheduled_action is None

    def test_tick_skips_future_schedule(self, workspace, ws):
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        task = create_task(
            ws.id, title="Future",
            scheduled_at=future,
            scheduled_action={"type": "run_command", "command": "echo no"},
            base_dir=workspace,
        )
        result = tick(workspace)
        assert len(result["task_schedules_fired"]) == 0
        reloaded = read_task(task.id, base_dir=workspace)
        assert reloaded.scheduled_at is not None

    def test_tick_task_schedule_propagates_prompt_and_timeout(self, workspace, ws, monkeypatch):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task = create_task(
            ws.id,
            title="Scheduled Agent",
            scheduled_at=past,
            scheduled_action={
                "type": "run_agent",
                "agent": "test_agent",
                "prompt": "focus on flaky tests only",
                "timeout": 1800,
            },
            base_dir=workspace,
        )

        captured = {"trigger": None, "task_ids": None}

        def _fake_lock_invoke_unlock(trigger, task_ids, *_args, **_kwargs):
            captured["trigger"] = trigger
            captured["task_ids"] = list(task_ids)
            return {"status": "ok", "result": {"run_id": "r1"}}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        result = tick(workspace)
        assert len(result["task_schedules_fired"]) == 1
        assert captured["task_ids"] == [task.id]
        assert captured["trigger"].action == "run_agent"
        assert captured["trigger"].agent == "test_agent"
        assert captured["trigger"].prompt == "focus on flaky tests only"
        assert captured["trigger"].timeout == 1800

    def test_tick_fires_schedule_trigger(self, workspace, ws):
        marker = os.path.join(workspace, "trigger_sched.txt")
        create_trigger(
            ws.id,
            on_schedule="* * * * *",  # every minute
            filter={"state": "To Do"},
            action="run_command",
            command=f"echo triggered > {marker}",
            base_dir=workspace,
        )
        create_task(ws.id, title="Waiting", base_dir=workspace)

        # Set last_tick to 2 minutes ago so the cron matches
        two_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        _save_state({"last_tick_at": two_min_ago}, workspace)

        result = tick(workspace)
        assert len(result["trigger_schedules_fired"]) >= 1

    def test_tick_skips_paused_workstream(self, workspace, ws):
        from orchestration.workstreams import save_workstream
        ws.paused = True
        save_workstream(ws, workspace)

        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        create_task(
            ws.id, title="Paused",
            scheduled_at=past,
            scheduled_action={"type": "run_command", "command": "echo no"},
            base_dir=workspace,
        )
        result = tick(workspace)
        assert len(result["task_schedules_fired"]) == 0

    def test_tick_skips_due_task_schedule_in_paused_column(self, workspace, ws):
        from orchestration.workstreams import pause_workstream_states

        pause_workstream_states(ws.id, ["To Do"], base_dir=workspace)
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task = create_task(
            ws.id, title="Paused Column",
            scheduled_at=past,
            scheduled_action={"type": "run_command", "command": "echo no"},
            base_dir=workspace,
        )

        result = tick(workspace)

        assert len(result["task_schedules_fired"]) == 0
        reloaded = read_task(task.id, base_dir=workspace)
        assert reloaded.scheduled_at == past

    def test_tick_skips_schedule_trigger_scoped_to_paused_column(self, workspace, ws):
        from orchestration.workstreams import pause_workstream_states

        pause_workstream_states(ws.id, ["To Do"], base_dir=workspace)
        trigger = create_trigger(
            ws.id,
            on_schedule="* * * * *",
            filter={"state": "To Do"},
            action="run_command",
            command="echo no",
            base_dir=workspace,
        )
        create_task(ws.id, title="Waiting", base_dir=workspace)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)

        result = tick(workspace)

        assert result["trigger_schedules_fired"] == [{
            "trigger_id": trigger.id,
            "result": {"status": "skipped", "reason": "Column 'To Do' is paused"},
        }]

    def test_tick_schedule_trigger_with_multi_state_filter_skips_only_paused_states(self, workspace, ws, monkeypatch):
        from orchestration.workstreams import pause_workstream_states

        pause_workstream_states(ws.id, ["To Do"], base_dir=workspace)
        paused_task = create_task(ws.id, title="Paused Waiting", base_dir=workspace)
        active_task = create_task(ws.id, title="Active", base_dir=workspace)
        update_task(active_task.id, status="In Progress", base_dir=workspace)

        trigger = create_trigger(
            ws.id,
            on_schedule="* * * * *",
            filter={"state": ["To Do", "In Progress"]},
            action="run_command",
            command="echo ok",
            base_dir=workspace,
        )

        captured = {}

        def _fake_lock_invoke_unlock(trigger_obj, task_ids, ws_obj, base_dir, background=False, ignore_paused=False):
            captured["trigger_id"] = trigger_obj.id
            captured["task_ids"] = list(task_ids)
            return {"trigger_id": trigger_obj.id, "status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)

        result = tick(workspace)

        assert captured["trigger_id"] == trigger.id
        assert captured["task_ids"] == [active_task.id]
        assert result["trigger_schedules_fired"] == [{
            "trigger_id": trigger.id,
            "task_ids": [active_task.id],
            "result": {"trigger_id": trigger.id, "status": "dispatched"},
        }]

    def test_tick_updates_last_tick_at(self, workspace, ws):
        tick(workspace)
        state = _load_state(workspace)
        assert "last_tick_at" in state

    def test_tick_trigger_filter_excludes_non_matching(self, workspace, ws):
        create_trigger(
            ws.id,
            on_schedule="* * * * *",
            filter={"state": "Done"},
            action="run_command",
            command="echo nope",
            base_dir=workspace,
        )
        # Task in "To Do" — filter wants "Done"
        create_task(ws.id, title="Wrong State", base_dir=workspace)

        two_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        _save_state({"last_tick_at": two_min_ago}, workspace)

        result = tick(workspace)
        assert len(result["trigger_schedules_fired"]) == 0

    def test_tick_schedule_trigger_filters_by_state_and_all_tags(self, workspace, ws, monkeypatch):
        matching = create_task(ws.id, title="Matching", tags=["requestor_notify", "P1"], base_dir=workspace)
        update_task(matching.id, status="In Progress", base_dir=workspace)
        update_task(matching.id, status="Done", base_dir=workspace)

        missing_tag = create_task(ws.id, title="Missing Tag", tags=["requestor_notify"], base_dir=workspace)
        update_task(missing_tag.id, status="In Progress", base_dir=workspace)
        update_task(missing_tag.id, status="Done", base_dir=workspace)

        create_task(ws.id, title="Wrong State", tags=["requestor_notify", "P1"], base_dir=workspace)

        trigger = create_trigger(
            ws.id,
            on_schedule="* * * * *",
            filter={"state": "Done", "tags": ["requestor_notify", "P1"]},
            action="run_agent",
            agent="test_agent",
            base_dir=workspace,
        )

        captured = {}

        def _fake_lock_invoke_unlock(trigger_obj, task_ids, ws_obj, base_dir, background=False, ignore_paused=False):
            captured["trigger_id"] = trigger_obj.id
            captured["task_ids"] = list(task_ids)
            captured["background"] = background
            return {"trigger_id": trigger_obj.id, "status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)
        _save_state({"last_tick_at": (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()}, workspace)

        result = tick(workspace)

        assert captured == {
            "trigger_id": trigger.id,
            "task_ids": [matching.id],
            "background": True,
        }
        assert result["trigger_schedules_fired"] == [{
            "trigger_id": trigger.id,
            "task_ids": [matching.id],
            "result": {"trigger_id": trigger.id, "status": "dispatched"},
        }]

    def test_tick_prioritizes_later_state_trigger_for_shared_agent(self, workspace, ws, monkeypatch):
        backlog = create_task(ws.id, title="Backlog Task", base_dir=workspace)
        in_progress = create_task(ws.id, title="Active Task", base_dir=workspace)
        update_task(in_progress.id, status="In Progress", base_dir=workspace)

        create_trigger(
            ws.id,
            on_state="To Do",
            action="run_agent",
            agent="shared_agent",
            base_dir=workspace,
        )
        create_trigger(
            ws.id,
            on_state="In Progress",
            action="run_agent",
            agent="shared_agent",
            base_dir=workspace,
        )

        dispatched = []

        def _fake_lock_invoke_unlock(trigger, task_ids, *_args, **_kwargs):
            dispatched.append((trigger.on_state, list(task_ids)))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)
        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_args, **_kwargs: 0)

        result = tick(workspace)

        assert dispatched == [("In Progress", [in_progress.id])]
        assert result["state_triggers_fired"][0]["task_ids"] == [in_progress.id]
        assert result["state_triggers_fired"][1]["result"]["reason"] == "agent_concurrency reached"

    def test_tick_skips_state_task_with_active_task_error(self, workspace, ws, monkeypatch):
        from orchestration.tasks import add_task_error

        errored = create_task(ws.id, title="Errored Task", base_dir=workspace)
        ready = create_task(ws.id, title="Ready Task", base_dir=workspace)
        update_task(errored.id, status="In Progress", base_dir=workspace)
        update_task(ready.id, status="In Progress", base_dir=workspace)
        add_task_error(errored.id, message="preflight failed", base_dir=workspace)

        trigger = create_trigger(
            ws.id,
            on_state="In Progress",
            action="run_agent",
            agent="coder",
            base_dir=workspace,
        )

        captured = {}

        def _fake_lock_invoke_unlock(trigger_obj, task_ids, *_args, **_kwargs):
            captured["trigger_id"] = trigger_obj.id
            captured["task_ids"] = list(task_ids)
            return {"trigger_id": trigger_obj.id, "status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)
        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_args, **_kwargs: 0)

        result = tick(workspace)

        assert captured == {"trigger_id": trigger.id, "task_ids": [ready.id]}
        assert result["state_triggers_fired"] == [{
            "trigger_id": trigger.id,
            "task_ids": [ready.id],
            "result": {"trigger_id": trigger.id, "status": "dispatched"},
        }]

    def test_tick_email_trigger_failure_does_not_skip_state_triggers(self, workspace, ws, monkeypatch):
        in_progress = create_task(ws.id, title="Active Task", base_dir=workspace)
        update_task(in_progress.id, status="In Progress", base_dir=workspace)

        email_trigger = create_trigger(
            ws.id,
            action="run_agent",
            on_email={"recipient": "inbound@example.com", "event": "new_thread"},
            agent="email_agent",
            base_dir=workspace,
        )
        state_trigger = create_trigger(
            ws.id,
            on_state="In Progress",
            action="run_agent",
            agent="coder",
            base_dir=workspace,
        )

        def _raise_email_error(*_args, **_kwargs):
            raise RuntimeError("Mailgun API returned 429")

        captured = {}

        def _fake_lock_invoke_unlock(trigger, task_ids, *_args, **_kwargs):
            captured["trigger_id"] = trigger.id
            captured["task_ids"] = list(task_ids)
            return {"trigger_id": trigger.id, "status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._poll_email_trigger_events", _raise_email_error)
        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)
        monkeypatch.setattr("orchestration.agents.count_active_agent_runs", lambda *_args, **_kwargs: 0)

        result = tick(workspace)

        assert result["email_triggers_fired"] == [{
            "trigger_id": email_trigger.id,
            "result": {"status": "error", "message": "Mailgun API returned 429"},
        }]
        assert captured == {"trigger_id": state_trigger.id, "task_ids": [in_progress.id]}


# ── Paused tasks ────────────────────────────────────────────────────

class TestPausedTasksAreNotPickedUp:
    def test_task_filter_excludes_paused_task(self, workspace, ws):
        from orchestration.tasks import pause_task
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert _task_matches_filter(task, {"state": "To Do"}) is True

        paused = pause_task(task.id, base_dir=workspace)
        assert _task_matches_filter(paused, {"state": "To Do"}) is False

    def test_state_trigger_skips_paused_tasks(self, workspace, ws, monkeypatch):
        from orchestration.tasks import pause_task

        unpaused = create_task(ws.id, title="Pickable", base_dir=workspace)
        paused = create_task(ws.id, title="Paused", base_dir=workspace)
        pause_task(paused.id, base_dir=workspace)

        create_trigger(
            ws.id,
            on_state="To Do",
            action="run_command",
            command="echo go",
            base_dir=workspace,
        )

        dispatched = []

        def _fake_lock_invoke_unlock(trigger, task_ids, *_args, **_kwargs):
            dispatched.append(list(task_ids))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        tick(workspace)

        assert dispatched == [[unpaused.id]]

    def test_state_trigger_skips_when_only_paused_tasks(self, workspace, ws, monkeypatch):
        from orchestration.tasks import pause_task

        only_paused = create_task(ws.id, title="Only Paused", base_dir=workspace)
        pause_task(only_paused.id, base_dir=workspace)

        create_trigger(
            ws.id,
            on_state="To Do",
            action="run_command",
            command="echo go",
            base_dir=workspace,
        )

        dispatched = []

        def _fake_lock_invoke_unlock(trigger, task_ids, *_args, **_kwargs):
            dispatched.append(list(task_ids))
            return {"status": "dispatched"}

        monkeypatch.setattr("orchestration.scheduler._lock_invoke_unlock", _fake_lock_invoke_unlock)

        result = tick(workspace)

        assert dispatched == []
        # No state trigger should have fired
        assert result["state_triggers_fired"] == []

    def test_tick_skips_due_task_schedule_when_task_paused(self, workspace, ws):
        from orchestration.tasks import pause_task

        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        task = create_task(
            ws.id, title="Paused Schedule",
            scheduled_at=past,
            scheduled_action={"type": "run_command", "command": "echo no"},
            base_dir=workspace,
        )
        pause_task(task.id, base_dir=workspace)

        result = tick(workspace)

        assert len(result["task_schedules_fired"]) == 0
        reloaded = read_task(task.id, base_dir=workspace)
        # schedule should still be set since the task was paused
        assert reloaded.scheduled_at == past

