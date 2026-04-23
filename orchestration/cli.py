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
    if args.mounted_workspace_path:
        kwargs["mounted_workspace_path"] = args.mounted_workspace_path
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


def cmd_workstream_descendants(args):
    from .workstreams import list_workstreams

    root_id = args.id
    include_self = args.include_self
    workstreams = list_workstreams(base_dir=args.base_dir)

    # Build parent -> children mapping and id -> workstream lookup.
    children = {}
    by_id = {}
    for ws in workstreams:
        by_id[ws.id] = ws
        if ws.parent_id is not None:
            children.setdefault(ws.parent_id, []).append(ws)

    if root_id not in by_id:
        raise FileNotFoundError(f"Workstream {root_id} not found")

    # DFS from the requested root. Return deterministic sibling ordering.
    result = []
    stack = [(root_id, 0)]
    while stack:
        ws_id, depth = stack.pop()
        ws = by_id[ws_id]
        if include_self or depth > 0:
            result.append({
                "id": ws.id,
                "name": ws.name,
                "parent_id": ws.parent_id,
                "depth": depth,
            })

        kids = sorted(children.get(ws_id, []), key=lambda w: (w.name.lower(), w.id), reverse=True)
        for kid in kids:
            stack.append((kid.id, depth + 1))

    _output(result)


# ── Env commands ─────────────────────────────────────────────────────

def cmd_env_set(args):
    from .workstreams import set_workstream_env_key
    result = set_workstream_env_key(
        ws_id=args.workstream_id,
        key=args.key,
        value=args.value,
        base_dir=args.base_dir,
    )
    _output(result)


def cmd_env_unset(args):
    from .workstreams import unset_workstream_env_key
    result = unset_workstream_env_key(
        ws_id=args.workstream_id,
        key=args.key,
        base_dir=args.base_dir,
    )
    _output(result)


def cmd_env_get(args):
    from .workstreams import resolve_env_key
    value = resolve_env_key(
        key=args.key,
        workstream_id=args.workstream,
        task_id=args.task,
        base_dir=args.base_dir,
    )
    _output({
        "key": args.key,
        "value": value,
        "found": value is not None,
    })


