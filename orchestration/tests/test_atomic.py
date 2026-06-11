"""Tests for the shared crash-safe file writer module.

These tests guard against the corruption pattern observed on 2026-06-10
where a laptop restart mid-write left a task YAML truncated (an
unterminated quoted scalar in the last ``comments`` entry, which made
the file unloadable). The fix is ``orchestration._atomic``: write to a
tempfile in the same directory, ``fsync``, then ``os.replace`` into
place. A SIGKILL, OOM kill, or power loss mid-write can only ever
leave the *previous* good copy in place — never a truncated final file.

This file has two kinds of tests:

1. **Direct unit tests** of the ``atomic_write_yaml`` / ``atomic_write_json``
   / ``atomic_write_text`` helpers — exercise the write mechanics
   directly with simulated crashes.
2. **Call-site smoke tests** for every production code site that routes
   its writes through the shared helper. These tests monkey-patch the
   shared helper to raise, then assert that the destination file
   (which already exists) is left untouched. If a future refactor
   reverts any of these sites to ``open(path, "w")``, the test will
   fail loudly.
"""

import json
import os
import threading

import pytest
import yaml

from orchestration import _atomic
from orchestration._atomic import (
    atomic_write_json,
    atomic_write_text,
    atomic_write_yaml,
)


# ---------------------------------------------------------------------------
# Direct unit tests for the helpers
# ---------------------------------------------------------------------------


class TestAtomicWriteYaml:
    def test_writes_valid_yaml(self, tmp_path):
        path = str(tmp_path / "out.yaml")
        data = {"a": 1, "b": ["x", "y"], "c": {"nested": True}}
        atomic_write_yaml(path, data)
        with open(path) as f:
            loaded = yaml.safe_load(f)
        assert loaded == data

    def test_creates_parent_directory(self, tmp_path):
        path = str(tmp_path / "deep" / "nested" / "out.yaml")
        atomic_write_yaml(path, {"k": "v"})
        assert os.path.exists(path)

    def test_no_tempfile_left_after_success(self, tmp_path):
        path = str(tmp_path / "out.yaml")
        atomic_write_yaml(path, {"k": "v"})
        leftovers = [
            n for n in os.listdir(tmp_path)
            if n.startswith(".atomic.") and n.endswith(".tmp")
        ]
        assert leftovers == [], f"Tempfile leaked: {leftovers}"

    def test_overwrites_existing_file_atomically(self, tmp_path):
        # Write a known good first version, then overwrite it. The first
        # version must remain parseable until the very end of the second
        # write.
        path = str(tmp_path / "out.yaml")
        atomic_write_yaml(path, {"version": 1, "long": "x" * 1000})
        first_bytes = open(path, "rb").read()

        atomic_write_yaml(path, {"version": 2, "long": "y" * 1000})
        with open(path) as f:
            loaded = yaml.safe_load(f)
        assert loaded == {"version": 2, "long": "y" * 1000}
        # The first version's bytes are no longer there (it was replaced),
        # but the second version is on disk in full.
        assert first_bytes != open(path, "rb").read()

    def test_interrupted_write_leaves_prior_file_intact(self, tmp_path):
        # Simulate a SIGKILL mid-dump: the destination file already
        # contains valid data, then atomic_write_yaml is called with
        # a writer that raises before it can populate the temp file.
        path = str(tmp_path / "out.yaml")
        original = {"version": 1, "data": "ORIGINAL"}
        atomic_write_yaml(path, original)
        original_bytes = open(path, "rb").read()

        def boom(_handle):
            raise RuntimeError("simulated crash mid-dump")
        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            _atomic._atomic_write(path, boom)

        # Prior good copy must be untouched.
        assert open(path, "rb").read() == original_bytes
        with open(path) as f:
            assert yaml.safe_load(f) == original

    def test_interrupted_write_cleans_up_tempfile(self, tmp_path):
        path = str(tmp_path / "out.yaml")
        # First write succeeds so the directory exists.
        atomic_write_yaml(path, {"k": "v"})

        def boom(_handle):
            raise RuntimeError("simulated crash")
        with pytest.raises(RuntimeError):
            _atomic._atomic_write(path, boom)

        leftovers = [
            n for n in os.listdir(tmp_path)
            if n.startswith(".atomic.") and n.endswith(".tmp")
        ]
        assert leftovers == [], f"Tempfile leaked after failed write: {leftovers}"

    def test_keyboard_interrupt_is_also_clean(self, tmp_path):
        # A SIGINT (KeyboardInterrupt) is BaseException, not Exception.
        # The helper must clean up the temp file in that case too —
        # otherwise a Ctrl-C during a long write would leave a .tmp on
        # disk.
        path = str(tmp_path / "out.yaml")

        def boom(_handle):
            raise KeyboardInterrupt("simulated Ctrl-C")
        with pytest.raises(KeyboardInterrupt):
            _atomic._atomic_write(path, boom)

        leftovers = [
            n for n in os.listdir(tmp_path)
            if n.startswith(".atomic.") and n.endswith(".tmp")
        ]
        assert leftovers == [], f"Tempfile leaked on KeyboardInterrupt: {leftovers}"

    def test_handles_path_with_no_directory_component(self, tmp_path, monkeypatch):
        # Edge case: writing to a bare filename in cwd. The helper should
        # handle the case where os.path.dirname returns "".
        monkeypatch.chdir(tmp_path)
        atomic_write_yaml("bare.yaml", {"k": "v"})
        assert (tmp_path / "bare.yaml").exists()


