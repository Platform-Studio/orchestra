#!/usr/bin/env python3
"""Install the Xmas Movies multi-agent workflow through the Orchestra CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setup_support import ensure_trigger, ensure_workstream, install_definition_files, run_cli


XMAS_STATES = {
    "Advocate": ["Skeptic"],
    "Skeptic": ["Judgement"],
    "Judgement": ["Decided", "Advocate"],
    "Decided": [],
}


def setup_example(workspace: Path, *, run_initial: bool = True) -> dict[str, object]:
    workspace.mkdir(parents=True, exist_ok=True)
    installed_files = install_definition_files(Path(__file__).parent, workspace)

    examples, _ = ensure_workstream(
        workspace,
        name="Examples",
        description="Runnable Orchestra examples.",
    )
    xmas_movies, workstream_created = ensure_workstream(
        workspace,
        name="Xmas Movies",
        parent_id=examples["id"],
        description="Agents debate and judge whether ambiguous movies are Christmas movies.",
        context=(
            "Each task represents a movie. The advocate and skeptic make opposing cases, "
            "then the judge decides or returns the movie for further discussion."
        ),
        states=XMAS_STATES,
    )

    trigger_specs = [
        {
            "action": "run_agent",
            "on_schedule": "0 * * * *",
            "agent": "xmas_movie_proposer",
            "prompt": "Propose exactly one debatable Christmas movie.",
        },
        {
            "action": "run_agent",
            "on_state": "Advocate",
            "task_selection": "first_unlocked",
            "agent": "xmas_movie_advocate",
            "prompt": "Make the strongest case that the assigned movie is a Christmas movie.",
        },
        {
            "action": "run_agent",
            "on_state": "Skeptic",
            "task_selection": "first_unlocked",
            "agent": "xmas_movie_skeptic",
            "prompt": "Make the strongest case that the assigned movie is not a Christmas movie.",
        },
        {
            "action": "run_agent",
            "on_state": "Judgement",
            "task_selection": "first_unlocked",
            "agent": "xmas_movie_judge",
            "prompt": "Judge the assigned movie using the Xmas movie judging skill.",
        },
    ]

    triggers = []
    for index, spec in enumerate(trigger_specs):
        trigger, trigger_created = ensure_trigger(
            workspace,
            xmas_movies["id"],
            **spec,
        )
        if index == 0 and trigger_created:
            trigger = run_cli(workspace, "trigger", "pause", trigger["id"])
        triggers.append(trigger)

    initial_run = None
    if workstream_created and run_initial:
        try:
            initial_run = run_cli(
                workspace,
                "agent",
                "run",
                "xmas_movie_proposer",
                "--workstream",
                xmas_movies["id"],
            )
        except RuntimeError as exc:
            print(
                "WARNING: Initial agent run failed. Ensure your agent CLI is authenticated, "
                "or set ORCHESTRATION_AGENT_RUNTIME to copilot or cline as shown in "
                f".env.example: {exc}",
                file=sys.stderr,
            )

    return {
        "workstreams": {
            "examples": {"id": examples["id"], "name": examples["name"]},
            "xmas_movies": {"id": xmas_movies["id"], "name": xmas_movies["name"]},
        },
        "triggers": triggers,
        "installed_files": installed_files,
        "initial_run": initial_run,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("xmas-movies-workspace"),
    )
    parser.add_argument(
        "--no-run",
        action="store_true",
        help="Install the workflow without starting the initial proposer run.",
    )
    args = parser.parse_args()
    result = setup_example(args.workspace, run_initial=not args.no_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())