#!/usr/bin/env python3
"""Install a repository-mounted Product Development workflow through Orchestra."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from setup_support import ensure_trigger, ensure_workstream, install_definition_files, run_cli


PRODUCT_DEVELOPMENT_STATES = {
    "Backlog": ["On Deck"],
    "On Deck": ["Backlog", "Implementation Plan", "Blocked"],
    "Blocked": [
        "On Deck",
        "Implementation Plan",
        "In Progress",
        "Code Review",
        "Integration Test",
        "Deploy",
    ],
    "Implementation Plan": ["On Deck", "In Progress", "Blocked"],
    "In Progress": ["On Deck", "Code Review", "Blocked"],
    "Code Review": ["In Progress", "Integration Test", "Blocked"],
    "Integration Test": ["In Progress", "Code Review", "Deploy", "Blocked"],
    "Deploy": ["In Progress", "Integration Test", "Live", "Blocked"],
    "Live": [],
}

TRIGGER_SPECS = [
    {
        "on_state": "Implementation Plan",
        "task_selection": "first_unlocked",
        "agent": "implementation_planner",
        "prompt": "Plan the assigned task and move ready work to In Progress.",
    },
    {
        "on_state": "In Progress",
        "task_selection": "first_unlocked",
        "agent": "coder",
        "prompt": "Implement and test the assigned task, then move it to Code Review.",
    },
    {
        "on_state": "Code Review",
        "task_selection": "first_unlocked",
        "agent": "code_reviewer",
        "prompt": "Review the assigned change and approve it or return actionable findings.",
    },
    {
        "on_state": "Integration Test",
        "task_selection": "first_unlocked",
        "agent": "integration_tester",
        "prompt": "Run the repository's pre-deployment checks and record the evidence.",
    },
    {
        "on_state": "Deploy",
        "task_selection": "first_unlocked",
        "agent": "devops",
        "prompt": "Deploy the approved revision using the repository's documented process.",
    },
    {
        "on_schedule": "0 * * * *",
        "timezone": "UTC",
        "filter": {"state": "On Deck"},
        "agent": "kanban_ninja",
        "prompt": "Review On Deck tasks for readiness and preserve their priority order.",
        "pause_on_create": True,
    },
    {
        "on_schedule": "*/30 * * * *",
        "timezone": "UTC",
        "filter": {"state": "Blocked"},
        "agent": "unblocker",
        "prompt": "Revisit blocked tasks and resolve or clearly escalate each blocker.",
        "pause_on_create": True,
    },
]

WORKSTREAM_CONTEXT = """# Product Development

This workstream is mounted to the product repository configured as its working directory.

Agents must read and follow the repository's own README, contribution guidance, agent instructions, architecture documentation, test commands, and deployment procedures. Repository-specific instructions take precedence over this generic example.

Humans create feature, bug, and engineering chore tasks in Backlog, define acceptance criteria, prioritize ready tasks into On Deck, make consequential product decisions, provide credentials and approvals, and may pause or redirect work at any time.

Do not deploy unless the repository documents a safe deployment and verification process. If required instructions, access, or approvals are missing, record the blocker and move the task to Blocked rather than guessing."""


def _validated_repository(repository: Path) -> Path:
    resolved = repository.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"Product repository is not an existing directory: {resolved}")
    return resolved


def setup_example(workspace: Path, repository: Path) -> dict[str, object]:
    repository = _validated_repository(repository)
    workspace = workspace.expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    installed_files = install_definition_files(Path(__file__).parent, workspace)

    examples, _ = ensure_workstream(
        workspace,
        name="Examples",
        description="Runnable Orchestra examples.",
    )
    product_development, _ = ensure_workstream(
        workspace,
        name="Product Development",
        parent_id=examples["id"],
        description="Human-agent product delivery from backlog through deployment.",
        context=WORKSTREAM_CONTEXT,
        states=PRODUCT_DEVELOPMENT_STATES,
        working_directory=repository,
    )

    triggers = []
    for raw_spec in TRIGGER_SPECS:
        spec = dict(raw_spec)
        pause_on_create = spec.pop("pause_on_create", False)
        trigger, created = ensure_trigger(
            workspace,
            product_development["id"],
            action="run_agent",
            **spec,
        )
        if created and pause_on_create:
            trigger = run_cli(workspace, "trigger", "pause", trigger["id"])
        triggers.append(trigger)

    return {
        "workspace": str(workspace),
        "repository": str(repository),
        "workstreams": {
            "examples": {"id": examples["id"], "name": examples["name"]},
            "product_development": {
                "id": product_development["id"],
                "name": product_development["name"],
            },
        },
        "triggers": triggers,
        "installed_files": installed_files,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("product-development-workspace"),
        help="Directory for Orchestra state and installed definitions.",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        required=True,
        help="Existing product repository that agents will read and modify.",
    )
    args = parser.parse_args()
    try:
        result = setup_example(args.workspace, args.repository)
    except (RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())