class TestAtomicWriteJson:
    def test_writes_valid_json(self, tmp_path):
        path = str(tmp_path / "out.json")
        data = {"a": 1, "b": ["x", "y"], "c": {"nested": True}}
        atomic_write_json(path, data)
        with open(path) as f:
            assert json.load(f) == data

    def test_preserves_indent(self, tmp_path):
        path = str(tmp_path / "out.json")
        atomic_write_json(path, {"k": "v"}, indent=4)
        text = open(path).read()
        # indent=4 should produce a line like '    "k": "v"'
        assert '    "k": "v"' in text

    def test_default_ensure_ascii(self, tmp_path):
        # Default should match json.dump's default (ensure_ascii=True)
        # so non-ASCII content is escaped — keeps the helper a true
        # drop-in replacement.
        path = str(tmp_path / "out.json")
        atomic_write_json(path, {"name": "café"})
        text = open(path).read()
        assert "caf\\u00e9" in text

    def test_can_opt_into_ensure_ascii_false(self, tmp_path):
        path = str(tmp_path / "out.json")
        atomic_write_json(path, {"name": "café"}, ensure_ascii=False)
        text = open(path).read()
        assert "café" in text

    def test_interrupted_write_leaves_prior_file_intact(self, tmp_path):
        path = str(tmp_path / "out.json")
        original = {"version": 1}
        atomic_write_json(path, original)
        original_bytes = open(path, "rb").read()

        def boom(_handle):
            raise RuntimeError("simulated crash")
        with pytest.raises(RuntimeError):
            _atomic._atomic_write(path, boom)

        assert open(path, "rb").read() == original_bytes


class TestAtomicWriteText:
    def test_writes_text(self, tmp_path):
        path = str(tmp_path / "out.txt")
        atomic_write_text(path, "hello\nworld\n")
        assert open(path).read() == "hello\nworld\n"

    def test_interrupted_write_leaves_prior_file_intact(self, tmp_path):
        path = str(tmp_path / "out.txt")
        atomic_write_text(path, "first version")
        original_bytes = open(path, "rb").read()

        def boom(_handle):
            raise RuntimeError("simulated crash")
        with pytest.raises(RuntimeError):
            _atomic._atomic_write(path, boom)

        assert open(path, "rb").read() == original_bytes


# ---------------------------------------------------------------------------
# Call-site smoke tests: every production writer that claims to use the
# shared helper must actually use it. A refactor that reverts any of these
# to ``open(path, "w")`` will fail this test.
# ---------------------------------------------------------------------------


def _patched_atomic_write_to_boom(monkeypatch):
    """Patch the shared ``_atomic_write`` core to raise.

    This is the single chokepoint every helper (``atomic_write_yaml``,
    ``atomic_write_json``, ``atomic_write_text``) routes through, so
    patching it simulates a crash for *any* call site that uses the
    helpers. Patching the per-helper name (e.g.
    ``orchestration.agents.atomic_write_json``) doesn't work because
    Python resolves the function by the import in the *call-site*
    module's globals, which is the original function object.
    """
    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash mid-dump")
    monkeypatch.setattr(_atomic, "_atomic_write", boom)


def _list_atomic_tempfiles(directory):
    return [
        n for n in os.listdir(directory)
        if n.startswith(".atomic.") and n.endswith(".tmp")
    ]


