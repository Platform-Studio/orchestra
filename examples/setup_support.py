"""Shared helpers for installing Orchestra examples through the public CLI."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent


def run_cli(workspace: Path, *args: str) -> Any:
    """Run an Orchestra CLI command and return its JSON data payload."""
    command = [
        sys.executable,
        "-m",
        "orchestration.cli",
        "--base-dir",
        str(workspace),
        *args,
    ]
    display_command = ["orc", "--base-dir", str(workspace), *args]
    print(f"+ {shlex.join(display_command)}", file=sys.stderr)
    env = os.environ.copy()
    for key in ("WORKSTREAM_ROOT", "ARTIFACT_ROOT", "ARTICACT_ROOT"):
        env[key] = ""
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"CLI command failed: {' '.join(args)}\n{detail}")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"CLI command returned invalid JSON: {' '.join(args)}"
        ) from exc
    if result.get("status") != "ok":
        raise RuntimeError(f"CLI command failed: {' '.join(args)}\n{result}")
    return result.get("data")


def install_definition_files(example_dir: Path, workspace: Path) -> list[str]:
    """Copy an example's definitions and bundled audio into a workspace."""
    installed: list[str] = []
    for source_dir_name, target_dir_name in (
        ("agents", "Agents"),
        ("skills", "Agents/skills"),
    ):
        source_dir = example_dir / source_dir_name
        if not source_dir.is_dir():
            continue
        target_dir = workspace / target_dir_name
        target_dir.mkdir(parents=True, exist_ok=True)
        for source in sorted(source_dir.glob("*.md")):
            destination = target_dir / source.name
            shutil.copyfile(source, destination)
            installed.append(str(destination))

    source_audio = REPO_ROOT / "audio"
    target_audio = workspace / "audio"
    target_audio.mkdir(parents=True, exist_ok=True)
    for source in sorted(source_audio.glob("*.mp3")):
        destination = target_audio / source.name
        shutil.copyfile(source, destination)
        installed.append(str(destination))
    return installed


def ensure_workstream(
    workspace: Path,
    *,
    name: str,
    parent_id: str | None = None,
    description: str | None = None,
    context: str | None = None,
    states: dict[str, list[str]] | None = None,
    working_directory: Path | None = None,
) -> tuple[dict[str, Any], bool]:
    """Find an exact workstream or create it; reject incompatible matches."""
    resolved_working_directory = (
        str(working_directory.resolve()) if working_directory is not None else None
    )
    workstreams = run_cli(workspace, "workstream", "list")
    matches = [
        workstream
        for workstream in workstreams
        if workstream.get("name") == name and workstream.get("parent_id") == parent_id
    ]
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple workstreams named {name!r} exist under parent {parent_id!r}"
        )
    if matches:
        workstream = matches[0]
        if states is not None and workstream.get("task_states") != states:
            raise RuntimeError(
                f"Existing workstream {name!r} has a different state map; "
                "remove or rename it before installing this example"
            )
        if (
            resolved_working_directory is not None
            and workstream.get("working_directory") != resolved_working_directory
        ):
            raise RuntimeError(
                f"Existing workstream {name!r} uses working directory "
                f"{workstream.get('working_directory')!r}, not "
                f"{resolved_working_directory!r}; remove or rename it before "
                "installing this example"
            )
        return workstream, False

    args = ["workstream", "create", "--name", name]
    if parent_id:
        args.extend(["--parent", parent_id])
    if description:
        args.extend(["--description", description])
    if context is not None:
        args.extend(["--context", context])
    if states is not None:
        args.extend(["--states", json.dumps(states, separators=(",", ":"))])
    if resolved_working_directory is not None:
        args.extend(["--working-directory", resolved_working_directory])
    return run_cli(workspace, *args), True


def ensure_trigger(
    workspace: Path,
    workstream_id: str,
    *,
    action: str,
    on_state: str | None = None,
    on_schedule: str | None = None,
    timezone: str | None = None,
    task_selection: str | None = None,
    filter: dict[str, Any] | None = None,
    agent: str | None = None,
    prompt: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Find an exact trigger or create it; reject a conflicting event trigger."""
    desired = {
        "action": action,
        "on_state": on_state,
        "on_schedule": on_schedule,
        "timezone": timezone,
        "on_email": None,
        "task_selection": task_selection,
        "filter": filter,
        "agent": agent,
        "command": None,
        "prompt": prompt,
        "timeout": None,
    }
    triggers = run_cli(workspace, "trigger", "list", workstream_id)
    exact = [
        trigger
        for trigger in triggers
        if all(trigger.get(key) == value for key, value in desired.items())
    ]
    if exact:
        return exact[0], False

    same_event = [
        trigger
        for trigger in triggers
        if (on_state is not None and trigger.get("on_state") == on_state)
        or (on_schedule is not None and trigger.get("on_schedule") == on_schedule)
    ]
    if same_event:
        event = f"state {on_state!r}" if on_state is not None else f"schedule {on_schedule!r}"
        raise RuntimeError(
            f"Existing trigger for {event} conflicts with the example definition"
        )

    args = ["trigger", "create", workstream_id, "--action", action]
    for flag, value in (
        ("--on-state", on_state),
        ("--on-schedule", on_schedule),
        ("--timezone", timezone),
        ("--task-selection", task_selection),
        ("--filter", json.dumps(filter, separators=(",", ":")) if filter else None),
        ("--agent", agent),
        ("--prompt", prompt),
    ):
        if value is not None:
            args.extend([flag, value])
    return run_cli(workspace, *args), True