def cmd_env_list(args):
    from .workstreams import (
        list_effective_env,
        read_workstream_env,
    )
    if args.local:
        if args.task:
            from .tasks import read_task
            task = read_task(args.task, base_dir=args.base_dir)
            workstream_id = task.workstream_id
        else:
            workstream_id = args.workstream
        env_map = read_workstream_env(workstream_id, base_dir=args.base_dir)
    else:
        env_map = list_effective_env(
            workstream_id=args.workstream,
            task_id=args.task,
            base_dir=args.base_dir,
            include_system=args.include_system,
        )
    _output(env_map)


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
    if args.scheduled_at:
        kwargs["scheduled_at"] = args.scheduled_at
    if args.scheduled_action:
        kwargs["scheduled_action"] = json.loads(args.scheduled_action)
    if args.attachment is not None:
        kwargs["attachments"] = args.attachment
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
    if args.scheduled_at:
        kwargs["scheduled_at"] = args.scheduled_at
    if args.scheduled_action:
        kwargs["scheduled_action"] = json.loads(args.scheduled_action)
    if args.attachment is not None:
        kwargs["attachments"] = args.attachment
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
    task = comment_task(args.task_id, args.message, author=args.author, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_archive(args):
    from .tasks import archive_task
    result = archive_task(args.task_id, base_dir=args.base_dir)
    _output(result)


def cmd_task_audit(args):
    from .tasks import get_audit
    audit = get_audit(args.task_id, base_dir=args.base_dir)
    _output(audit)


def cmd_task_clear_schedule(args):
    from .tasks import clear_schedule
    task = clear_schedule(args.task_id, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_move(args):
    from .tasks import move_task
    kwargs = {"task_id": args.task_id, "target_workstream_id": args.workstream_id, "base_dir": args.base_dir}
    if args.status:
        kwargs["target_status"] = args.status
    task = move_task(**kwargs)
    _output(task.to_dict())


def cmd_task_duplicate(args):
    from .tasks import duplicate_task
    kwargs = {"task_id": args.task_id, "base_dir": args.base_dir}
    if args.workstream_id:
        kwargs["target_workstream_id"] = args.workstream_id
    if args.status:
        kwargs["target_status"] = args.status
    task = duplicate_task(**kwargs)
    _output(task.to_dict())


def cmd_task_attach(args):
    from .tasks import attach_to_task
    task = attach_to_task(args.task_id, args.path, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_detach(args):
    from .tasks import detach_from_task
    task = detach_from_task(args.task_id, args.path, base_dir=args.base_dir)
    _output(task.to_dict())


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
        "action": args.action,
        "base_dir": args.base_dir,
    }
    if args.on_state:
        kwargs["on_state"] = args.on_state
    if args.on_schedule:
        kwargs["on_schedule"] = args.on_schedule
    if args.filter:
        kwargs["filter"] = json.loads(args.filter)
    if args.agent:
        kwargs["agent"] = args.agent
    if args.command:
        kwargs["command"] = args.command
    if args.max_concurrent is not None:
        kwargs["max_concurrent"] = args.max_concurrent
    if args.prompt:
        kwargs["prompt"] = args.prompt
    if args.timeout is not None:
        kwargs["timeout"] = args.timeout
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
    task_ids = [args.task] if args.task else []
    result = run_agent(args.agent_name, task_ids=task_ids, workstream_id=args.workstream or None, base_dir=args.base_dir)
    _output(result)


def cmd_agent_active(args):
    from .agents import list_active_agents
    runs = list_active_agents(base_dir=args.base_dir)
    _output(runs)


def cmd_agent_tail(args):
    from .agents import tail_active_agent
    result = tail_active_agent(args.run_id, lines=args.lines, base_dir=args.base_dir)
    _output(result)


# ── Workstream pause/resume ──────────────────────────────────────────

def cmd_workstream_pause(args):
    from .workstreams import read_workstream, save_workstream
    from .workspace_audit import log_event
    ws = read_workstream(args.id, base_dir=args.base_dir)
    ws.paused = True
    save_workstream(ws, args.base_dir)
    log_event("workstream_paused", f"Workstream '{ws.name}' paused", args.base_dir, workstream_id=ws.id)
    _output(ws.to_dict())


def cmd_workstream_resume(args):
    from .workstreams import read_workstream, save_workstream
    from .workspace_audit import log_event
    ws = read_workstream(args.id, base_dir=args.base_dir)
    ws.paused = False
    save_workstream(ws, args.base_dir)
    log_event("workstream_resumed", f"Workstream '{ws.name}' resumed", args.base_dir, workstream_id=ws.id)
    _output(ws.to_dict())


# ── Scheduler commands ───────────────────────────────────────────────

def cmd_scheduler_run(args):
    from .scheduler import run
    run(base_dir=args.base_dir)


def cmd_scheduler_stop(args):
    from .scheduler import stop
    result = stop(base_dir=args.base_dir)
    _output(result)


def cmd_scheduler_status(args):
    from .scheduler import status
    result = status(base_dir=args.base_dir)
    _output(result)


def cmd_scheduler_tick(args):
    from .scheduler import tick
    result = tick(base_dir=args.base_dir)
    _output(result)


# ── Artifact commands ────────────────────────────────────────────────

def cmd_audit_log(args):
    from .workspace_audit import get_audit_log
    limit = args.limit if args.limit else 50
    entries = get_audit_log(
        base_dir=args.base_dir,
        limit=limit,
        workstream_id=args.workstream or None,
        event_type=args.type or None,
    )
    _output(entries)


def cmd_artifact_create(args):
    from .artifacts import create_artifact
    result = create_artifact(args.path, args.content, base_dir=args.base_dir, workstream_id=args.workstream)
    _output(result)


def cmd_artifact_read(args):
    from .artifacts import read_artifact
    from .workspace_audit import log_event
    import os
    import json as _json
    content = read_artifact(args.path, base_dir=args.base_dir, workstream_id=args.workstream)

    # If an agent invocation is active, record which artifact it accessed.
    run_id = os.getenv("ORCHESTRATION_AGENT_RUN_ID")
    if run_id:
        extra = {
            "status": "ok",
            "run_id": run_id,
            "artifact_path": args.path,
        }
        agent_name = os.getenv("ORCHESTRATION_AGENT_NAME")
        if agent_name:
            extra["agent"] = agent_name
        ws_id = args.workstream or os.getenv("ORCHESTRATION_AGENT_WORKSTREAM_ID")
        if ws_id:
            extra["workstream_id"] = ws_id
        raw_task_ids = os.getenv("ORCHESTRATION_AGENT_TASK_IDS")
        if raw_task_ids:
            try:
                task_ids = _json.loads(raw_task_ids)
            except Exception:
                task_ids = []
            if isinstance(task_ids, list) and task_ids:
                if len(task_ids) == 1:
                    extra["task_id"] = task_ids[0]
                else:
                    extra["task_ids"] = task_ids

        log_event(
            "artifact_read",
            f"Artifact read: {args.path}",
            args.base_dir,
            **extra,
        )

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
    p.add_argument("--mounted-workspace-path", help="Path to mounted workspace root for descendants")
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

    p = ws_sub.add_parser("descendants")
    p.add_argument("id", help="Root workstream ID to traverse")
    p.add_argument("--include-self", action="store_true", help="Include the root workstream in output")
    p.set_defaults(func=cmd_workstream_descendants)

    # ── Task ─────────────────────────────────────────────────────────
    env_parser = subparsers.add_parser("env")
    env_sub = env_parser.add_subparsers(dest="method", required=True)

    p = env_sub.add_parser("set")
    p.add_argument("workstream_id")
    p.add_argument("key")
    p.add_argument("value")
    p.set_defaults(func=cmd_env_set)

    p = env_sub.add_parser("unset")
    p.add_argument("workstream_id")
    p.add_argument("key")
    p.set_defaults(func=cmd_env_unset)

    p = env_sub.add_parser("get")
    p.add_argument("key")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--workstream")
    scope.add_argument("--task")
    p.set_defaults(func=cmd_env_get)

    p = env_sub.add_parser("list")
    scope = p.add_mutually_exclusive_group(required=True)
    scope.add_argument("--workstream")
    scope.add_argument("--task")
    p.add_argument("--local", action="store_true", help="List only .env keys from the selected scope")
    p.add_argument("--include-system", action="store_true", help="Include process environment keys in effective output")
    p.set_defaults(func=cmd_env_list)

    # ── Task ─────────────────────────────────────────────────────────
    task_parser = subparsers.add_parser("task")
    task_sub = task_parser.add_subparsers(dest="method", required=True)

    p = task_sub.add_parser("create")
    p.add_argument("workstream_id")
    p.add_argument("--title", required=True)
    p.add_argument("--description")
    p.add_argument("--tags")
    p.add_argument("--retry", help="JSON string of retry config")
    p.add_argument("--scheduled-at", help="ISO datetime for one-shot schedule")
    p.add_argument("--scheduled-action", help="JSON string of action to run")
    p.add_argument("--attachment", action="append", dest="attachment", help="Artifact path to attach (repeatable)")
    p.set_defaults(func=cmd_task_create)

    p = task_sub.add_parser("read")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_read)

    p = task_sub.add_parser("update")
    p.add_argument("task_id")
    p.add_argument("--status")
    p.add_argument("--description")
    p.add_argument("--tags")
    p.add_argument("--scheduled-at", help="ISO datetime for one-shot schedule")
    p.add_argument("--scheduled-action", help="JSON string of action to run")
    p.add_argument("--attachment", action="append", dest="attachment", help="Set attachments to these artifact paths (repeatable)")
    p.set_defaults(func=cmd_task_update)

    p = task_sub.add_parser("list")
    p.add_argument("workstream_id")
    p.add_argument("--status")
    p.add_argument("--tags")
    p.set_defaults(func=cmd_task_list)

    p = task_sub.add_parser("comment")
    p.add_argument("task_id")
    p.add_argument("--message", required=True)
    p.add_argument("--author", help="Optional comment author (agent/user)")
    p.set_defaults(func=cmd_task_comment)

    p = task_sub.add_parser("archive")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_archive)

    p = task_sub.add_parser("audit")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_audit)

    p = task_sub.add_parser("clear-schedule")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_clear_schedule)

    p = task_sub.add_parser("move")
    p.add_argument("task_id")
    p.add_argument("workstream_id", help="Target workstream ID")
    p.add_argument("--status", help="Target state in the destination workstream (defaults to initial state)")
    p.set_defaults(func=cmd_task_move)

    p = task_sub.add_parser("duplicate")
    p.add_argument("task_id")
    p.add_argument("--workstream-id", dest="workstream_id", help="Target workstream ID (defaults to same workstream)")
    p.add_argument("--status", help="Target state in the destination workstream (defaults to initial state)")
    p.set_defaults(func=cmd_task_duplicate)

    p = task_sub.add_parser("attach")
    p.add_argument("task_id")
    p.add_argument("--path", required=True, help="Artifact path to attach")
    p.set_defaults(func=cmd_task_attach)

    p = task_sub.add_parser("detach")
    p.add_argument("task_id")
    p.add_argument("--path", required=True, help="Artifact path to detach")
    p.set_defaults(func=cmd_task_detach)

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
    p.add_argument("--on-state", help="State transition that fires this trigger")
    p.add_argument("--on-schedule", help="Cron expression for schedule-based trigger")
    p.add_argument("--filter", help="JSON string of task filter for schedule triggers")
    p.add_argument("--action", required=True, choices=["run_agent", "run_command"])
    p.add_argument("--agent")
    p.add_argument("--command")
    p.add_argument("--max-concurrent", type=int, default=None, help="Max parallel executions within the workstream (default: 1)")
    p.add_argument("--prompt", help="Custom prompt to inject into the agent when this trigger fires")
    p.add_argument("--timeout", type=int, default=None, help="Agent execution timeout in seconds (overrides agent x-timeout)")
    p.set_defaults(func=cmd_trigger_create)

    p = trigger_sub.add_parser("delete")
    p.add_argument("trigger_id")
    p.set_defaults(func=cmd_trigger_delete)

    p = ws_sub.add_parser("pause")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_pause)

    p = ws_sub.add_parser("resume")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_resume)

    # ── Agent ────────────────────────────────────────────────────────
    agent_parser = subparsers.add_parser("agent")
    agent_sub = agent_parser.add_subparsers(dest="method", required=True)

    p = agent_sub.add_parser("list")
    p.set_defaults(func=cmd_agent_list)

    p = agent_sub.add_parser("run")
    p.add_argument("agent_name")
    p.add_argument("--task", default=None)
    p.add_argument("--workstream", default=None)
    p.set_defaults(func=cmd_agent_run)

    p = agent_sub.add_parser("active")
    p.set_defaults(func=cmd_agent_active)

    p = agent_sub.add_parser("tail")
    p.add_argument("run_id")
    p.add_argument("--lines", type=int, default=200)
    p.set_defaults(func=cmd_agent_tail)

    # ── Scheduler ────────────────────────────────────────────────────
    sched_parser = subparsers.add_parser("scheduler")
    sched_sub = sched_parser.add_subparsers(dest="method", required=True)

    p = sched_sub.add_parser("run", help="Run scheduler as a foreground process (ticks every 60s)")
    p.set_defaults(func=cmd_scheduler_run)

    p = sched_sub.add_parser("stop", help="Stop a running scheduler process")
    p.set_defaults(func=cmd_scheduler_stop)

    p = sched_sub.add_parser("status")
    p.set_defaults(func=cmd_scheduler_status)

    p = sched_sub.add_parser("tick", help="Execute a single scheduler tick")
    p.set_defaults(func=cmd_scheduler_tick)

    # ── Audit ────────────────────────────────────────────────────────
    audit_parser = subparsers.add_parser("audit")
    audit_sub = audit_parser.add_subparsers(dest="method", required=True)

    p = audit_sub.add_parser("log")
    p.add_argument("--workstream", default=None, help="Filter by workstream ID")
    p.add_argument("--type", default=None, help="Filter by event type")
    p.add_argument("--limit", type=int, default=50, help="Max entries to return")
    p.set_defaults(func=cmd_audit_log)

    # ── Artifact ─────────────────────────────────────────────────────
    artifact_parser = subparsers.add_parser("artifact")
    artifact_sub = artifact_parser.add_subparsers(dest="method", required=True)

    p = artifact_sub.add_parser("create")
    p.add_argument("--path", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--workstream", default=None, help="Optional workstream context for mounted artifact routing")
    p.set_defaults(func=cmd_artifact_create)

    p = artifact_sub.add_parser("read")
    p.add_argument("path")
    p.add_argument("--workstream", default=None, help="Optional workstream context for mounted artifact routing")
    p.set_defaults(func=cmd_artifact_read)

    p = artifact_sub.add_parser("list")
    p.add_argument("--prefix")
    p.set_defaults(func=cmd_artifact_list)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    run_id = os.getenv("ORCHESTRATION_AGENT_RUN_ID")
    agent_name = os.getenv("ORCHESTRATION_AGENT_NAME")

    def _log_cli_call(status: str, error_code: str = None, error_message: str = None):
        if not run_id:
            return
        from .workspace_audit import log_event
        extra = {
            "run_id": run_id,
            "status": status,
            "command": " ".join(sys.argv[1:]),
            "concept": getattr(args, "concept", None),
            "method": getattr(args, "method", None),
        }
        if agent_name:
            extra["agent"] = agent_name
        ws_id = getattr(args, "workstream", None) or os.getenv("ORCHESTRATION_AGENT_WORKSTREAM_ID")
        if ws_id:
            extra["workstream_id"] = ws_id
        if error_code:
            extra["error_code"] = error_code
        if error_message:
            extra["error_message"] = error_message
        log_event(
            "orchestration_cli_call",
            f"orchestration cli: {' '.join(sys.argv[1:])}",
            args.base_dir,
            **extra,
        )

    try:
        args.func(args)
        _log_cli_call("ok")
    except FileNotFoundError as e:
        _log_cli_call("error", "NOT_FOUND", str(e))
        _error(str(e), "NOT_FOUND")
    except ValueError as e:
        _log_cli_call("error", "INVALID_TRANSITION", str(e))
        _error(str(e), "INVALID_TRANSITION")
    except RuntimeError as e:
        msg = str(e)
        if "locked" in msg.lower() or "contention" in msg.lower():
            _log_cli_call("error", "TASK_LOCKED", msg)
            _error(msg, "TASK_LOCKED")
        else:
            _log_cli_call("error", "RUNTIME_ERROR", msg)
            _error(msg, "RUNTIME_ERROR")
    except json.JSONDecodeError as e:
        _log_cli_call("error", "INVALID_JSON", f"Invalid JSON: {e}")
        _error(f"Invalid JSON: {e}", "INVALID_JSON")
    except Exception as e:
        _log_cli_call("error", "ERROR", str(e))
        _error(str(e), "ERROR")


if __name__ == "__main__":
    main()
