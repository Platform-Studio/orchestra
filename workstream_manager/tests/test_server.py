"""Tests for workstream_manager.server — HTTP bridge to orchestration modules.

Strategy: spin up a real test server on a random port with all orchestration
imports mocked, then make real HTTP requests.  This tests routing, request
parsing, response formatting, and error mapping end-to-end.
"""

import json
import base64
import os
import threading
import urllib.request
import urllib.error
from http.server import HTTPServer
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from orchestration.tasks import CorruptTaskError


# ── Helpers ──────────────────────────────────────────────────────

def _fake_workstream(**kwargs):
    defaults = {
        "id": "ws-1",
        "name": "Test WS",
        "description": "desc",
        "parent_id": None,
        "working_directory": "/tmp/workspace",
        "artifact_root": "/tmp/artifacts",
        "child_workstream_root": "/tmp/workstreams",
        "mount_available": False,
        "agent_concurrency": {},
        "task_states": {"backlog": ["doing"], "doing": ["done"], "done": []},
        "paused": False,
        "paused_states": [],
        "retry": None,
    }
    defaults.update(kwargs)
    obj = SimpleNamespace(**defaults)
    obj.to_dict = lambda include_transient=True: {k: getattr(obj, k) for k in defaults}
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
        "paused": False,
    }
    defaults.update(kwargs)
    obj = SimpleNamespace(**defaults)
    obj.to_dict = lambda: {k: getattr(obj, k) for k in defaults}
    return obj


