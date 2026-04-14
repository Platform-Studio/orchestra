"""Tests for workspace audit trail."""

import pytest
from orchestration.workspace_audit import log_event, get_audit_log, _load_audit
from orchestration.workstreams import create_workstream
import orchestration.scheduler as _sched_mod


@pytest.fixture(autouse=True)
def _reset_scheduler_loop():
    """Ensure the scheduler loop is stopped between tests."""
    yield
    with _sched_mod._loop_lock:
        _sched_mod._loop_base_dir = None
        if _sched_mod._loop_timer is not None:
            _sched_mod._loop_timer.cancel()
            _sched_mod._loop_timer = None


@pytest.fixture
def ws(workspace):
    return create_workstream(
        name="Audit WS",
        task_states={"To Do": ["Done"], "Done": []},
        base_dir=workspace,
    )


class TestLogEvent:
    def test_basic_log(self, workspace):
        entry = log_event("test_event", "Something happened", workspace)
        assert entry["type"] == "test_event"
        assert entry["description"] == "Something happened"
        assert "timestamp" in entry

    def test_extra_fields(self, workspace, ws):
        entry = log_event(
            "trigger_fired", "Trigger ran", workspace,
            workstream_id=ws.id, trigger_id="t123", status="ok",
        )
        assert entry["workstream_id"] == ws.id
        assert entry["trigger_id"] == "t123"
        assert entry["status"] == "ok"

    def test_persists(self, workspace):
        log_event("ev1", "First", workspace)
        log_event("ev2", "Second", workspace)
        raw = _load_audit(workspace)
        assert len(raw) == 2
        assert raw[0]["type"] == "ev1"
        assert raw[1]["type"] == "ev2"

    def test_caps_at_max(self, workspace):
        from orchestration.workspace_audit import MAX_ENTRIES
        for i in range(MAX_ENTRIES + 10):
            log_event("bulk", f"Entry {i}", workspace)
        raw = _load_audit(workspace)
        assert len(raw) == MAX_ENTRIES
        # Oldest entries should have been trimmed
        assert raw[0]["description"] == "Entry 10"


class TestGetAuditLog:
    def test_returns_newest_first(self, workspace):
        log_event("a", "First", workspace)
        log_event("b", "Second", workspace)
        log_event("c", "Third", workspace)
        entries = get_audit_log(workspace)
        assert entries[0]["type"] == "c"
        assert entries[2]["type"] == "a"

    def test_limit(self, workspace):
        for i in range(10):
            log_event("ev", f"Entry {i}", workspace)
        entries = get_audit_log(workspace, limit=3)
        assert len(entries) == 3
        assert entries[0]["description"] == "Entry 9"

    def test_filter_by_workstream(self, workspace, ws):
        log_event("global", "No ws", workspace)
        log_event("ws_event", "In ws", workspace, workstream_id=ws.id)
        log_event("other_ws", "Other", workspace, workstream_id="other-id")

        entries = get_audit_log(workspace, workstream_id=ws.id)
        types = [e["type"] for e in entries]
        # Should include global events + matching ws events
        assert "global" in types
        assert "ws_event" in types
        assert "other_ws" not in types

    def test_filter_by_type(self, workspace):
        log_event("trigger_fired", "Fired", workspace)
        log_event("scheduler_started", "Started", workspace)
        log_event("trigger_fired", "Fired again", workspace)

        entries = get_audit_log(workspace, event_type="trigger_fired")
        assert len(entries) == 2
        assert all(e["type"] == "trigger_fired" for e in entries)

    def test_empty_log(self, workspace):
        entries = get_audit_log(workspace)
        assert entries == []


class TestPauseResumeAudit:
    def test_pause_logs_event(self, workspace, ws):
        from orchestration.cli import cmd_workstream_pause
        import argparse
        args = argparse.Namespace(id=ws.id, base_dir=workspace)
        cmd_workstream_pause(args)
        entries = get_audit_log(workspace, event_type="workstream_paused")
        assert len(entries) == 1
        assert ws.id in entries[0]["description"] or entries[0].get("workstream_id") == ws.id

    def test_resume_logs_event(self, workspace, ws):
        from orchestration.cli import cmd_workstream_resume
        import argparse
        args = argparse.Namespace(id=ws.id, base_dir=workspace)
        cmd_workstream_resume(args)
        entries = get_audit_log(workspace, event_type="workstream_resumed")
        assert len(entries) == 1
        assert entries[0].get("workstream_id") == ws.id


class TestSchedulerAudit:
    def test_start_logs_event(self, workspace):
        from orchestration.scheduler import start, stop
        start(workspace)
        entries = get_audit_log(workspace, event_type="scheduler_started")
        assert len(entries) == 1
        stop(workspace)

    def test_stop_logs_event(self, workspace):
        from orchestration.scheduler import start, stop
        start(workspace)
        stop(workspace)
        entries = get_audit_log(workspace, event_type="scheduler_stopped")
        assert len(entries) == 1


class TestTriggerAudit:
    def test_state_trigger_logs_event(self, workspace, ws):
        from orchestration.triggers import create_trigger, evaluate_triggers
        from orchestration.tasks import create_task
        create_trigger(
            ws.id, action="run_command", on_state="Done",
            command="echo done", base_dir=workspace,
        )
        task = create_task(ws.id, "Test task", base_dir=workspace)
        # Re-read to get triggers
        from orchestration.workstreams import read_workstream
        ws_fresh = read_workstream(ws.id, workspace)
        evaluate_triggers(ws_fresh, "Done", task.id, workspace)

        entries = get_audit_log(workspace, event_type="trigger_fired")
        assert len(entries) == 1
        assert entries[0].get("workstream_id") == ws.id
        assert entries[0].get("task_id") == task.id
        assert "status" in entries[0]