class TestCallSiteTasks:
    """tasks._save_task must route through atomic_write_yaml."""

    def test_update_task_with_crash_leaves_file_intact(self, tmp_path, monkeypatch):
        # Build a minimal workstream/task in a tmp workspace.
        from orchestration.workstreams import create_workstream
        from orchestration.tasks import create_task, update_task, read_task

        states = {
            "To Do": ["In Progress", "Invalid"],
            "In Progress": ["Done", "Failed"],
            "Done": [],
            "Failed": ["To Do"],
            "Invalid": [],
        }
        ws = create_workstream(name="T", task_states=states, base_dir=str(tmp_path))
        task = create_task(ws.id, title="Original", base_dir=str(tmp_path))
        path = os.path.join(
            str(tmp_path), "workstreams", ws.id, "tasks", f"{task.id}.yaml",
        )
        original_bytes = open(path, "rb").read()
        assert original_bytes

        _patched_atomic_write_to_boom(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            update_task(task.id, description="X", base_dir=str(tmp_path))

        assert open(path, "rb").read() == original_bytes
        assert read_task(task.id, base_dir=str(tmp_path)).title == "Original"

        tasks_dir = os.path.dirname(path)
        assert _list_atomic_tempfiles(tasks_dir) == []


class TestCallSiteWorkstreams:
    """workstreams._write_workstream_to_root must route through atomic_write_yaml."""

    def test_write_workstream_to_root_with_crash_leaves_file_intact(self, tmp_path, monkeypatch):
        from orchestration.workstreams import (
            create_workstream,
            save_workstream,
            read_workstream,
            _ws_path,
            _state_base_dir,
        )

        states = {
            "To Do": ["In Progress", "Done"],
            "In Progress": ["Done"],
            "Done": [],
        }
        ws = create_workstream(name="Atomic", task_states=states, base_dir=str(tmp_path))
        # The first save (from create_workstream) already wrote the file
        # cleanly via the atomic helper, so capture those bytes.
        path = _ws_path(_state_base_dir(str(tmp_path)), ws.id)
        assert os.path.exists(path)
        original_bytes = open(path, "rb").read()
        original_loaded = read_workstream(ws.id, base_dir=str(tmp_path))

        _patched_atomic_write_to_boom(monkeypatch)

        # Trigger a re-save by mutating a field and calling save_workstream.
        original_loaded.name = "Renamed"
        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            save_workstream(original_loaded, base_dir=str(tmp_path))

        # File unchanged.
        assert open(path, "rb").read() == original_bytes
        # Reload shows the old name (the new write was never persisted).
        assert read_workstream(ws.id, base_dir=str(tmp_path)).name == "Atomic"
        # No temp files leaked.
        assert _list_atomic_tempfiles(os.path.dirname(path)) == []

    def test_write_workstream_env_atomic(self, tmp_path, monkeypatch):
        # The env writer is plain text, so we patch the underlying
        # _atomic_write to raise (which is what would happen if any
        # step of the rename failed).
        from orchestration.workstreams import (
            create_workstream,
            write_workstream_env,
            read_workstream_env,
            resolve_workstream_state_root,
        )
        from orchestration.workstreams import _ws_env_path

        states = {
            "To Do": ["In Progress", "Done"],
            "In Progress": ["Done"],
            "Done": [],
        }
        ws = create_workstream(
            name="AtomicEnv", task_states=states, base_dir=str(tmp_path),
        )
        write_workstream_env(ws.id, {"FOO": "bar"}, base_dir=str(tmp_path))
        env_path = _ws_env_path(
            resolve_workstream_state_root(ws.id, base_dir=str(tmp_path)),
            ws.id,
        )
        original = open(env_path).read()
        assert "FOO=bar" in original

        _patched_atomic_write_to_boom(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            write_workstream_env(ws.id, {"FOO": "baz"}, base_dir=str(tmp_path))

        # File unchanged.
        assert open(env_path).read() == original
        assert read_workstream_env(ws.id, base_dir=str(tmp_path)) == {"FOO": "bar"}


class TestCallSiteLocks:
    """locks.update_lock_pid must route through atomic_write_yaml."""

    def test_update_lock_pid_atomic(self, tmp_path, monkeypatch):
        from orchestration.workstreams import create_workstream
        from orchestration.tasks import create_task
        from orchestration.locks import acquire_lock, update_lock_pid

        states = {
            "To Do": ["In Progress", "Done"],
            "In Progress": ["Done"],
            "Done": [],
        }
        ws = create_workstream(name="AtomicLock", task_states=states, base_dir=str(tmp_path))
        task = create_task(ws.id, title="T", base_dir=str(tmp_path))
        acquire_lock(task.id, agent_id="test-agent", base_dir=str(tmp_path))
        from orchestration.locks import _lock_path_for_task
        lock_path = _lock_path_for_task(task.id, str(tmp_path))
        assert os.path.exists(lock_path), f"lock not at expected path: {lock_path}"
        original_bytes = open(lock_path, "rb").read()
        original_yaml = yaml.safe_load(original_bytes)
        assert original_yaml["agent_id"] == "test-agent"

        # Now break the atomic helper and try to update. The prior lock
        # file must remain parseable.
        _patched_atomic_write_to_boom(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            update_lock_pid(task.id, 99999, base_dir=str(tmp_path))

        # File unchanged.
        assert open(lock_path, "rb").read() == original_bytes
        # And the lock is still valid (not corrupted to empty).
        with open(lock_path) as f:
            roundtrip = yaml.safe_load(f)
        assert roundtrip["agent_id"] == "test-agent"
        assert roundtrip.get("subprocess_pid") != 99999
        # No temp files leaked.
        assert _list_atomic_tempfiles(os.path.dirname(lock_path)) == []


class TestCallSiteScheduler:
    """scheduler._save_state and _save_trigger_state must be atomic."""

    def test_save_state_atomic(self, tmp_path, monkeypatch):
        from orchestration.scheduler import _save_state, _state_path

        state_path = _state_path(str(tmp_path))
        _save_state({"last_tick": "2026-01-01"}, str(tmp_path))
        original_bytes = open(state_path, "rb").read()

        _patched_atomic_write_to_boom(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            _save_state({"last_tick": "2026-02-02"}, str(tmp_path))

        assert open(state_path, "rb").read() == original_bytes
        assert _list_atomic_tempfiles(os.path.dirname(state_path)) == []

    def test_save_trigger_state_atomic(self, tmp_path, monkeypatch):
        from orchestration.scheduler import _save_trigger_state, _trigger_state_path
        from orchestration.workstreams import create_workstream

        states = {
            "To Do": ["In Progress", "Done"],
            "In Progress": ["Done"],
            "Done": [],
        }
        ws = create_workstream(name="AtomicSched", task_states=states, base_dir=str(tmp_path))
        trigger_id = "trig-1"
        path = _trigger_state_path(ws.id, trigger_id, str(tmp_path))
        _save_trigger_state(ws.id, trigger_id, {"last_fired": "2026-01-01"}, str(tmp_path))
        original_bytes = open(path, "rb").read()

        _patched_atomic_write_to_boom(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            _save_trigger_state(ws.id, trigger_id, {"last_fired": "2026-02-02"}, str(tmp_path))

        assert open(path, "rb").read() == original_bytes
        assert _list_atomic_tempfiles(os.path.dirname(path)) == []


class TestCallSiteAgents:
    """agents._write_active_agents / _write_run_meta / _write_context_manifest."""

    def test_write_active_agents_atomic(self, tmp_path, monkeypatch):
        from orchestration.agents import _write_active_agents, _active_agents_path

        path = _active_agents_path(str(tmp_path))
        _write_active_agents(str(tmp_path), [{"run_id": "r1"}])
        original_bytes = open(path, "rb").read()

        _patched_atomic_write_to_boom(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            _write_active_agents(str(tmp_path), [{"run_id": "r2"}])

        assert open(path, "rb").read() == original_bytes
        with open(path) as f:
            assert yaml.safe_load(f) == {"runs": [{"run_id": "r1"}]}
        assert _list_atomic_tempfiles(os.path.dirname(path)) == []

    def test_write_run_meta_atomic(self, tmp_path, monkeypatch):
        from orchestration.agents import _write_run_meta, _agent_run_meta_path

        path = _agent_run_meta_path(str(tmp_path), "run-1")
        _write_run_meta(str(tmp_path), "run-1", {"status": "started"})
        original_bytes = open(path, "rb").read()

        # Break the atomic helper to simulate a crash during the write
        # of a different run id. The existing run-1 file must stay
        # intact.
        _patched_atomic_write_to_boom(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            _write_run_meta(str(tmp_path), "run-2", {"status": "started"})

        assert open(path, "rb").read() == original_bytes
        with open(path) as f:
            assert json.load(f) == {"status": "started"}

    def test_write_context_manifest_atomic(self, tmp_path, monkeypatch):
        from orchestration.agents import (
            _write_context_manifest,
            _agent_run_context_manifest_path,
        )

        # Establish a prior good manifest.
        _write_context_manifest(str(tmp_path), "run-1", {"ctx": "v1"})
        path = _agent_run_context_manifest_path(str(tmp_path), "run-1")
        original_bytes = open(path, "rb").read()

        _patched_atomic_write_to_boom(monkeypatch)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            _write_context_manifest(str(tmp_path), "run-1", {"ctx": "v2"})

        assert open(path, "rb").read() == original_bytes
        with open(path) as f:
            assert json.load(f) == {"ctx": "v1"}


class TestConcurrencyAndRobustness:
    """Misc robustness checks for the helper."""

    def test_concurrent_writes_dont_corrupt_each_other(self, tmp_path):
        # Two threads writing to the same path simultaneously should
        # still leave a parseable file behind (no half-and-half
        # interleave from a non-atomic open(path, "w")).
        path = str(tmp_path / "concurrent.yaml")
        errors = []

        def writer(idx):
            try:
                for i in range(20):
                    atomic_write_yaml(path, {"thread": idx, "i": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent writes raised: {errors}"
        # File is a valid YAML mapping.
        with open(path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert data["thread"] in {0, 1, 2, 3}
        assert isinstance(data["i"], int)
        # No leftover temp files.
        assert _list_atomic_tempfiles(tmp_path) == []
