"""Tests for expired lock cleanup and retry logic."""

import os
import pytest
import yaml
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from orchestration.workstreams import create_workstream
from orchestration.tasks import create_task, read_task, _save_task
from orchestration.locks import (
    acquire_lock, lock_status, force_release_lock,
    find_expired_locks, find_orphaned_locks, update_lock_pid,
)
from orchestration.models import RetryConfig, Lock
from orchestration.retry import (
    handle_expired_lock, cleanup_expired_locks, manual_retry,
    cleanup_orphaned_locks,
    _compute_backoff, _get_retry_config, _find_last_agent,
    _is_process_alive, _is_our_process,
)


@pytest.fixture(autouse=True)
def _clear_persistence_root_env(monkeypatch):
    monkeypatch.setenv("WORKSTREAM_ROOT", "")
    monkeypatch.setenv("ARTIFACT_ROOT", "")
    monkeypatch.setenv("ARTICACT_ROOT", "")


@pytest.fixture
def ws(workspace):
    """Create a workstream with default states (includes pending, in_progress, completed, failed)."""
    return create_workstream(name="Retry Test WS", base_dir=workspace)


@pytest.fixture
def ws_with_retry(workspace):
    """Create a workstream with custom retry config."""
    ws = create_workstream(name="Retry Config WS", base_dir=workspace)
    ws.retry = RetryConfig(max_retries=2, backoff="linear", base_seconds=30)
    from orchestration.workstreams import save_workstream
    save_workstream(ws, workspace)
    return ws


@pytest.fixture
def task(workspace, ws):
    return create_task(ws.id, title="Retry Test Task", base_dir=workspace)


def _make_expired_lock(task_id, workspace, agent_id="test-agent", pid=99999, subprocess_pid=99998):
    """Create an expired lock file for a task."""
    from orchestration.tasks import _find_task_file
    result = _find_task_file(task_id, workspace)
    assert result is not None
    _, task_file = result
    lock_path = task_file + ".lock"

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    lock_data = {
        "agent_id": agent_id,
        "acquired_at": (past - timedelta(minutes=15)).isoformat(),
        "expires_at": past.isoformat(),
        "pid": pid,
        "subprocess_pid": subprocess_pid,
    }
    with open(lock_path, "w") as f:
        yaml.dump(lock_data, f)
    return lock_path


# ── Lock PID storage ─────────────────────────────────────────────

