#!/usr/bin/env python3
"""Orchestration Framework CLI.

Usage:
    python orchestration/cli.py <concept> <method> [arguments]

Examples:
    python orchestration/cli.py workstream create --name "SDR Outreach"
    python orchestration/cli.py task create <ws_id> --title "Contact John"
    python orchestration/cli.py task update <task_id> --status "in_progress"
    python orchestration/cli.py lock acquire <task_id> --agent sdr-worker-1
"""

import argparse
import json
import os
import sys


def _output(data):
    """Print JSON success response to stdout."""
    print(json.dumps({"status": "ok", "data": data}, indent=2, default=str))


def _error(message: str, code: str = "ERROR"):
    """Print JSON error response to stderr and exit."""
    print(json.dumps({"status": "error", "message": message, "code": code}), file=sys.stderr)
    sys.exit(1)


# ── Workstream commands ──────────────────────────────────────────────

def cmd_workstream_create(args):
    from .workstreams import create_workstream
    kwargs = {"name": args.name, "base_dir": args.base_dir}
    if args.description:
        kwargs["description"] = args.description
    if args.parent:
        kwargs["parent_id"] = args.parent
    if args.states:
        kwargs["task_states"] = json.loads(args.states)
    if args.retry:
        kwargs["retry"] = json.loads(args.retry)
    ws = create_workstream(**kwargs)
    _output(ws.to_dict())


def cmd_workstream_list(args):
    from .workstreams import list_workstreams
    workstreams = list_workstreams(base_dir=args.base_dir)
    _output([ws.to_dict() for ws in workstreams])


def cmd_workstream_read(args):
    from .workstreams import read_workstream
    ws = read_workstream(args.id, base_dir=args.base_dir)
    _output(ws.to_dict())


def cmd_workstream_find(args):
    from .workstreams import find_workstreams
    workstreams = find_workstreams(args.query, base_dir=args.base_dir)
    _output([ws.to_dict() for ws in workstreams])


def cmd_workstream_tree(args):
    from .workstreams import list_workstreams
    workstreams = list_workstreams(base_dir=args.base_dir)

    # Build parent -> children mapping
    children = {}
    roots = []
    for ws in workstreams:
        pid = ws.parent_id
        if pid is None:
            roots.append(ws)
        else:
            children.setdefault(pid, []).append(ws)

    lines = []
    def _walk(ws, prefix="", is_last=True):
        connector = "└── " if is_last else "├── "
        lines.append(f"{prefix}{connector}{ws.name} ({ws.id})")
        child_prefix = prefix + ("    " if is_last else "│   ")
        kids = children.get(ws.id, [])
        for i, kid in enumerate(kids):
            _walk(kid, child_prefix, i == len(kids) - 1)

    for i, root in enumerate(roots):
        if i == 0:
            lines.append(f"{root.name} ({root.id})")
        else:
            lines.append(f"\n{root.name} ({root.id})")
        kids = children.get(root.id, [])
        for j, kid in enumerate(kids):
            _walk(kid, "", j == len(kids) - 1)

    tree = "\n".join(lines)
    print(tree)


# ── Task commands ────────────────────────────────────────────────────

def cmd_task_create(args):
    from .tasks import create_task
    kwargs = {
        "workstream_id": args.workstream_id,
        "title": args.title,
        "base_dir": args.base_dir,
    }
    if args.description:
        kwargs["description"] = args.description
    if args.tags:
        kwargs["tags"] = [t.strip() for t in args.tags.split(",")]
    if args.retry:
        kwargs["retry"] = json.loads(args.retry)
    task = create_task(**kwargs)
    _output(task.to_dict())


