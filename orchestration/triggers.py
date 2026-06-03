"""Trigger operations."""

import os
import subprocess
import sys
import threading
import copy

from .models import Trigger, Workstream, new_id
from .workstreams import read_workstream, save_workstream
from .workspace_audit import log_event

# Thread-safe set of currently-executing trigger IDs
_active_triggers_lock = threading.Lock()
_active_triggers: set = set()


def get_active_triggers() -> list:
    """Return list of trigger IDs currently executing."""
    with _active_triggers_lock:
        return list(_active_triggers)


def _trigger_filter_state(trigger: Trigger) -> str | None:
    filter_def = getattr(trigger, "filter", None)
    if not isinstance(filter_def, dict):
        return None
    state = filter_def.get("state") or filter_def.get("status")
    if state is None:
        return None
    return str(state)


def trigger_column_state(trigger: Trigger) -> str | None:
    """Return the board column/state this trigger is scoped to, if any."""
    if getattr(trigger, "on_state", None) is not None:
        return str(trigger.on_state)
    return _trigger_filter_state(trigger)


def _current_trigger_for_workstream(trigger: Trigger, ws: Workstream) -> Trigger:
    for candidate in getattr(ws, "triggers", []) or []:
        if candidate.id == trigger.id:
            return candidate
    return trigger


def trigger_pause_reason(trigger: Trigger, ws: Workstream) -> str | None:
    """Return a human-readable reason this trigger should not run."""
    current = _current_trigger_for_workstream(trigger, ws)
    if getattr(current, "paused", False):
        return f"Trigger '{current.id}' is paused"

    state = trigger_column_state(current)
    paused_states = set(getattr(ws, "paused_states", []) or [])
    if state in paused_states:
        return f"Column '{state}' is paused"
    return None


def execute_trigger(
    trigger: Trigger,
    task_ids: list,
    workstream_id: str,
    base_dir: str = ".",
    ignore_paused: bool = False,
) -> dict:
    """Execute a trigger against 0-N tasks.

    Callers are responsible for locking/unlocking task_ids. This function
    does not acquire or release locks.

    Args:
        trigger: The trigger to execute.
        task_ids: List of task IDs to process. May be empty for standalone triggers.
        workstream_id: The workstream context.
        base_dir: Workspace root.
    """
    ws = read_workstream(workstream_id, base_dir)
    if ws.paused and not ignore_paused:
        return {
            "trigger_id": trigger.id,
            "status": "skipped",
            "message": f"Workstream '{ws.name}' is paused",
        }

    current_trigger = _current_trigger_for_workstream(trigger, ws)
    pause_reason = trigger_pause_reason(current_trigger, ws)
    if pause_reason:
        return {
            "trigger_id": current_trigger.id,
            "status": "skipped",
            "message": pause_reason,
        }

    with _active_triggers_lock:
        _active_triggers.add(current_trigger.id)
    try:
        return _execute_trigger_inner(
            current_trigger,
            task_ids,
            workstream_id,
            base_dir,
            ignore_paused=ignore_paused,
        )
    finally:
        with _active_triggers_lock:
            _active_triggers.discard(current_trigger.id)


