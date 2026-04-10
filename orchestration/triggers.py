"""Trigger operations."""

import subprocess

from .models import Trigger, Workstream, new_id
from .workstreams import read_workstream, save_workstream


def evaluate_triggers(
    ws: Workstream,
    new_state: str,
    task_id: str,
    base_dir: str = ".",
) -> list:
    """Evaluate and execute triggers for a given state change."""
    results = []
    for trigger in ws.triggers:
        if trigger.on_state == new_state:
            result = _execute_trigger(trigger, task_id, ws.id, base_dir)
            results.append(result)
    return results


def _execute_trigger(trigger: Trigger, task_id: str, workstream_id: str, base_dir: str = ".") -> dict:
    """Execute a single trigger."""
    if trigger.action == "run_agent":
        if trigger.agent is None:
            return {"trigger_id": trigger.id, "status": "error", "message": "No agent specified"}
        try:
            from .agents import run_agent
            result = run_agent(trigger.agent, task_id, base_dir)
            return {"trigger_id": trigger.id, "status": "ok", "result": result}
        except Exception as e:
            return {"trigger_id": trigger.id, "status": "error", "message": str(e)}

    elif trigger.action == "run_command":
        if trigger.command is None:
            return {"trigger_id": trigger.id, "status": "error", "message": "No command specified"}
        # Template variable substitution
        cmd = trigger.command.replace("{task_id}", task_id).replace("{workstream_id}", workstream_id)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=300, cwd=base_dir,
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
    on_state: str,
    action: str,
    agent: str = None,
    command: str = None,
    base_dir: str = ".",
) -> Trigger:
    ws = read_workstream(workstream_id, base_dir)
    trigger = Trigger(
        id=new_id(),
        on_state=on_state,
        action=action,
        agent=agent,
        command=command,
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
