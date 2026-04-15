"""Trigger operations."""

import os
import subprocess
import sys

from .models import Trigger, Workstream, new_id
from .workstreams import read_workstream, save_workstream
from .workspace_audit import log_event


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
    if trigger.action == "run_agent":
        if trigger.agent is None:
            return {"trigger_id": trigger.id, "status": "error", "message": "No agent specified"}
        try:
            from .agents import run_agent
            result = run_agent(trigger.agent, task_ids=task_ids, workstream_id=workstream_id, base_dir=base_dir)
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
    max_concurrent: int = 1,
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
        max_concurrent=max_concurrent,
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
