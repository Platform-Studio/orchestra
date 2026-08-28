import importlib.util
import os
from pathlib import Path
import subprocess

import pytest

from orchestration.agents import _resolve_agent_file
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