def _execute_trigger_inner(
    trigger: Trigger,
    task_ids: list,
    workstream_id: str,
    base_dir: str = ".",
    ignore_paused: bool = False,
) -> dict:
    if trigger.action == "run_agent":
        if trigger.agent is None:
            return {"trigger_id": trigger.id, "status": "error", "message": "No agent specified"}
        try:
            from .agents import run_agent
            result = run_agent(
                trigger.agent,
                task_ids=task_ids,
                workstream_id=workstream_id,
                prompt=trigger.prompt,
                timeout=trigger.timeout,
                base_dir=base_dir,
                allow_paused_workstream=ignore_paused,
                concurrency_state=trigger.on_state,
            )
            return {"trigger_id": trigger.id, "status": "ok", "result": result}
        except Exception as e:
            return {"trigger_id": trigger.id, "status": "error", "message": str(e)}

    elif trigger.action == "run_command":
        if trigger.command is None:
            return {"trigger_id": trigger.id, "status": "error", "message": "No command specified"}
        # Template variable substitution — join task IDs for command templates
        task_id_str = task_ids[0] if len(task_ids) == 1 else ",".join(task_ids)
        cmd = trigger.command.replace("{task_id}", task_id_str).replace("{workstream_id}", workstream_id)
        for key, value in (getattr(trigger, "event_context", None) or {}).items():
            cmd = cmd.replace("{" + str(key) + "}", str(value or ""))

        try:
            # Ensure the same Python that runs the scheduler is available to subprocesses
            env = os.environ.copy()
            python_dir = os.path.dirname(sys.executable)
            env["PATH"] = python_dir + os.pathsep + env.get("PATH", "")
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=300, cwd=base_dir,
                env=env,
            )
            return {
                "trigger_id": trigger.id,
                "status": "ok" if result.returncode == 0 else "error",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "returncode": result.returncode,
            }
        except Exception as e:
            return {"trigger_id": trigger.id, "status": "error", "message": str(e)}

    return {"trigger_id": trigger.id, "status": "error", "message": f"Unknown action: {trigger.action}"}


def list_triggers(workstream_id: str, base_dir: str = ".") -> list:
    ws = read_workstream(workstream_id, base_dir)
    return ws.triggers


def create_trigger(
    workstream_id: str,
    action: str,
    on_state: str = None,
    on_schedule: str = None,
    on_email: dict = None,
    task_selection: str = None,
    filter: dict = None,
    agent: str = None,
    command: str = None,
    prompt: str = None,
    timeout: int = None,
    base_dir: str = ".",
) -> Trigger:
    trigger_conditions = [
        on_state is not None,
        on_schedule is not None,
        on_email is not None,
    ]
    if sum(1 for item in trigger_conditions if item) != 1:
        raise ValueError("Exactly one trigger condition must be specified")
    if task_selection is not None and task_selection not in ("first_unlocked", "all_unlocked"):
        raise ValueError("--task-selection must be either 'first_unlocked' or 'all_unlocked'")
    if task_selection is not None and on_state is None:
        raise ValueError("--task-selection is only supported for --on-state triggers")
    if on_email is not None:
        if not isinstance(on_email, dict):
            raise ValueError("--on-email must be an object")
        recipient = str(on_email.get("recipient") or "").strip().lower()
        event = str(on_email.get("event") or "new_thread").strip().lower()
        if not recipient:
            raise ValueError("Email triggers require a recipient")
        if event != "new_thread":
            raise ValueError("Email trigger event must be 'new_thread'")
        on_email = {"recipient": recipient, "event": event}

    ws = read_workstream(workstream_id, base_dir)
    trigger = Trigger(
        id=new_id(),
        action=action,
        on_state=on_state,
        on_schedule=on_schedule,
        on_email=on_email,
        task_selection=task_selection,
        filter=filter,
        agent=agent,
        command=command,
        prompt=prompt,
        timeout=timeout,
    )
    ws.triggers.append(trigger)
    save_workstream(ws, base_dir)

    return trigger


def delete_trigger(trigger_id: str, base_dir: str = ".") -> bool:
    """Delete a trigger by scanning all workstreams."""
    from .workstreams import list_workstreams
    for ws in list_workstreams(base_dir):
        original_len = len(ws.triggers)
        ws.triggers = [t for t in ws.triggers if t.id != trigger_id]
        if len(ws.triggers) < original_len:
            save_workstream(ws, base_dir)
            return True
    raise FileNotFoundError(f"Trigger {trigger_id} not found")


