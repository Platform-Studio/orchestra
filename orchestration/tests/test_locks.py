"""Tests for lock operations."""

import os
import pytest
import yaml
from datetime import datetime, timezone, timedelta

from orchestration.workstreams import create_workstream
from orchestration.tasks import create_task
from orchestration.locks import acquire_lock, release_lock, lock_status


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
