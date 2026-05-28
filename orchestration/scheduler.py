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
import json
import yaml
import threading
import copy

from datetime import datetime, timezone

from .models import now_iso
from .persistence import resolve_workstream_root
from .workstreams import list_workstreams
from .tasks import list_tasks, list_tasks_for_workstream, read_task, _save_task
from .triggers import execute_trigger, trigger_pause_reason


SCHEDULER_STATE_FILE = "scheduler_state.yaml"
TICK_INTERVAL = 60  # seconds


def _state_path(base_dir: str) -> str:
    return os.path.join(resolve_workstream_root(base_dir), SCHEDULER_STATE_FILE)


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


def _trigger_state_path(ws_id: str, trigger_id: str, base_dir: str) -> str:
    from .workstreams import resolve_workstream_state_root

    state_root = resolve_workstream_state_root(ws_id, base_dir=base_dir)
    return os.path.join(state_root, ".orchestration", "triggers", f"{trigger_id}.yaml")


def _load_trigger_state(ws_id: str, trigger_id: str, base_dir: str) -> dict:
    path = _trigger_state_path(ws_id, trigger_id, base_dir)
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save_trigger_state(ws_id: str, trigger_id: str, trigger_state: dict, base_dir: str) -> None:
    path = _trigger_state_path(ws_id, trigger_id, base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(trigger_state, f, default_flow_style=False, sort_keys=False)


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
    stop_reason = "loop_exit"

    def handle_signal(sig, frame):
        nonlocal running, stop_reason
        running = False
        if sig == signal.SIGTERM:
            stop_reason = "signal:SIGTERM"
        elif sig == signal.SIGINT:
            stop_reason = "signal:SIGINT"
        else:
            stop_reason = f"signal:{sig}"

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
        log_event(
            "scheduler_stopped",
            f"Scheduler process stopped ({stop_reason})",
            abs_dir,
            reason=stop_reason,
            pid=os.getpid(),
        )
        print("Scheduler stopped.")


def stop(base_dir: str = ".") -> dict:
    """Stop a running scheduler process by sending SIGTERM to its PID."""
    from .workspace_audit import log_event

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

    log_event(
        "scheduler_stop_requested",
        f"Scheduler stop requested for pid={pid}",
        os.path.abspath(base_dir),
        pid=pid,
    )
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


def _group_tasks_by_status(tasks: list) -> dict[str, list]:
    """Group tasks by status while preserving their existing order."""
    grouped = {}
    for task in tasks:
        grouped.setdefault(task.status, []).append(task)
    return grouped


def _matching_task_ids(tasks: list, filter_def: dict, tasks_by_status: dict[str, list] | None = None) -> list[str]:
    """Return matching task IDs, using status prefiltering when available."""
    if not filter_def:
        return [task.id for task in tasks]

    candidates = tasks
    state_filter = filter_def.get("state") or filter_def.get("status")
    if state_filter is not None and tasks_by_status is not None:
        candidates = tasks_by_status.get(state_filter, [])

    return [task.id for task in candidates if _task_matches_filter(task, filter_def)]


def _ordered_state_triggers(ws) -> list:
    """Return state-based triggers ordered by workflow progression.

    Later workflow states are evaluated before earlier ones so shared-agent
    concurrency favors advancing active work over repeatedly pulling new work
    from earlier states.
    """
    state_positions = {
        state: index for index, state in enumerate(getattr(ws, "task_states", {}).keys())
    }
    ordered = []
    for original_index, trigger in enumerate(getattr(ws, "triggers", [])):
        if trigger.on_state is None:
            continue
        position = state_positions.get(trigger.on_state)
        if position is None:
            sort_key = (1, original_index)
        else:
            sort_key = (0, -position, original_index)
        ordered.append((sort_key, trigger))
    ordered.sort(key=lambda item: item[0])
    return [trigger for _, trigger in ordered]


def _select_state_trigger_task_ids(trigger, eligible_task_ids: list) -> list:
    if getattr(trigger, "task_selection", None) == "all_unlocked":
        return list(eligible_task_ids)
    return list(eligible_task_ids[:1])


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


def _resolve_agent_concurrency_bucket(ws, agent_ref: str, task_state: str = None, base_dir: str = ".") -> tuple[tuple, int, str | None]:
    """Resolve the bucket key and limit for an agent concurrency check."""
    policy = getattr(ws, "agent_concurrency", None)
    if not isinstance(policy, dict):
        policy = {}

    from .agents import _agent_identity_keys

    identity_keys = _agent_identity_keys(agent_ref, base_dir=base_dir) if agent_ref else set()

    def _resolve_limit(default_limit, overrides) -> int:
        try:
            resolved_default = int(default_limit or 1)
        except (TypeError, ValueError):
            resolved_default = 1
        resolved_default = max(1, resolved_default)

        if not identity_keys or not isinstance(overrides, dict):
            return resolved_default

        for key, value in overrides.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key and normalized_key in identity_keys:
                try:
                    return max(1, int(value))
                except (TypeError, ValueError):
                    return resolved_default
        return resolved_default

    if task_state:
        raw_state_overrides = policy.get("state_overrides", {})
        if isinstance(raw_state_overrides, dict):
            target_state = str(task_state or "").strip().lower()
            for state_name, state_policy in raw_state_overrides.items():
                if str(state_name or "").strip().lower() != target_state:
                    continue
                if isinstance(state_policy, dict):
                    state_bucket = str(state_name or task_state).strip()
                    return (
                        (ws.id, str(agent_ref or "").strip().lower(), target_state),
                        _resolve_limit(state_policy.get("default", 1), state_policy.get("overrides", {})),
                        state_bucket,
                    )
                break

    return (
        (ws.id, str(agent_ref or "").strip().lower()),
        _resolve_limit(policy.get("default", 1), policy.get("overrides", {})),
        None,
    )


def _run_and_unlock(trigger, locked_ids: list, ws, base_dir: str, agent_id: str, ignore_paused: bool = False) -> None:
    """Execute a trigger and release locks on a background daemon thread.

    Lock acquisition is handled by the caller on the main scheduler thread so
    that concurrency checks in subsequent ticks see the lock immediately.
    This function owns only execution and cleanup.
    """
    try:
        result = execute_trigger(trigger, locked_ids, ws.id, base_dir, ignore_paused=ignore_paused)
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


def _run_without_lock(trigger, ws, base_dir: str, task_ids: list = None, ignore_paused: bool = False) -> None:
    """Execute a trigger with no lock lifecycle on a background daemon thread."""
    ids = task_ids or []
    try:
        result = execute_trigger(trigger, ids, ws.id, base_dir, ignore_paused=ignore_paused)
        _audit_trigger(trigger, result, ws, base_dir, task_ids=ids)
    except Exception as e:
        print(f"[scheduler] background standalone trigger error: {e}", file=sys.stderr)


def _email_event_context(event: dict) -> dict:
    attachments = list(event.get("attachments") or [])
    return {
        "email_from": str(event.get("from") or ""),
        "email_to": str(event.get("to") or ""),
        "email_subject": str(event.get("subject") or ""),
        "email_date": str(event.get("date") or ""),
        "email_body": str(event.get("body") or ""),
        "email_storage_key": str(event.get("storage_key") or ""),
        "email_attachment_count": len(attachments),
        "email_attachments_json": json.dumps(attachments, sort_keys=True),
    }


def _email_event_prompt(event: dict) -> str:
    context = _email_event_context(event)
    attachments = list(event.get("attachments") or [])
    lines = [
        "Inbound email event:",
        f"From: {context['email_from']}",
        f"To: {context['email_to']}",
        f"Subject: {context['email_subject']}",
    ]
    if context["email_date"]:
        lines.append(f"Date: {context['email_date']}")
    if context["email_storage_key"]:
        lines.append(f"Storage Key: {context['email_storage_key']}")
    if attachments:
        lines.append(f"Attachments ({len(attachments)}):")
        for attachment in attachments:
            name = attachment.get("name") or "(unnamed attachment)"
            content_type = attachment.get("content_type") or "unknown"
            size = attachment.get("size")
            size_text = f", {size} bytes" if size not in (None, "") else ""
            lines.append(f"- {name} ({content_type}{size_text})")
    if context["email_body"]:
        lines.extend(["", context["email_body"]])
    return "\n".join(lines)


def _dispatch_email_trigger(trigger, event: dict, ws, base_dir: str) -> dict:
    trigger_copy = copy.copy(trigger)
    trigger_copy.event_context = _email_event_context(event)
    if trigger_copy.action == "run_agent":
        injected_prompt = _email_event_prompt(event)
        if trigger_copy.prompt:
            trigger_copy.prompt = f"{trigger_copy.prompt}\n\n{injected_prompt}"
        else:
            trigger_copy.prompt = injected_prompt

    thread = threading.Thread(
        target=_run_without_lock,
        args=(trigger_copy, ws, base_dir, []),
        daemon=True,
    )
    thread.start()
    return {"trigger_id": trigger.id, "status": "dispatched"}


def _poll_email_trigger_events(trigger, ws, base_dir: str) -> tuple[list[dict], dict]:
    from .email_inbox import list_new_thread_messages

    email_config = getattr(trigger, "on_email", None) or {}
    recipient = str(email_config.get("recipient") or "").strip().lower()
    event_name = str(email_config.get("event") or "new_thread").strip().lower()
    if not recipient or event_name != "new_thread":
        return [], _load_trigger_state(ws.id, trigger.id, base_dir)

    trigger_state = _load_trigger_state(ws.id, trigger.id, base_dir)
    processed_keys = list(trigger_state.get("processed_keys") or [])
    processed_set = set(processed_keys)
    new_events = []
    for event in list_new_thread_messages(recipient):
        storage_key = str(event.get("storage_key") or "").strip()
        if not storage_key or storage_key in processed_set:
            continue
        new_events.append(event)
    return new_events, trigger_state


def _lock_invoke_unlock(trigger, task_ids: list, ws, base_dir: str, background: bool = False, ignore_paused: bool = False) -> dict:
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
        ignore_paused: When True, bypass the paused-workstream guard for an
            explicit manual force-run path.

    Returns:
        Result dict from execute_trigger, or {"status": "dispatched"} if background=True.
    """
    from .locks import acquire_lock, release_lock
    from .workstreams import read_workstream

    # Re-check paused state at execution time so direct calls and races with
    # pause/unpause cannot dispatch work for paused workstreams.
    latest_ws = read_workstream(ws.id, base_dir=base_dir)
    if latest_ws.paused and not ignore_paused:
        return {
            "trigger_id": trigger.id,
            "status": "skipped",
            "message": f"Workstream '{latest_ws.name}' is paused",
        }
    pause_reason = trigger_pause_reason(trigger, latest_ws)
    if pause_reason:
        return {
            "trigger_id": trigger.id,
            "status": "skipped",
            "message": pause_reason,
        }

    agent_id = trigger.agent or f"trigger:{trigger.id}"

    # Enforce workstream-level concurrency, with optional state-scoped buckets,
    # only for run_agent triggers.
    if trigger.action == "run_agent" and trigger.agent:
        from .agents import count_active_agent_runs
        _, max_runs, concurrency_state = _resolve_agent_concurrency_bucket(
            ws,
            trigger.agent,
            task_state=trigger.on_state,
            base_dir=base_dir,
        )
        current_runs = count_active_agent_runs(
            ws.id,
            trigger.agent,
            base_dir=base_dir,
            concurrency_state=concurrency_state,
        )
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
            # Copy IDs for the thread. We only clear local ownership after the
            # thread starts successfully; otherwise finally will release locks.
            thread_ids = locked_ids[:]
            t = threading.Thread(
                target=_run_and_unlock,
                args=(trigger, thread_ids, ws, base_dir, agent_id, ignore_paused),
                daemon=True,
            )
            try:
                t.start()
            except Exception as e:
                return {
                    "trigger_id": trigger.id,
                    "status": "error",
                    "message": f"failed to start trigger thread: {e}",
                }
            locked_ids.clear()
            return {"trigger_id": trigger.id, "status": "dispatched"}

        # Synchronous path: invoke then fall through to finally for unlock
        result = execute_trigger(trigger, locked_ids, ws.id, base_dir, ignore_paused=ignore_paused)
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

    1. Clean up orphaned and expired locks
    2. Load all non-paused workstreams
    3. Fire task-level schedules that have passed
    4. Evaluate schedule-based triggers
    5. Update last_tick_at
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

    results = {
        "orphaned_locks_cleaned": [],
        "expired_locks_cleaned": [],
        "task_schedules_fired": [],
        "trigger_schedules_fired": [],
        "email_triggers_fired": [],
        "state_triggers_fired": [],
    }

    from .retry import cleanup_expired_locks, cleanup_orphaned_locks
    results["orphaned_locks_cleaned"] = cleanup_orphaned_locks(base_dir)
    results["expired_locks_cleaned"] = cleanup_expired_locks(base_dir)

    # Tracks optimistic slots consumed in this tick before agent runs are
    # registered in active_agents.yaml (avoids same-tick over-dispatch races).
    pending_agent_slots = {}

    workstreams = list_workstreams(base_dir)
    for ws in workstreams:
        if ws.paused:
            continue

        tasks = list_tasks_for_workstream(ws, base_dir=base_dir)
        tasks_by_status = _group_tasks_by_status(tasks)
        paused_states = set(getattr(ws, "paused_states", []) or [])

        # 1. Task-level schedules
        for task in tasks:
            if task.scheduled_at is None:
                continue
            if task.status in paused_states:
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
                        prompt=action.get("prompt"),
                        timeout=action.get("timeout"),
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
            pause_reason = trigger_pause_reason(trigger, ws)
            if pause_reason:
                results["trigger_schedules_fired"].append({
                    "trigger_id": trigger.id,
                    "result": {"status": "skipped", "reason": pause_reason},
                })
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
                matching_ids = _matching_task_ids(tasks, trigger.filter, tasks_by_status)
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

        # 2b. Email-based triggers
        for trigger in ws.triggers:
            if getattr(trigger, "on_email", None) is None:
                continue

            email_events, trigger_state = _poll_email_trigger_events(trigger, ws, base_dir)
            processed_keys = list(trigger_state.get("processed_keys") or [])
            processed_changed = False

            for event in email_events:
                result = _dispatch_email_trigger(trigger, event, ws, base_dir)
                results["email_triggers_fired"].append({
                    "trigger_id": trigger.id,
                    "storage_key": event.get("storage_key"),
                    "subject": event.get("subject"),
                    "result": result,
                })

                storage_key = str(event.get("storage_key") or "").strip()
                if storage_key:
                    processed_keys.append(storage_key)
                    processed_changed = True

            if processed_changed:
                trigger_state["processed_keys"] = processed_keys[-200:]
                _save_trigger_state(ws.id, trigger.id, trigger_state, base_dir)

        # 3. State-based triggers
        from .locks import list_workstream_locks_for_workstream

        active_locks = set()
        state_triggers = _ordered_state_triggers(ws)
        if state_triggers:
            active_locks = set(
                list_workstream_locks_for_workstream(
                    ws,
                    task_ids=[task.id for task in tasks],
                    base_dir=base_dir,
                ).keys()
            )

        for trigger in state_triggers:
            pause_reason = trigger_pause_reason(trigger, ws)
            if pause_reason:
                results["state_triggers_fired"].append({
                    "trigger_id": trigger.id,
                    "task_ids": [],
                    "result": {"status": "skipped", "reason": pause_reason},
                })
                continue

            # Find tasks in the trigger's target state that aren't already locked
            matching_ids = [
                task.id for task in tasks_by_status.get(trigger.on_state, [])
                if task.id not in active_locks
            ]
            if not matching_ids:
                continue

            # For agent triggers, respect workstream-level per-agent
            # concurrency, with optional state-scoped buckets.
            # Command triggers are unconstrained here.
            if trigger.action == "run_agent" and trigger.agent:
                from .agents import count_active_agent_runs
                slot_key, max_runs, concurrency_state = _resolve_agent_concurrency_bucket(
                    ws,
                    trigger.agent,
                    task_state=trigger.on_state,
                    base_dir=base_dir,
                )
                current_runs = count_active_agent_runs(
                    ws.id,
                    trigger.agent,
                    base_dir=base_dir,
                    concurrency_state=concurrency_state,
                )
                pending_slots = pending_agent_slots.get(slot_key, 0)
                free_slots = max(0, max_runs - current_runs - pending_slots)
            else:
                free_slots = 1

            if free_slots <= 0:
                results["state_triggers_fired"].append({
                    "trigger_id": trigger.id,
                    "task_ids": [],
                    "result": {"status": "skipped", "reason": "agent_concurrency reached"},
                })
                continue

            selected_task_ids = _select_state_trigger_task_ids(trigger, matching_ids)
            # background=True: locks acquired here (synchronous), execution on daemon thread.
            # _audit_trigger is called inside _run_and_unlock for dispatched results.
            result = _lock_invoke_unlock(trigger, selected_task_ids, ws, base_dir, background=True)
            if result.get("status") not in ("dispatched",):
                # Only audit non-dispatched outcomes (e.g. skipped due to lock contention)
                _audit_trigger(trigger, result, ws, base_dir, task_ids=selected_task_ids)
            elif trigger.action == "run_agent" and trigger.agent:
                active_locks.update(selected_task_ids)
                slot_key, _, _ = _resolve_agent_concurrency_bucket(
                    ws,
                    trigger.agent,
                    task_state=trigger.on_state,
                    base_dir=base_dir,
                )
                pending_agent_slots[slot_key] = pending_agent_slots.get(slot_key, 0) + 1
            elif result.get("status") == "dispatched":
                active_locks.update(selected_task_ids)
            results["state_triggers_fired"].append({
                "trigger_id": trigger.id,
                "task_ids": selected_task_ids,
                "result": result,
            })

    # Update state
    state["last_tick_at"] = now.isoformat()
    _save_state(state, base_dir)

    results["last_tick_at"] = now.isoformat()
    return results
