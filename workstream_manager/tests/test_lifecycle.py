import os
import signal

import pytest

from workstream_manager import server


def test_status_cleans_stale_pid(tmp_path, monkeypatch):
    server._save_state({"pid": 12345, "port": 8080}, str(tmp_path))
    monkeypatch.setattr(server, "_is_live_non_zombie", lambda pid: False)

    assert server.status(str(tmp_path)) == {"running": False}
    assert server._load_state(str(tmp_path)) == {}


def test_status_reports_running_process(tmp_path, monkeypatch):
    server._save_state({"pid": 12345, "port": 9090}, str(tmp_path))
    monkeypatch.setattr(server, "_is_live_non_zombie", lambda pid: True)

    assert server.status(str(tmp_path)) == {
        "running": True,
        "pid": 12345,
        "port": 9090,
    }


def test_stop_signals_recorded_process(tmp_path, monkeypatch):
    server._save_state({"pid": 12345, "port": 8080}, str(tmp_path))
    monkeypatch.setattr(server, "_is_live_non_zombie", lambda pid: True)
    signals = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))

    result = server.stop(str(tmp_path))

    assert signals == [(12345, signal.SIGTERM)]
    assert result == {"message": "Sent SIGTERM to Workstream Manager (pid=12345)"}


def test_stop_cleans_stale_pid(tmp_path, monkeypatch):
    server._save_state({"pid": 12345, "port": 8080}, str(tmp_path))
    monkeypatch.setattr(server, "_is_live_non_zombie", lambda pid: False)

    result = server.stop(str(tmp_path))

    assert result == {"message": "Workstream Manager pid 12345 is not running (stale)"}
    assert server._load_state(str(tmp_path)) == {}


def test_run_rejects_duplicate_process(tmp_path, monkeypatch):
    server._save_state({"pid": 12345, "port": 8080}, str(tmp_path))
    monkeypatch.setattr(server, "_is_live_non_zombie", lambda pid: True)

    with pytest.raises(RuntimeError, match="already running"):
        server.run(str(tmp_path), open_browser=False)


def test_run_clears_process_state_on_exit(tmp_path, monkeypatch):
    class FakeHTTPServer:
        def __init__(self, address, handler):
            self.address = address

        def serve_forever(self):
            state = server._load_state(str(tmp_path))
            assert state == {"pid": os.getpid(), "port": 9090}

        def shutdown(self):
            pass

        def server_close(self):
            pass

    monkeypatch.setattr(server, "ThreadingHTTPServer", FakeHTTPServer)
    monkeypatch.setattr(signal, "signal", lambda sig, handler: signal.SIG_DFL)

    server.run(str(tmp_path), port=9090, open_browser=False)

    assert server._load_state(str(tmp_path)) == {}