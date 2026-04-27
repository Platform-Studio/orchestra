import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import dev_servers


def test_ensure_process_started_raises_with_log_tail(tmp_path):
    log_path = tmp_path / "service.log"
    log_path.write_text("line 1\nline 2\nboom\n", encoding="utf-8")
    proc = dev_servers.ManagedProc(name="web", cmd=["python"], log_path=log_path)
    proc.popen = SimpleNamespace(poll=lambda: 1, returncode=1)

    with pytest.raises(RuntimeError, match="exited immediately") as exc:
        dev_servers._ensure_process_started(proc, grace_seconds=0)

    assert "boom" in str(exc.value)


def test_stop_existing_scheduler_stops_running_scheduler():
    responses = iter([
        {"running": True, "pid": 123},
        {"running": False},
    ])

    with patch("dev_servers._orchestration_cli_json") as mock_cli:
        mock_cli.side_effect = lambda py, args: next(responses) if args == ["scheduler", "status"] else {"message": "stopped"}
        dev_servers._stop_existing_scheduler("/tmp/python")

    assert mock_cli.call_args_list[0].args[1] == ["scheduler", "status"]
    assert mock_cli.call_args_list[1].args[1] == ["scheduler", "stop"]


def test_orchestration_cli_json_returns_data(tmp_path):
    with patch("dev_servers.subprocess.run") as mock_run:
        mock_run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({"status": "ok", "data": {"running": False}}), stderr="")
        result = dev_servers._orchestration_cli_json("/tmp/python", ["scheduler", "status"])

    assert result == {"running": False}