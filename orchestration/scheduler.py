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
import threading

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


def _existing_running_pid(base_dir: str):
    """Return an existing live scheduler PID from state, else None.

    Also cleans up stale PID entries when the process is no longer alive.
    """
    state = _load_state(base_dir)
    pid = state.get("pid")
    if pid is None:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        state.pop("pid", None)
        _save_state(state, base_dir)
        return None


def run(base_dir: str = ".") -> None:
    """Run the scheduler as a foreground process. Ticks every 60 seconds."""
    abs_dir = os.path.abspath(base_dir)

    existing_pid = _existing_running_pid(abs_dir)
    if existing_pid is not None and existing_pid != os.getpid():
        raise RuntimeError(
            f"Scheduler is already running for this workspace (pid={existing_pid}). "
            "Stop it first via `python -m orchestration.cli scheduler stop`."
        )

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
    # Accept both "state" and "status" as aliases for the task state field
    state_filter = filter_def.get("state") or filter_def.get("status")
    if state_filter is not None:
        if task.status != state_filter:
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


def _audit_trigger(trigger, result, ws, base_dir, task_ids=None):
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
    if task_ids and len(task_ids) == 1:
        kwargs["task_id"] = task_ids[0]
    elif task_ids:
        kwargs["task_ids"] = task_ids
    log_event("trigger_fired", desc, base_dir, **kwargs)


def _workstream_agent_limit(ws, agent_ref: str, base_dir: str = ".") -> int:
    """Resolve per-agent concurrency limit from workstream policy."""
    default_limit = 1
    overrides = {}

    policy = getattr(ws, "agent_concurrency", None)
    if isinstance(policy, dict):
        try:
            default_limit = int(policy.get("default", 1) or 1)
        except (TypeError, ValueError):
            default_limit = 1
        raw_overrides = policy.get("overrides", {})
        if isinstance(raw_overrides, dict):
            overrides = raw_overrides

    default_limit = max(1, default_limit)

    if not agent_ref:
        return default_limit

    from .agents import _agent_identity_keys

    identity_keys = _agent_identity_keys(agent_ref, base_dir=base_dir)
    for key, value in overrides.items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key and normalized_key in identity_keys:
            try:
                return max(1, int(value))
            except (TypeError, ValueError):
                return default_limit

    return default_limit


def _run_and_unlock(trigger, locked_ids: list, ws, base_dir: str, agent_id: str) -> None:
    """Execute a trigger and release locks on a background daemon thread.

    Lock acquisition is handled by the caller on the main scheduler thread so
    that concurrency checks in subsequent ticks see the lock immediately.
    This function owns only execution and cleanup.
    """
    try:
        result = execute_trigger(trigger, locked_ids, ws.id, base_dir)
        _audit_trigger(trigger, result, ws, base_dir, task_ids=locked_ids)
    except Exception as e:
        print(f"[scheduler] background trigger error: {e}", file=sys.stderr)
    finally:
        from .locks import release_lock
        for tid in locked_ids:
            try:
                release_lock(tid, agent_id=agent_id, base_dir=base_dir)
            except Exception:
                pass  # Lock may have already expired or been released


def _run_without_lock(trigger, ws, base_dir: str, task_ids: list = None) -> None:
    """Execute a trigger with no lock lifecycle on a background daemon thread."""
    ids = task_ids or []
    try:
        result = execute_trigger(trigger, ids, ws.id, base_dir)
        _audit_trigger(trigger, result, ws, base_dir, task_ids=ids)
    except Exception as e:
        print(f"[scheduler] background standalone trigger error: {e}", file=sys.stderr)


