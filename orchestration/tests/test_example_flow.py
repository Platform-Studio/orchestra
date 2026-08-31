import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest

from orchestration.agents import _parse_agent_md, _resolve_agent_file
from orchestration.workstreams import list_workstreams, read_workstream

EXAMPLES_ROOT = Path(__file__).resolve().parents[2] / "examples"
EXPECTED_AUDIO_FILES = {
    "bass_start.mp3",
    "drum_end.mp3",
    "drum_start.mp3",
    "error.mp3",
    "finish.mp3",
    "flute_end.mp3",
    "flute_start.mp3",
    "oboe_end.mp3",
    "oboe_start.mp3",
    "start.mp3",
    "violin_end.mp3",
    "violin_start.mp3",
    "workstream_startup.mp3",
}


def _load_example_module(name: str, script: str):
    path = EXAMPLES_ROOT / name / script
    spec = importlib.util.spec_from_file_location(f"{name}_example", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_smoke_example_reaches_expected_state(tmp_path):
    result = _load_example_module("install_smoke", "run.py").run_example(tmp_path / "workspace")

    assert result["tasks"] == [
        {"title": "Verify Orchestra installation", "status": "Done"},
    ]


def test_fruit_and_veg_setup_installs_agents_and_triggers(tmp_path):
    workspace = tmp_path / "workspace"
    result = _load_example_module("fruit_and_veg", "setup.py").setup_example(workspace)

    workstream = read_workstream(result["workstream"]["id"], base_dir=str(workspace))
    assert workstream.task_states == {
        "Unsorted": ["Fruit", "Vegetable"],
        "Fruit": [],
        "Vegetable": [],
    }
    assert len(workstream.triggers) == 2
    generator_trigger, classifier_trigger = workstream.triggers
    assert generator_trigger.action == "run_agent"
    assert generator_trigger.on_schedule == "* * * * *"
    assert generator_trigger.agent == "fruit_vegetable_generator"
    assert classifier_trigger.action == "run_agent"
    assert classifier_trigger.on_state == "Unsorted"
    assert classifier_trigger.agent == "fruit_vegetable_classifier"
    assert classifier_trigger.task_selection == "first_unlocked"
    assert Path(_resolve_agent_file("fruit_vegetable_generator", str(workspace))).is_file()
    assert Path(_resolve_agent_file("fruit_vegetable_classifier", str(workspace))).is_file()
    assert {path.name for path in (workspace / "audio").glob("*.mp3")} == EXPECTED_AUDIO_FILES

    repeated = _load_example_module("fruit_and_veg", "setup.py").setup_example(workspace)
    assert repeated["workstream"]["id"] == result["workstream"]["id"]
    assert len(read_workstream(result["workstream"]["id"], base_dir=str(workspace)).triggers) == 2


def test_xmas_movies_setup_installs_idempotent_hierarchy_and_triggers(tmp_path):
    workspace = tmp_path / "workspace"
    module = _load_example_module("xmas_movies", "setup.py")

    result = module.setup_example(workspace, run_initial=False)
    workstreams = list_workstreams(base_dir=str(workspace))
    examples = next(ws for ws in workstreams if ws.id == result["workstreams"]["examples"]["id"])
    xmas_movies = next(ws for ws in workstreams if ws.id == result["workstreams"]["xmas_movies"]["id"])

    assert examples.parent_id is None
    assert xmas_movies.parent_id == examples.id
    assert xmas_movies.task_states == module.XMAS_STATES
    assert len(xmas_movies.triggers) == 4
    triggers_by_agent = {trigger.agent: trigger for trigger in xmas_movies.triggers}
    assert triggers_by_agent["xmas_movie_proposer"].on_schedule == "0 * * * *"
    assert triggers_by_agent["xmas_movie_proposer"].paused is True
    assert triggers_by_agent["xmas_movie_advocate"].on_state == "Advocate"
    assert triggers_by_agent["xmas_movie_skeptic"].on_state == "Skeptic"
    assert triggers_by_agent["xmas_movie_judge"].on_state == "Judgement"
    assert Path(_resolve_agent_file("xmas_movie_proposer", str(workspace))).is_file()
    for agent_ref in (
        "xmas_movie_proposer",
        "xmas_movie_advocate",
        "xmas_movie_skeptic",
        "xmas_movie_judge",
    ):
        agent_path = _resolve_agent_file(agent_ref, str(workspace))
        assert _parse_agent_md(agent_path)["progress_checklist_enabled"] is True
    assert (workspace / "Agents" / "skills" / "judging_xmas_movies.md").is_file()
    assert {path.name for path in (workspace / "audio").glob("*.mp3")} == EXPECTED_AUDIO_FILES

    repeated = module.setup_example(workspace, run_initial=False)
    assert repeated["workstreams"] == result["workstreams"]
    assert len(list_workstreams(base_dir=str(workspace))) == 2
    assert len(read_workstream(xmas_movies.id, base_dir=str(workspace)).triggers) == 4


def test_xmas_movies_runs_initial_proposer_only_for_new_workstream(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    module = _load_example_module("xmas_movies", "setup.py")
    agent_runs = []
    real_run_cli = module.run_cli

    def fake_run_cli(target_workspace, *args):
        if args[:2] == ("agent", "run"):
            agent_runs.append((target_workspace, args))
            return {"run_id": "initial-run"}
        return real_run_cli(target_workspace, *args)

    monkeypatch.setattr(module, "run_cli", fake_run_cli)

    first = module.setup_example(workspace)
    second = module.setup_example(workspace)

    assert first["initial_run"] == {"run_id": "initial-run"}
    assert second["initial_run"] is None
    assert agent_runs == [
        (
            workspace,
            (
                "agent",
                "run",
                "xmas_movie_proposer",
                "--workstream",
                first["workstreams"]["xmas_movies"]["id"],
            ),
        )
    ]


def test_xmas_movies_initial_run_failure_explains_runtime_configuration(
    tmp_path, monkeypatch, capsys
):
    workspace = tmp_path / "workspace"
    module = _load_example_module("xmas_movies", "setup.py")
    real_run_cli = module.run_cli

    def fake_run_cli(target_workspace, *args):
        if args[:2] == ("agent", "run"):
            raise RuntimeError("Not logged in")
        return real_run_cli(target_workspace, *args)

    monkeypatch.setattr(module, "run_cli", fake_run_cli)

    result = module.setup_example(workspace)

    assert result["initial_run"] is None
    warning = capsys.readouterr().err
    assert any(line.startswith("WARNING:") for line in warning.splitlines())
    assert "agent CLI is authenticated" in warning
    assert "ORCHESTRATION_AGENT_RUNTIME to copilot or cline" in warning
    assert ".env.example" in warning


@pytest.mark.skipif(os.name == "nt", reason="setup.sh requires a POSIX shell")
def test_setup_shell_summarizes_warnings_and_confirms_success(tmp_path):
    setup_script = tmp_path / "setup.sh"
    setup_script.write_text((EXAMPLES_ROOT.parent / "setup.sh").read_text())
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\nprintf 'WARNING: test warning\\n'\n")
    example_script = tmp_path / "example.sh"
    example_script.write_text("#!/bin/sh\nprintf 'example installed\\n'\n")
    install_script.chmod(0o755)
    example_script.chmod(0o755)

    completed = subprocess.run(
        ["sh", str(setup_script)],
        cwd=tmp_path,
        env={**os.environ, "VIRTUAL_ENV": str(tmp_path / "venv"), "NO_COLOR": "1"},
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.startswith("                 _               _\n")
    assert completed.stdout.count("WARNING: test warning") == 2
    assert "Warnings:" in completed.stdout
    assert "Orchestra installed successfully." in completed.stdout
    assert "Open the Workstream Manager with ./run-orchestra.sh" in completed.stdout


@pytest.mark.skipif(os.name == "nt", reason="setup.sh requires a POSIX shell")
def test_setup_shell_summarizes_errors_and_returns_failure(tmp_path):
    setup_script = tmp_path / "setup.sh"
    setup_script.write_text((EXAMPLES_ROOT.parent / "setup.sh").read_text())
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\nprintf 'install failed\\n' >&2\nexit 7\n")
    example_script = tmp_path / "example.sh"
    example_script.write_text("#!/bin/sh\ntouch example-ran\n")
    install_script.chmod(0o755)
    example_script.chmod(0o755)

    completed = subprocess.run(
        ["sh", str(setup_script)],
        cwd=tmp_path,
        env={**os.environ, "VIRTUAL_ENV": str(tmp_path / "venv"), "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 7
    assert "Errors:" in completed.stdout
    assert "ERROR: Command failed with exit code 7:" in completed.stdout
    assert "Orchestra installed successfully." not in completed.stdout
    assert not (tmp_path / "example-ran").exists()


@pytest.mark.skipif(os.name == "nt", reason="run-orchestra.sh requires a POSIX shell")
def test_run_orchestra_shell_prints_banner_and_classifies_output(tmp_path):
    launcher = tmp_path / "run-orchestra.sh"
    launcher.write_text((EXAMPLES_ROOT.parent / "run-orchestra.sh").read_text())
    fake_python = tmp_path / ".venv" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_open = tmp_path / "open"
    run_log = tmp_path / "run.log"
    browser_log = tmp_path / "browser.log"
    fake_python.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >>\"$RUN_LOG\"\n"
        "case \"$*\" in\n"
        "  *'scheduler run'*) printf 'WARNING: scheduler warning\\n'; sleep 1 ;;\n"
        "  *'worksm start'*) printf 'ERROR: server test error\\n' ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)
    fake_open.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\" >\"$BROWSER_LOG\"\n")
    fake_open.chmod(0o755)
    (tmp_path / "xdg-open").symlink_to(fake_open)

    completed = subprocess.run(
        ["sh", str(launcher), "-p", "9000"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHON": "",
            "VIRTUAL_ENV": "",
            "RUN_LOG": str(run_log),
            "BROWSER_LOG": str(browser_log),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "NO_COLOR": "1",
        },
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.startswith("                 _               _\n")
    assert "WARNING: scheduler warning" in completed.stdout
    assert "ERROR: server test error" in completed.stdout
    assert "Opening Orchestra at http://localhost:9000" in completed.stdout
    launcher_commands = run_log.read_text()
    assert "-u -c" in launcher_commands
    assert "urlopen(sys.argv[1], timeout=0.2).read()" in launcher_commands
    assert "--base-dir ./xmas-movies-workspace" in launcher_commands
    assert "worksm start --port 9000 --no-open" in launcher_commands
    assert browser_log.read_text().strip() == "http://localhost:9000"


@pytest.mark.skipif(os.name == "nt", reason="run-orchestra.sh requires a POSIX shell")
def test_run_orchestra_shell_uses_configured_workstream_root(tmp_path):
    launcher = tmp_path / "run-orchestra.sh"
    launcher.write_text((EXAMPLES_ROOT.parent / "run-orchestra.sh").read_text())
    fake_python = tmp_path / "python"
    run_log = tmp_path / "run.log"
    configured_root = tmp_path / "configured-workspace"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *resolve_workstream_root*) printf '%s\\n' \"$WORKSTREAM_ROOT\" ;;\n"
        "  *) printf '%s\\n' \"$*\" >>\"$RUN_LOG\" ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)

    subprocess.run(
        ["sh", str(launcher)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHON": str(fake_python),
            "WORKSTREAM_ROOT": str(configured_root),
            "RUN_LOG": str(run_log),
            "NO_COLOR": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    launcher_commands = run_log.read_text()
    assert f"--base-dir {configured_root}" in launcher_commands
    assert "--base-dir ./xmas-movies-workspace" not in launcher_commands


@pytest.mark.skipif(os.name == "nt", reason="run-orchestra.sh requires a POSIX shell")
def test_run_orchestra_shell_rejects_invalid_port(tmp_path):
    completed = subprocess.run(
        ["sh", str(EXAMPLES_ROOT.parent / "run-orchestra.sh"), "-p", "70000"],
        cwd=tmp_path,
        env={**os.environ, "NO_COLOR": "1"},
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 2
    assert "ERROR: Port must be an integer from 1 to 65535." in completed.stderr
    assert "Usage:" in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="run-orchestra.sh requires POSIX signals")
def test_run_orchestra_shell_exits_after_one_interrupt(tmp_path):
    fake_python = tmp_path / "python"
    fake_open = tmp_path / "open"
    browser_log = tmp_path / "browser.log"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *urlopen*) exit 0 ;;\n"
        "  *'scheduler run'*)\n"
        "    trap 'printf \"Scheduler stopped.\\n\"; exit 0' INT TERM\n"
        "    printf 'Scheduler running.\\n'\n"
        "    while :; do sleep 1; done ;;\n"
        "  *'worksm start'*)\n"
        "    trap 'printf \"Shutting down.\\n\"; exit 0' INT TERM\n"
        "    printf 'Workstream Manager running.\\n'\n"
        "    while :; do sleep 1; done ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)
    fake_open.write_text("#!/bin/sh\nprintf '%s\\n' \"$1\" >\"$BROWSER_LOG\"\n")
    fake_open.chmod(0o755)
    (tmp_path / "xdg-open").symlink_to(fake_open)

    process = subprocess.Popen(
        ["sh", str(EXAMPLES_ROOT.parent / "run-orchestra.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHON": str(fake_python),
            "BROWSER_LOG": str(browser_log),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "NO_COLOR": "1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + 3
    while not browser_log.exists() and time.monotonic() < deadline:
        time.sleep(0.01)

    assert browser_log.exists()
    os.killpg(process.pid, signal.SIGINT)
    try:
        output, _ = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        pytest.fail("run-orchestra.sh did not exit after one SIGINT")

    assert process.returncode == 130
    assert "Scheduler stopped." in output


@pytest.mark.skipif(os.name == "nt", reason="run-orchestra.sh requires POSIX signals")
def test_run_orchestra_shell_exits_when_scheduler_stops(tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *urlopen*) exit 0 ;;\n"
        "  *'scheduler run'*) printf 'Scheduler stopped.\\n'; exit 0 ;;\n"
        "  *'worksm start'*)\n"
        "    trap 'printf \"Shutting down.\\n\"; exit 0' INT TERM\n"
        "    printf 'Workstream Manager running.\\n'\n"
        "    while :; do sleep 1; done ;;\n"
        "esac\n"
    )
    fake_python.chmod(0o755)

    process = subprocess.Popen(
        ["sh", str(EXAMPLES_ROOT.parent / "run-orchestra.sh")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PYTHON": str(fake_python),
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "NO_COLOR": "1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        pytest.fail("run-orchestra.sh stayed running after its scheduler stopped")

    assert process.returncode != 0
    assert "ERROR: Scheduler stopped unexpectedly." in output


@pytest.mark.skipif(os.name == "nt", reason="example.sh requires a POSIX shell")
def test_example_shell_dispatches_named_example(tmp_path):
    workspace = tmp_path / "workspace"
    completed = subprocess.run(
        [
            "sh",
            str(EXAMPLES_ROOT.parent / "example.sh"),
            "xmas_movies",
            "--workspace",
            str(workspace),
            "--no-run",
        ],
        cwd=EXAMPLES_ROOT.parent,
        check=True,
        capture_output=True,
        text=True,
    )

    result = __import__("json").loads(completed.stdout)
    assert result["workstreams"]["xmas_movies"]["name"] == "Xmas Movies"