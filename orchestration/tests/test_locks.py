"""Tests for lock operations."""

import os
import pytest
import yaml
from datetime import datetime, timezone, timedelta

from orchestration.workstreams import create_workstream
from orchestration.tasks import create_task
from orchestration.locks import (
    acquire_lock,
    release_lock,
    lock_status,
    lock_status_for_workstream,
    list_workstream_locks,
    list_workstream_locks_for_workstream,
    acquire_process_lock,
    release_process_lock,
    process_lock_status,
    find_stale_process_locks,
    _is_process_alive,
    _process_lock_path,
    _process_locks_dir,
)
from orchestration.workstreams import list_workstreams, save_workstream


@pytest.fixture
def ws(workspace):
    return create_workstream(name="Lock Test WS", base_dir=workspace)


@pytest.fixture
def task(workspace, ws):
    return create_task(ws.id, title="Lock Test Task", base_dir=workspace)


class TestAcquireLock:
    def test_acquire_basic(self, workspace, task):
        lock = acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        assert lock.agent_id == "agent-1"
        assert lock.acquired_at is not None
        assert lock.expires_at is not None

    def test_acquire_with_custom_ttl(self, workspace, task):
        lock = acquire_lock(task.id, agent_id="agent-1", ttl_seconds=60, base_dir=workspace)
        acquired = datetime.fromisoformat(lock.acquired_at)
        expires = datetime.fromisoformat(lock.expires_at)
        diff = (expires - acquired).total_seconds()
        assert abs(diff - 60) < 1

    def test_acquire_creates_lock_file(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        ws_dir = os.path.join(workspace, "workstreams")
        # Find the lock file
        found = False
        for ws_name in os.listdir(ws_dir):
            ws_path = os.path.join(ws_dir, ws_name)
            if os.path.isdir(ws_path):
                lock_file = os.path.join(ws_path, "tasks", f"{task.id}.yaml.lock")
                if os.path.exists(lock_file):
                    found = True
                    break
        assert found

    def test_acquire_on_locked_task(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        with pytest.raises(RuntimeError, match="locked"):
            acquire_lock(task.id, agent_id="agent-2", base_dir=workspace)

    def test_acquire_task_not_found(self, workspace):
        with pytest.raises(FileNotFoundError):
            acquire_lock("nonexistent", agent_id="agent-1", base_dir=workspace)


class TestReleaseLock:
    def test_release_own_lock(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        result = release_lock(task.id, agent_id="agent-1", base_dir=workspace)
        assert result is True

    def test_release_wrong_agent(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        with pytest.raises(RuntimeError, match="owned by"):
            release_lock(task.id, agent_id="agent-2", base_dir=workspace)

    def test_release_no_lock(self, workspace, task):
        result = release_lock(task.id, agent_id="agent-1", base_dir=workspace)
        assert result is False

    def test_release_then_reacquire(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        release_lock(task.id, agent_id="agent-1", base_dir=workspace)
        lock = acquire_lock(task.id, agent_id="agent-2", base_dir=workspace)
        assert lock.agent_id == "agent-2"


class TestLockStatus:
    def test_status_locked(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        status = lock_status(task.id, base_dir=workspace)
        assert status is not None
        assert status.agent_id == "agent-1"

    def test_status_unlocked(self, workspace, task):
        status = lock_status(task.id, base_dir=workspace)
        assert status is None

    def test_status_for_workstream(self, workspace, ws, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        status = lock_status_for_workstream(ws.id, task.id, base_dir=workspace)
        assert status is not None
        assert status.agent_id == "agent-1"

    def test_status_expired(self, workspace, task):
        # Create a lock with expired timestamp
        lock = acquire_lock(task.id, agent_id="agent-1", ttl_seconds=1, base_dir=workspace)
        # Manually set the expiry to the past
        from orchestration.tasks import _find_task_file
        _, task_file = _find_task_file(task.id, workspace)
        lock_path = task_file + ".lock"
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        lock_data = {
            "agent_id": "agent-1",
            "acquired_at": past.isoformat(),
            "expires_at": past.isoformat(),
        }
        with open(lock_path, "w") as f:
            yaml.dump(lock_data, f)

        status = lock_status(task.id, base_dir=workspace)
        assert status is None  # expired = unlocked

    def test_acquire_after_expired(self, workspace, task):
        # Create expired lock
        from orchestration.tasks import _find_task_file
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        _, task_file = _find_task_file(task.id, workspace)
        lock_path = task_file + ".lock"
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        lock_data = {
            "agent_id": "agent-1",
            "acquired_at": past.isoformat(),
            "expires_at": past.isoformat(),
        }
        with open(lock_path, "w") as f:
            yaml.dump(lock_data, f)

        # Should be able to acquire since old lock is expired
        lock = acquire_lock(task.id, agent_id="agent-2", base_dir=workspace)
        assert lock.agent_id == "agent-2"

    def test_list_workstream_locks_returns_only_active_locks(self, workspace, ws):
        task_a = create_task(ws.id, title="Task A", base_dir=workspace)
        task_b = create_task(ws.id, title="Task B", base_dir=workspace)
        acquire_lock(task_a.id, agent_id="agent-1", base_dir=workspace)
        acquire_lock(task_b.id, agent_id="agent-2", base_dir=workspace)

        from orchestration.tasks import _find_task_file
        _, task_b_file = _find_task_file(task_b.id, workspace)
        lock_path = task_b_file + ".lock"
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with open(lock_path, "w") as f:
            yaml.dump({
                "agent_id": "agent-2",
                "acquired_at": past.isoformat(),
                "expires_at": past.isoformat(),
            }, f)

        locks = list_workstream_locks(ws.id, base_dir=workspace)
        assert set(locks.keys()) == {task_a.id}
        assert locks[task_a.id]["locked"] is True
        assert locks[task_a.id]["agent_id"] == "agent-1"

    def test_list_workstream_locks_for_workstream_uses_cached_workspace_root_for_working_directory_child(self, workspace, tmp_path, monkeypatch):
        working_root = tmp_path / "career_pivot_repo"
        working_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Sales", parent_id=parent.id, base_dir=workspace)
        task = create_task(child.id, title="Task A", base_dir=workspace)
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)

        loaded_child = next(ws for ws in list_workstreams(base_dir=workspace) if ws.id == child.id)

        def _boom(*args, **kwargs):
            raise AssertionError("should use cached workspace root")

        monkeypatch.setattr("orchestration.locks._tasks_dir", _boom)

        locks = list_workstream_locks_for_workstream(loaded_child, base_dir=workspace)
        assert set(locks.keys()) == {task.id}
        assert locks[task.id]["agent_id"] == "agent-1"


class TestProcessLocks:
    RUN_ID = "test-run-abc123"

    def test_acquire_basic(self, workspace):
        lock = acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", base_dir=workspace)
        assert lock.agent_id == "seo-indexer"
        assert lock.acquired_at is not None
        assert lock.expires_at is not None

    def test_acquire_creates_lock_file(self, workspace):
        acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", base_dir=workspace)
        path = _process_lock_path(self.RUN_ID, workspace)
        assert os.path.exists(path)

    def test_acquire_stores_pid(self, workspace):
        lock = acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", pid=99999, base_dir=workspace)
        assert lock.pid == 99999

    def test_acquire_with_custom_ttl(self, workspace):
        lock = acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", ttl_seconds=120, base_dir=workspace)
        acquired = datetime.fromisoformat(lock.acquired_at)
        expires = datetime.fromisoformat(lock.expires_at)
        assert abs((expires - acquired).total_seconds() - 120) < 1

    def test_acquire_duplicate_active_lock_raises(self, workspace):
        acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", base_dir=workspace)
        with pytest.raises(RuntimeError, match="already held"):
            acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", base_dir=workspace)

    def test_acquire_after_expired_lock_succeeds(self, workspace):
        path = _process_lock_path(self.RUN_ID, workspace)
        os.makedirs(_process_locks_dir(workspace), exist_ok=True)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with open(path, "w") as f:
            yaml.dump({"agent_id": "old-agent", "acquired_at": past.isoformat(), "expires_at": past.isoformat()}, f)
        lock = acquire_process_lock(self.RUN_ID, agent_id="new-agent", base_dir=workspace)
        assert lock.agent_id == "new-agent"

    def test_release_removes_file(self, workspace):
        acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", base_dir=workspace)
        result = release_process_lock(self.RUN_ID, base_dir=workspace)
        assert result is True
        assert not os.path.exists(_process_lock_path(self.RUN_ID, workspace))

    def test_release_nonexistent_returns_false(self, workspace):
        assert release_process_lock(self.RUN_ID, base_dir=workspace) is False

    def test_status_active(self, workspace):
        acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", base_dir=workspace)
        lock = process_lock_status(self.RUN_ID, base_dir=workspace)
        assert lock is not None
        assert lock.agent_id == "seo-indexer"

    def test_status_absent(self, workspace):
        assert process_lock_status(self.RUN_ID, base_dir=workspace) is None

    def test_status_expired_returns_none(self, workspace):
        path = _process_lock_path(self.RUN_ID, workspace)
        os.makedirs(_process_locks_dir(workspace), exist_ok=True)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with open(path, "w") as f:
            yaml.dump({"agent_id": "seo-indexer", "acquired_at": past.isoformat(), "expires_at": past.isoformat()}, f)
        assert process_lock_status(self.RUN_ID, base_dir=workspace) is None

    def test_find_stale_no_locks(self, workspace):
        assert find_stale_process_locks(base_dir=workspace) == []

    def test_find_stale_expired(self, workspace):
        path = _process_lock_path(self.RUN_ID, workspace)
        os.makedirs(_process_locks_dir(workspace), exist_ok=True)
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with open(path, "w") as f:
            yaml.dump({"agent_id": "seo-indexer", "acquired_at": past.isoformat(), "expires_at": past.isoformat()}, f)
        stale = find_stale_process_locks(base_dir=workspace)
        assert len(stale) == 1
        assert stale[0]["run_id"] == self.RUN_ID
        assert stale[0]["reason"] == "expired"

    def test_find_stale_dead_pid(self, workspace):
        # PID 1 is always alive (init) and 2**22 is almost certainly dead.
        dead_pid = 2**22
        acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", pid=dead_pid, ttl_seconds=9999, base_dir=workspace)
        stale = find_stale_process_locks(base_dir=workspace)
        assert any(s["run_id"] == self.RUN_ID and s["reason"] == "dead_pid" for s in stale)

    def test_find_stale_live_pid_not_returned(self, workspace):
        import os as _os
        acquire_process_lock(self.RUN_ID, agent_id="seo-indexer", pid=_os.getpid(), ttl_seconds=9999, base_dir=workspace)
        stale = find_stale_process_locks(base_dir=workspace)
        assert not any(s["run_id"] == self.RUN_ID for s in stale)
        # Cleanup
        release_process_lock(self.RUN_ID, base_dir=workspace)


class TestProcessAlive:
    def test_zombie_pid_is_not_alive(self, monkeypatch):
        import builtins
        import io

        monkeypatch.setattr(os, "kill", lambda pid, sig: None)

        def fake_open(path, *args, **kwargs):
            if path == "/proc/123/stat":
                return io.StringIO("123 (python3) Z 1 1 1 0 -1 0 0 0")
            return builtins.open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fake_open)

        assert _is_process_alive(123) is False
