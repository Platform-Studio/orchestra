"""Task lock operations."""

import os
import yaml
from datetime import datetime, timezone, timedelta

from .models import Lock, now_iso
from .tasks import _find_task_file, _tasks_dir

DEFAULT_TTL_SECONDS = 900  # 15 minutes


def _lock_path_for_task(task_id: str, base_dir: str = "."):
    """Get the lock file path for a task. Returns path or None if task not found."""
    result = _find_task_file(task_id, base_dir)
    if result is None:
        return None
    _, task_file = result
    return task_file + ".lock"


def acquire_lock(
    task_id: str,
    agent_id: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    base_dir: str = ".",
    pid: int = None,
) -> Lock:
    lock_path = _lock_path_for_task(task_id, base_dir)
    if lock_path is None:
        raise FileNotFoundError(f"Task {task_id} not found")

    # Check existing lock
    existing = lock_status(task_id, base_dir)
    if existing is not None:
        raise RuntimeError(
            f"Task is locked by agent {existing.agent_id} until {existing.expires_at}"
        )

    # Remove expired lock file if present (lock_status returned None but file may exist)
    if os.path.exists(lock_path):
        os.remove(lock_path)

    # Create lock atomically using O_CREAT | O_EXCL
    now = datetime.now(timezone.utc)
    lock = Lock(
        agent_id=agent_id,
        acquired_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        pid=pid or os.getpid(),
    )

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"Lock contention on task {task_id} — another agent acquired the lock")

    with os.fdopen(fd, "w") as f:
        yaml.dump(lock.to_dict(), f, default_flow_style=False, sort_keys=False)

    return lock


def release_lock(task_id: str, agent_id: str, base_dir: str = ".") -> bool:
    lock_path = _lock_path_for_task(task_id, base_dir)
    if lock_path is None:
        raise FileNotFoundError(f"Task {task_id} not found")

    if not os.path.exists(lock_path):
        return False

    # Verify lock ownership
    with open(lock_path) as f:
        data = yaml.safe_load(f)
    if data and data.get("agent_id") != agent_id:
        raise RuntimeError(
            f"Lock is owned by agent '{data.get('agent_id')}', not '{agent_id}'"
        )

    os.remove(lock_path)
    return True


def lock_status(task_id: str, base_dir: str = "."):
    """Get the current lock status. Returns Lock if locked and not expired, None otherwise."""
    lock_path = _lock_path_for_task(task_id, base_dir)
    if lock_path is None:
        raise FileNotFoundError(f"Task {task_id} not found")

    if not os.path.exists(lock_path):
        return None

    with open(lock_path) as f:
        data = yaml.safe_load(f)
    if data is None:
        return None

    lock = Lock.from_dict(data)
    if lock.is_expired():
        return None  # Treat expired as unlocked

    return lock


def active_lock_count(workstream_id: str, agent_id: str = None, base_dir: str = ".") -> int:
    """Count active (non-expired) locks in a workstream, optionally filtered by agent_id."""
    tasks_dir = _tasks_dir(base_dir, workstream_id)
    if not os.path.isdir(tasks_dir):
        return 0
    count = 0
    for fname in os.listdir(tasks_dir):
        if not fname.endswith(".yaml.lock"):
            continue
        lock_path = os.path.join(tasks_dir, fname)
        try:
            with open(lock_path) as f:
                data = yaml.safe_load(f)
            if data is None:
                continue
            lock = Lock.from_dict(data)
            if lock.is_expired():
                continue
            if agent_id is not None and lock.agent_id != agent_id:
                continue
            count += 1
        except Exception:
            continue
    return count


def update_lock_pid(task_id: str, subprocess_pid: int, base_dir: str = ".") -> None:
    """Update the subprocess_pid field in an existing lock file."""
    lock_path = _lock_path_for_task(task_id, base_dir)
    if lock_path is None or not os.path.exists(lock_path):
        return
    with open(lock_path) as f:
        data = yaml.safe_load(f)
    if data is None:
        return
    data["subprocess_pid"] = subprocess_pid
    with open(lock_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def force_release_lock(task_id: str, base_dir: str = ".") -> bool:
    """Remove a lock file regardless of ownership. Used for expired lock cleanup."""
    lock_path = _lock_path_for_task(task_id, base_dir)
    if lock_path is None:
        return False
    if not os.path.exists(lock_path):
        return False
    os.remove(lock_path)
    return True


def find_expired_locks(base_dir: str = ".") -> list:
    """Scan all workstreams for expired lock files.

    Returns list of dicts: {task_id, workstream_id, lock, lock_path}
    """
    ws_dir = os.path.join(base_dir, "workstreams")
    if not os.path.isdir(ws_dir):
        return []
    expired = []
    for ws_name in os.listdir(ws_dir):
        ws_path = os.path.join(ws_dir, ws_name)
        tasks_dir = os.path.join(ws_path, "tasks")
        if not os.path.isdir(tasks_dir):
            continue
        for fname in os.listdir(tasks_dir):
            if not fname.endswith(".yaml.lock"):
                continue
            lock_path = os.path.join(tasks_dir, fname)
            try:
                with open(lock_path) as f:
                    data = yaml.safe_load(f)
                if data is None:
                    continue
                lock = Lock.from_dict(data)
                if lock.is_expired():
                    task_id = fname.replace(".yaml.lock", "")
                    expired.append({
                        "task_id": task_id,
                        "workstream_id": ws_name,
                        "lock": lock,
                        "lock_path": lock_path,
                    })
            except Exception:
                continue
    return expired
