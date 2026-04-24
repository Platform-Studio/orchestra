"""Trigger operations."""

import os
import subprocess
import sys
import threading

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


def execute_trigger(trigger: Trigger, task_ids: list, workstream_id: str, base_dir: str = ".") -> dict:
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
    if ws.paused:
        return {
            "trigger_id": trigger.id,
            "status": "skipped",
            "message": f"Workstream '{ws.name}' is paused",
        }

    with _active_triggers_lock:
        _active_triggers.add(trigger.id)
    try:
        return _execute_trigger_inner(trigger, task_ids, workstream_id, base_dir)
    finally:
        with _active_triggers_lock:
            _active_triggers.discard(trigger.id)


def _execute_trigger_inner(trigger: Trigger, task_ids: list, workstream_id: str, base_dir: str = ".") -> dict:
    if trigger.action == "run_agent":
        if trigger.agent is None:
            return {"trigger_id": trigger.id, "status": "error", "message": "No agent specified"}
        try:
            from .agents import run_agent
            result = run_agent(trigger.agent, task_ids=task_ids, workstream_id=workstream_id, prompt=trigger.prompt, timeout=trigger.timeout, base_dir=base_dir)
            return {"trigger_id": trigger.id, "status": "ok", "result": result}
        except Exception as e:
            return {"trigger_id": trigger.id, "status": "error", "message": str(e)}

    elif trigger.action == "run_command":
        if trigger.command is None:
            return {"trigger_id": trigger.id, "status": "error", "message": "No command specified"}
        # Template variable substitution — join task IDs for command templates
        task_id_str = task_ids[0] if len(task_ids) == 1 else ",".join(task_ids)
        cmd = trigger.command.replace("{task_id}", task_id_str).replace("{workstream_id}", workstream_id)

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
    filter: dict = None,
    agent: str = None,
    command: str = None,
    prompt: str = None,
    timeout: int = None,
    base_dir: str = ".",
) -> Trigger:
    if on_state is None and on_schedule is None:
        raise ValueError("Either --on-state or --on-schedule must be specified")
    if on_state is not None and on_schedule is not None:
        raise ValueError("Cannot specify both --on-state and --on-schedule")

    ws = read_workstream(workstream_id, base_dir)
    trigger = Trigger(
        id=new_id(),
        action=action,
        on_state=on_state,
        on_schedule=on_schedule,
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


def run_trigger_now(trigger_id: str, base_dir: str = ".") -> dict:
    """Kick off a schedule-based trigger immediately in a background thread.

    Validates the trigger exists and is schedule-based, then spawns a
    daemon thread to execute it. Returns immediately with an acknowledgement.
    Results are visible in the audit log.
    """
    import threading
    from .workstreams import list_workstreams

    for ws in list_workstreams(base_dir):
        for trigger in ws.triggers:
            if trigger.id != trigger_id:
                continue
            if trigger.on_schedule is None:
                raise ValueError("Run Now is only supported for schedule-based triggers")
            if ws.paused:
                raise RuntimeError(f"Workstream '{ws.name}' is paused")

            # Capture references for the background thread
            _trigger, _ws = trigger, ws

            def _run():
                from .tasks import list_tasks
                from .scheduler import _lock_invoke_unlock, _task_matches_filter, _audit_trigger
                try:
                    if _trigger.filter is None:
                        result = _lock_invoke_unlock(_trigger, [], _ws, base_dir)
                    else:
                        tasks = list_tasks(_ws.id, base_dir=base_dir)
                        matching_ids = [
                            t.id for t in tasks
                            if _task_matches_filter(t, _trigger.filter)
                        ]
                        result = _lock_invoke_unlock(_trigger, matching_ids, _ws, base_dir)
                    _audit_trigger(_trigger, result, _ws, base_dir,
                                   task_ids=(matching_ids if _trigger.filter else []))
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