def test_send_json_ignores_client_disconnect_during_headers():
    from workstream_manager.server import Handler

    handler = object.__new__(Handler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock(side_effect=BrokenPipeError())
    handler.wfile = SimpleNamespace(write=MagicMock())

    Handler._send_json(handler, 200, '{"status":"ok"}')

    handler.wfile.write.assert_not_called()
    assert not hasattr(handler, "_json_response")


def test_send_json_ignores_client_disconnect_during_body_write():
    from workstream_manager.server import Handler

    handler = object.__new__(Handler)
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.wfile = SimpleNamespace(write=MagicMock(side_effect=ConnectionResetError()))

    Handler._send_json(handler, 200, '{"status":"ok"}')

    handler.wfile.write.assert_called_once()
    assert not hasattr(handler, "_json_response")


def test_log_message_suppresses_api_logs_by_default(monkeypatch):
    from workstream_manager.server import Handler

    handler = object.__new__(Handler)
    monkeypatch.delenv("WORKSTREAM_MANAGER_LOG_API_REQUESTS", raising=False)

    with patch("workstream_manager.server.sys.stderr.write") as write:
        Handler.log_message(handler, "%s", '"GET /api/poll/ws-1 HTTP/1.1" 200 -')

    write.assert_not_called()


def test_log_message_emits_api_logs_when_enabled(monkeypatch):
    from workstream_manager.server import Handler

    handler = object.__new__(Handler)
    monkeypatch.setenv("WORKSTREAM_MANAGER_LOG_API_REQUESTS", "1")

    with patch("workstream_manager.server.sys.stderr.write") as write:
        Handler.log_message(handler, "%s", '"GET /api/poll/ws-1 HTTP/1.1" 200 -')

    write.assert_called_once_with('[API] "GET /api/poll/ws-1 HTTP/1.1" 200 -\n')


# ── Patched module names ────────────────────────────────────────

_PATCHES = {
    "list_workstreams":  "workstream_manager.server.list_workstreams",
    "read_workstream":   "workstream_manager.server.read_workstream",
    "create_workstream": "workstream_manager.server.create_workstream",
    "find_workstreams":  "workstream_manager.server.find_workstreams",
    "save_workstream":   "workstream_manager.server.save_workstream",
    "read_workstream_agent_concurrency": "workstream_manager.server.read_workstream_agent_concurrency",
    "set_workstream_agent_concurrency": "workstream_manager.server.set_workstream_agent_concurrency",
    "get_workstream_code_mount_statuses": "workstream_manager.server.get_workstream_code_mount_statuses",
    "get_workstream_tags": "workstream_manager.server.get_workstream_tags",
    "pause_workstream_states": "workstream_manager.server.pause_workstream_states",
    "resume_workstream_states": "workstream_manager.server.resume_workstream_states",
    "upsert_workstream_tag": "workstream_manager.server.upsert_workstream_tag",
    "list_workstream_hierarchy_env": "workstream_manager.server.list_workstream_hierarchy_env",
    "list_effective_workstream_env": "workstream_manager.server.list_effective_workstream_env",
    "resolve_workstream_workspace": "workstream_manager.server.resolve_workstream_workspace",
    "resolve_workstream_artifact_root": "workstream_manager.server.resolve_workstream_artifact_root",
    "resolve_workstream_child_state_root": "workstream_manager.server.resolve_workstream_child_state_root",
    "_task_counts_by_workstream": "workstream_manager.server._task_counts_by_workstream",
    "create_task":       "workstream_manager.server.create_task",
    "read_task":         "workstream_manager.server.read_task",
    "read_task_from_workstream": "workstream_manager.server.read_task_from_workstream",
    "update_task":       "workstream_manager.server.update_task",
    "list_tasks":        "workstream_manager.server.list_tasks",
    "comment_task":      "workstream_manager.server.comment_task",
    "delete_task_comment": "workstream_manager.server.delete_task_comment",
    "edit_task_comment": "workstream_manager.server.edit_task_comment",
    "archive_task":      "workstream_manager.server.archive_task",
    "get_audit":         "workstream_manager.server.get_audit",
    "clear_schedule":    "workstream_manager.server.clear_schedule",
    "move_task_before":  "workstream_manager.server.move_task_before",
    "move_task_after":   "workstream_manager.server.move_task_after",
    "move_task_to_index": "workstream_manager.server.move_task_to_index",
    "_run_orchestration_cli": "workstream_manager.server._run_orchestration_cli",
    "lock_status":      "workstream_manager.server.lock_status",
    "lock_status_for_workstream": "workstream_manager.server.lock_status_for_workstream",
    "list_workstream_locks_for_workstream": "workstream_manager.server.list_workstream_locks_for_workstream",
    "acquire_lock":     "workstream_manager.server.acquire_lock",
    "release_lock":     "workstream_manager.server.release_lock",
    "create_trigger":    "workstream_manager.server.create_trigger",
    "list_triggers":     "workstream_manager.server.list_triggers",
    "delete_trigger":    "workstream_manager.server.delete_trigger",
    "pause_trigger":     "workstream_manager.server.pause_trigger",
    "resume_trigger":    "workstream_manager.server.resume_trigger",
    "scheduler_status":  "workstream_manager.server.scheduler_status",
    "get_global_sound_mute": "workstream_manager.server.get_global_sound_mute",
    "set_global_sound_mute": "workstream_manager.server.set_global_sound_mute",
    "list_active_agents": "workstream_manager.server.list_active_agents",
    "list_agent_runs": "workstream_manager.server.list_agent_runs",
    "get_agent_run": "workstream_manager.server.get_agent_run",
    "read_agent_run_context_file": "workstream_manager.server.read_agent_run_context_file",
    "tail_active_agent": "workstream_manager.server.tail_active_agent",
    "kill_agent_run": "workstream_manager.server.kill_agent_run",
    "retry_agent_run":   "workstream_manager.server.retry_agent_run",
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
    mocks["lock_status"].return_value = None
    mocks["lock_status_for_workstream"].return_value = None
    mocks["list_workstream_locks_for_workstream"].return_value = {}
    mocks["acquire_lock"].return_value = _fake_lock()
    mocks["release_lock"].return_value = True
    mocks["scheduler_status"].return_value = {"running": False, "last_tick": None}
    mocks["get_global_sound_mute"].return_value = False
    mocks["set_global_sound_mute"].return_value = False
    mocks["list_active_agents"].return_value = []
    mocks["list_agent_runs"].return_value = []
    mocks["get_agent_run"].return_value = {"run": {"run_id": "run-1"}, "output": "", "cli_calls": [], "retry": {}, "interruption_reason": None}
    mocks["read_agent_run_context_file"].return_value = {"run_id": "run-1", "path": "source_0/file.json", "content": "{}", "bytes": 2}
    mocks["tail_active_agent"].return_value = {"run": {"run_id": "run-1", "status": "running"}, "tail": "live output", "line_count": 1}
    mocks["kill_agent_run"].return_value = {"run_id": "run-1", "status": "killed"}
    mocks["list_workstream_hierarchy_env"].return_value = []
    mocks["list_effective_workstream_env"].return_value = {}
    mocks["resolve_workstream_workspace"].return_value = "/tmp/workspace"
    mocks["resolve_workstream_artifact_root"].return_value = "/tmp/artifact-base"
    mocks["resolve_workstream_child_state_root"].return_value = "/tmp/state-base"
    mocks["get_workstream_code_mount_statuses"].return_value = {}
    mocks["get_workstream_tags"].return_value = []
    mocks["pause_workstream_states"].return_value = _fake_workstream(paused_states=["doing"])
    mocks["resume_workstream_states"].return_value = _fake_workstream(paused_states=[])
    mocks["upsert_workstream_tag"].return_value = {"name": "Urgent", "color": "#eb5a46"}
    mocks["_task_counts_by_workstream"].return_value = {}
    mocks["_run_orchestration_cli"].return_value = {}
    mocks["pause_trigger"].return_value = _fake_trigger(paused=True)
    mocks["resume_trigger"].return_value = _fake_trigger(paused=False)

    from workstream_manager.server import Handler, ThreadingHTTPServer, _invalidate_poll_sidebar_cache

    _invalidate_poll_sidebar_cache()

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
        def get_text(path):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
            resp = urllib.request.urlopen(req)
            return resp.status, resp.read().decode("utf-8")

        @staticmethod
        def get_raw(path):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
            try:
                resp = urllib.request.urlopen(req)
                return resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as e:
                return e.code, e.read(), e.headers

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
    _invalidate_poll_sidebar_cache()
    for p in patches.values():
        p.stop()


# ── Routing tests ───────────────────────────────────────────────

class TestRouting:
    def test_direct_workstream_route_serves_app_shell(self, api):
        code, body = api.get_text("/workstreams/ws-123")

        assert code == 200
        assert 'id="sidebar-tree"' in body

    def test_direct_task_route_serves_app_shell(self, api):
        code, body = api.get_text("/task/task-123")

        assert code == 200
        assert 'id="task-modal"' in body

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

    def test_unknown_retry_method(self, api):
        code, body = api.get("/api/retry/nope")
        assert code == 400


# ── Error mapping tests ─────────────────────────────────────────

class TestErrorMapping:
    def test_corrupt_task_returns_422(self, api):
        api.mocks["read_task"].side_effect = CorruptTaskError(
            "Task file task-bad.yaml contains invalid YAML: bad indentation"
        )
        code, body = api.get("/api/task/read/task-bad")
        assert code == 422
        assert body["code"] == "CORRUPT_TASK"
        assert "invalid YAML" in body["message"]

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


class TestArtifactPreview:
    def test_artifact_read_returns_image_preview_data(self, api):
        image_bytes = b"\x89PNG\r\n\x1a\nPNGDATA"
        with patch("workstream_manager.server._resolve_artifact_root", return_value=("/tmp/artifacts", "demo.png")), \
             patch("workstream_manager.server._validate_path", return_value="/tmp/artifacts/demo.png"), \
             patch("workstream_manager.server.os.path.exists", return_value=True), \
             patch("workstream_manager.server.open", MagicMock()) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = image_bytes

            code, body = api.get("/api/artifact/read?path=demo.png&workstream_id=ws-1")

        assert code == 200
        data = body["data"]
        assert data["resolved_path"] == "/tmp/artifacts/demo.png"
        assert data["preview_type"] == "image"
        assert data["mime_type"] == "image/png"
        assert data["content"] is None
        assert data["preview_error"] is None
        assert data["data_url"] == (
            "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        )

    def test_artifact_read_returns_text_preview_data(self, api):
        text_bytes = b"# Hello\nworld\n"
        with patch("workstream_manager.server._resolve_artifact_root", return_value=("/tmp/artifacts", "notes.md")), \
             patch("workstream_manager.server._validate_path", return_value="/tmp/artifacts/notes.md"), \
             patch("workstream_manager.server.os.path.exists", return_value=True), \
             patch("workstream_manager.server.open", MagicMock()) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = text_bytes

            code, body = api.get("/api/artifact/read?path=notes.md&workstream_id=ws-1")

        assert code == 200
        data = body["data"]
        assert data["preview_type"] == "text"
        assert data["mime_type"] == "text/markdown"
        assert data["content"] == "# Hello\nworld\n"
        assert data["data_url"] is None
        assert data["preview_error"] is None

    def test_artifact_read_returns_preview_error_for_binary_files(self, api):
        binary_bytes = b"\x00\x01\x02\x03"
        with patch("workstream_manager.server._resolve_artifact_root", return_value=("/tmp/artifacts", "archive.bin")), \
             patch("workstream_manager.server._validate_path", return_value="/tmp/artifacts/archive.bin"), \
             patch("workstream_manager.server.os.path.exists", return_value=True), \
             patch("workstream_manager.server.open", MagicMock()) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = binary_bytes

            code, body = api.get("/api/artifact/read?path=archive.bin&workstream_id=ws-1")

        assert code == 200
        data = body["data"]
        assert data["preview_type"] == "unsupported"
        assert data["mime_type"] == "application/octet-stream"
        assert data["content"] is None
        assert data["data_url"] is None
        assert data["preview_error"] == "Preview unavailable for application/octet-stream files."

    def test_runtime_error_returns_409(self, api):
        api.mocks["acquire_lock"].side_effect = RuntimeError("already locked")
        code, body = api.post("/api/lock/acquire/task-1", {"agent": "a"})
        assert code == 409
        assert body["code"] == "CONFLICT"
        api.mocks["acquire_lock"].side_effect = None

    def test_generic_exception_returns_500(self, api):
        api.mocks["read_task"].side_effect = Exception("boom")
        code, body = api.get("/api/task/read/task-x")
        assert code == 500
        assert body["code"] == "ERROR"
        assert body["message"] == "boom"

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


class TestAudioFiles:
    def test_audio_file_served_from_override_root(self, api, tmp_path, monkeypatch):
        sound_path = tmp_path / "workstream_startup.mp3"
        payload = b"ID3demo"
        sound_path.write_bytes(payload)
        monkeypatch.setenv("AUDIO_FILE_PATH", str(tmp_path))

        code, body, headers = api.get_raw("/audio/workstream_startup.mp3")

        assert code == 200
        assert body == payload
        assert headers.get_content_type() == "audio/mpeg"

    def test_audio_file_rejects_outside_paths(self, api, tmp_path, monkeypatch):
        monkeypatch.setenv("AUDIO_FILE_PATH", str(tmp_path))

        code, _, _ = api.get_raw("/audio/../secret.wav")

        assert code == 404


# ── Board endpoint ──────────────────────────────────────────────

class TestBoard:
    def test_board_returns_workstream_and_tasks(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        api.mocks["read_workstream"].return_value = ws
        api.mocks["list_tasks"].return_value = [t]

        code, body = api.get("/api/board/ws-1")
        assert code == 200
        data = body["data"]
        assert data["workstream"]["id"] == "ws-1"
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["lock"]["locked"] is False
        assert "revision" in data

    def test_board_returns_summary_tasks_only(self, api):
        ws = _fake_workstream()
        t = _fake_task(
            description="very long description",
            comments=[{"message": "hello"}],
            audit=[{"timestamp": "2026-01-01T00:00:00", "type": "comment", "description": "Comment added"}],
            attachments=["foo/bar.md"],
            scheduled_at="2026-01-01T01:00:00",
            retry_count=2,
            last_failure_at="2026-01-01T02:00:00",
        )
        api.mocks["read_workstream"].return_value = ws
        api.mocks["list_tasks"].return_value = [t]

        code, body = api.get("/api/board/ws-1")

        assert code == 200
        task = body["data"]["tasks"][0]
        assert task["id"] == "task-1"
        assert task["title"] == "Test Task"
        assert task["status"] == "backlog"
        assert task["workstream_id"] == "ws-1"
        assert task["scheduled_at"] == "2026-01-01T01:00:00"
        assert task["retry_count"] == 2
        assert task["last_failure_at"] == "2026-01-01T02:00:00"
        assert "description" not in task
        assert "comments" not in task
        assert "audit" not in task
        assert "attachments" not in task

    def test_board_meta_skips_task_yaml_load(self, api):
        ws = _fake_workstream()
        api.mocks["read_workstream"].return_value = ws

        code, body = api.get("/api/board/ws-1?meta=1")

        assert code == 200
        assert body["data"]["workstream"]["id"] == "ws-1"
        assert "revision" in body["data"]
        api.mocks["list_tasks"].assert_not_called()

    def test_board_meta_can_include_locks_without_task_yaml_load(self, api):
        ws = _fake_workstream()
        api.mocks["read_workstream"].return_value = ws
        api.mocks["list_workstream_locks_for_workstream"].return_value = {
            "task-1": {"locked": True, "agent_id": "agent-1"},
        }

        code, body = api.get("/api/board/ws-1?meta=1&locks=1")

        assert code == 200
        assert body["data"]["locks"]["task-1"]["locked"] is True
        api.mocks["list_tasks"].assert_not_called()

    def test_board_does_not_wait_on_lock_lookup(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        api.mocks["read_workstream"].return_value = ws
        api.mocks["list_tasks"].return_value = [t]
        api.mocks["_run_orchestration_cli"].side_effect = AssertionError("board should not load locks")

        code, body = api.get("/api/board/ws-1")
        assert code == 200
        task_data = body["data"]["tasks"][0]
        assert task_data["lock"]["locked"] is False
        api.mocks["_run_orchestration_cli"].side_effect = None

    def test_board_missing_id(self, api):
        # /api/board/ with no ID → the route gets concept=board, method="" (empty)
        # Actually URL parsing: /api/board → parts = ["board"] → len <2 → 400
        code, body = api.get("/api/board")
        assert code == 400


# ── Workstream handler ──────────────────────────────────────────

class TestWorkstream:
    def test_list(self, api):
        from workstream_manager.server import WORKSPACE_DIR

        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        code, body = api.get("/api/workstream/list")
        assert code == 200
        assert len(body["data"]) == 1
        assert body["data"][0]["name"] == "Test WS"
        api.mocks["list_workstreams"].assert_called_once_with(base_dir=WORKSPACE_DIR, include_mount_status=False)

    def test_code_status(self, api):
        from workstream_manager.server import WORKSPACE_DIR

        api.mocks["get_workstream_code_mount_statuses"].return_value = {
            "ws-1": {"configured": True, "exists": True, "resolved_path": "/tmp/workspace"}
        }

        code, body = api.post("/api/workstream/code-status", {"ids": ["ws-1"]})

        assert code == 200
        assert body["data"]["ws-1"]["exists"] is True
        api.mocks["get_workstream_code_mount_statuses"].assert_called_once_with(["ws-1"], base_dir=WORKSPACE_DIR)

    def test_counts(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["_task_counts_by_workstream"].return_value = {"ws-1": 2}
        code, body = api.get("/api/workstream/counts")
        assert code == 200
        assert body["data"]["ws-1"] == 2
        api.mocks["list_tasks"].assert_not_called()

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
        assert body["data"]["mount_available"] is False
        assert body["data"]["working_directory"] == "/tmp/workspace"
        assert body["data"]["artifact_root"] == "/tmp/artifacts"
        assert body["data"]["child_workstream_root"] == "/tmp/workstreams"
        assert body["data"]["resolved_artifact_directory"] == "/tmp/artifact-base/artifacts"
        assert body["data"]["resolved_child_workstream_root"] == "/tmp/state-base"

    def test_info_includes_agent_concurrency(self, api):
        ws = _fake_workstream(id="ws-42", agent_concurrency={"default": 1})
        api.mocks["read_workstream"].return_value = ws
        code, body = api.get("/api/workstream/info/ws-42")
        assert code == 200
        assert body["data"]["agent_concurrency"] == {"default": 1}

    def test_concurrency_get(self, api):
        ws = _fake_workstream(id="ws-42")
        api.mocks["read_workstream"].return_value = ws
        api.mocks["read_workstream_agent_concurrency"].return_value = {
            "workstream_id": "ws-42",
            "name": "Test WS",
            "agent_concurrency": {"default": 1},
        }

        code, body = api.get("/api/workstream/concurrency/ws-42")

        assert code == 200
        assert body["data"]["agent_concurrency"] == {"default": 1}

    def test_concurrency_post(self, api):
        ws = _fake_workstream(id="ws-42", agent_concurrency={"default": 2})
        api.mocks["read_workstream"].return_value = ws
        api.mocks["set_workstream_agent_concurrency"].return_value = ws

        code, body = api.post("/api/workstream/concurrency/ws-42", {"agent_concurrency": {"default": 2}})

        assert code == 200
        call = api.mocks["set_workstream_agent_concurrency"].call_args
        assert call.args[:1] == ("ws-42",)
        assert call.kwargs["agent_concurrency"] == {"default": 2}
        assert call.kwargs["updated_by"] == "Workspace Manager"
        assert body["data"]["agent_concurrency"] == {"default": 2}

    def test_find(self, api):
        ws = _fake_workstream()
        api.mocks["find_workstreams"].return_value = [ws]
        code, body = api.get("/api/workstream/find?query=test")
        assert code == 200
        assert len(body["data"]) == 1
        api.mocks["find_workstreams"].assert_called_once()

    def test_gettags(self, api):
        api.mocks["get_workstream_tags"].return_value = [{"name": "Urgent", "color": "#eb5a46"}]

        code, body = api.get("/api/workstream/gettags/ws-1")

        assert code == 200
        assert body["data"] == [{"name": "Urgent", "color": "#eb5a46"}]

    def test_upsert_tag(self, api):
        code, body = api.post("/api/workstream/upsert-tag/ws-1", {"name": "Urgent", "color": "#eb5a46"})

        assert code == 200
        call = api.mocks["upsert_workstream_tag"].call_args
        assert call.args[:3] == ("ws-1", "Urgent", "#eb5a46")
        assert call.kwargs["base_dir"]
        assert body["data"] == {"name": "Urgent", "color": "#eb5a46"}

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

    def test_pause_columns(self, api):
        ws = _fake_workstream(paused_states=["doing"])
        api.mocks["pause_workstream_states"].return_value = ws
        code, body = api.post("/api/workstream/pause-columns/ws-1", {"states": ["doing", "done"]})

        assert code == 200
        assert body["data"]["paused_states"] == ["doing"]
        call = api.mocks["pause_workstream_states"].call_args
        assert call.args[:2] == ("ws-1", ["doing", "done"])
        assert call.kwargs.get("base_dir")

    def test_resume_columns_accepts_comma_delimited_states(self, api):
        code, body = api.post("/api/workstream/resume-columns/ws-1", {"states": "doing,done"})

        assert code == 200
        call = api.mocks["resume_workstream_states"].call_args
        assert call.args[:2] == ("ws-1", ["doing", "done"])
        assert call.kwargs.get("base_dir")


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

    def test_read_with_workstream_id_uses_direct_lookup(self, api):
        api.mocks["read_task_from_workstream"].return_value = _fake_task(id="t-99", workstream_id="ws-1")
        code, body = api.get("/api/task/read/t-99?workstream_id=ws-1")
        assert code == 200
        assert body["data"]["id"] == "t-99"
        api.mocks["read_task_from_workstream"].assert_called_once()
        api.mocks["read_task"].assert_not_called()

    def test_create(self, api):
        api.mocks["create_task"].return_value = _fake_task(title="Apple")
        code, body = api.post("/api/task/create/ws-1", {
            "title": "Apple",
            "description": "A fruit",
            "tags": "red,crunchy",
        })
        assert code == 200
        assert body["data"]["title"] == "Apple"

    def test_create_passes_explicit_initial_status(self, api):
        api.mocks["create_task"].return_value = _fake_task(status="doing")
        code, body = api.post("/api/task/create/ws-1", {
            "title": "Apple",
            "status": "doing",
        })
        assert code == 200
        call_kw = api.mocks["create_task"].call_args
        kw = call_kw.kwargs if call_kw.kwargs else call_kw[1]
        assert kw["initial_status"] == "doing"

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
        call = api.mocks["comment_task"].call_args
        assert call.args[:2] == ("t-1", "hello")
        assert call.kwargs.get("author") == "Anonymous Human"

    def test_comment_uses_human_name_env(self, api):
        os.environ["HUMAN_NAME"] = "Jeremy"
        try:
            api.mocks["comment_task"].return_value = _fake_task()
            code, body = api.post("/api/task/comment/t-1", {"message": "hello"})
            assert code == 200
            call = api.mocks["comment_task"].call_args
            assert call.kwargs.get("author") == "Jeremy"
        finally:
            os.environ.pop("HUMAN_NAME", None)

    def test_edit_comment(self, api):
        api.mocks["edit_task_comment"].return_value = _fake_task()
        code, body = api.post("/api/task/edit-comment/t-1", {"index": 0, "message": "updated"})
        assert code == 200
        call = api.mocks["edit_task_comment"].call_args
        assert call.args[:3] == ("t-1", 0, "updated")
        assert call.kwargs.get("author") == "Anonymous Human"

    def test_edit_comment_requires_index_and_message(self, api):
        code, body = api.post("/api/task/edit-comment/t-1", {"message": "updated"})
        assert code == 400
        assert "index" in body["message"]

        code, body = api.post("/api/task/edit-comment/t-1", {"index": 0})
        assert code == 400
        assert "message" in body["message"]

    def test_delete_comment(self, api):
        api.mocks["delete_task_comment"].return_value = _fake_task()
        code, body = api.post("/api/task/delete-comment/t-1", {"index": 0})
        assert code == 200
        call = api.mocks["delete_task_comment"].call_args
        assert call.args == ("t-1", 0)
        assert call.kwargs.get("base_dir")

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
        api.mocks["lock_status"].assert_called_once()

    def test_status_locked(self, api):
        api.mocks["lock_status"].return_value = _fake_lock(agent_id="agent-1")
        code, body = api.get("/api/lock/status/t-1")
        assert code == 200
        assert body["data"]["locked"] is True
        assert body["data"]["agent_id"] == "agent-1"
        api.mocks["lock_status"].assert_called_once()

    def test_status_with_workstream_id_uses_direct_lookup(self, api):
        api.mocks["lock_status_for_workstream"].return_value = None
        code, body = api.get("/api/lock/status/t-1?workstream_id=ws-1")
        assert code == 200
        assert body["data"]["locked"] is False
        api.mocks["lock_status_for_workstream"].assert_called_once()
        api.mocks["lock_status"].assert_not_called()

    def test_list(self, api):
        ws = _fake_workstream(id="ws-1")
        api.mocks["read_workstream"].return_value = ws
        api.mocks["list_workstream_locks_for_workstream"].return_value = {"t-1": {"locked": True, "agent_id": "agent-1"}}
        code, body = api.get("/api/lock/list/ws-1")
        assert code == 200
        assert body["data"]["t-1"]["locked"] is True
        api.mocks["list_workstream_locks_for_workstream"].assert_called_once()

    def test_acquire(self, api):
        api.mocks["acquire_lock"].return_value = _fake_lock()
        code, body = api.post("/api/lock/acquire/t-1", {"agent": "bot-1"})
        assert code == 200
        assert body["data"]["agent_id"] == "agent-1"
        api.mocks["acquire_lock"].assert_called_once()

    def test_acquire_with_ttl(self, api):
        api.mocks["acquire_lock"].return_value = _fake_lock()
        code, body = api.post("/api/lock/acquire/t-1", {"agent": "bot-1", "ttl": "300"})
        assert code == 200
        assert api.mocks["acquire_lock"].call_args.kwargs["ttl_seconds"] == 300

    def test_release(self, api):
        api.mocks["release_lock"].return_value = True
        code, body = api.post("/api/lock/release/t-1", {"agent": "bot-1"})
        assert code == 200
        assert body["data"]["released"] is True
        api.mocks["release_lock"].assert_called_once()


class TestRetry:
    def test_retry_task(self, api):
        api.mocks["retry_agent_run"].reset_mock()
        with patch("orchestration.retry.manual_retry") as mock_manual_retry:
            mock_manual_retry.return_value = {"task_id": "t-1", "status": "pending"}
            code, body = api.post("/api/retry/task/t-1")
        assert code == 200
        assert body["data"]["status"] == "pending"
        mock_manual_retry.assert_called_once()

    def test_retry_agent_run(self, api):
        api.mocks["retry_agent_run"].return_value = {"run_id": "new-run", "retried_from_run_id": "old-run"}
        code, body = api.post("/api/retry/agent-run/old-run")
        assert code == 200
        assert body["data"]["run_id"] == "new-run"
        call = api.mocks["retry_agent_run"].call_args
        assert call.args == ("old-run",)
        assert call.kwargs.get("base_dir")

    def test_retry_agent_run_allows_paused_override(self, api):
        api.mocks["retry_agent_run"].return_value = {"run_id": "new-run", "retried_from_run_id": "old-run"}
        code, body = api.post("/api/retry/agent-run/old-run?allow_paused_workstream=1")
        assert code == 200
        assert body["data"]["run_id"] == "new-run"
        call = api.mocks["retry_agent_run"].call_args
        assert call.args == ("old-run",)
        assert call.kwargs.get("base_dir")
        assert call.kwargs.get("allow_paused_workstream") is True

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

    def test_pause(self, api):
        api.mocks["pause_trigger"].return_value = _fake_trigger(paused=True)
        code, body = api.post("/api/trigger/pause/trig-1")

        assert code == 200
        assert body["data"]["paused"] is True
        api.mocks["pause_trigger"].assert_called_once()

    def test_resume(self, api):
        api.mocks["resume_trigger"].return_value = _fake_trigger(paused=False)
        code, body = api.post("/api/trigger/resume/trig-1")

        assert code == 200
        assert body["data"]["paused"] is False
        api.mocks["resume_trigger"].assert_called_once()


# ── Scheduler handler ──────────────────────────────────────────

class TestScheduler:
    def test_status(self, api):
        api.mocks["scheduler_status"].return_value = {"running": True, "last_tick": "2026-01-01"}
        code, body = api.get("/api/scheduler/status")
        assert code == 200
        assert body["data"]["running"] is True


class TestAgent:
    def test_runs_summary_is_sorted_running_first(self, api):
        api.mocks["list_agent_runs"].return_value = [
            {
                "run_id": "run-completed",
                "agent": "Completed Agent",
                "agent_ref": "completed_agent",
                "workstream_id": "ws-1",
                "workstream_path": "WS / Completed",
                "tasks": [{"id": "t-1", "title": "Done task"}],
                "started_at": "2026-01-02T00:00:00+00:00",
                "ended_at": "2026-01-02T00:10:00+00:00",
                "status": "completed",
                "prompt": "very large prompt",
                "system_prompt": "large system prompt",
                "command_line": "secretly large command",
                "log_path": ".orchestration/agent_runs/run-completed.log",
            },
            {
                "run_id": "run-running",
                "agent": "Running Agent",
                "agent_ref": "running_agent",
                "workstream_id": "ws-2",
                "workstream_path": "WS / Running",
                "tasks": [{"id": "t-2", "title": "Live task"}],
                "started_at": "2026-01-01T00:00:00+00:00",
                "ended_at": None,
                "status": "running",
                "prompt": "another large prompt",
                "system_prompt": "another large system prompt",
                "command_line": "another large command",
                "log_path": ".orchestration/agent_runs/run-running.log",
                "runtime": "cline",
                "model": "deepseek/deepseek-v4-pro",
                "effort": "high",
            },
        ]

        code, body = api.get("/api/agent/runs?limit=25")

        assert code == 200
        assert [run["run_id"] for run in body["data"]] == ["run-running", "run-completed"]
        assert body["data"][0]["status"] == "running"
        assert "task_ids" in body["data"][0]
        assert "prompt" not in body["data"][0]
        assert "system_prompt" not in body["data"][0]
        assert "command_line" not in body["data"][0]
        assert "log_path" not in body["data"][0]

    def test_tail(self, api):
        from workstream_manager.server import WORKSPACE_DIR

        api.mocks["tail_active_agent"].return_value = {
            "run": {"run_id": "run-1", "status": "running"},
            "tail": "hello\nworld\n",
            "line_count": 2,
        }

        code, body = api.get("/api/agent/tail/run-1?lines=400")

        assert code == 200
        assert body["data"]["tail"] == "hello\nworld\n"
        api.mocks["tail_active_agent"].assert_called_with("run-1", lines=400, base_dir=WORKSPACE_DIR)

    def test_context_file(self, api):
        from workstream_manager.server import WORKSPACE_DIR

        code, body = api.get("/api/agent/context/run-1?path=source_0%2Ffile.json")

        assert code == 200
        assert body["data"]["content"] == "{}"
        api.mocks["read_agent_run_context_file"].assert_called_with(
            "run-1",
            "source_0/file.json",
            base_dir=WORKSPACE_DIR,
        )


# ── Poll endpoint ───────────────────────────────────────────────

class TestPoll:
    def test_poll_returns_workstreams_counts_scheduler(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["_task_counts_by_workstream"].return_value = {"ws-1": 2}
        api.mocks["scheduler_status"].return_value = {"running": True, "last_tick": "2026-01-01"}

        code, body = api.get("/api/poll/all")
        assert code == 200
        data = body["data"]
        assert len(data["workstreams"]) == 1
        assert data["counts"]["ws-1"] == 2
        assert data["scheduler"]["running"] is True
        assert "board" not in data
        api.mocks["list_tasks"].assert_not_called()


class TestSound:
    def test_mute_status(self, api):
        from workstream_manager.server import WORKSPACE_DIR

        api.mocks["get_global_sound_mute"].return_value = True

        code, body = api.get("/api/sound/mute")

        assert code == 200
        assert body["data"] == {"muted": True}
        api.mocks["get_global_sound_mute"].assert_called_with(base_dir=WORKSPACE_DIR)

    def test_set_mute_status(self, api):
        from workstream_manager.server import WORKSPACE_DIR

        api.mocks["set_global_sound_mute"].return_value = True

        code, body = api.post("/api/sound/mute", {"muted": True})

        assert code == 200
        assert body["data"] == {"muted": True}
        api.mocks["set_global_sound_mute"].assert_called_with(True, base_dir=WORKSPACE_DIR)


class TestPollBoard:
    def test_poll_with_board_id(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["_task_counts_by_workstream"].return_value = {"ws-1": 1}
        api.mocks["list_tasks"].return_value = [t]
        api.mocks["read_workstream"].return_value = ws
        api.mocks["scheduler_status"].return_value = {"running": False}

        code, body = api.get("/api/poll/ws-1")
        assert code == 200
        data = body["data"]
        assert "board" in data
        assert data["board"]["workstream"]["id"] == "ws-1"
        assert len(data["board"]["tasks"]) == 1
        assert data["board"]["tasks"][0]["lock"]["locked"] is False

    def test_poll_status_skips_workstream_and_task_routes(self, api):
        api.mocks["scheduler_status"].return_value = {"running": False}
        api.mocks["list_active_agents"].return_value = [{"run_id": "live-1", "pid": 12345}]

        code, body = api.get("/api/poll/status")

        assert code == 200
        data = body["data"]
        assert data["scheduler"]["running"] is False
        assert data["active_agent_runs"] == 1
        assert "workstreams" not in data
        assert "counts" not in data
        assert "board" not in data
        api.mocks["list_workstreams"].assert_not_called()
        api.mocks["_task_counts_by_workstream"].assert_not_called()
        api.mocks["list_tasks"].assert_not_called()

    def test_poll_with_missing_board_skips_board(self, api):
        api.mocks["list_workstreams"].return_value = []
        api.mocks["_task_counts_by_workstream"].return_value = {}
        api.mocks["read_workstream"].side_effect = FileNotFoundError("nope")
        api.mocks["scheduler_status"].return_value = {"running": False}

        code, body = api.get("/api/poll/nonexistent-ws")
        assert code == 200
        assert "board" not in body["data"]

    def test_poll_board_does_not_wait_on_lock_lookup(self, api):
        ws = _fake_workstream()
        t = _fake_task()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["_task_counts_by_workstream"].return_value = {"ws-1": 1}
        api.mocks["list_tasks"].return_value = [t]
        api.mocks["read_workstream"].return_value = ws
        api.mocks["scheduler_status"].return_value = {"running": False}
        api.mocks["_run_orchestration_cli"].side_effect = AssertionError("poll should not load locks")

        code, body = api.get("/api/poll/ws-1")
        assert code == 200
        assert body["data"]["board"]["tasks"][0]["lock"]["locked"] is False
        api.mocks["_run_orchestration_cli"].side_effect = None


class TestTaskCountsHelper:
    def test_counts_only_task_yaml_files(self, tmp_path):
        from workstream_manager.server import _task_counts_by_workstream

        ws_root = tmp_path / "mounted"
        tasks_dir = ws_root / "workstreams" / "ws-1" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "task-1.yaml").write_text("id: task-1\n", encoding="utf-8")
        (tasks_dir / "task-1.yaml.lock").write_text("agent_id: bot\n", encoding="utf-8")
        (tasks_dir / "notes.txt").write_text("ignore\n", encoding="utf-8")
        (tasks_dir / "task-2.yaml").write_text("id: task-2\n", encoding="utf-8")

        ws = _fake_workstream(id="ws-1")
        ws._workspace_root = str(ws_root)

        assert _task_counts_by_workstream([ws]) == {"ws-1": 2}

    def test_poll_active_agent_runs_excludes_pidless_entries(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["scheduler_status"].return_value = {"running": False}
        api.mocks["list_active_agents"].return_value = [
            {"run_id": "live-1", "pid": 12345},
            {"run_id": "legacy-no-pid"},
            {"run_id": "live-2", "pid": 67890},
        ]

        code, body = api.get("/api/poll/all")
        assert code == 200
        assert body["data"]["active_agent_runs"] == 2
        assert [run["run_id"] for run in body["data"]["active_agent_run_summaries"]] == ["live-1", "live-2"]

    def test_poll_active_agent_run_summaries_include_task_ids(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["scheduler_status"].return_value = {"running": False}
        api.mocks["list_active_agents"].return_value = [
            {
                "run_id": "live-1",
                "pid": 12345,
                "agent": "Coder",
                "workstream_id": "ws-1",
                "task_ids": ["task-1"],
                "started_at": "2026-01-02T00:00:00+00:00",
            },
        ]

        code, body = api.get("/api/poll/all")
        assert code == 200
        assert body["data"]["active_agent_run_summaries"][0]["task_ids"] == ["task-1"]
        assert body["data"]["active_agent_run_summaries"][0]["status"] == "running"

    def test_poll_active_agent_runs_falls_back_to_zero_on_error(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["scheduler_status"].return_value = {"running": False}
        api.mocks["list_active_agents"].side_effect = RuntimeError("boom")

        code, body = api.get("/api/poll/all")
        assert code == 200
        assert body["data"]["active_agent_runs"] == 0
        assert body["data"]["active_agent_run_summaries"] == []

    def test_poll_reuses_cached_sidebar_snapshot_between_requests(self, api):
        ws = _fake_workstream()
        api.mocks["list_workstreams"].return_value = [ws]
        api.mocks["_task_counts_by_workstream"].return_value = {"ws-1": 2}
        api.mocks["scheduler_status"].return_value = {"running": False}

        first_code, first_body = api.get("/api/poll/all")
        second_code, second_body = api.get("/api/poll/all")

        assert first_code == 200
        assert second_code == 200
        assert first_body["data"]["counts"] == second_body["data"]["counts"]
        api.mocks["list_workstreams"].assert_called_once()
        api.mocks["_task_counts_by_workstream"].assert_called_once_with([ws])
        api.mocks["list_tasks"].assert_not_called()

    def test_successful_post_invalidates_poll_sidebar_snapshot(self, api):
        ws_one = _fake_workstream(id="ws-1")
        ws_two = _fake_workstream(id="ws-2", name="Second WS")
        api.mocks["list_workstreams"].side_effect = [[ws_one], [ws_one, ws_two]]
        api.mocks["list_tasks"].return_value = []
        api.mocks["scheduler_status"].return_value = {"running": False}

        first_code, first_body = api.get("/api/poll/all")
        cached_code, cached_body = api.get("/api/poll/all")
        post_code, _ = api.post("/api/workstream/upsert-tag/ws-1", {"name": "Urgent", "color": "#eb5a46"})
        refreshed_code, refreshed_body = api.get("/api/poll/all")

        assert first_code == 200
        assert cached_code == 200
        assert post_code == 200
        assert refreshed_code == 200
        assert [ws["id"] for ws in first_body["data"]["workstreams"]] == ["ws-1"]
        assert [ws["id"] for ws in cached_body["data"]["workstreams"]] == ["ws-1"]
        assert [ws["id"] for ws in refreshed_body["data"]["workstreams"]] == ["ws-1", "ws-2"]


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

    def test_tag_arrays_pass_through_for_create_and_update(self, api):
        api.mocks["create_task"].return_value = _fake_task()
        api.post("/api/task/create/ws-1", {"title": "X", "tags": ["a", "b", "c"]})
        create_call = api.mocks["create_task"].call_args
        create_kw = create_call.kwargs if create_call.kwargs else create_call[1]
        assert create_kw["tags"] == ["a", "b", "c"]

        api.mocks["update_task"].return_value = _fake_task()
        api.post("/api/task/update/t-1", {"tags": ["x", "y"]})
        update_call = api.mocks["update_task"].call_args
        update_kw = update_call.kwargs if update_call.kwargs else update_call[1]
        assert update_kw["tags"] == ["x", "y"]

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
