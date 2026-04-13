"""Tests for scheduler operations and schedule-based features."""

import os
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from orchestration.workstreams import create_workstream, read_workstream
from orchestration.tasks import create_task, read_task, update_task, clear_schedule
from orchestration.triggers import create_trigger
from orchestration.scheduler import (
    _cron_tag,
    _state_path,
    _load_state,
    _save_state,
    _cron_matches_time,
    _cron_matches_between,
    _task_matches_filter,
    tick,
    status,
)


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


# ── Task filter matching ────────────────────────────────────────────

class TestTaskFilterMatching:
    def test_matches_state(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert _task_matches_filter(task, {"state": "To Do"}) is True
        assert _task_matches_filter(task, {"state": "Done"}) is False

    def test_matches_tags(self, workspace, ws):
        task = create_task(ws.id, title="T", tags=["urgent"], base_dir=workspace)
        assert _task_matches_filter(task, {"tags": ["urgent"]}) is True
        assert _task_matches_filter(task, {"tags": ["low"]}) is False

    def test_empty_filter_matches_all(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert _task_matches_filter(task, {}) is True


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

    def test_cron_tag(self, workspace):
        tag = _cron_tag(workspace)
        assert "orchestration-scheduler:" in tag


# ── Scheduler status ────────────────────────────────────────────────

class TestSchedulerStatus:
    def test_status_not_running(self, workspace):
        with patch("orchestration.scheduler._cron_exists", return_value=False):
            result = status(workspace)
            assert result["running"] is False

    def test_status_with_last_tick(self, workspace):
        _save_state({"last_tick_at": "2026-04-15T10:00:00+00:00"}, workspace)
        with patch("orchestration.scheduler._cron_exists", return_value=True):
            result = status(workspace)
            assert result["running"] is True
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

    def test_schedule_trigger_requires_filter(self, workspace, ws):
        with pytest.raises(ValueError, match="filter"):
            create_trigger(
                ws.id,
                on_schedule="0 9 * * 1",
                action="run_command",
                command="echo x",
                base_dir=workspace,
            )

    def test_cannot_set_both_on_state_and_on_schedule(self, workspace, ws):
        with pytest.raises(ValueError, match="Cannot specify both"):
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
        with pytest.raises(ValueError, match="Either"):
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
    @patch("orchestration.scheduler._cron_exists", return_value=True)
    def test_tick_fires_past_due_task_schedule(self, mock_cron, workspace, ws):
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

    @patch("orchestration.scheduler._cron_exists", return_value=True)
    def test_tick_skips_future_schedule(self, mock_cron, workspace, ws):
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

    @patch("orchestration.scheduler._cron_exists", return_value=True)
    def test_tick_fires_schedule_trigger(self, mock_cron, workspace, ws):
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

    @patch("orchestration.scheduler._cron_exists", return_value=True)
    def test_tick_skips_paused_workstream(self, mock_cron, workspace, ws):
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

    @patch("orchestration.scheduler._cron_exists", return_value=True)
    def test_tick_updates_last_tick_at(self, mock_cron, workspace, ws):
        tick(workspace)
        state = _load_state(workspace)
        assert "last_tick_at" in state

    @patch("orchestration.scheduler._cron_exists", return_value=True)
    def test_tick_trigger_filter_excludes_non_matching(self, mock_cron, workspace, ws):
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
