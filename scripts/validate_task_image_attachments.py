#!/usr/bin/env python3
"""Validate image attachments for one or more tasks.

Usage:
  python scripts/validate_task_image_attachments.py --task <task_id>
  python scripts/validate_task_image_attachments.py --task <task_id> --task <task_id2>
"""

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from orchestration.image_validation import validate_task_image_attachments
from orchestration.tasks import read_task


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate task image attachments")
    parser.add_argument(
        "--task",
        action="append",
        dest="task_ids",
        required=True,
        help="Task id to validate (repeatable)",
    )
    parser.add_argument(
        "--base-dir",
        default=".",
        help="Workspace root (default: current directory)",
    )
    args = parser.parse_args()

    any_issues = False
    for task_id in args.task_ids:
        task = read_task(task_id, base_dir=args.base_dir)
        issues = validate_task_image_attachments(task, base_dir=args.base_dir)
        if not issues:
            print(f"OK {task_id}: all image attachments are valid")
            continue

        any_issues = True
        print(f"FAIL {task_id}: invalid image attachments detected")
        for issue in issues:
            print(f"  - {issue['path']}: {issue['reason']}")

    return 1 if any_issues else 0


if __name__ == "__main__":
    sys.exit(main())