def _lock_invoke_unlock(trigger, task_ids: list, ws, base_dir: str, background: bool = False) -> dict:
    """Lock all task_ids, invoke the trigger, then unlock all.

    This is the single code path for all trigger execution. Every agent
    invocation goes through here so locking is always handled by the
    scheduler, never by the agent or trigger execution layer.

    Args:
        trigger: The Trigger to execute.
        task_ids: List of task IDs (may be empty for standalone triggers).
        ws: The Workstream context.
        base_dir: Workspace root.
        background: When True, lock acquisition is synchronous but execution
            and unlock are dispatched to a daemon thread. The caller must NOT
            call _audit_trigger for a "dispatched" result — it runs in the thread.

    Returns:
        Result dict from execute_trigger, or {"status": "dispatched"} if background=True.
    """
    from .locks import acquire_lock, release_lock
    from .workstreams import read_workstream

    # Re-check paused state at execution time so direct calls and races with
    # pause/unpause cannot dispatch work for paused workstreams.
    latest_ws = read_workstream(ws.id, base_dir=base_dir)
    if latest_ws.paused:
        return {
            "trigger_id": trigger.id,
            "status": "skipped",
            "message": f"Workstream '{latest_ws.name}' is paused",
        }

    agent_id = trigger.agent or f"trigger:{trigger.id}"

    # Enforce workstream-level concurrency only for run_agent triggers.
    if trigger.action == "run_agent" and trigger.agent:
        from .agents import count_active_agent_runs
        current_runs = count_active_agent_runs(ws.id, trigger.agent, base_dir=base_dir)
        max_runs = _workstream_agent_limit(ws, trigger.agent, base_dir=base_dir)
        if current_runs >= max_runs:
            from .workspace_audit import log_event as _log_event
            _log_event(
                "trigger_skipped",
                f"Trigger '{trigger.action}' skipped — "
                f"{current_runs}/{max_runs} active agent runs for '{trigger.agent}'",
                base_dir, trigger_id=trigger.id, workstream_id=ws.id,
                status="skipped",
            )
            return {
                "trigger_id": trigger.id,
                "status": "skipped",
                "message": f"agent_concurrency ({max_runs}) reached for '{trigger.agent}'",
            }

    # Lock all tasks
    locked_ids = []
    try:
        for tid in task_ids:
            try:
                # Lock TTL matches the agent execution timeout
                # Priority: trigger.timeout > agent x-timeout > DEFAULT_AGENT_TIMEOUT
                from .agents import DEFAULT_AGENT_TIMEOUT, _resolve_agent_file, _parse_agent_md
                lock_ttl = trigger.timeout
                if not lock_ttl and trigger.agent:
                    try:
                        afile = _resolve_agent_file(trigger.agent, base_dir)
                        adef = _parse_agent_md(afile)
                        lock_ttl = adef.get("timeout")
                    except FileNotFoundError:
                        pass
                lock_ttl = lock_ttl or DEFAULT_AGENT_TIMEOUT
                acquire_lock(tid, agent_id=agent_id, ttl_seconds=lock_ttl, base_dir=base_dir)
                locked_ids.append(tid)
            except (RuntimeError, FileNotFoundError):
                # Task already locked or not found — skip it
                continue

        if background and locked_ids:
            # Transfer lock ownership to the background thread.
            # Copy IDs for the thread, then clear so the finally block does NOT
            # release — the thread is now responsible for unlock.
            thread_ids = locked_ids[:]
            locked_ids.clear()
            t = threading.Thread(
                target=_run_and_unlock,
                args=(trigger, thread_ids, ws, base_dir, agent_id),
                daemon=True,
            )
            t.start()
            return {"trigger_id": trigger.id, "status": "dispatched"}

        # Synchronous path: invoke then fall through to finally for unlock
        result = execute_trigger(trigger, locked_ids, ws.id, base_dir)
        return result
    finally:
        # Unlock tasks we still own (empty when background dispatched above)
        for tid in locked_ids:
            try:
                release_lock(tid, agent_id=agent_id, base_dir=base_dir)
            except Exception:
                pass  # Lock may have been released by agent or expired


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
                    result = _lock_invoke_unlock(
                        temp_trigger, [task.id], ws, base_dir
                    )
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
                # No filter — fire once with no tasks (standalone) in background
                t = threading.Thread(
                    target=_run_without_lock,
                    args=(trigger, ws, base_dir, []),
                    daemon=True,
                )
                t.start()
                result = {"trigger_id": trigger.id, "status": "dispatched"}
                results["trigger_schedules_fired"].append({
                    "trigger_id": trigger.id,
                    "result": result,
                })
            else:
                # Find all tasks matching the filter, lock them all, invoke once
                matching_ids = [
                    t.id for t in tasks
                    if _task_matches_filter(t, trigger.filter)
                ]
                if not matching_ids:
                    continue
                result = _lock_invoke_unlock(
                    trigger,
                    matching_ids,
                    ws,
                    base_dir,
                    background=True,
                )
                if result.get("status") not in ("dispatched",):
                    _audit_trigger(trigger, result, ws, base_dir, task_ids=matching_ids)
                results["trigger_schedules_fired"].append({
                    "trigger_id": trigger.id,
                    "task_ids": matching_ids,
                    "result": result,
                })

        # 3. State-based triggers
        from .locks import lock_status
        for trigger in ws.triggers:
            if trigger.on_state is None:
                continue

            # Find tasks in the trigger's target state that aren't already locked
            matching_ids = [
                t.id for t in tasks
                if t.status == trigger.on_state and lock_status(t.id, base_dir) is None
            ]
            if not matching_ids:
                continue

            # For agent triggers, respect workstream-level per-agent
            # concurrency. Command triggers are unconstrained here.
            if trigger.action == "run_agent" and trigger.agent:
                from .agents import count_active_agent_runs
                current_runs = count_active_agent_runs(ws.id, trigger.agent, base_dir=base_dir)
                max_runs = _workstream_agent_limit(ws, trigger.agent, base_dir=base_dir)
                free_slots = max(0, max_runs - current_runs)
                ids_to_run = matching_ids[:free_slots]
            else:
                ids_to_run = matching_ids

            if not ids_to_run:
                results["state_triggers_fired"].append({
                    "trigger_id": trigger.id,
                    "task_ids": [],
                    "result": {"status": "skipped", "reason": "agent_concurrency reached"},
                })
                continue

            for task_id in ids_to_run:
                # background=True: locks acquired here (synchronous), execution on daemon thread.
                # _audit_trigger is called inside _run_and_unlock for dispatched results.
                result = _lock_invoke_unlock(trigger, [task_id], ws, base_dir, background=True)
                if result.get("status") not in ("dispatched",):
                    # Only audit non-dispatched outcomes (e.g. skipped due to lock contention)
                    _audit_trigger(trigger, result, ws, base_dir, task_ids=[task_id])
                results["state_triggers_fired"].append({
                    "trigger_id": trigger.id,
                    "task_ids": [task_id],
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
