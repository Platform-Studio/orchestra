"""Task operations."""

import os
import yaml

from .models import Task, RetryConfig, new_id
from .workstreams import read_workstream


def _tasks_dir(base_dir: str, ws_id: str) -> str:
    return os.path.join(base_dir, "workstreams", ws_id, "tasks")


def _task_path(base_dir: str, ws_id: str, task_id: str) -> str:
    return os.path.join(_tasks_dir(base_dir, ws_id), f"{task_id}.yaml")


def _find_task_file(task_id: str, base_dir: str = "."):
    """Find a task file by ID across all workstreams. Returns (ws_id, file_path) or None."""
    ws_dir = os.path.join(base_dir, "workstreams")
    if not os.path.exists(ws_dir):
        return None
    for ws_name in os.listdir(ws_dir):
        ws_path = os.path.join(ws_dir, ws_name)
        if not os.path.isdir(ws_path):
            continue
        task_file = os.path.join(ws_path, "tasks", f"{task_id}.yaml")
        if os.path.exists(task_file):
            return (ws_name, task_file)
    return None


def _save_task(task: Task, base_dir: str = ".") -> None:
    tasks_dir = _tasks_dir(base_dir, task.workstream_id)
    os.makedirs(tasks_dir, exist_ok=True)
    path = _task_path(base_dir, task.workstream_id, task.id)
    with open(path, "w") as f:
        yaml.dump(task.to_dict(), f, default_flow_style=False, sort_keys=False)


def create_task(
    workstream_id: str,
    title: str,
    description: str = None,
    tags: list = None,
    retry: dict = None,
    creator: str = None,
    scheduled_at: str = None,
    scheduled_action: dict = None,
    base_dir: str = ".",
) -> Task:
    ws = read_workstream(workstream_id, base_dir)
    initial_status = ws.initial_status()

    task_id = new_id()
    task = Task(
        id=task_id,
        workstream_id=workstream_id,
        title=title,
        description=description,
        status=initial_status,
        creator=creator,
        tags=tags or [],
        retry=RetryConfig.from_dict(retry) if retry else None,
        scheduled_at=scheduled_at,
        scheduled_action=scheduled_action,
    )
    task.add_audit("created", f"Task created with status '{initial_status}'")
    if scheduled_at:
        task.add_audit("scheduled", f"Scheduled action at {scheduled_at}")
    _save_task(task, base_dir)

    return task


def read_task(task_id: str, base_dir: str = ".") -> Task:
    result = _find_task_file(task_id, base_dir)
    if result is None:
        raise FileNotFoundError(f"Task {task_id} not found")
    _, file_path = result
    with open(file_path) as f:
        data = yaml.safe_load(f)
    return Task.from_dict(data)


def update_task(
    task_id: str,
    status: str = None,
    description: str = None,
    tags: list = None,
    scheduled_at: str = None,
    scheduled_action: dict = None,
    base_dir: str = ".",
) -> Task:
    task = read_task(task_id, base_dir)
    ws = read_workstream(task.workstream_id, base_dir)

    status_changed = False
    if status is not None and status != task.status:
        if not ws.validate_transition(task.status, status):
            raise ValueError(
                f"Invalid state transition: '{task.status}' -> '{status}'. "
                f"Allowed transitions from '{task.status}': {ws.task_states.get(task.status, [])}"
            )
        old_status = task.status
        task.status = status
        task.add_audit("status_change", f"Status changed from '{old_status}' to '{status}'")
        status_changed = True

    if description is not None:
        task.description = description
        task.add_audit("updated", "Description updated")

    if tags is not None:
        task.tags = tags
        task.add_audit("updated", f"Tags updated to {tags}")

    if scheduled_at is not None:
        task.scheduled_at = scheduled_at
        task.scheduled_action = scheduled_action
        task.add_audit("scheduled", f"Scheduled action at {scheduled_at}")

    _save_task(task, base_dir)

    return task


def list_tasks(
    workstream_id: str,
    status: str = None,
    tags: list = None,
    base_dir: str = ".",
) -> list:
    tasks_dir = _tasks_dir(base_dir, workstream_id)
    if not os.path.exists(tasks_dir):
        return []
    result = []
    for fname in sorted(os.listdir(tasks_dir)):
        if not fname.endswith(".yaml"):
            continue
        path = os.path.join(tasks_dir, fname)
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
        except Exception as e:
            # Corrupt YAML — return a placeholder so the UI can show it
            task_id = fname.replace(".yaml", "")
            broken = Task(
                id=task_id,
                workstream_id=workstream_id,
                title=f"[CORRUPT] {fname}",
                status="_error",
                tags=["_error"],
            )
            broken._parse_error = str(e)
            result.append(broken)
            continue
        if data:
            try:
                task = Task.from_dict(data)
            except Exception as e:
                task_id = data.get("id", fname.replace(".yaml", ""))
                broken = Task(
                    id=task_id,
                    workstream_id=workstream_id,
                    title=f"[CORRUPT] {data.get('title', fname)}",
                    status="_error",
                    tags=["_error"],
                )
                broken._parse_error = str(e)
                result.append(broken)
                continue
            if status and task.status != status:
                continue
            if tags and not all(t in task.tags for t in tags):
                continue
            result.append(task)
    return result


def comment_task(task_id: str, message: str, base_dir: str = ".") -> Task:
    task = read_task(task_id, base_dir)
    task.comments.append(message)
    task.add_audit("comment", f"Comment added: {message}")
    _save_task(task, base_dir)
    return task


def archive_task(task_id: str, base_dir: str = ".") -> dict:
    result = _find_task_file(task_id, base_dir)
    if result is None:
        raise FileNotFoundError(f"Task {task_id} not found")
    _, file_path = result
    os.remove(file_path)
    # Also remove lock file if it exists
    lock_path = file_path + ".lock"
    if os.path.exists(lock_path):
        os.remove(lock_path)
    return {"id": task_id, "archived": True}


def get_audit(task_id: str, base_dir: str = ".") -> list:
    task = read_task(task_id, base_dir)
    return [a.to_dict() for a in task.audit]


def clear_schedule(task_id: str, base_dir: str = ".") -> Task:
    """Clear the scheduled_at and scheduled_action fields from a task."""
    task = read_task(task_id, base_dir)
    task.scheduled_at = None
    task.scheduled_action = None
    task.add_audit("schedule_cleared", "Scheduled action cleared")
    _save_task(task, base_dir)
    return task


def clear_schedule(task_id: str, base_dir: str = ".") -> Task:
    """Clear the scheduled_at and scheduled_action fields from a task."""
    task = read_task(task_id, base_dir)
    task.scheduled_at = None
    task.scheduled_action = None
    task.add_audit("schedule_cleared", "Scheduled action cleared")
    _save_task(task, base_dir)
    return task
