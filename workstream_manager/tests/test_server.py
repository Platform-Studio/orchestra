"""Tests for workstream_manager.server — HTTP bridge to orchestration modules.

Strategy: spin up a real test server on a random port with all orchestration
imports mocked, then make real HTTP requests.  This tests routing, request
parsing, response formatting, and error mapping end-to-end.
"""

import json
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


# ── Helpers ──────────────────────────────────────────────────────

def _fake_workstream(**kwargs):
    defaults = {
        "id": "ws-1",
        "name": "Test WS",
        "description": "desc",
        "parent_id": None,
        "mounted_workspace_path": "/tmp/workspace",
        "task_states": {"backlog": ["doing"], "doing": ["done"], "done": []},
        "paused": False,
        "retry": None,
    }
    defaults.update(kwargs)
    obj = SimpleNamespace(**defaults)
    obj.to_dict = lambda: {k: getattr(obj, k) for k in defaults}
    return obj


def _fake_task(**kwargs):
    defaults = {
        "id": "task-1",
        "workstream_id": "ws-1",
        "title": "Test Task",
        "description": "desc",
        "status": "backlog",
        "tags": ["a"],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    defaults.update(kwargs)
    obj = SimpleNamespace(**defaults)
    obj.to_dict = lambda: {k: getattr(obj, k) for k in defaults}
    return obj


def _fake_lock(expired=False, **kwargs):
    defaults = {
        "task_id": "task-1",
        "agent_id": "agent-1",
        "acquired_at": "2026-01-01T00:00:00",
        "ttl_seconds": 900,
    }
    defaults.update(kwargs)
    obj = SimpleNamespace(**defaults)
    obj.to_dict = lambda: {k: getattr(obj, k) for k in defaults}
    obj.is_expired = lambda: expired
    return obj


def _fake_trigger(**kwargs):
    defaults = {
        "id": "trig-1",
        "workstream_id": "ws-1",
        "action": "run_agent",
        "on_state": "doing",
        "on_schedule": None,
        "filter": None,
        "agent": "test_agent",
        "command": None,
    }
    defaults.update(kwargs)
    obj = SimpleNamespace(**defaults)
    obj.to_dict = lambda: {k: getattr(obj, k) for k in defaults}
    return obj


# ── Patched module names ────────────────────────────────────────

_PATCHES = {
    "list_workstreams":  "workstream_manager.server.list_workstreams",
    "read_workstream":   "workstream_manager.server.read_workstream",
    "create_workstream": "workstream_manager.server.create_workstream",
    "find_workstreams":  "workstream_manager.server.find_workstreams",
    "save_workstream":   "workstream_manager.server.save_workstream",
    "list_workstream_hierarchy_env": "workstream_manager.server.list_workstream_hierarchy_env",
    "list_effective_workstream_env": "workstream_manager.server.list_effective_workstream_env",
    "resolve_workstream_workspace": "workstream_manager.server.resolve_workstream_workspace",
    "create_task":       "workstream_manager.server.create_task",
    "read_task":         "workstream_manager.server.read_task",
    "update_task":       "workstream_manager.server.update_task",
    "list_tasks":        "workstream_manager.server.list_tasks",
    "comment_task":      "workstream_manager.server.comment_task",
    "archive_task":      "workstream_manager.server.archive_task",
    "get_audit":         "workstream_manager.server.get_audit",
    "clear_schedule":    "workstream_manager.server.clear_schedule",
    "move_task_before":  "workstream_manager.server.move_task_before",
    "move_task_after":   "workstream_manager.server.move_task_after",
    "move_task_to_index": "workstream_manager.server.move_task_to_index",
    "acquire_lock":      "workstream_manager.server.acquire_lock",
    "release_lock":      "workstream_manager.server.release_lock",
    "lock_status":       "workstream_manager.server.lock_status",
    "create_trigger":    "workstream_manager.server.create_trigger",
    "list_triggers":     "workstream_manager.server.list_triggers",
    "delete_trigger":    "workstream_manager.server.delete_trigger",
    "scheduler_status":  "workstream_manager.server.scheduler_status",
}


# ── Server fixture ──────────────────────────────────────────────

@pytest.fixture()
def api(tmp_path):
    """Start a test server with all orchestration functions mocked.

    Yields a helper object with .get() / .post() / .mocks dict.
    """
    patches = {name: patch(target) for name, target in _PATCHES.items()}
    mocks = {name: p.start() for name, p in patches.items()}

    # Default return values for list endpoints (so board etc. don't crash)
    mocks["list_workstreams"].return_value = []
    mocks["list_tasks"].return_value = []
    mocks["list_triggers"].return_value = []
    mocks["scheduler_status"].return_value = {"running": False, "last_tick": None}
    mocks["list_workstream_hierarchy_env"].return_value = []
    mocks["list_effective_workstream_env"].return_value = {}
    mocks["resolve_workstream_workspace"].return_value = "/tmp/workspace"

    # Default for lock_status (used by board/poll)
    mocks["lock_status"].return_value = None

    from workstream_manager.server import Handler, ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class API:
        base = f"http://127.0.0.1:{port}"

        @staticmethod
        def get(path):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
            try:
                resp = urllib.request.urlopen(req)
                return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        @staticmethod
        def post(path, body=None):
            data = json.dumps(body).encode() if body else b""
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                resp = urllib.request.urlopen(req)
                return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

    api_obj = API()
    api_obj.mocks = mocks

    yield api_obj

    server.shutdown()
    for p in patches.values():
        p.stop()


# ── Routing tests ───────────────────────────────────────────────

class TestRouting:
    def test_short_api_path_returns_400(self, api):
        code, body = api.get("/api/workstream")
        assert code == 400
        assert "API path must be" in body["message"]

    def test_unknown_concept_returns_400(self, api):
        code, body = api.get("/api/bogus/list")
        assert code == 400
        assert "Unknown concept" in body["message"]

    def test_unknown_workstream_method(self, api):
        code, body = api.get("/api/workstream/nope")
        assert code == 400
        assert "Unknown workstream method" in body["message"]

    def test_unknown_task_method(self, api):
        code, body = api.get("/api/task/nope")
        assert code == 400

    def test_unknown_lock_method(self, api):
        code, body = api.get("/api/lock/nope")
        assert code == 400

    def test_unknown_trigger_method(self, api):
        code, body = api.get("/api/trigger/nope")
        assert code == 400

    def test_unknown_scheduler_method(self, api):
        code, body = api.get("/api/scheduler/nope")
        assert code == 400


# ── Error mapping tests ─────────────────────────────────────────

class TestErrorMapping:
    def test_file_not_found_returns_404(self, api):
        api.mocks["read_workstream"].side_effect = FileNotFoundError("not found")
        code, body = api.get("/api/workstream/read/ws-missing")
        assert code == 404
        assert body["code"] == "NOT_FOUND"

    def test_value_error_returns_400(self, api):
        api.mocks["read_task"].side_effect = ValueError("bad value")
        code, body = api.get("/api/task/read/task-bad")
        assert code == 400
        assert body["code"] == "INVALID"

    def test_runtime_error_returns_409(self, api):
        api.mocks["acquire_lock"].side_effect = RuntimeError("already locked")
        code, body = api.post("/api/lock/acquire/task-1", {"agent": "a"})
        assert code == 409
        assert body["code"] == "CONFLICT"

    def test_generic_exception_returns_500(self, api):
        api.mocks["read_task"].side_effect = Exception("boom")
        code, body = api.get("/api/task/read/task-x")
        assert code == 500
        assert body["code"] == "ERROR"

    def test_invalid_json_body_returns_400(self, api):
        data = b"not-json"
        req = urllib.request.Request(
            f"{api.base}/api/task/create/ws-1",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req)
            code, body = resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            code, body = e.code, json.loads(e.read())
        assert code == 400
        assert "Invalid JSON" in body["message"]


# ── Board endpoint ──────────────────────────────────────────────

class TestBoard:
    def test_board_returns_workstream_and_tasks(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        api.mocks["read_workstream"].return_value = ws
        api.mocks["list_tasks"].return_value = [t]
        api.mocks["lock_status"].return_value = None

        code, body = api.get("/api/board/ws-1")
        assert code == 200
        data = body["data"]
        assert data["workstream"]["id"] == "ws-1"
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["lock"]["locked"] is False

    def test_board_includes_active_lock(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        lock = _fake_lock(expired=False)
        api.mocks["read_workstream"].return_value = ws
        api.mocks["list_tasks"].return_value = [t]
        api.mocks["lock_status"].return_value = lock

        code, body = api.get("/api/board/ws-1")
        assert code == 200
        task_data = body["data"]["tasks"][0]
        assert task_data["lock"]["locked"] is True
        assert task_data["lock"]["agent_id"] == "agent-1"

    def test_board_ignores_expired_lock(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        lock = _fake_lock(expired=True)
        api.mocks["read_workstream"].return_value = ws
        api.mocks["list_tasks"].return_value = [t]
        api.mocks["lock_status"].return_value = lock

        code, body = api.get("/api/board/ws-1")
        assert body["data"]["tasks"][0]["lock"]["locked"] is False

    def test_board_missing_id(self, api):
        # /api/board/ with no ID → the route gets concept=board, method="" (empty)
        # Actually URL parsing: /api/board → parts = ["board"] → len <2 → 400
        code, body = api.get("/api/board")
        assert code == 400


# ── Workstream handler ──────────────────────────────────────────

class TestWorkstream:
    def test_list(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        code, body = api.get("/api/workstream/list")
        assert code == 200
        assert len(body["data"]) == 1
        assert body["data"][0]["name"] == "Test WS"

    def test_counts(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["list_tasks"].return_value = [_fake_task(), _fake_task()]
        code, body = api.get("/api/workstream/counts")
        assert code == 200
        assert body["data"]["ws-1"] == 2

    def test_read(self, api):
        ws = _fake_workstream(id="ws-42")
        api.mocks["read_workstream"].return_value = ws
        code, body = api.get("/api/workstream/read/ws-42")
        assert code == 200
        assert body["data"]["id"] == "ws-42"

    def test_info_includes_task_states(self, api):
        ws = _fake_workstream(id="ws-42")
        api.mocks["read_workstream"].return_value = ws
        code, body = api.get("/api/workstream/info/ws-42")
        assert code == 200
        assert body["data"]["id"] == "ws-42"
        assert body["data"]["task_states"] == ws.task_states

    def test_find(self, api):
        ws = _fake_workstream()
        api.mocks["find_workstreams"].return_value = [ws]
        code, body = api.get("/api/workstream/find?query=test")
        assert code == 200
        assert len(body["data"]) == 1
        api.mocks["find_workstreams"].assert_called_once()

    def test_create(self, api):
        ws = _fake_workstream(name="New")
        api.mocks["create_workstream"].return_value = ws
        code, body = api.post("/api/workstream/create", {
            "name": "New",
            "description": "a new one",
            "states": '{"todo": ["done"], "done": []}',
        })
        assert code == 200
        call_kw = api.mocks["create_workstream"].call_args
        assert call_kw.kwargs["name"] == "New" or call_kw[1]["name"] == "New"

    def test_pause(self, api):
        ws = _fake_workstream()
        api.mocks["read_workstream"].return_value = ws
        code, body = api.post("/api/workstream/pause/ws-1")
        assert code == 200
        assert ws.paused is True
        api.mocks["save_workstream"].assert_called_once()

    def test_resume(self, api):
        ws = _fake_workstream(paused=True)
        api.mocks["read_workstream"].return_value = ws
        code, body = api.post("/api/workstream/resume/ws-1")
        assert code == 200
        assert ws.paused is False
        api.mocks["save_workstream"].assert_called_once()


# ── Task handler ────────────────────────────────────────────────

class TestTask:
    def test_list(self, api):
        api.mocks["list_tasks"].return_value = [_fake_task()]
        code, body = api.get("/api/task/list/ws-1")
        assert code == 200
        assert len(body["data"]) == 1

    def test_list_with_filters(self, api):
        api.mocks["list_tasks"].return_value = []
        code, body = api.get("/api/task/list/ws-1?status=doing&tags=a,b")
        assert code == 200
        call_kw = api.mocks["list_tasks"].call_args
        assert call_kw.kwargs.get("status") == "doing" or call_kw[1].get("status") == "doing"

    def test_read(self, api):
        api.mocks["read_task"].return_value = _fake_task(id="t-99")
        code, body = api.get("/api/task/read/t-99")
        assert code == 200
        assert body["data"]["id"] == "t-99"

    def test_create(self, api):
        api.mocks["create_task"].return_value = _fake_task(title="Apple")
        code, body = api.post("/api/task/create/ws-1", {
            "title": "Apple",
            "description": "A fruit",
            "tags": "red,crunchy",
        })
        assert code == 200
        assert body["data"]["title"] == "Apple"

    def test_create_with_schedule(self, api):
        api.mocks["create_task"].return_value = _fake_task()
        code, body = api.post("/api/task/create/ws-1", {
            "title": "Scheduled",
            "scheduled-at": "2026-12-01T00:00:00",
            "scheduled-action": '{"action": "run_agent", "agent": "test"}',
        })
        assert code == 200
        call_kw = api.mocks["create_task"].call_args
        kw = call_kw.kwargs if call_kw.kwargs else call_kw[1]
        assert kw["scheduled_at"] == "2026-12-01T00:00:00"

    def test_update_status(self, api):
        api.mocks["update_task"].return_value = _fake_task(status="doing")
        code, body = api.post("/api/task/update/t-1", {"status": "doing"})
        assert code == 200
        assert body["data"]["status"] == "doing"

    def test_comment(self, api):
        api.mocks["comment_task"].return_value = _fake_task()
        code, body = api.post("/api/task/comment/t-1", {"message": "hello"})
        assert code == 200
        api.mocks["comment_task"].assert_called_once()

    def test_archive(self, api):
        api.mocks["archive_task"].return_value = {"archived": True}
        code, body = api.post("/api/task/archive/t-1")
        assert code == 200

    def test_audit(self, api):
        api.mocks["get_audit"].return_value = [
            {"type": "created", "timestamp": "2026-01-01"},
        ]
        code, body = api.get("/api/task/audit/t-1")
        assert code == 200
        assert len(body["data"]) == 1

    def test_clear_schedule(self, api):
        api.mocks["clear_schedule"].return_value = _fake_task()
        code, body = api.post("/api/task/clear-schedule/t-1")
        assert code == 200

    def test_reorder_before(self, api):
        api.mocks["move_task_before"].return_value = _fake_task()
        code, body = api.post("/api/task/reorder/t-1", {
            "target_task_id": "t-2",
            "position": "before",
        })
        assert code == 200
        call = api.mocks["move_task_before"].call_args
        assert call.args == ("t-1", "t-2")
        assert call.kwargs.get("base_dir")

    def test_reorder_to_index(self, api):
        api.mocks["move_task_to_index"].return_value = _fake_task()
        code, body = api.post("/api/task/reorder/t-1", {"index": 0})
        assert code == 200
        call = api.mocks["move_task_to_index"].call_args
        assert call.args == ("t-1", 0)
        assert call.kwargs.get("base_dir")


# ── Lock handler ────────────────────────────────────────────────

class TestLock:
    def test_status_unlocked(self, api):
        api.mocks["lock_status"].return_value = None
        code, body = api.get("/api/lock/status/t-1")
        assert code == 200
        assert body["data"]["locked"] is False

    def test_status_locked(self, api):
        api.mocks["lock_status"].return_value = _fake_lock()
        code, body = api.get("/api/lock/status/t-1")
        assert code == 200
        assert body["data"]["locked"] is True

    def test_status_expired_lock(self, api):
        api.mocks["lock_status"].return_value = _fake_lock(expired=True)
        code, body = api.get("/api/lock/status/t-1")
        assert code == 200
        assert body["data"]["locked"] is False

    def test_acquire(self, api):
        api.mocks["acquire_lock"].return_value = _fake_lock()
        code, body = api.post("/api/lock/acquire/t-1", {"agent": "bot-1"})
        assert code == 200
        assert body["data"]["agent_id"] == "agent-1"

    def test_acquire_with_ttl(self, api):
        api.mocks["acquire_lock"].return_value = _fake_lock()
        code, body = api.post("/api/lock/acquire/t-1", {"agent": "bot-1", "ttl": "300"})
        assert code == 200
        call_kw = api.mocks["acquire_lock"].call_args
        kw = call_kw.kwargs if call_kw.kwargs else call_kw[1]
        assert kw["ttl_seconds"] == 300

    def test_release(self, api):
        api.mocks["release_lock"].return_value = True
        code, body = api.post("/api/lock/release/t-1", {"agent": "bot-1"})
        assert code == 200
        assert body["data"]["released"] is True


# ── Trigger handler ─────────────────────────────────────────────

class TestTrigger:
    def test_list(self, api):
        api.mocks["list_triggers"].return_value = [_fake_trigger()]
        code, body = api.get("/api/trigger/list/ws-1")
        assert code == 200
        assert len(body["data"]) == 1

    def test_create(self, api):
        api.mocks["create_trigger"].return_value = _fake_trigger()
        code, body = api.post("/api/trigger/create/ws-1", {
            "action": "run_agent",
            "on-state": "doing",
            "agent": "test_agent",
        })
        assert code == 200
        call_kw = api.mocks["create_trigger"].call_args
        kw = call_kw.kwargs if call_kw.kwargs else call_kw[1]
        assert kw["on_state"] == "doing"

    def test_delete(self, api):
        api.mocks["delete_trigger"].return_value = True
        code, body = api.post("/api/trigger/delete/trig-1")
        assert code == 200
        assert body["data"]["deleted"] is True


# ── Scheduler handler ──────────────────────────────────────────

class TestScheduler:
    def test_status(self, api):
        api.mocks["scheduler_status"].return_value = {"running": True, "last_tick": "2026-01-01"}
        code, body = api.get("/api/scheduler/status")
        assert code == 200
        assert body["data"]["running"] is True


# ── Poll endpoint ───────────────────────────────────────────────

class TestPoll:
    def test_poll_returns_workstreams_counts_scheduler(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["list_tasks"].return_value = [_fake_task(), _fake_task()]
        api.mocks["scheduler_status"].return_value = {"running": True, "last_tick": "2026-01-01"}

        code, body = api.get("/api/poll/all")
        assert code == 200
        data = body["data"]
        assert len(data["workstreams"]) == 1
        assert data["counts"]["ws-1"] == 2
        assert data["scheduler"]["running"] is True
        assert "board" not in data

    def test_poll_with_board_id(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["list_tasks"].return_value = [t]
        api.mocks["read_workstream"].return_value = ws
        api.mocks["lock_status"].return_value = None
        api.mocks["scheduler_status"].return_value = {"running": False}

        code, body = api.get("/api/poll/ws-1")
        assert code == 200
        data = body["data"]
        assert "board" in data
        assert data["board"]["workstream"]["id"] == "ws-1"
        assert len(data["board"]["tasks"]) == 1
        assert data["board"]["tasks"][0]["lock"]["locked"] is False

    def test_poll_with_missing_board_skips_board(self, api):
        api.mocks["list_workstreams"].return_value = []
        api.mocks["read_workstream"].side_effect = FileNotFoundError("nope")
        api.mocks["scheduler_status"].return_value = {"running": False}

        code, body = api.get("/api/poll/nonexistent-ws")
        assert code == 200
        assert "board" not in body["data"]

    def test_poll_board_includes_lock(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        lock = _fake_lock(expired=False)
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["list_tasks"].return_value = [t]
        api.mocks["read_workstream"].return_value = ws
        api.mocks["lock_status"].return_value = lock
        api.mocks["scheduler_status"].return_value = {"running": False}

        code, body = api.get("/api/poll/ws-1")
        assert code == 200
        assert body["data"]["board"]["tasks"][0]["lock"]["locked"] is True


# ── Concurrency / resilience tests ──────────────────────────────

class TestConcurrency:
    def test_concurrent_requests_all_succeed(self, api):
        """Verify the threaded server handles multiple simultaneous requests."""
        import concurrent.futures

        api.mocks["list_workstreams"].return_value = [_fake_workstream()]
        api.mocks["list_tasks"].return_value = []
        api.mocks["scheduler_status"].return_value = {"running": False}

        def do_request(_):
            return api.get("/api/poll/all")

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(do_request, i) for i in range(10)]
            results = [f.result(timeout=10) for f in futures]

        assert all(code == 200 for code, _ in results)
        assert all(body["status"] == "ok" for _, body in results)

    def test_slow_handler_does_not_block_other_requests(self, api):
        """A slow endpoint should not prevent other endpoints from responding."""
        import concurrent.futures
        import time

        original_return = [_fake_workstream()]

        def slow_list(*args, **kwargs):
            time.sleep(1)
            return original_return

        api.mocks["list_workstreams"].side_effect = slow_list
        api.mocks["scheduler_status"].return_value = {"running": False}

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            # Start a slow request
            slow_future = pool.submit(api.get, "/api/poll/all")
            time.sleep(0.1)  # let it start
            # A fast request should still complete quickly
            fast_future = pool.submit(api.get, "/api/scheduler/status")
            fast_code, fast_body = fast_future.result(timeout=2)
            slow_code, slow_body = slow_future.result(timeout=5)

        assert fast_code == 200
        assert slow_code == 200

    def test_abandoned_connection_does_not_block(self, api):
        """Opening a TCP connection and abandoning it should not block the server."""
        import socket
        import concurrent.futures

        api.mocks["list_workstreams"].return_value = []
        api.mocks["scheduler_status"].return_value = {"running": False}

        # Parse port from api.base
        port = int(api.base.rsplit(":", 1)[1])

        # Open a connection and just leave it (simulates stale browser connection)
        stale = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        stale.connect(("127.0.0.1", port))
        # Don't send anything — just hold it open

        # The server should still handle other requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(api.get, "/api/poll/all")
            code, body = future.result(timeout=5)

        assert code == 200
        stale.close()


# ── Request parsing ─────────────────────────────────────────────

class TestRequestParsing:
    def test_query_params_passed_to_handler(self, api):
        api.mocks["find_workstreams"].return_value = []
        api.get("/api/workstream/find?query=hello")
        api.mocks["find_workstreams"].assert_called_once()
        call_args = api.mocks["find_workstreams"].call_args
        assert call_args[0][0] == "hello" or call_args.kwargs.get("query") == "hello"

    def test_post_body_merged_with_query_params(self, api):
        api.mocks["create_task"].return_value = _fake_task()
        api.post("/api/task/create/ws-1?title=FromQuery", {"description": "FromBody"})
        call_kw = api.mocks["create_task"].call_args
        kw = call_kw.kwargs if call_kw.kwargs else call_kw[1]
        # Body should override or merge with query params
        assert kw["description"] == "FromBody"

    def test_tags_split_into_list(self, api):
        api.mocks["create_task"].return_value = _fake_task()
        api.post("/api/task/create/ws-1", {"title": "X", "tags": "a, b, c"})
        call_kw = api.mocks["create_task"].call_args
        kw = call_kw.kwargs if call_kw.kwargs else call_kw[1]
        assert kw["tags"] == ["a", "b", "c"]

    def test_task_update_force_passed_through(self, api):
        api.mocks["update_task"].return_value = _fake_task()
        api.post("/api/task/update/t-1", {"status": "Done", "force": True})
        call_kw = api.mocks["update_task"].call_args
        kw = call_kw.kwargs if call_kw.kwargs else call_kw[1]
        assert kw["status"] == "Done"
        assert kw["force"] is True

    def test_response_envelope(self, api):
        api.mocks["list_workstreams"].return_value = []
        code, body = api.get("/api/workstream/list")
        assert body["status"] == "ok"
        assert "data" in body

    def test_error_envelope(self, api):
        api.mocks["read_task"].side_effect = FileNotFoundError("nope")
        code, body = api.get("/api/task/read/missing")
        assert body["status"] == "error"
        assert "message" in body
