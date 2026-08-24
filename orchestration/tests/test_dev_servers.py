import os
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


def test_log_dir_uses_resolved_artifact_root(monkeypatch, tmp_path):
    artifact_root = tmp_path / "jb_workstreams"
    monkeypatch.setattr(dev_servers, "BASE_DIR", tmp_path / "foundation")
    monkeypatch.setenv("ARTIFACT_ROOT", str(artifact_root))

    assert dev_servers._log_dir() == artifact_root / "artifacts" / "logs"


def test_display_path_falls_back_to_absolute_for_external_paths(tmp_path, monkeypatch):
    monkeypatch_base_dir = tmp_path / "foundation"
    external_path = tmp_path / "jb_workstreams" / "artifacts" / "logs" / "scheduler.log"
    monkeypatch.setattr(dev_servers, "BASE_DIR", monkeypatch_base_dir)

    assert dev_servers._display_path(external_path) == str(external_path)


def test_reload_dotenv_overrides_existing_values(monkeypatch, tmp_path):
    base_dir = tmp_path / "foundation"
    base_dir.mkdir()
    (base_dir / ".env").write_text("COPILOT_CODING_LLM=gpt-5.4\n", encoding="utf-8")
    monkeypatch.setattr(dev_servers, "BASE_DIR", base_dir)
    monkeypatch.setattr(dev_servers, "load_dotenv", __import__("dotenv").load_dotenv)
    monkeypatch.setenv("COPILOT_CODING_LLM", "gpt-5.3-codex")

    dev_servers._reload_dotenv()

    assert os.environ["COPILOT_CODING_LLM"] == "gpt-5.4"


def test_build_processes_uses_current_workstream_root(monkeypatch, tmp_path):
    base_dir = tmp_path / "foundation"
    base_dir.mkdir()
    workstream_root = tmp_path / "updated_root"
    artifact_root = tmp_path / "artifacts_root"
    monkeypatch.setattr(dev_servers, "BASE_DIR", base_dir)
    monkeypatch.setattr(dev_servers, "resolve_workstream_root", lambda _: str(workstream_root))
    monkeypatch.setattr(dev_servers, "resolve_artifact_root", lambda _: str(artifact_root))

    processes = dev_servers._build_processes("/tmp/python")

    assert processes[0].cmd[4] == str(workstream_root)
    assert processes[1].cmd[-1] == str(workstream_root)
    assert processes[0].log_path == artifact_root / "artifacts" / "logs" / "scheduler.log"