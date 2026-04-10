"""Task lock operations."""

import os
import yaml
from datetime import datetime, timezone, timedelta

from .models import Lock, now_iso
from .tasks import _find_task_file

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