def cmd_task_read(args):
    from .tasks import read_task
    task = read_task(args.task_id, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_update(args):
    from .tasks import update_task
    kwargs = {"task_id": args.task_id, "base_dir": args.base_dir}
    if args.status:
        kwargs["status"] = args.status
    if args.description:
        kwargs["description"] = args.description
    if args.tags is not None:
        kwargs["tags"] = [t.strip() for t in args.tags.split(",")]
    task = update_task(**kwargs)
    _output(task.to_dict())


def cmd_task_list(args):
    from .tasks import list_tasks
    kwargs = {"workstream_id": args.workstream_id, "base_dir": args.base_dir}
    if args.status:
        kwargs["status"] = args.status
    if args.tags:
        kwargs["tags"] = [t.strip() for t in args.tags.split(",")]
    tasks = list_tasks(**kwargs)
    _output([t.to_dict() for t in tasks])


def cmd_task_comment(args):
    from .tasks import comment_task
    task = comment_task(args.task_id, args.message, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_archive(args):
    from .tasks import archive_task
    result = archive_task(args.task_id, base_dir=args.base_dir)
    _output(result)


def cmd_task_audit(args):
    from .tasks import get_audit
    audit = get_audit(args.task_id, base_dir=args.base_dir)
    _output(audit)


# ── Lock commands ────────────────────────────────────────────────────

def cmd_lock_acquire(args):
    from .locks import acquire_lock
    kwargs = {"task_id": args.task_id, "agent_id": args.agent, "base_dir": args.base_dir}
    if args.ttl:
        kwargs["ttl_seconds"] = int(args.ttl)
    lock = acquire_lock(**kwargs)
    _output(lock.to_dict())


def cmd_lock_release(args):
    from .locks import release_lock
    result = release_lock(args.task_id, args.agent, base_dir=args.base_dir)
    _output({"released": result})


def cmd_lock_status(args):
    from .locks import lock_status
    lock = lock_status(args.task_id, base_dir=args.base_dir)
    if lock is None:
        _output({"locked": False})
    else:
        data = lock.to_dict()
        data["locked"] = True
        _output(data)


# ── Trigger commands ─────────────────────────────────────────────────

def cmd_trigger_list(args):
    from .triggers import list_triggers
    triggers = list_triggers(args.workstream_id, base_dir=args.base_dir)
    _output([t.to_dict() for t in triggers])


def cmd_trigger_create(args):
    from .triggers import create_trigger
    kwargs = {
        "workstream_id": args.workstream_id,
        "on_state": args.on_state,
        "action": args.action,
        "base_dir": args.base_dir,
    }
    if args.agent:
        kwargs["agent"] = args.agent
    if args.command:
        kwargs["command"] = args.command
    trigger = create_trigger(**kwargs)
    _output(trigger.to_dict())


def cmd_trigger_delete(args):
    from .triggers import delete_trigger
    delete_trigger(args.trigger_id, base_dir=args.base_dir)
    _output({"deleted": True, "trigger_id": args.trigger_id})


# ── Agent commands ───────────────────────────────────────────────────

def cmd_agent_list(args):
    from .agents import list_agents
    agents = list_agents(base_dir=args.base_dir)
    _output(agents)


def cmd_agent_run(args):
    from .agents import run_agent
    result = run_agent(args.agent_name, args.task, base_dir=args.base_dir)
    _output(result)


# ── Artifact commands ────────────────────────────────────────────────

def cmd_artifact_create(args):
    from .artifacts import create_artifact
    result = create_artifact(args.path, args.content, base_dir=args.base_dir)
    _output(result)


def cmd_artifact_read(args):
    from .artifacts import read_artifact
    content = read_artifact(args.path, base_dir=args.base_dir)
    _output({"path": args.path, "content": content})


def cmd_artifact_list(args):
    from .artifacts import list_artifacts
    artifacts = list_artifacts(prefix=args.prefix, base_dir=args.base_dir)
    _output(artifacts)


# ── Parser ───────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestration Framework CLI",
        prog="orchestration",
    )
    parser.add_argument(
        "--base-dir", default=".",
        help="Base directory for workspace data (default: current directory)",
    )
    subparsers = parser.add_subparsers(dest="concept", required=True)

    # ── Workstream ───────────────────────────────────────────────────
    ws_parser = subparsers.add_parser("workstream")
    ws_sub = ws_parser.add_subparsers(dest="method", required=True)

    p = ws_sub.add_parser("create")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.add_argument("--parent")
    p.add_argument("--states", help="JSON string of task states map")
    p.add_argument("--retry", help="JSON string of retry config")
    p.set_defaults(func=cmd_workstream_create)

    p = ws_sub.add_parser("list")
    p.set_defaults(func=cmd_workstream_list)

    p = ws_sub.add_parser("read")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_read)

    p = ws_sub.add_parser("find")
    p.add_argument("--query", required=True)
    p.set_defaults(func=cmd_workstream_find)

    p = ws_sub.add_parser("tree")
    p.set_defaults(func=cmd_workstream_tree)

    # ── Task ─────────────────────────────────────────────────────────
    task_parser = subparsers.add_parser("task")
    task_sub = task_parser.add_subparsers(dest="method", required=True)

    p = task_sub.add_parser("create")
    p.add_argument("workstream_id")
    p.add_argument("--title", required=True)
    p.add_argument("--description")
    p.add_argument("--tags")
    p.add_argument("--retry", help="JSON string of retry config")
    p.set_defaults(func=cmd_task_create)

    p = task_sub.add_parser("read")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_read)

    p = task_sub.add_parser("update")
    p.add_argument("task_id")
    p.add_argument("--status")
    p.add_argument("--description")
    p.add_argument("--tags")
    p.set_defaults(func=cmd_task_update)

    p = task_sub.add_parser("list")
    p.add_argument("workstream_id")
    p.add_argument("--status")
    p.add_argument("--tags")
    p.set_defaults(func=cmd_task_list)

    p = task_sub.add_parser("comment")
    p.add_argument("task_id")
    p.add_argument("--message", required=True)
    p.set_defaults(func=cmd_task_comment)

    p = task_sub.add_parser("archive")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_archive)

    p = task_sub.add_parser("audit")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_audit)

    # ── Lock ─────────────────────────────────────────────────────────
    lock_parser = subparsers.add_parser("lock")
    lock_sub = lock_parser.add_subparsers(dest="method", required=True)

    p = lock_sub.add_parser("acquire")
    p.add_argument("task_id")
    p.add_argument("--agent", required=True)
    p.add_argument("--ttl", help="TTL in seconds (default: 900)")
    p.set_defaults(func=cmd_lock_acquire)

    p = lock_sub.add_parser("release")
    p.add_argument("task_id")
    p.add_argument("--agent", required=True)
    p.set_defaults(func=cmd_lock_release)

    p = lock_sub.add_parser("status")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_lock_status)

    # ── Trigger ──────────────────────────────────────────────────────
    trigger_parser = subparsers.add_parser("trigger")
    trigger_sub = trigger_parser.add_subparsers(dest="method", required=True)

    p = trigger_sub.add_parser("list")
    p.add_argument("workstream_id")
    p.set_defaults(func=cmd_trigger_list)

    p = trigger_sub.add_parser("create")
    p.add_argument("workstream_id")
    p.add_argument("--on-state", required=True)
    p.add_argument("--action", required=True, choices=["run_agent", "run_command"])
    p.add_argument("--agent")
    p.add_argument("--command")
    p.set_defaults(func=cmd_trigger_create)

    p = trigger_sub.add_parser("delete")
    p.add_argument("trigger_id")
    p.set_defaults(func=cmd_trigger_delete)

    # ── Agent ────────────────────────────────────────────────────────
    agent_parser = subparsers.add_parser("agent")
    agent_sub = agent_parser.add_subparsers(dest="method", required=True)

    p = agent_sub.add_parser("list")
    p.set_defaults(func=cmd_agent_list)

    p = agent_sub.add_parser("run")
    p.add_argument("agent_name")
    p.add_argument("--task", required=True)
    p.set_defaults(func=cmd_agent_run)

    # ── Artifact ─────────────────────────────────────────────────────
    artifact_parser = subparsers.add_parser("artifact")
    artifact_sub = artifact_parser.add_subparsers(dest="method", required=True)

    p = artifact_sub.add_parser("create")
    p.add_argument("--path", required=True)
    p.add_argument("--content", required=True)
    p.set_defaults(func=cmd_artifact_create)

    p = artifact_sub.add_parser("read")
    p.add_argument("path")
    p.set_defaults(func=cmd_artifact_read)

    p = artifact_sub.add_parser("list")
    p.add_argument("--prefix")
    p.set_defaults(func=cmd_artifact_list)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        args.func(args)
    except FileNotFoundError as e:
        _error(str(e), "NOT_FOUND")
    except ValueError as e:
        _error(str(e), "INVALID_TRANSITION")
    except RuntimeError as e:
        msg = str(e)
        if "locked" in msg.lower() or "contention" in msg.lower():
            _error(msg, "TASK_LOCKED")
        else:
            _error(msg, "RUNTIME_ERROR")
    except json.JSONDecodeError as e:
        _error(f"Invalid JSON: {e}", "INVALID_JSON")
    except Exception as e:
        _error(str(e), "ERROR")


if __name__ == "__main__":
    main()
