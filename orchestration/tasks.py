"""Task operations."""

import os
import re
import yaml

from .models import Task, RetryConfig, new_id, now_iso
from .workstreams import read_workstream, resolve_workstream_workspace, list_workstreams


COMMENT_DATE_PREFIX_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s*")


def _normalize_comment_message(message: str) -> str:
    """Normalize comment body by stripping legacy date prefix markers."""
    text = str(message or "").strip()
    return COMMENT_DATE_PREFIX_RE.sub("", text).strip()


def _default_comment_author() -> str:
    """Best-effort author identity for agent-produced comments."""
    for env_key in ("ORCHESTRATION_AGENT_NAME", "AGENT_NAME", "CLAUDE_AGENT_NAME"):
        value = os.getenv(env_key)
        if value:
            return value
    shell_user = os.getenv("USER")
    if shell_user:
        return shell_user
    return None


def _normalize_attachment_path(path: str) -> str:
    """Normalize a task attachment path.

    Attachments are logical artifact paths, so they should be stored as
    workspace-relative artifact identifiers without a leading slash.
    """
    normalized = str(path or "").strip().lstrip("/")
    if not normalized:
        raise ValueError("Attachment path must be a non-empty artifact path")
    return normalized


def _normalize_attachment_list(paths: list) -> list:
    """Normalize and deduplicate attachment paths while preserving order."""
    normalized = []
    seen = set()
    for raw in paths or []:
        path = _normalize_attachment_path(raw)
        if path in seen:
            continue
        seen.add(path)
        normalized.append(path)
    return normalized


def _tasks_dir(base_dir: str, ws_id: str) -> str:
    ws_root = resolve_workstream_workspace(ws_id, base_dir=base_dir)
    return os.path.join(ws_root, "workstreams", ws_id, "tasks")


def _task_path(base_dir: str, ws_id: str, task_id: str) -> str:
    return os.path.join(_tasks_dir(base_dir, ws_id), f"{task_id}.yaml")


def _find_task_file(task_id: str, base_dir: str = "."):
    """Find a task file by ID across all workstreams. Returns (ws_id, file_path) or None."""
    for ws in list_workstreams(base_dir=base_dir):
        ws_root = getattr(ws, "_workspace_root", os.path.abspath(base_dir))
        task_file = os.path.join(ws_root, "workstreams", ws.id, "tasks", f"{task_id}.yaml")
        if os.path.exists(task_file):
            return (ws.id, task_file)
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
    attachments: list = None,
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
        attachments=_normalize_attachment_list(attachments),
    )
    task.add_audit("created", f"Task created with status '{initial_status}'")
    if task.attachments:
        task.add_audit("attachments_updated", f"Attachments set to {task.attachments}")
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
    attachments: list = None,
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

    if attachments is not None:
        task.attachments = _normalize_attachment_list(attachments)
        task.add_audit("attachments_updated", f"Attachments set to {task.attachments}")

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

    # Default ordering: oldest last-audit timestamp first (FIFO-style pull).
    # Fallback for malformed/empty audit is empty string, which sorts first.
    def _last_audit_ts(task: Task) -> str:
        if not task.audit:
            return ""
        return getattr(task.audit[-1], "timestamp", "") or ""

    result.sort(key=_last_audit_ts)
    return result


def comment_task(task_id: str, message: str, author: str = None, base_dir: str = ".") -> Task:
    task = read_task(task_id, base_dir)
    normalized_message = _normalize_comment_message(message)

    comment_author = author
    if comment_author is None:
        # If an agent lock is present, infer author from lock owner.
        try:
            from .locks import lock_status
            lock = lock_status(task_id, base_dir)
            if lock is not None:
                comment_author = lock.agent_id
        except Exception:
            comment_author = None
    if comment_author is None:
        comment_author = _default_comment_author()

    comment = {
        "message": normalized_message,
        "timestamp": now_iso(),
    }
    if comment_author:
        comment["author"] = comment_author

    task.comments.append(comment)

    if comment_author:
        task.add_audit("comment", f"Comment added by {comment_author}: {normalized_message}")
    else:
        task.add_audit("comment", f"Comment added: {normalized_message}")
    _save_task(task, base_dir)
    return task