class TestLockPID:
    def test_acquire_stores_pid(self, workspace, task):
        lock = acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        assert lock.pid == os.getpid()

    def test_acquire_with_custom_pid(self, workspace, task):
        lock = acquire_lock(task.id, agent_id="agent-1", pid=12345, base_dir=workspace)
        assert lock.pid == 12345

    def test_update_lock_pid(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        update_lock_pid(task.id, subprocess_pid=54321, base_dir=workspace)
        lock = lock_status(task.id, base_dir=workspace)
        assert lock.subprocess_pid == 54321

    def test_lock_to_dict_includes_pids(self, workspace, task):
        lock = acquire_lock(task.id, agent_id="agent-1", pid=111, base_dir=workspace)
        update_lock_pid(task.id, subprocess_pid=222, base_dir=workspace)
        lock = lock_status(task.id, base_dir=workspace)
        d = lock.to_dict()
        assert d["pid"] == 111
        assert d["subprocess_pid"] == 222

    def test_lock_from_dict_handles_missing_pids(self):
        """Old lock files without pid fields should still parse."""
        data = {
            "agent_id": "old-agent",
            "acquired_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        }
        lock = Lock.from_dict(data)
        assert lock.pid is None
        assert lock.subprocess_pid is None
        assert lock.agent_id == "old-agent"


class TestProcessDetection:
    @patch("subprocess.run")
    def test_is_our_process_accepts_cline_cli_process(self, mock_run):
        mock_run.return_value = MagicMock(stdout="node /usr/lib/node_modules/cline/dist/cli.mjs")

        assert _is_our_process(12345) is True

    @patch("subprocess.run")
    def test_is_our_process_accepts_copilot_cli_process(self, mock_run):
        mock_run.return_value = MagicMock(stdout="/opt/homebrew/bin/gh copilot -p task")

        assert _is_our_process(12345) is True

    @patch("subprocess.run")
    def test_is_our_process_rejects_unrelated_node_process(self, mock_run):
        mock_run.return_value = MagicMock(stdout="node server.js")

        assert _is_our_process(12345) is False


# ── Find expired locks ───────────────────────────────────────────

class TestFindExpiredLocks:
    def test_no_locks(self, workspace, ws):
        create_task(ws.id, title="T", base_dir=workspace)
        expired = find_expired_locks(workspace)
        assert expired == []

    def test_active_lock_not_found(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        expired = find_expired_locks(workspace)
        assert expired == []

    def test_expired_lock_found(self, workspace, task):
        _make_expired_lock(task.id, workspace)
        expired = find_expired_locks(workspace)
        assert len(expired) == 1
        assert expired[0]["task_id"] == task.id
        assert expired[0]["lock"].is_expired()

    def test_multiple_expired_locks(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        _make_expired_lock(t1.id, workspace)
        _make_expired_lock(t2.id, workspace)
        expired = find_expired_locks(workspace)
        assert len(expired) == 2

    def test_finds_expired_lock_in_working_directory_descendant(self, workspace, tmp_path):
        working_root = tmp_path / "mounted_repo"
        working_root.mkdir()

        parent = create_workstream(name="Mounted Parent", base_dir=workspace)
        parent.working_directory = str(working_root)
        from orchestration.workstreams import save_workstream
        save_workstream(parent, base_dir=workspace)

        mounted_child = create_workstream(name="Mounted Child", parent_id=parent.id, base_dir=workspace)
        task = create_task(mounted_child.id, title="Mounted Task", base_dir=workspace)

        _make_expired_lock(task.id, workspace)

        expired = find_expired_locks(workspace)
        assert len(expired) == 1
        assert expired[0]["task_id"] == task.id
        assert expired[0]["workstream_id"] == mounted_child.id


class TestFindOrphanedLocks:
    @patch("orchestration.locks._is_process_alive", return_value=False)
    def test_finds_nonexpired_lock_with_dead_pid(self, _mock_alive, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)
        orphaned = find_orphaned_locks(workspace)
        assert len(orphaned) == 1
        assert orphaned[0]["task_id"] == task.id

    @patch("orchestration.locks._is_process_alive", return_value=True)
    def test_skips_lock_with_live_pid(self, _mock_alive, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)
        orphaned = find_orphaned_locks(workspace)
        assert orphaned == []

    @patch("orchestration.locks._is_process_alive")
    def test_skips_lock_with_dead_parent_but_live_subprocess(self, mock_alive, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)
        update_lock_pid(task.id, subprocess_pid=434343, base_dir=workspace)
        mock_alive.side_effect = lambda pid: pid == 434343

        orphaned = find_orphaned_locks(workspace)

        assert orphaned == []

    @patch("orchestration.locks._is_process_alive")
    def test_finds_lock_when_parent_and_subprocess_dead(self, mock_alive, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)
        update_lock_pid(task.id, subprocess_pid=434343, base_dir=workspace)
        mock_alive.return_value = False

        orphaned = find_orphaned_locks(workspace)

        assert len(orphaned) == 1
        assert orphaned[0]["task_id"] == task.id

    @patch("orchestration.locks._is_process_alive", return_value=False)
    def test_skips_expired_locks(self, _mock_alive, workspace, task):
        _make_expired_lock(task.id, workspace, pid=424242)
        orphaned = find_orphaned_locks(workspace)
        assert orphaned == []

    def test_skips_lock_when_parent_dead_but_subprocess_alive(self, workspace, task, monkeypatch):
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)
        update_lock_pid(task.id, subprocess_pid=525252, base_dir=workspace)

        monkeypatch.setattr(
            "orchestration.locks._is_process_alive",
            lambda pid: pid == 525252,
        )

        orphaned = find_orphaned_locks(workspace)
        assert orphaned == []


# ── Force release lock ───────────────────────────────────────────

class TestForceReleaseLock:
    def test_force_release(self, workspace, task):
        acquire_lock(task.id, agent_id="agent-1", base_dir=workspace)
        result = force_release_lock(task.id, base_dir=workspace)
        assert result is True
        assert lock_status(task.id, base_dir=workspace) is None

    def test_force_release_no_lock(self, workspace, task):
        result = force_release_lock(task.id, base_dir=workspace)
        assert result is False


# ── Backoff computation ──────────────────────────────────────────

class TestBackoff:
    def test_exponential_backoff(self):
        cfg = RetryConfig(base_seconds=60, backoff="exponential")
        assert _compute_backoff(cfg, 0) == 60
        assert _compute_backoff(cfg, 1) == 120
        assert _compute_backoff(cfg, 2) == 240

    def test_linear_backoff(self):
        cfg = RetryConfig(base_seconds=30, backoff="linear")
        assert _compute_backoff(cfg, 0) == 30
        assert _compute_backoff(cfg, 1) == 60
        assert _compute_backoff(cfg, 2) == 90

    def test_fixed_backoff(self):
        cfg = RetryConfig(base_seconds=45, backoff="fixed")
        assert _compute_backoff(cfg, 0) == 45
        assert _compute_backoff(cfg, 1) == 45
        assert _compute_backoff(cfg, 2) == 45


# ── Retry config resolution ─────────────────────────────────────

class TestRetryConfigResolution:
    def test_task_config_wins(self, workspace, ws):
        task = create_task(ws.id, title="T", retry={"max_retries": 5}, base_dir=workspace)
        task_obj = read_task(task.id, workspace)
        cfg = _get_retry_config(task_obj, ws)
        assert cfg.max_retries == 5

    def test_workstream_config_fallback(self, workspace, ws_with_retry):
        task = create_task(ws_with_retry.id, title="T", base_dir=workspace)
        task_obj = read_task(task.id, workspace)
        from orchestration.workstreams import read_workstream
        ws = read_workstream(ws_with_retry.id, workspace)
        cfg = _get_retry_config(task_obj, ws)
        assert cfg.max_retries == 2
        assert cfg.backoff == "linear"

    def test_default_fallback(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        task_obj = read_task(task.id, workspace)
        cfg = _get_retry_config(task_obj, ws)
        assert cfg.max_retries == 3
        assert cfg.backoff == "exponential"
        assert cfg.base_seconds == 60


# ── Find last agent ─────────────────────────────────────────────

class TestFindLastAgent:
    def test_finds_agent_from_audit(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        task.add_audit("agent_started", "Agent 'sorter' started processing")
        _save_task(task, workspace)
        task = read_task(task.id, workspace)
        action = _find_last_agent(task)
        assert action == {"type": "run_agent", "agent": "sorter"}

    def test_no_agent_returns_none(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        action = _find_last_agent(task)
        assert action is None


# ── Handle expired lock ──────────────────────────────────────────

class TestHandleExpiredLock:
    @patch("orchestration.retry._kill_process", return_value=None)
    def test_first_failure_schedules_retry(self, mock_kill, workspace, task, ws):
        _make_expired_lock(task.id, workspace)
        # Move task to in_progress first
        from orchestration.tasks import update_task
        update_task(task.id, status="in_progress", base_dir=workspace)

        expired = find_expired_locks(workspace)
        lock = expired[0]["lock"]

        result = handle_expired_lock(task.id, ws.id, lock, workspace)

        assert "retry_scheduled" in result["actions"]
        assert result["retry_count"] == 1

        # Verify task state
        t = read_task(task.id, workspace)
        assert t.status == "pending"
        assert t.retry_count == 1
        assert t.scheduled_at is not None
        assert t.last_failure_at is not None

        # Verify audit trail
        audit_types = [a.type for a in t.audit]
        assert "lock_expired" in audit_types
        assert "retry_scheduled" in audit_types

    @patch("orchestration.retry._kill_process", return_value=None)
    def test_max_retries_moves_to_failed(self, mock_kill, workspace, task, ws):
        _make_expired_lock(task.id, workspace)

        # Set retry count to max-1
        t = read_task(task.id, workspace)
        t.status = "in_progress"
        t.retry_count = 2  # default max is 3
        _save_task(t, workspace)

        expired = find_expired_locks(workspace)
        lock = expired[0]["lock"]

        result = handle_expired_lock(task.id, ws.id, lock, workspace)

        assert "max_retries_exceeded" in result["actions"]

        t = read_task(task.id, workspace)
        assert t.status == "failed"
        assert t.retry_count == 3
        audit_types = [a.type for a in t.audit]
        assert "max_retries_exceeded" in audit_types

    @patch("orchestration.retry._kill_process", return_value=None)
    def test_lock_removed_after_handling(self, mock_kill, workspace, task, ws):
        lock_path = _make_expired_lock(task.id, workspace)
        expired = find_expired_locks(workspace)
        lock = expired[0]["lock"]

        handle_expired_lock(task.id, ws.id, lock, workspace)

        assert not os.path.exists(lock_path)

    @patch("orchestration.retry._kill_process", return_value="SIGTERM")
    def test_process_killed_logged(self, mock_kill, workspace, task, ws):
        _make_expired_lock(task.id, workspace, subprocess_pid=12345)
        expired = find_expired_locks(workspace)
        lock = expired[0]["lock"]

        result = handle_expired_lock(task.id, ws.id, lock, workspace)

        assert "process_killed" in result["actions"]
        t = read_task(task.id, workspace)
        audit_types = [a.type for a in t.audit]
        assert "process_killed" in audit_types

    @patch("orchestration.retry._kill_process", return_value=None)
    def test_workspace_audit_logged(self, mock_kill, workspace, task, ws):
        _make_expired_lock(task.id, workspace)
        expired = find_expired_locks(workspace)
        lock = expired[0]["lock"]

        handle_expired_lock(task.id, ws.id, lock, workspace)

        # Check workspace audit
        from orchestration.workspace_audit import get_audit_log
        entries = get_audit_log(workspace, limit=10)
        types = [e["type"] for e in entries]
        assert "lock_expired" in types

    @patch("orchestration.retry._kill_process", return_value=None)
    def test_custom_retry_config(self, mock_kill, workspace, ws_with_retry):
        task = create_task(ws_with_retry.id, title="T", base_dir=workspace)
        t = read_task(task.id, workspace)
        t.status = "in_progress"
        _save_task(t, workspace)
        _make_expired_lock(task.id, workspace)

        expired = find_expired_locks(workspace)
        lock = expired[0]["lock"]

        from orchestration.workstreams import read_workstream
        ws = read_workstream(ws_with_retry.id, workspace)

        result = handle_expired_lock(task.id, ws.id, lock, workspace)

        assert "retry_scheduled" in result["actions"]
        t = read_task(task.id, workspace)
        # linear backoff, base_seconds=30, retry_count=1 → 30*(1+1)=60s?
        # Actually _compute_backoff linear: base * (retry_count + 1) where retry_count is 1
        # But retry_count was 0 before, incremented to 1, backoff computed with 1
        # linear: 30 * (1 + 1) = 60
        assert t.retry_count == 1

    @patch("orchestration.retry._kill_process", return_value=None)
    def test_handles_unreadable_task(self, mock_kill, workspace, ws):
        """If the task YAML is corrupt, just clean up the lock."""
        task = create_task(ws.id, title="T", base_dir=workspace)
        lock_path = _make_expired_lock(task.id, workspace)

        # Corrupt the task file
        from orchestration.tasks import _find_task_file
        _, task_file = _find_task_file(task.id, workspace)
        with open(task_file, "w") as f:
            f.write("{{bad yaml::")

        expired = find_expired_locks(workspace)
        lock = expired[0]["lock"]

        result = handle_expired_lock(task.id, ws.id, lock, workspace)
        assert "lock_cleaned_unreadable_task" in result["actions"]
        assert not os.path.exists(lock_path)


# ── Cleanup expired locks (batch) ────────────────────────────────

class TestCleanupExpiredLocks:
    @patch("orchestration.retry._kill_process", return_value=None)
    def test_cleans_multiple_locks(self, mock_kill, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        _make_expired_lock(t1.id, workspace)
        _make_expired_lock(t2.id, workspace)

        results = cleanup_expired_locks(workspace)
        assert len(results) == 2
        assert all("lock_expired" in r["actions"] for r in results)

    def test_no_expired_locks(self, workspace, ws):
        create_task(ws.id, title="T", base_dir=workspace)
        results = cleanup_expired_locks(workspace)
        assert results == []


class TestCleanupOrphanedLocks:
    @patch("orchestration.locks._is_process_alive", return_value=False)
    def test_cleans_orphaned_lock(self, _mock_alive, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)

        results = cleanup_orphaned_locks(workspace)
        assert len(results) == 1
        assert results[0]["task_id"] == task.id
        assert results[0]["released"] is True
        assert lock_status(task.id, base_dir=workspace) is None

    @patch("orchestration.locks._is_process_alive", return_value=True)
    def test_no_orphaned_locks_when_pid_alive(self, _mock_alive, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)

        results = cleanup_orphaned_locks(workspace)
        assert results == []
        assert lock_status(task.id, base_dir=workspace) is not None

    def test_keeps_lock_when_scheduler_pid_dead_but_subprocess_alive(self, workspace, ws, monkeypatch):
        task = create_task(ws.id, title="T", base_dir=workspace)
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)
        update_lock_pid(task.id, subprocess_pid=525252, base_dir=workspace)

        monkeypatch.setattr(
            "orchestration.locks._is_process_alive",
            lambda pid: pid == 525252,
        )

        results = cleanup_orphaned_locks(workspace)
        assert results == []
        assert lock_status(task.id, base_dir=workspace) is not None


# ── Manual retry ─────────────────────────────────────────────────

class TestManualRetry:
    def test_retry_failed_task(self, workspace, task, ws):
        # Move to failed
        from orchestration.tasks import update_task
        update_task(task.id, status="in_progress", base_dir=workspace)
        update_task(task.id, status="failed", base_dir=workspace)

        # Set retry count
        t = read_task(task.id, workspace)
        t.retry_count = 3
        t.last_failure_at = datetime.now(timezone.utc).isoformat()
        _save_task(t, workspace)

        result = manual_retry(task.id, workspace)
        assert result["status"] == "pending"
        assert result["retry_count"] == 0

        t = read_task(task.id, workspace)
        assert t.status == "pending"
        assert t.retry_count == 0
        assert t.last_failure_at is None

    def test_retry_non_failed_raises(self, workspace, task, ws):
        with pytest.raises(ValueError, match="not 'failed'"):
            manual_retry(task.id, workspace)

    def test_retry_logs_to_workspace_audit(self, workspace, task, ws):
        from orchestration.tasks import update_task
        update_task(task.id, status="in_progress", base_dir=workspace)
        update_task(task.id, status="failed", base_dir=workspace)

        manual_retry(task.id, workspace)

        from orchestration.workspace_audit import get_audit_log
        entries = get_audit_log(workspace, limit=10)
        types = [e["type"] for e in entries]
        assert "manual_retry" in types


# ── Task retry_count field ───────────────────────────────────────

class TestTaskRetryCount:
    def test_default_retry_count(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert task.retry_count == 0
        assert task.last_failure_at is None

    def test_retry_count_persists(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        task.retry_count = 2
        task.last_failure_at = "2026-04-15T10:00:00+00:00"
        _save_task(task, workspace)

        t = read_task(task.id, workspace)
        assert t.retry_count == 2
        assert t.last_failure_at == "2026-04-15T10:00:00+00:00"

    def test_retry_count_in_to_dict(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        # Zero retry_count should not appear
        d = task.to_dict()
        assert "retry_count" not in d

        task.retry_count = 1
        d = task.to_dict()
        assert d["retry_count"] == 1

    def test_old_task_without_retry_count_loads(self, workspace, ws):
        """Tasks saved before retry_count was added should still load."""
        task = create_task(ws.id, title="T", base_dir=workspace)
        # Manually write a task without retry_count
        from orchestration.tasks import _find_task_file
        _, task_file = _find_task_file(task.id, workspace)
        with open(task_file) as f:
            data = yaml.safe_load(f)
        data.pop("retry_count", None)
        data.pop("last_failure_at", None)
        with open(task_file, "w") as f:
            yaml.dump(data, f)

        t = read_task(task.id, workspace)
        assert t.retry_count == 0
        assert t.last_failure_at is None


# ── Scheduler integration ────────────────────────────────────────

class TestSchedulerExpiredLockPhase:
    @patch("orchestration.retry._kill_process", return_value=None)
    def test_tick_cleans_expired_locks(self, mock_kill, workspace, ws):
        """The scheduler tick should include expired lock cleanup."""
        task = create_task(ws.id, title="T", base_dir=workspace)
        t = read_task(task.id, workspace)
        t.status = "in_progress"
        _save_task(t, workspace)
        _make_expired_lock(task.id, workspace)

        from orchestration.scheduler import tick
        results = tick(workspace)

        assert "expired_locks_cleaned" in results
        assert len(results["expired_locks_cleaned"]) == 1

        # Verify lock was removed
        assert lock_status(task.id, base_dir=workspace) is None

    @patch("orchestration.retry._kill_process", return_value=None)
    def test_tick_with_no_expired_locks(self, mock_kill, workspace, ws):
        create_task(ws.id, title="T", base_dir=workspace)

        from orchestration.scheduler import tick
        results = tick(workspace)

        assert results["expired_locks_cleaned"] == []

    @patch("orchestration.retry._kill_process", return_value=None)
    @patch("orchestration.locks._is_process_alive", return_value=False)
    def test_tick_cleans_orphaned_locks(self, _mock_alive, mock_kill, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        acquire_lock(task.id, agent_id="agent-1", ttl_seconds=600, pid=424242, base_dir=workspace)

        from orchestration.scheduler import tick
        results = tick(workspace)

        assert "orphaned_locks_cleaned" in results
        assert len(results["orphaned_locks_cleaned"]) == 1
        assert results["orphaned_locks_cleaned"][0]["task_id"] == task.id
        assert lock_status(task.id, base_dir=workspace) is None


# ── Process alive check ─────────────────────────────────────────

class TestProcessAlive:
    def test_current_process_alive(self):
        assert _is_process_alive(os.getpid()) is True

    def test_nonexistent_pid(self):
        assert _is_process_alive(99999999) is False

    def test_none_pid(self):
        assert _is_process_alive(None) is False
