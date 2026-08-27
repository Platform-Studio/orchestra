#!/usr/bin/env python3
"""Set up the live, agent-driven Fruit and Vegetable Sorter tutorial."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setup_support import ensure_trigger, ensure_workstream, install_definition_files


def setup_example(workspace: Path) -> dict[str, object]:
    workspace.mkdir(parents=True, exist_ok=True)
    installed_agents = install_definition_files(Path(__file__).parent, workspace)

    workstream, _ = ensure_workstream(
        workspace,
        name="Fruit and Vegetable Sorter",
        description="Live tutorial using an LLM generator and a trigger-driven classifier.",
        context=(
            "This tutorial classifies foods using ordinary culinary categories. "
            "Generator tasks begin in Unsorted; the classifier explains its decision, "
            "adds useful tags, and moves each task to Fruit or Vegetable."
        ),
        states={
            "Unsorted": ["Fruit", "Vegetable"],
            "Fruit": [],
            "Vegetable": [],
        },
    )
    generator_trigger, _ = ensure_trigger(
        workspace,
        workstream["id"],
        action="run_agent",
        on_schedule="* * * * *",
        agent="fruit_vegetable_generator",
        prompt="Add exactly one new food to the Unsorted column.",
    )
    classifier_trigger, _ = ensure_trigger(
        workspace,
        workstream["id"],
        action="run_agent",
        on_state="Unsorted",
        task_selection="first_unlocked",
        agent="fruit_vegetable_classifier",
        prompt="Classify the assigned food and move it out of Unsorted.",
    )
    return {
        "workstream": {"id": workstream["id"], "name": workstream["name"]},
        "triggers": [
            {
                "id": generator_trigger["id"],
                "on_schedule": generator_trigger["on_schedule"],
                "agent": generator_trigger["agent"],
            },
            {
                "id": classifier_trigger["id"],
                "on_state": classifier_trigger["on_state"],
                "agent": classifier_trigger["agent"],
            },
        ],
        "agents": installed_agents,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("fruit-and-veg-workspace"),
    )
    args = parser.parse_args()
    print(json.dumps(setup_example(args.workspace), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())