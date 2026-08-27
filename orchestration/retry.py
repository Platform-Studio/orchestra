"""Expired lock cleanup and retry logic.

Handles:
- Detecting and cleaning up expired locks
- Killing orphaned agent processes
- Scheduling retries with backoff
- Moving tasks to 'failed' after max retries
"""

import os
import signal
import time

from datetime import datetime, timezone, timedelta

from .models import RetryConfig, now_iso
from .locks import find_expired_locks, find_orphaned_locks, force_release_lock
from .tasks import read_task, _save_task
from .workstreams import read_workstream

DEFAULT_RETRY_CONFIG = RetryConfig(max_retries=3, backoff="exponential", base_seconds=60)
_FORCE_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


def _is_process_alive(pid: int) -> bool:
    """Check if a process is alive (signal 0 = existence check)."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _is_our_process(pid: int) -> bool:
    """Check if a PID belongs to an agent runtime process we spawned.

    Guards against PID reuse — if the OS reassigned the PID to an
    unrelated process, we must not kill it.
    """
    if pid is None:
        return False
    try:
        import subprocess
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        cmd = result.stdout.strip().lower()
        return "claude" in cmd or "anthropic" in cmd or "cline" in cmd or "copilot" in cmd
    except Exception:
        return False


def _kill_process(pid: int) -> str:
    """Kill a process, trying SIGTERM first then SIGKILL. Returns signal used."""
    if pid is None or not _is_process_alive(pid):
        return None
    if not _is_our_process(pid):
        return None

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            time.sleep(0.5)
            if not _is_process_alive(pid):
                return "SIGTERM"
        # Still alive — force kill
        os.kill(pid, _FORCE_KILL_SIGNAL)
        return "SIGKILL"
    except (OSError, ProcessLookupError):
        return "SIGTERM"


def _get_retry_config(task, workstream) -> RetryConfig:
    """Resolve retry config: task > workstream > default."""
    if task.retry is not None:
        return task.retry
    if workstream.retry is not None:
        return workstream.retry
    return DEFAULT_RETRY_CONFIG


def _compute_backoff(retry_config: RetryConfig, retry_count: int) -> int:
    """Compute backoff delay in seconds."""
    if retry_config.backoff == "exponential":
        return retry_config.base_seconds * (2 ** retry_count)
    elif retry_config.backoff == "linear":
        return retry_config.base_seconds * (retry_count + 1)
    else:  # fixed
        return retry_config.base_seconds


def _find_last_agent(task) -> dict:
    """Find the last agent that ran on this task from audit trail.

    Returns a scheduled_action dict like {"type": "run_agent", "agent": "sorter"}
    or None if no agent run found.
    """
    for entry in reversed(task.audit):
        if entry.type == "agent_started":
            # Extract agent name from description like "Agent 'sorter' started processing"
            desc = entry.description
            if "'" in desc:
                agent_name = desc.split("'")[1]
                return {"type": "run_agent", "agent": agent_name}
    return None


def _state_trigger_can_retry_task(task, workstream, action, base_dir=".") -> bool:
    """Return True when normal state triggers will re-dispatch this retry.

    This keeps expired-lock cleanup from creating redundant one-shot schedules for
    tasks that are already in a triggerable state for the same agent.
    """
    if not isinstance(action, dict) or action.get("type") != "run_agent":
        return False
    if getattr(workstream, "paused", False):
        return False

    task_state = str(getattr(task, "status", "") or "").strip()
    action_agent = str(action.get("agent", "") or "").strip()
    if not task_state or not action_agent:
        return False

    paused_states = {str(state or "").strip() for state in (getattr(workstream, "paused_states", []) or [])}
    if task_state in paused_states:
        return False

    from .agents import _agent_identity_keys

    action_keys = _agent_identity_keys(action_agent, base_dir=base_dir) or {action_agent.lower()}
    for trigger in getattr(workstream, "triggers", []) or []:
        if getattr(trigger, "paused", False):
            continue
        if str(getattr(trigger, "on_state", "") or "").strip() != task_state:
            continue
        if getattr(trigger, "action", None) != "run_agent":
            continue

        trigger_agent = str(getattr(trigger, "agent", "") or "").strip()
        if not trigger_agent:
            continue
        trigger_keys = _agent_identity_keys(trigger_agent, base_dir=base_dir) or {trigger_agent.lower()}
        if action_keys & trigger_keys:
            return True
    return False


def handle_expired_lock(task_id, workstream_id, lock, base_dir="."):
    """Handle a single expired lock: kill process, clean up, decide retry.

    Returns a dict describing what happened.
    """
    from .workspace_audit import log_event

    result = {"task_id": task_id, "workstream_id": workstream_id, "actions": []}

    # Read task (may fail if corrupt)
    try:
        task = read_task(task_id, base_dir)
    except Exception:
        # Can't read task — just remove the lock
        force_release_lock(task_id, base_dir)
        log_event("lock_cleaned",
            f"Removed expired lock for unreadable task {task_id}",
            base_dir, task_id=task_id, workstream_id=workstream_id)
        result["actions"].append("lock_cleaned_unreadable_task")
        return result

    try:
        ws = read_workstream(workstream_id, base_dir)
    except Exception:
        force_release_lock(task_id, base_dir)
        result["actions"].append("lock_cleaned_unreadable_workstream")
        return result

    # Calculate how long the lock was held
    acquired = datetime.fromisoformat(lock.acquired_at)
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=timezone.utc)
    expires = datetime.fromisoformat(lock.expires_at)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    ttl = int((expires - acquired).total_seconds())

    # 1. Log expired lock
    task.add_audit("lock_expired",
        f"Lock held by {lock.agent_id} expired after {ttl}s.")
    log_event("lock_expired",
        f"Lock expired for task '{task.title}' ({lock.agent_id}, {ttl}s TTL)",
        base_dir,
        task_id=task_id, workstream_id=workstream_id,
        agent_id=lock.agent_id, subprocess_pid=lock.subprocess_pid)
    result["actions"].append("lock_expired")

    # 2. Kill processes if alive
    killed_sub = _kill_process(lock.subprocess_pid)
    killed_parent = None
    if lock.pid and lock.pid != lock.subprocess_pid:
        killed_parent = _kill_process(lock.pid)

    if killed_sub or killed_parent:
        parts = []
        if killed_sub:
            parts.append(f"subprocess {lock.subprocess_pid} ({killed_sub})")
        if killed_parent:
            parts.append(f"parent {lock.pid} ({killed_parent})")
        kill_desc = "Killed " + ", ".join(parts)

        task.add_audit("process_killed", kill_desc)
        log_event("process_killed",
            f"Killed process for task '{task.title}': {kill_desc}",
            base_dir,
            task_id=task_id, workstream_id=workstream_id,
            signal=killed_sub or killed_parent)
        result["actions"].append("process_killed")
    else:
        task.add_audit("lock_cleaned",
            f"Lock cleaned up. Processes already dead (pid={lock.pid}, subprocess={lock.subprocess_pid}).")
        log_event("lock_cleaned",
            f"Expired lock cleaned for task '{task.title}' (processes already dead)",
            base_dir,
            task_id=task_id, workstream_id=workstream_id)
        result["actions"].append("lock_cleaned")

    # 3. Remove lock file
    force_release_lock(task_id, base_dir)

    # 4. Decide retry
    retry_config = _get_retry_config(task, ws)
    task.retry_count += 1
    task.last_failure_at = now_iso()

    if task.retry_count < retry_config.max_retries:
        # Find what agent to re-run
        action = _find_last_agent(task)

        # Transition back to pending if currently in_progress
        if task.status == "in_progress" and ws.validate_transition(task.status, "pending"):
            task.status = "pending"
            task.add_audit("status_change", "Status changed from 'in_progress' to 'pending'")
        elif task.status == "in_progress":
            # If pending isn't a valid transition, find the initial state
            initial = ws.initial_status()
            if ws.validate_transition(task.status, initial):
                task.status = initial
                task.add_audit("status_change", f"Status changed from 'in_progress' to '{initial}'")

        if _state_trigger_can_retry_task(task, ws, action, base_dir=base_dir):
            task.scheduled_at = None
            task.scheduled_action = None
            task.add_audit(
                "retry_available",
                f"Retry {task.retry_count}/{retry_config.max_retries} left to normal state triggers.",
            )
            log_event(
                "retry_available",
                f"Retry {task.retry_count}/{retry_config.max_retries} for task '{task.title}' left to state triggers",
                base_dir,
                task_id=task_id,
                workstream_id=workstream_id,
                retry_count=task.retry_count,
                max_retries=retry_config.max_retries,
            )
            result["actions"].append("retry_available")
        else:
            # Schedule retry with backoff when no matching state trigger will pick it up.
            backoff = _compute_backoff(retry_config, task.retry_count)
            retry_at = datetime.now(timezone.utc) + timedelta(seconds=backoff)

            task.scheduled_at = retry_at.isoformat()
            task.scheduled_action = action
            task.add_audit("retry_scheduled",
                f"Retry {task.retry_count}/{retry_config.max_retries} "
                f"scheduled for {retry_at.isoformat()} (backoff: {backoff}s)")
            log_event("retry_scheduled",
                f"Retry {task.retry_count}/{retry_config.max_retries} for task '{task.title}' in {backoff}s",
                base_dir,
                task_id=task_id, workstream_id=workstream_id,
                retry_count=task.retry_count, max_retries=retry_config.max_retries,
                next_run_at=retry_at.isoformat())
            result["actions"].append("retry_scheduled")
            result["next_run_at"] = retry_at.isoformat()

        result["retry_count"] = task.retry_count
    else:
        # Max retries exceeded — move to failed
        if ws.validate_transition(task.status, "failed"):
            task.status = "failed"
            task.add_audit("max_retries_exceeded",
                f"Failed after {retry_config.max_retries} retries. Manual intervention required.")
            task.add_audit("status_change",
                f"Status changed from 'in_progress' to 'failed'")
        else:
            task.add_audit("max_retries_exceeded",
                f"Failed after {retry_config.max_retries} retries. "
                f"Cannot transition to 'failed' — manual intervention required.")

        log_event("max_retries_exceeded",
            f"Task '{task.title}' failed after {retry_config.max_retries} retries",
            base_dir,
            task_id=task_id, workstream_id=workstream_id,
            retry_count=task.retry_count)
        result["actions"].append("max_retries_exceeded")

    # 5. Save task
    _save_task(task, base_dir)

    return result


def cleanup_expired_locks(base_dir: str = ".") -> list:
    """Scan all workstreams for expired locks and handle each one.

    Called by the scheduler tick. Returns list of results from handle_expired_lock.
    """
    expired = find_expired_locks(base_dir)
    results = []
    for entry in expired:
        try:
            r = handle_expired_lock(
                entry["task_id"],
                entry["workstream_id"],
                entry["lock"],
                base_dir,
            )
            results.append(r)
        except Exception as e:
            # Don't let one bad lock break the whole cleanup
            results.append({
                "task_id": entry["task_id"],
                "workstream_id": entry["workstream_id"],
                "error": str(e),
            })
    return results


def cleanup_orphaned_locks(base_dir: str = ".") -> list:
    """Release non-expired locks whose owner pid no longer exists.

    This is a fast safety sweep run every scheduler tick to recover from
    interrupted runs (crash/restart) without waiting for TTL expiry.
    """
    from .workspace_audit import log_event

    orphaned = find_orphaned_locks(base_dir)
    results = []

    for entry in orphaned:
        task_id = entry["task_id"]
        workstream_id = entry["workstream_id"]
        lock = entry["lock"]

        released = force_release_lock(task_id, base_dir)
        if released:
            try:
                task = read_task(task_id, base_dir)
                task.add_audit(
                    "lock_orphaned",
                    f"Released orphaned lock from {lock.agent_id} (pid={lock.pid}).",
                )
                _save_task(task, base_dir)
            except Exception:
                pass

            log_event(
                "lock_orphaned",
                f"Released orphaned lock for task {task_id} (agent={lock.agent_id}, pid={lock.pid})",
                base_dir,
                task_id=task_id,
                workstream_id=workstream_id,
                agent_id=lock.agent_id,
                pid=lock.pid,
            )

        results.append(
            {
                "task_id": task_id,
                "workstream_id": workstream_id,
                "released": bool(released),
                "agent_id": lock.agent_id,
                "pid": lock.pid,
            }
        )

    return results


def manual_retry(task_id: str, base_dir: str = ".") -> dict:
    """Manually retry a failed task — resets retry_count and transitions to pending.

    Called from the workstream manager UI "Retry" button.
    """
    from .workspace_audit import log_event

    task = read_task(task_id, base_dir)
    ws = read_workstream(task.workstream_id, base_dir)

    if task.status != "failed":
        raise ValueError(f"Task is in '{task.status}' state, not 'failed'")

    if not ws.validate_transition("failed", "pending"):
        initial = ws.initial_status()
        if not ws.validate_transition("failed", initial):
            raise ValueError(f"No valid transition from 'failed' to restart task")
        target = initial
    else:
        target = "pending"

    task.retry_count = 0
    task.last_failure_at = None
    task.status = target
    task.add_audit("manual_retry", f"Manual retry — status reset to '{target}', retry count cleared")
    task.add_audit("status_change", f"Status changed from 'failed' to '{target}'")
    _save_task(task, base_dir)

    log_event("manual_retry",
        f"Manual retry for task '{task.title}'",
        base_dir,
        task_id=task_id, workstream_id=task.workstream_id)

    return {"task_id": task_id, "status": target, "retry_count": 0}
