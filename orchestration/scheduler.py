"""Scheduler operations.

Manages schedule-based triggers and task-level schedules via system cron.
The scheduler installs a cron job that runs `scheduler tick` every minute.
"""

import os
import subprocess
import sys
import yaml

from datetime import datetime, timezone

from .models import now_iso
from .workstreams import list_workstreams
from .tasks import list_tasks, read_task, _save_task
from .triggers import _execute_trigger


SCHEDULER_STATE_FILE = "scheduler_state.yaml"


def _cron_tag(base_dir: str) -> str:
    """Generate a unique cron job tag for this workspace."""
    abs_dir = os.path.abspath(base_dir)
    return f"# orchestration-scheduler:{abs_dir}"


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


def _get_crontab() -> str:
    """Get the current crontab content."""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout
        return ""
    except Exception:
        return ""


def _set_crontab(content: str) -> None:
    """Set the crontab content."""
    proc = subprocess.run(
        ["crontab", "-"], input=content, capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to set crontab: {proc.stderr}")


def _cron_exists(base_dir: str) -> bool:
    """Check if the scheduler cron job exists for this workspace."""
    tag = _cron_tag(base_dir)
    crontab = _get_crontab()
    return tag in crontab


def start(base_dir: str = ".") -> dict:
    """Install the scheduler cron job. Idempotent."""
    if _cron_exists(base_dir):
        return {"message": "Scheduler is already running"}

    abs_dir = os.path.abspath(base_dir)
    python = sys.executable
    tag = _cron_tag(base_dir)

    crontab = _get_crontab()
    # Add the cron entry: every minute
    cron_line = f"* * * * * cd {abs_dir} && {python} -m orchestration.cli --base-dir {abs_dir} scheduler tick {tag}\n"
    crontab += cron_line

    _set_crontab(crontab)
    return {"message": "Scheduler started"}


def stop(base_dir: str = ".") -> dict:
    """Remove the scheduler cron job. Idempotent."""
    if not _cron_exists(base_dir):
        return {"message": "Scheduler is not running"}

    tag = _cron_tag(base_dir)
    crontab = _get_crontab()
    lines = [line for line in crontab.splitlines(True) if tag not in line]
    _set_crontab("".join(lines))
    return {"message": "Scheduler stopped"}


def status(base_dir: str = ".") -> dict:
    """Report scheduler status."""
    running = _cron_exists(base_dir)
    state = _load_state(base_dir)
    result = {"running": running}
    if "last_tick_at" in state:
        result["last_tick_at"] = state["last_tick_at"]
    return result


def ensure_started(base_dir: str = ".") -> None:
    """Ensure the scheduler cron job is running. Called internally."""
    if not _cron_exists(base_dir):
        start(base_dir)


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

    results = {"task_schedules_fired": [], "trigger_schedules_fired": []}

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
                    result = _execute_trigger(temp_trigger, task.id, ws.id, base_dir)
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

            if trigger.filter is None:
                # No filter — fire once (standalone command, not per-task)
                result = _execute_trigger(trigger, "", ws.id, base_dir)
                results["trigger_schedules_fired"].append({
                    "trigger_id": trigger.id,
                    "result": result,
                })
            else:
                # Find tasks matching the filter
                filter_def = trigger.filter
                for task in tasks:
                    if _task_matches_filter(task, filter_def):
                        result = _execute_trigger(trigger, task.id, ws.id, base_dir)
                        results["trigger_schedules_fired"].append({
                            "trigger_id": trigger.id,
                            "task_id": task.id,
                            "result": result,
                        })

    # Update state
    state["last_tick_at"] = now.isoformat()
    _save_state(state, base_dir)

    results["last_tick_at"] = now.isoformat()
    return results
