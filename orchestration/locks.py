"""Task lock operations."""

import os
import yaml
from datetime import datetime, timezone, timedelta

from .models import Lock, now_iso
from .tasks import _find_task_file, _tasks_dir, _tasks_dir_for_workstream
from .workstreams import list_workstreams

DEFAULT_TTL_SECONDS = 1800  # 30 minutes


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


def _list_locks_from_tasks_dir(tasks_dir: str, task_ids: list[str] = None) -> dict:
    if not os.path.isdir(tasks_dir):
        return {}

    requested_ids = set(task_ids or [])
    locks = {}
    for fname in os.listdir(tasks_dir):
        if not fname.endswith(".yaml.lock"):
            continue

        task_id = fname[: -len(".yaml.lock")]
        if requested_ids and task_id not in requested_ids:
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

            lock_data = lock.to_dict()
            lock_data["locked"] = True
            locks[task_id] = lock_data
        except Exception:
            continue

    return locks


def list_workstream_locks(workstream_id: str, task_ids: list[str] = None, base_dir: str = ".") -> dict:
    """List active non-expired locks for a workstream keyed by task ID."""
    return _list_locks_from_tasks_dir(_tasks_dir(base_dir, workstream_id), task_ids=task_ids)


def list_workstream_locks_for_workstream(workstream, task_ids: list[str] = None, base_dir: str = ".") -> dict:
    return _list_locks_from_tasks_dir(
        _tasks_dir_for_workstream(workstream, base_dir=base_dir),
        task_ids=task_ids,
    )


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
    expired = []
    for ws in list_workstreams(base_dir=base_dir):
        tasks_dir = _tasks_dir(base_dir, ws.id)
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
                        "workstream_id": ws.id,
                        "lock": lock,
                        "lock_path": lock_path,
                    })
            except Exception:
                continue
    return expired


def _is_process_alive(pid: int) -> bool:
    """Return True when pid exists and is not a zombie, False otherwise."""
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False

    # On Linux, a stopped scheduler parent can remain as a zombie briefly. kill(0)
    # still succeeds for zombies, but they cannot own active work or locks.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            stat = f.read()
        end = stat.rfind(")")
        if end != -1:
            fields = stat[end + 2 :].split()
            if fields and fields[0] == "Z":
                return False
    except OSError:
        pass

    return True


def find_orphaned_locks(base_dir: str = ".") -> list:
    """Scan all workstreams for non-expired locks whose owner process is dead.

    Scheduler-launched agents store the scheduler pid as ``pid`` and the actual
    runtime process as ``subprocess_pid``. Treat the lock as live when either
    process still exists so a scheduler restart does not orphan active agent work.

    Returns list of dicts: {task_id, workstream_id, lock, lock_path}
    """
    orphaned = []
    for ws in list_workstreams(base_dir=base_dir):
        tasks_dir = _tasks_dir(base_dir, ws.id)
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
                # Expired locks are handled by the retry pipeline.
                if lock.is_expired():
                    continue
                candidate_pids = [pid for pid in (lock.pid, lock.subprocess_pid) if pid is not None]
                if not candidate_pids:
                    continue
                if any(_is_process_alive(pid) for pid in candidate_pids):
                    continue
                task_id = fname.replace(".yaml.lock", "")
                orphaned.append({
                    "task_id": task_id,
                    "workstream_id": ws.id,
                    "lock": lock,
                    "lock_path": lock_path,
                })
            except Exception:
                continue
    return orphaned


# ---------------------------------------------------------------------------
# Process locks — not tied to any task, keyed by run_id
# ---------------------------------------------------------------------------

_PROCESS_LOCKS_SUBDIR = "process_locks"


def _process_locks_dir(base_dir: str) -> str:
    return os.path.join(base_dir, ".orchestration", _PROCESS_LOCKS_SUBDIR)


def _process_lock_path(run_id: str, base_dir: str) -> str:
    return os.path.join(_process_locks_dir(base_dir), f"{run_id}.lock")


def acquire_process_lock(
    run_id: str,
    agent_id: str,
    pid: int = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    base_dir: str = ".",
) -> Lock:
    """Acquire a process-level lock for an agent run that is not tied to any task.

    Stores the lock under .orchestration/process_locks/{run_id}.lock.
    Useful for scheduler-driven standalone agents so they can be detected and
    cleaned up automatically if they hang past their TTL.
    """
    os.makedirs(_process_locks_dir(base_dir), exist_ok=True)
    lock_path = _process_lock_path(run_id, base_dir)

    # Remove expired lock file if present
    if os.path.exists(lock_path):
        try:
            with open(lock_path) as f:
                data = yaml.safe_load(f)
            if data:
                existing = Lock.from_dict(data)
                if not existing.is_expired():
                    raise RuntimeError(
                        f"Process lock for run {run_id} already held by agent {existing.agent_id}"
                    )
        except RuntimeError:
            raise
        except Exception:
            pass
        os.remove(lock_path)

    now = datetime.now(timezone.utc)
    lock = Lock(
        agent_id=agent_id,
        acquired_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=ttl_seconds)).isoformat(),
        pid=pid if pid is not None else os.getpid(),
    )

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"Process lock contention on run {run_id}")

    with os.fdopen(fd, "w") as f:
        yaml.dump(lock.to_dict(), f, default_flow_style=False, sort_keys=False)

    return lock


def release_process_lock(run_id: str, base_dir: str = ".") -> bool:
    """Release a process lock. Returns True if removed, False if already absent."""
    lock_path = _process_lock_path(run_id, base_dir)
    if not os.path.exists(lock_path):
        return False
    os.remove(lock_path)
    return True


def process_lock_status(run_id: str, base_dir: str = "."):
    """Return the Lock if the process lock is active (not expired), else None."""
    lock_path = _process_lock_path(run_id, base_dir)
    if not os.path.exists(lock_path):
        return None
    try:
        with open(lock_path) as f:
            data = yaml.safe_load(f)
    except Exception:
        return None
    if not data:
        return None
    lock = Lock.from_dict(data)
    if lock.is_expired():
        return None
    return lock


def find_stale_process_locks(base_dir: str = ".") -> list:
    """Return process locks that are expired or whose owner PID is dead.

    Each entry is a dict: {run_id, lock, lock_path, reason}
    where reason is 'expired' or 'dead_pid'.
    """
    locks_dir = _process_locks_dir(base_dir)
    if not os.path.isdir(locks_dir):
        return []
    stale = []
    for fname in os.listdir(locks_dir):
        if not fname.endswith(".lock"):
            continue
        run_id = fname[: -len(".lock")]
        lock_path = os.path.join(locks_dir, fname)
        try:
            with open(lock_path) as f:
                data = yaml.safe_load(f)
            if not data:
                continue
            lock = Lock.from_dict(data)
            if lock.is_expired():
                stale.append({"run_id": run_id, "lock": lock, "lock_path": lock_path, "reason": "expired"})
            elif lock.pid is not None and not _is_process_alive(lock.pid):
                stale.append({"run_id": run_id, "lock": lock, "lock_path": lock_path, "reason": "dead_pid"})
        except Exception:
            continue
    return stale
