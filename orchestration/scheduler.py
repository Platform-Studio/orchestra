"""Scheduler operations.

The scheduler is a standalone process that calls `tick()` every 60 seconds.
Run it via:  python -m orchestration.cli scheduler run

It is decoupled from the workstream manager web server — the web server
reads scheduler_state.yaml to display status but does not drive ticks.
"""

import os
import signal
import subprocess
import sys
import time
import yaml

from datetime import datetime, timezone

from .models import now_iso
from .workstreams import list_workstreams
from .tasks import list_tasks, read_task, _save_task
from .triggers import execute_trigger


SCHEDULER_STATE_FILE = "scheduler_state.yaml"
TICK_INTERVAL = 60  # seconds


def _state_path(base_dir: str) -> str:
    return os.path.join(base_dir, SCHEDULER_STATE_FILE)


def _load_state(base_dir: str) -> dict:
    path = _state_path(base_dir)
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_state(state: dict, base_dir: str) -> None:
    path = _state_path(base_dir)
    with open(path, "w") as f:
        yaml.dump(state, f, default_flow_style=False, sort_keys=False)


def run(base_dir: str = ".") -> None:
    """Run the scheduler as a foreground process. Ticks every 60 seconds."""
    abs_dir = os.path.abspath(base_dir)
    running = True

    def handle_signal(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Record that we're running
    state = _load_state(abs_dir)
    state["pid"] = os.getpid()
    _save_state(state, abs_dir)

    from .workspace_audit import log_event
    log_event("scheduler_started", f"Scheduler process started (pid={os.getpid()})", abs_dir)
    print(f"Scheduler running (pid={os.getpid()}), ticking every {TICK_INTERVAL}s. Ctrl+C to stop.")

    try:
        while running:
            try:
                tick(abs_dir)
            except Exception as e:
                print(f"[scheduler] tick error: {e}", file=sys.stderr)
            # Sleep in small increments so we can respond to signals promptly
            for _ in range(TICK_INTERVAL):
                if not running:
                    break
                time.sleep(1)
    finally:
        state = _load_state(abs_dir)
        state.pop("pid", None)
        _save_state(state, abs_dir)
        log_event("scheduler_stopped", "Scheduler process stopped", abs_dir)
        print("Scheduler stopped.")


def stop(base_dir: str = ".") -> dict:
    """Stop a running scheduler process by sending SIGTERM to its PID."""
    state = _load_state(base_dir)
    pid = state.get("pid")
    if pid is None:
        return {"message": "Scheduler is not running (no pid in state file)"}
    try:
        os.kill(pid, 0)  # check if alive
    except OSError:
        state.pop("pid", None)
        _save_state(state, base_dir)
        return {"message": f"Scheduler pid {pid} is not running (stale)"}
    os.kill(pid, signal.SIGTERM)
    return {"message": f"Sent SIGTERM to scheduler (pid={pid})"}


def status(base_dir: str = ".") -> dict:
    """Report scheduler status by checking state file."""
    state = _load_state(base_dir)
    result = {}

    # Check if the scheduler process is alive via its pid
    pid = state.get("pid")
    if pid is not None:
        try:
            os.kill(pid, 0)  # signal 0 = check if process exists
            result["running"] = True
            result["pid"] = pid
        except OSError:
            result["running"] = False  # stale pid
    else:
        result["running"] = False

    if "last_tick_at" in state:
        result["last_tick_at"] = state["last_tick_at"]
    return result


def _cron_matches_between(cron_expr: str, last_tick: datetime, now: datetime) -> bool:
    """Check if a cron expression matched at any point between last_tick and now.

    Supports standard 5-field cron: minute hour day_of_month month day_of_week
    Uses simple matching (no ranges/lists/steps, just * and exact values).
    """
    parts = cron_expr.strip().split()
    if len(parts) != 5:
        return False

    # Walk through each minute from last_tick+1min to now
    from datetime import timedelta
    check = last_tick.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while check <= now:
        if _cron_matches_time(parts, check):
            return True
        check += timedelta(minutes=1)
    return False


def _cron_matches_time(parts: list, dt: datetime) -> bool:
    """Check if a 5-field cron expression matches a specific datetime."""
    fields = [dt.minute, dt.hour, dt.day, dt.month, (dt.weekday() + 1) % 7]  # cron: 0=Sunday
    for field_val, pattern in zip(fields, parts):
        if pattern == "*":
            continue
        try:
            if int(pattern) != field_val:
                return False
        except ValueError:
            return False
    return True


def _task_matches_filter(task, filter_def: dict) -> bool:
    """Check if a task matches a trigger filter."""
    if "state" in filter_def:
        if task.status != filter_def["state"]:
            return False

    if "tags" in filter_def:
        required_tags = filter_def["tags"]
        if not any(t in task.tags for t in required_tags):
            return False

    if "older_than_days" in filter_def:
        days = filter_def["older_than_days"]
        # Find the created audit entry
        for entry in task.audit:
            if entry.type == "created":
                created = datetime.fromisoformat(entry.timestamp)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                if (now - created).days < days:
                    return False
                break

    return True


def _audit_trigger(trigger, result, ws, base_dir, task_id=None):
    """Log a workspace audit entry for a scheduled trigger fire."""
    from .workspace_audit import log_event
    status = result.get("status", "unknown")
    desc = f"Scheduled trigger '{trigger.action}' fired"
    if trigger.agent:
        desc += f" (agent: {trigger.agent})"
    if trigger.command:
        desc += f" (command: {trigger.command})"
    if status == "error":
        desc += f" — ERROR: {result.get('message', result.get('stderr', ''))}"
    kwargs = dict(
        trigger_id=trigger.id, workstream_id=ws.id, status=status,
    )
    if task_id:
        kwargs["task_id"] = task_id
    log_event("trigger_fired", desc, base_dir, **kwargs)


def tick(base_dir: str = ".") -> dict:
    """Execute one scheduler tick.

    1. Load all non-paused workstreams
    2. Fire task-level schedules that have passed
    3. Evaluate schedule-based triggers
    4. Update last_tick_at
    """
    now = datetime.now(timezone.utc)
    state = _load_state(base_dir)
    last_tick_str = state.get("last_tick_at")
    if last_tick_str:
        last_tick = datetime.fromisoformat(last_tick_str)
        if last_tick.tzinfo is None:
            last_tick = last_tick.replace(tzinfo=timezone.utc)
    else:
        # First tick ever — set last_tick to 1 minute ago
        from datetime import timedelta
        last_tick = now - timedelta(minutes=1)

    results = {"task_schedules_fired": [], "trigger_schedules_fired": [], "state_triggers_fired": []}

    workstreams = list_workstreams(base_dir)
    for ws in workstreams:
        if ws.paused:
            continue

        tasks = list_tasks(ws.id, base_dir=base_dir)

        # 1. Task-level schedules
        for task in tasks:
            if task.scheduled_at is None:
                continue
            sched_time = datetime.fromisoformat(task.scheduled_at)
            if sched_time.tzinfo is None:
                sched_time = sched_time.replace(tzinfo=timezone.utc)
            if sched_time <= now:
                action = task.scheduled_action
                if action:
                    # Build a temporary trigger to execute the action
                    from .models import Trigger, new_id
                    temp_trigger = Trigger(
                        id=new_id(),
                        action=action.get("type", "run_command"),
                        agent=action.get("agent"),
                        command=action.get("command"),
                    )
                    result = execute_trigger(temp_trigger, task.id, ws.id, base_dir)
                    results["task_schedules_fired"].append({
                        "task_id": task.id,
                        "result": result,
                    })

                # Clear the schedule — re-read to get latest state
                task_fresh = read_task(task.id, base_dir)
                task_fresh.scheduled_at = None
                task_fresh.scheduled_action = None
                task_fresh.add_audit("schedule_fired", f"Scheduled action fired at {now.isoformat()}")
                _save_task(task_fresh, base_dir)

        # 2. Schedule-based triggers
        for trigger in ws.triggers:
            if trigger.on_schedule is None:
                continue
            if not _cron_matches_between(trigger.on_schedule, last_tick, now):
                continue

            # Check max_concurrent limit
            from .locks import active_lock_count
            current_locks = active_lock_count(ws.id, base_dir=base_dir)
            if current_locks >= trigger.max_concurrent:
                from .workspace_audit import log_event as _log_event
                _log_event(
                    "trigger_skipped",
                    f"Scheduled trigger '{trigger.action}' skipped — "
                    f"{current_locks}/{trigger.max_concurrent} concurrent locks active",
                    base_dir, trigger_id=trigger.id, workstream_id=ws.id,
                    status="skipped",
                )
                results["trigger_schedules_fired"].append({
                    "trigger_id": trigger.id,
                    "status": "skipped",
                    "message": f"max_concurrent ({trigger.max_concurrent}) reached",
                })
                continue

            if trigger.filter is None:
                # No filter — fire once (standalone command, not per-task)
                result = execute_trigger(trigger, "", ws.id, base_dir)
                _audit_trigger(trigger, result, ws, base_dir)
                results["trigger_schedules_fired"].append({
                    "trigger_id": trigger.id,
                    "result": result,
                })
            else:
                # Find tasks matching the filter
                filter_def = trigger.filter
                for task in tasks:
                    if _task_matches_filter(task, filter_def):
                        result = execute_trigger(trigger, task.id, ws.id, base_dir)
                        _audit_trigger(trigger, result, ws, base_dir, task_id=task.id)
                        results["trigger_schedules_fired"].append({
                            "trigger_id": trigger.id,
                            "task_id": task.id,
                            "result": result,
                        })

        # 3. State-based triggers
        from .locks import active_lock_count, lock_status
        for trigger in ws.triggers:
            if trigger.on_state is None:
                continue

            # Find tasks in the trigger's target state that aren't locked
            matching_tasks = [t for t in tasks if t.status == trigger.on_state]
            for task in matching_tasks:
                # Skip tasks that already have an active lock
                if lock_status(task.id, base_dir) is not None:
                    continue

                # Check max_concurrent limit before each fire
                current = active_lock_count(ws.id, base_dir=base_dir)
                if current >= trigger.max_concurrent:
                    from .workspace_audit import log_event as _log_event
                    _log_event(
                        "trigger_skipped",
                        f"State trigger '{trigger.action}' skipped for task {task.id} "
                        f"— {current}/{trigger.max_concurrent} concurrent locks active",
                        base_dir, trigger_id=trigger.id, workstream_id=ws.id,
                        task_id=task.id, status="skipped",
                    )
                    break  # At limit — no point checking more tasks

                result = execute_trigger(trigger, task.id, ws.id, base_dir)
                status = result.get("status", "unknown")
                _audit_trigger(trigger, result, ws, base_dir, task_id=task.id)
                results["state_triggers_fired"].append({
                    "trigger_id": trigger.id,
                    "task_id": task.id,
                    "result": result,
                })

    # Update state
    state["last_tick_at"] = now.isoformat()
    _save_state(state, base_dir)

    # 4. Expired lock cleanup + retry
    from .retry import cleanup_expired_locks
    expired_results = cleanup_expired_locks(base_dir)
    results["expired_locks_cleaned"] = expired_results

    results["last_tick_at"] = now.isoformat()
    return results