def set_trigger_paused(trigger_id: str, paused: bool, base_dir: str = ".") -> Trigger:
    """Pause or resume a trigger by scanning all workstreams."""
    from .workstreams import list_workstreams
    for ws in list_workstreams(base_dir):
        for trigger in ws.triggers:
            if trigger.id != trigger_id:
                continue
            if trigger.paused == paused:
                return trigger
            trigger.paused = paused
            save_workstream(ws, base_dir)
            log_event(
                "trigger_paused" if paused else "trigger_resumed",
                f"Trigger '{trigger.id}' {'paused' if paused else 'resumed'}",
                base_dir,
                trigger_id=trigger.id,
                workstream_id=ws.id,
                state=trigger_column_state(trigger),
            )
            return trigger
    raise FileNotFoundError(f"Trigger {trigger_id} not found")


def pause_trigger(trigger_id: str, base_dir: str = ".") -> Trigger:
    return set_trigger_paused(trigger_id, True, base_dir=base_dir)


def resume_trigger(trigger_id: str, base_dir: str = ".") -> Trigger:
    return set_trigger_paused(trigger_id, False, base_dir=base_dir)


def run_trigger_now(trigger_id: str, base_dir: str = ".") -> dict:
    """Kick off a trigger immediately in a background thread.

    Schedule-based and state-based triggers can be run manually at any time,
    including while a workstream is paused. State-based triggers execute the
    first currently eligible task in the trigger's state as if pause were not
    set. Results are visible in the audit log.
    """
    import threading
    from .workstreams import list_workstreams

    for ws in list_workstreams(base_dir):
        for trigger in ws.triggers:
            if trigger.id != trigger_id:
                continue
            if trigger.on_schedule is None and trigger.on_state is None:
                raise ValueError("Run Now is only supported for schedule-based or state-based triggers")
            pause_reason = trigger_pause_reason(trigger, ws)
            if pause_reason:
                raise RuntimeError(pause_reason)

            # Capture references for the background thread
            _trigger, _ws = trigger, ws

            def _run():
                from .tasks import list_tasks
                from .scheduler import _lock_invoke_unlock, _task_matches_filter, _audit_trigger, _select_state_trigger_task_ids
                from .locks import lock_status
                try:
                    manual_task_ids = []
                    ignore_paused = True
                    if _trigger.on_state is not None:
                        tasks = list_tasks(_ws.id, base_dir=base_dir)
                        manual_task_ids = [
                            task.id for task in tasks
                            if task.status == _trigger.on_state
                            and not getattr(task, "paused", False)
                            and lock_status(task.id, base_dir) is None
                        ]
                        if not manual_task_ids:
                            result = {
                                "trigger_id": _trigger.id,
                                "status": "skipped",
                                "message": f"No unlocked tasks currently in state '{_trigger.on_state}'",
                            }
                        else:
                            manual_task_ids = _select_state_trigger_task_ids(_trigger, manual_task_ids)
                            result = _lock_invoke_unlock(
                                _trigger,
                                manual_task_ids,
                                _ws,
                                base_dir,
                                background=True,
                                ignore_paused=ignore_paused,
                            )
                    elif _trigger.filter is None:
                        result = _lock_invoke_unlock(
                            _trigger,
                            [],
                            _ws,
                            base_dir,
                            ignore_paused=ignore_paused,
                        )
                    else:
                        tasks = list_tasks(_ws.id, base_dir=base_dir)
                        matching_ids = [
                            t.id for t in tasks
                            if _task_matches_filter(t, _trigger.filter)
                        ]
                        result = _lock_invoke_unlock(
                            _trigger,
                            matching_ids,
                            _ws,
                            base_dir,
                            ignore_paused=ignore_paused,
                        )
                    audit_ids = manual_task_ids if _trigger.on_state else (matching_ids if _trigger.filter else [])
                    if result.get("status") not in ("dispatched",):
                        _audit_trigger(_trigger, result, _ws, base_dir, task_ids=audit_ids)
                except Exception as e:
                    from .workspace_audit import log_event
                    log_event("trigger_fired",
                              f"Run Now trigger '{_trigger.action}' failed — {e}",
                              base_dir, trigger_id=_trigger.id,
                              workstream_id=_ws.id, status="error")

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            return {"trigger_id": trigger_id, "status": "started",
                    "message": "Trigger kicked off in background"}

    raise FileNotFoundError(f"Trigger {trigger_id} not found")