def attach_to_task(task_id: str, path: str, base_dir: str = ".") -> Task:
    """Attach an artifact path to a task if it is not already attached."""
    task = read_task(task_id, base_dir)
    normalized_path = _normalize_attachment_path(path)
    if normalized_path not in task.attachments:
        task.attachments.append(normalized_path)
        task.add_audit("attachment_added", f"Attachment added: {normalized_path}")
        _save_task(task, base_dir)
    return task


def detach_from_task(task_id: str, path: str, base_dir: str = ".") -> Task:
    """Detach an artifact path from a task."""
    task = read_task(task_id, base_dir)
    normalized_path = _normalize_attachment_path(path)
    if normalized_path in task.attachments:
        task.attachments = [p for p in task.attachments if p != normalized_path]
        task.add_audit("attachment_removed", f"Attachment removed: {normalized_path}")
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


def move_task(
    task_id: str,
    target_workstream_id: str,
    target_status: str = None,
    base_dir: str = ".",
) -> Task:
    """Move a task to a different workstream (or to a new state in the same workstream).

    When moving cross-workstream the full task file is written into the target
    workstream and the original is deleted.  Within the same workstream this is
    equivalent to update_task with a status override that skips state-machine
    validation (because we're treating it as a board-level drag, same as Trello).
    """
    task = read_task(task_id, base_dir)
    original_ws_id = task.workstream_id
    cross_ws = target_workstream_id != original_ws_id

    target_ws = read_workstream(target_workstream_id, base_dir)

    if target_status is None:
        target_status = target_ws.initial_status()

    # Validate target status exists in the target workstream
    if target_status not in target_ws.task_states:
        raise ValueError(
            f"State '{target_status}' does not exist in workstream '{target_ws.name}'. "
            f"Valid states: {list(target_ws.task_states.keys())}"
        )

    old_status = task.status
    task.workstream_id = target_workstream_id
    task.status = target_status

    if cross_ws:
        task.add_audit(
            "moved",
            f"Moved from workstream '{original_ws_id}' to '{target_workstream_id}' "
            f"with status '{target_status}'",
        )
        # Write into target workstream first, then remove from source
        _save_task(task, base_dir)
        src_path = _task_path(base_dir, original_ws_id, task_id)
        if os.path.exists(src_path):
            os.remove(src_path)
        # Remove source lock file if present
        lock_path = src_path + ".lock"
        if os.path.exists(lock_path):
            os.remove(lock_path)
    else:
        task.add_audit(
            "status_change",
            f"Status changed from '{old_status}' to '{target_status}' (moved on board)",
        )
        _save_task(task, base_dir)

    return task


def duplicate_task(
    task_id: str,
    target_workstream_id: str = None,
    target_status: str = None,
    base_dir: str = ".",
) -> Task:
    """Duplicate a task into the same or a different workstream.

    The duplicate receives:
    - A new unique ID
    - The same title, description, tags, and comments as the original
    - A fresh audit trail (no history from the original)
    - An initial audit entry stating it was duplicated from <original_id>

    Audit trail is intentionally NOT copied.
    """
    import copy
    source = read_task(task_id, base_dir)
    dest_ws_id = target_workstream_id or source.workstream_id
    dest_ws = read_workstream(dest_ws_id, base_dir)

    if target_status is None:
        target_status = dest_ws.initial_status()

    if target_status not in dest_ws.task_states:
        raise ValueError(
            f"State '{target_status}' does not exist in workstream '{dest_ws.name}'. "
            f"Valid states: {list(dest_ws.task_states.keys())}"
        )

    new_task = Task(
        id=new_id(),
        workstream_id=dest_ws_id,
        title=source.title,
        description=source.description,
        status=target_status,
        creator=source.creator,
        tags=list(source.tags),
        comments=copy.deepcopy(source.comments),
        attachments=list(source.attachments),
        # audit intentionally empty — fresh trail below
    )
    new_task.add_audit(
        "created",
        f"Duplicated from task '{task_id}' in workstream '{source.workstream_id}'",
    )
    _save_task(new_task, base_dir)
    return new_task
