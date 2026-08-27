#!/usr/bin/env python3
"""Test clean wheel installation, package upgrade, and the bundled example."""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def run(command: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        output = "\n".join(part for part in (error.stdout, error.stderr) if part)
        raise RuntimeError(f"Command failed: {command}\n{output}") from error
    return result.stdout


def venv_executable(venv: Path, name: str) -> Path:
    scripts_dir = venv / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" and name != "python" else ""
    return scripts_dir / f"{name}{suffix}"


def build_wheel(source: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    run([sys.executable, "-m", "build", "--wheel", "--outdir", str(destination), str(source)])
    wheels = list(destination.glob("orchestra-*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected one Orchestra wheel in {destination}, found {wheels}")
    return wheels[0]


def extract_baseline(repo: Path, ref: str, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(destination, filter="data")
    pyproject = destination / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8").replace('version = "0.1.0"', 'version = "0.0.0"', 1),
        encoding="utf-8",
    )


def cli_json(command: list[str], *, cwd: Path, env: dict[str, str]) -> object:
    return json.loads(run(command, cwd=cwd, env=env))["data"]


def isolated_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["WORKSTREAM_ROOT"] = ""
    env["ARTIFACT_ROOT"] = ""
    env["ORCHESTRATION_BASE_ENV_PATH"] = ""
    return env


def run_release_flow(repo: Path, baseline_ref: str) -> None:
    with tempfile.TemporaryDirectory(prefix="orchestra-release-") as raw_temp:
        temp = Path(raw_temp)
        baseline_source = temp / "baseline-source"
        extract_baseline(repo, baseline_ref, baseline_source)
        baseline_wheel = build_wheel(baseline_source, temp / "baseline-dist")
        current_wheel = build_wheel(repo, temp / "current-dist")
        env = isolated_env()

        upgrade_venv = temp / "upgrade-venv"
        run([sys.executable, "-m", "venv", str(upgrade_venv)])
        upgrade_python = venv_executable(upgrade_venv, "python")
        run([str(upgrade_python), "-m", "pip", "install", str(baseline_wheel)], env=env)
        workspace = temp / "preserved-workspace"
        old_cli = [str(upgrade_python), "-m", "orchestration", "--base-dir", str(workspace)]
        workstream = cli_json(old_cli + ["workstream", "create", "--name", "Upgrade Test"], cwd=temp, env=env)
        task = cli_json(
            old_cli + ["task", "create", workstream["id"], "--title", "Preserve me"],
            cwd=temp,
            env=env,
        )
        cli_json(
            old_cli + ["artifact", "create", "--path", "upgrade/proof.md", "--content", "preserved"],
            cwd=temp,
            env=env,
        )
        env_path = workspace / ".env"
        env_contents = "ORCHESTRATION_AGENT_RUNTIME=copilot\nCUSTOM_RELEASE_MARKER=preserved\n"
        env_path.write_text(env_contents, encoding="utf-8")

        run([str(upgrade_python), "-m", "pip", "install", "--upgrade", str(current_wheel)], env=env)
        orc = venv_executable(upgrade_venv, "orc")
        upgraded_cli = [str(orc), "--base-dir", str(workspace)]
        persisted_task = cli_json(upgraded_cli + ["task", "read", task["id"]], cwd=temp, env=env)
        persisted_artifact = cli_json(
            upgraded_cli + ["artifact", "read", "upgrade/proof.md"],
            cwd=temp,
            env=env,
        )
        assert persisted_task["title"] == "Preserve me"
        assert persisted_artifact["content"] == "preserved"
        assert env_path.read_text(encoding="utf-8") == env_contents

        clean_venv = temp / "clean-venv"
        run([sys.executable, "-m", "venv", str(clean_venv)])
        clean_python = venv_executable(clean_venv, "python")
        run([str(clean_python), "-m", "pip", "install", str(current_wheel)], env=env)
        clean_orc = venv_executable(clean_venv, "orc")
        clean_orchestra = venv_executable(clean_venv, "orchestra")
        assert "usage: orc" in run([str(clean_orc), "--help"], cwd=temp, env=env)
        assert "usage: orc" in run([str(clean_orchestra), "--help"], cwd=temp, env=env)
        assert "usage: orc worksm start" in run(
            [str(clean_orc), "worksm", "start", "--help"],
            cwd=temp,
            env=env,
        )

        example_source = repo / "examples" / "install_smoke"
        example_copy = temp / "install_smoke"
        shutil.copytree(example_source, example_copy)
        example_workspace = temp / "example-workspace"
        output = run(
            [str(clean_python), str(example_copy / "run.py"), "--workspace", str(example_workspace)],
            cwd=temp,
            env=env,
        )
        assert json.loads(output)["workstream"] == "Installation Smoke Test"

        tutorial_source = repo / "examples" / "fruit_and_veg"
        tutorial_copy = temp / "fruit_and_veg"
        shutil.copytree(tutorial_source, tutorial_copy)
        tutorial_workspace = temp / "fruit-and-veg-workspace"
        tutorial = json.loads(
            run(
                [str(clean_python), str(tutorial_copy / "setup.py"), "--workspace", str(tutorial_workspace)],
                cwd=temp,
                env=env,
            )
        )
        tutorial_cli = [str(clean_orc), "--base-dir", str(tutorial_workspace)]
        agents = cli_json(tutorial_cli + ["agent", "list"], cwd=temp, env=env)
        triggers = cli_json(
            tutorial_cli + ["trigger", "list", tutorial["workstream"]["id"]],
            cwd=temp,
            env=env,
        )
        assert {agent["name"] for agent in agents} == {
            "Fruit and Vegetable Classifier",
            "Fruit and Vegetable Generator",
        }
        assert len(triggers) == 2
        assert triggers[0]["on_schedule"] == "* * * * *"
        assert triggers[0]["agent"] == "fruit_vegetable_generator"
        assert triggers[1]["on_state"] == "Unsorted"
        assert triggers[1]["agent"] == "fruit_vegetable_classifier"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--baseline-ref", default="11b26f3")
    args = parser.parse_args()
    run_release_flow(args.repo.resolve(), args.baseline_ref)
    print("Clean install, upgrade, and example flows passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())