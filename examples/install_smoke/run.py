#!/usr/bin/env python3
"""Run a deterministic installation smoke test without external services."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from orchestration.tasks import comment_task, create_task, list_tasks_for_workstream, update_task
from orchestration.workstreams import create_workstream


def run_example(workspace: Path) -> dict[str, object]:
    workspace.mkdir(parents=True, exist_ok=True)
    workstream = create_workstream(
        name="Installation Smoke Test",
        description="Deterministic package and persistence verification.",
        task_states={
            "New": ["In Progress"],
            "In Progress": ["Done"],
            "Done": [],
        },
        base_dir=str(workspace),
    )
    task = create_task(workstream.id, title="Verify Orchestra installation", base_dir=str(workspace))
    update_task(task.id, status="In Progress", base_dir=str(workspace))
    comment_task(
        task.id,
        "The installed package can create and update persisted Orchestra data.",
        author="Installation Smoke Test",
        base_dir=str(workspace),
    )
    update_task(task.id, status="Done", base_dir=str(workspace))

    tasks = list_tasks_for_workstream(workstream, base_dir=str(workspace))
    final_state = {
        "workstream": workstream.name,
        "tasks": [
            {"title": current.title, "status": current.status}
            for current in tasks
        ],
    }
    expected_path = Path(__file__).with_name("expected_state.yaml")
    expected = yaml.safe_load(expected_path.read_text(encoding="utf-8"))
    if final_state != expected:
        raise RuntimeError(f"Smoke test ended in an unexpected state: {final_state!r}")
    return final_state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_example(args.workspace), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())