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
import csv
import json
import os
from pathlib import Path
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


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
    if args.context is not None:
        kwargs["context"] = args.context
    if args.parent:
        kwargs["parent_id"] = args.parent
    if args.states:
        kwargs["task_states"] = json.loads(args.states)
    if args.retry:
        kwargs["retry"] = json.loads(args.retry)
    if args.working_directory:
        kwargs["working_directory"] = args.working_directory
    if args.artifact_root:
        kwargs["artifact_root"] = args.artifact_root
    if args.child_workstream_root:
        kwargs["child_workstream_root"] = args.child_workstream_root
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


def cmd_workstream_context(args):
    from .workstreams import read_workstream_context
    _output(read_workstream_context(args.id, base_dir=args.base_dir))


def cmd_workstream_gettags(args):
    from .workstreams import get_workstream_tags
    _output(get_workstream_tags(args.id, base_dir=args.base_dir))


def cmd_workstream_upsert_tag(args):
    from .workstreams import upsert_workstream_tag
    _output(upsert_workstream_tag(args.id, args.name, args.color, base_dir=args.base_dir))


def cmd_workstream_update_context(args):
    from .workstreams import set_workstream_context
    ws = set_workstream_context(
        args.id,
        context=args.content,
        base_dir=args.base_dir,
        updated_by=args.updated_by,
    )
    _output(ws.to_dict())


def cmd_workstream_agent_concurrency(args):
    from .workstreams import read_workstream_agent_concurrency
    _output(read_workstream_agent_concurrency(args.id, base_dir=args.base_dir))


def cmd_workstream_update_agent_concurrency(args):
    from .workstreams import set_workstream_agent_concurrency

    try:
        policy = json.loads(args.policy)
    except json.JSONDecodeError as exc:
        _error(f"Invalid JSON for --policy: {exc}")

    ws = set_workstream_agent_concurrency(
        args.id,
        agent_concurrency=policy,
        base_dir=args.base_dir,
        updated_by=args.updated_by,
    )
    _output(ws.to_dict())


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


def cmd_workstream_token_usage(args):
    from .tasks import list_tasks

    def clean_task_title(task):
        title = str(task.title or "")
        prefix = "[CORRUPT] "
        if title.startswith(prefix):
            title = title[len(prefix):]
        if title == f"{task.id}.yaml":
            title = task.id
        return title

    def latest_run_token_usage(task_id):
        runs_dir = Path(args.base_dir) / ".orchestration" / "agent_runs"
        best_updated_at = ""
        best_usage = None
        if not runs_dir.exists():
            return None

        for path in runs_dir.glob("*.json"):
            try:
                with path.open(encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            token_usage = (data.get("beans_proxy") or {}).get("task_totals")
            if not isinstance(token_usage, dict):
                continue
            if token_usage.get("pseudo_key") != f"task-{task_id}":
                continue
            updated_at = str(token_usage.get("updated_at") or "")
            if best_usage is None or updated_at > best_updated_at:
                best_updated_at = updated_at
                best_usage = token_usage
        return best_usage

    output_path = args.output or f"tokens_{args.id}.csv"
    output_dir = os.path.dirname(os.path.abspath(output_path))
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    tasks = list_tasks(args.id, base_dir=args.base_dir)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["task ID", "task title", "input tokens", "output tokens"])
        for task in tasks:
            token_usage = task.token_usage or latest_run_token_usage(task.id) or {}
            writer.writerow([
                task.id,
                clean_task_title(task),
                int(token_usage.get("input_tokens", 0) or 0),
                int(token_usage.get("output_tokens", 0) or 0),
            ])

    _output({"path": output_path, "task_count": len(tasks)})


def cmd_workstream_migrate_artifact_root_home(args):
    from .migration import migrate_artifact_root_home

    result = migrate_artifact_root_home(
        args.id,
        base_dir=args.base_dir,
        dry_run=not args.apply,
        target_root=args.target_root,
        archive_conflicts=args.archive_conflicts,
    )
    _output(result)


def cmd_workstream_migrate_child_layout(args):
    from .migration import migrate_child_workstream_layout

    result = migrate_child_workstream_layout(
        base_dir=args.base_dir,
        dry_run=not args.apply,
    )
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


def cmd_task_clear_errors(args):
    from .tasks import clear_task_errors
    task = clear_task_errors(args.task_id, error_id=args.error_id, base_dir=args.base_dir)
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


def cmd_task_pause(args):
    from .tasks import pause_task
    task = pause_task(args.task_id, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_resume(args):
    from .tasks import resume_task
    task = resume_task(args.task_id, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_reorder(args):
    from .tasks import reorder_tasks_in_workstream
    result = reorder_tasks_in_workstream(args.workstream_id, base_dir=args.base_dir)
    _output(result)


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


def cmd_task_move_up(args):
    from .tasks import move_task_up
    task = move_task_up(args.task_id, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_move_down(args):
    from .tasks import move_task_down
    task = move_task_down(args.task_id, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_move_before(args):
    from .tasks import move_task_before
    task = move_task_before(args.task_id, args.target_task_id, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_move_after(args):
    from .tasks import move_task_after
    task = move_task_after(args.task_id, args.target_task_id, base_dir=args.base_dir)
    _output(task.to_dict())


def cmd_task_move_to_index(args):
    from .tasks import move_task_to_index
    task = move_task_to_index(args.task_id, args.index, base_dir=args.base_dir)
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


def cmd_lock_list(args):
    from .locks import list_workstream_locks
    _output(list_workstream_locks(args.workstream_id, base_dir=args.base_dir))


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
    if args.on_email_recipient:
        kwargs["on_email"] = {
            "recipient": args.on_email_recipient,
            "event": args.on_email_event or "new_thread",
        }
    if args.task_selection:
        kwargs["task_selection"] = args.task_selection
    if args.filter:
        kwargs["filter"] = json.loads(args.filter)
    if args.agent:
        kwargs["agent"] = args.agent
    if args.command:
        kwargs["command"] = args.command
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


def cmd_trigger_pause(args):
    from .triggers import pause_trigger
    trigger = pause_trigger(args.trigger_id, base_dir=args.base_dir)
    _output(trigger.to_dict())


def cmd_trigger_resume(args):
    from .triggers import resume_trigger
    trigger = resume_trigger(args.trigger_id, base_dir=args.base_dir)
    _output(trigger.to_dict())


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


def cmd_agent_details(args):
    from .agents import get_agent_run
    result = get_agent_run(args.run_id, base_dir=args.base_dir)
    _output(result)


def cmd_agent_context(args):
    from .agents import get_agent_run_context, read_agent_run_context_file
    if args.path:
        result = read_agent_run_context_file(args.run_id, args.path, base_dir=args.base_dir)
    else:
        result = get_agent_run_context(args.run_id, base_dir=args.base_dir)
    _output(result)


def cmd_agent_kill(args):
    from .agents import kill_agent_run
    result = kill_agent_run(args.run_id, base_dir=args.base_dir)
    _output(result)


# ── Progress commands ────────────────────────────────────────────────

def _parse_progress_items(args):
    items = []
    if getattr(args, "items_json", None):
        loaded = json.loads(args.items_json)
        if not isinstance(loaded, list):
            raise ValueError("--items-json must be a JSON list")
        items.extend(loaded)
    for item in getattr(args, "item", None) or []:
        items.append(item)
    return items


def cmd_progress_init(args):
    from .progress import init_progress
    result = init_progress(
        _parse_progress_items(args),
        run_id=args.run,
        base_dir=args.base_dir,
    )
    _output(result)


def cmd_progress_add(args):
    from .progress import add_progress_items
    result = add_progress_items(
        _parse_progress_items(args),
        run_id=args.run,
        base_dir=args.base_dir,
    )
    _output(result)


def cmd_progress_read(args):
    from .progress import read_progress, read_progress_summary
    result = read_progress_summary(args.run, base_dir=args.base_dir) if args.summary else read_progress(args.run, base_dir=args.base_dir)
    _output(result or {"run_id": args.run, "items": [], "missing": True})


def cmd_progress_set(args):
    from .progress import update_progress_item
    result = update_progress_item(
        args.item_id,
        args.status,
        run_id=args.run,
        message=args.message,
        base_dir=args.base_dir,
    )
    _output(result)


def cmd_progress_current(args):
    from .progress import current_progress
    result = current_progress(args.run, base_dir=args.base_dir)
    _output(result)


def cmd_progress_next(args):
    from .progress import advance_progress
    result = advance_progress(
        args.item_id,
        run_id=args.run,
        message=args.message,
        base_dir=args.base_dir,
    )
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


def cmd_workstream_pause_columns(args):
    from .workstreams import pause_workstream_states
    ws = pause_workstream_states(args.id, args.state, base_dir=args.base_dir)
    _output(ws.to_dict())


def cmd_workstream_resume_columns(args):
    from .workstreams import resume_workstream_states
    ws = resume_workstream_states(args.id, args.state, base_dir=args.base_dir)
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
    from .scheduler import tick, status
    scheduler_status = status(base_dir=args.base_dir)
    if scheduler_status.get("running"):
        pid = scheduler_status.get("pid")
        pid_suffix = f" (pid={pid})" if pid else ""
        raise RuntimeError(
            "Scheduler tick is disabled while scheduler is running"
            f"{pid_suffix}. Stop scheduler first or wait for the next interval."
        )
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


def _artifact_workstream_context(args):
    return args.workstream or os.getenv("ORCHESTRATION_AGENT_WORKSTREAM_ID") or None


def cmd_artifact_create(args):
    from .artifacts import create_artifact, create_binary_artifact, _is_raster_image_path
    workstream_id = _artifact_workstream_context(args)
    if args.content is not None:
        if _is_raster_image_path(args.path):
            raise ValueError(
                "Raster image artifacts cannot be written with --content; use --content-base64 or --source-file"
            )
        result = create_artifact(args.path, args.content, base_dir=args.base_dir, workstream_id=workstream_id)
    else:
        result = create_binary_artifact(
            args.path,
            content_base64=args.content_base64,
            source_file=args.source_file,
            base_dir=args.base_dir,
            workstream_id=workstream_id,
        )
    _output(result)


def cmd_artifact_read(args):
    from .artifacts import read_artifact_for_cli
    from .workspace_audit import log_event
    import os
    import json as _json
    workstream_id = _artifact_workstream_context(args)
    artifact = read_artifact_for_cli(args.path, base_dir=args.base_dir, workstream_id=workstream_id)

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
        ws_id = workstream_id
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

    _output(artifact)


def cmd_artifact_list(args):
    from .artifacts import list_artifacts
    workstream_id = _artifact_workstream_context(args)
    artifacts = list_artifacts(prefix=args.prefix, base_dir=args.base_dir, workstream_id=workstream_id)
    _output(artifacts)


def cmd_artifact_copytree(args):
    from .artifacts import copy_artifact_tree
    result = copy_artifact_tree(
        args.source_prefix,
        base_dir=args.base_dir,
        workstream_id=args.workstream,
        source_base_dir=args.source_base,
        overwrite=args.overwrite,
        dry_run=args.dry_run,
    )
    _output(result)


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
    p.add_argument("--context")
    p.add_argument("--parent")
    p.add_argument("--states", help="JSON string of task states map")
    p.add_argument("--retry", help="JSON string of retry config")
    p.add_argument("--working-directory", help="Code workspace root for this workstream subtree")
    p.add_argument("--artifact-root", help="Artifact root for this workstream subtree")
    p.add_argument("--child-workstream-root", help="State root where child workstreams should be stored")
    p.set_defaults(func=cmd_workstream_create)

    p = ws_sub.add_parser("list")
    p.set_defaults(func=cmd_workstream_list)

    p = ws_sub.add_parser("read")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_read)

    p = ws_sub.add_parser("context")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_context)

    p = ws_sub.add_parser("agent-concurrency")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_agent_concurrency)

    p = ws_sub.add_parser("gettags")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_gettags)

    p = ws_sub.add_parser("upsert-tag")
    p.add_argument("id")
    p.add_argument("--name", required=True)
    p.add_argument("--color")
    p.set_defaults(func=cmd_workstream_upsert_tag)

    p = ws_sub.add_parser("update-context")
    p.add_argument("id")
    p.add_argument("--content", required=True)
    p.add_argument("--updated-by")
    p.set_defaults(func=cmd_workstream_update_context)

    p = ws_sub.add_parser("update-agent-concurrency")
    p.add_argument("id")
    p.add_argument("--policy", required=True, help="JSON object describing agent concurrency policy")
    p.add_argument("--updated-by")
    p.set_defaults(func=cmd_workstream_update_agent_concurrency)

    p = ws_sub.add_parser("find")
    p.add_argument("--query", required=True)
    p.set_defaults(func=cmd_workstream_find)

    p = ws_sub.add_parser("tree")
    p.set_defaults(func=cmd_workstream_tree)

    p = ws_sub.add_parser("descendants")
    p.add_argument("id", help="Root workstream ID to traverse")
    p.add_argument("--include-self", action="store_true", help="Include the root workstream in output")
    p.set_defaults(func=cmd_workstream_descendants)

    p = ws_sub.add_parser("token-usage", help="Export task token usage for a workstream to CSV")
    p.add_argument("id", help="Workstream ID to export")
    p.add_argument("--output", "-o", help="Output CSV path (default: tokens_<workstream_id>.csv)")
    p.set_defaults(func=cmd_workstream_token_usage)

    p = ws_sub.add_parser("migrate-artifact-root-home")
    p.add_argument("id", help="Root workstream ID whose explicit artifact_root should move into foundation storage")
    p.add_argument("--apply", action="store_true", help="Apply migration; default is dry run")
    p.add_argument("--target-root", help="Optional new artifact root path inside the foundation repo")
    p.add_argument("--archive-conflicts", action="store_true", help="Preserve conflicting source artifacts under artifacts/_migration_conflicts/<workstream_id>/ instead of failing")
    p.set_defaults(func=cmd_workstream_migrate_artifact_root_home)

    p = ws_sub.add_parser("migrate-child-layout")
    p.add_argument("--apply", action="store_true", help="Apply migration; default is dry run")
    p.set_defaults(func=cmd_workstream_migrate_child_layout)

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

    p = task_sub.add_parser("clear-errors")
    p.add_argument("task_id")
    p.add_argument("--error-id", default=None)
    p.set_defaults(func=cmd_task_clear_errors)

    p = task_sub.add_parser("archive")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_archive)

    p = task_sub.add_parser("audit")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_audit)

    p = task_sub.add_parser("clear-schedule")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_clear_schedule)

    p = task_sub.add_parser("pause")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_pause)

    p = task_sub.add_parser("resume")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_resume)

    p = task_sub.add_parser("move")
    p.add_argument("task_id")
    p.add_argument("workstream_id", help="Target workstream ID")
    p.add_argument("--status", help="Target state in the destination workstream (defaults to initial state)")
    p.set_defaults(func=cmd_task_move)

    p = task_sub.add_parser("reorder")
    p.add_argument("workstream_id")
    p.set_defaults(func=cmd_task_reorder)

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

    p = task_sub.add_parser("move-up")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_move_up)

    p = task_sub.add_parser("move-down")
    p.add_argument("task_id")
    p.set_defaults(func=cmd_task_move_down)

    p = task_sub.add_parser("move-before")
    p.add_argument("task_id")
    p.add_argument("target_task_id")
    p.set_defaults(func=cmd_task_move_before)

    p = task_sub.add_parser("move-after")
    p.add_argument("task_id")
    p.add_argument("target_task_id")
    p.set_defaults(func=cmd_task_move_after)

    p = task_sub.add_parser("move-to-index")
    p.add_argument("task_id")
    p.add_argument("index", type=int)
    p.set_defaults(func=cmd_task_move_to_index)

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

    p = lock_sub.add_parser("list")
    p.add_argument("workstream_id")
    p.set_defaults(func=cmd_lock_list)

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
    p.add_argument("--on-email-recipient", help="Recipient address for email-based triggers")
    p.add_argument("--on-email-event", choices=["new_thread"], help="Email event type")
    p.add_argument("--task-selection", choices=["first_unlocked", "all_unlocked"], help="For state triggers, run on the first unlocked task or batch all unlocked tasks in the state")
    p.add_argument("--filter", help="JSON string of task filter for schedule triggers")
    p.add_argument("--action", required=True, choices=["run_agent", "run_command"])
    p.add_argument("--agent")
    p.add_argument("--command")
    p.add_argument("--prompt", help="Custom prompt to inject into the agent when this trigger fires")
    p.add_argument("--timeout", type=int, default=None, help="Agent execution timeout in seconds (overrides agent x-timeout)")
    p.set_defaults(func=cmd_trigger_create)

    p = trigger_sub.add_parser("delete")
    p.add_argument("trigger_id")
    p.set_defaults(func=cmd_trigger_delete)

    p = trigger_sub.add_parser("pause")
    p.add_argument("trigger_id")
    p.set_defaults(func=cmd_trigger_pause)

    p = trigger_sub.add_parser("resume")
    p.add_argument("trigger_id")
    p.set_defaults(func=cmd_trigger_resume)

    p = ws_sub.add_parser("pause")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_pause)

    p = ws_sub.add_parser("resume")
    p.add_argument("id")
    p.set_defaults(func=cmd_workstream_resume)

    p = ws_sub.add_parser("pause-columns")
    p.add_argument("id")
    p.add_argument("state", nargs="+", help="One or more board column/state names to pause")
    p.set_defaults(func=cmd_workstream_pause_columns)

    p = ws_sub.add_parser("resume-columns")
    p.add_argument("id")
    p.add_argument("state", nargs="+", help="One or more board column/state names to resume")
    p.set_defaults(func=cmd_workstream_resume_columns)

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

    p = agent_sub.add_parser("details")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_agent_details)

    p = agent_sub.add_parser("context")
    p.add_argument("run_id")
    p.add_argument("--path", default=None, help="Read one copied raw context file by manifest copied_path")
    p.set_defaults(func=cmd_agent_context)

    p = agent_sub.add_parser("kill")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_agent_kill)

    # ── Progress ─────────────────────────────────────────────────────
    progress_parser = subparsers.add_parser("progress")
    progress_sub = progress_parser.add_subparsers(dest="method", required=True)

    p = progress_sub.add_parser("init")
    p.add_argument("--run", default=None, help="Agent run ID; defaults to ORCHESTRATION_AGENT_RUN_ID")
    p.add_argument("--item", action="append", help="Checklist item text (repeatable)")
    p.add_argument("--items-json", help="JSON list of item strings or objects")
    p.set_defaults(func=cmd_progress_init)

    p = progress_sub.add_parser("add")
    p.add_argument("--run", default=None, help="Agent run ID; defaults to ORCHESTRATION_AGENT_RUN_ID")
    p.add_argument("--item", action="append", help="Checklist item text to append (repeatable)")
    p.add_argument("--items-json", help="JSON list of item strings or objects to append")
    p.set_defaults(func=cmd_progress_add)

    p = progress_sub.add_parser("read")
    p.add_argument("--run", default=None, help="Agent run ID; defaults to ORCHESTRATION_AGENT_RUN_ID")
    p.add_argument("--summary", action="store_true", help="Return compact summary for UI display")
    p.set_defaults(func=cmd_progress_read)

    p = progress_sub.add_parser("set")
    p.add_argument("item_id")
    p.add_argument("status", choices=["pending", "active", "done", "blocked", "skipped"])
    p.add_argument("--run", default=None, help="Agent run ID; defaults to ORCHESTRATION_AGENT_RUN_ID")
    p.add_argument("--message")
    p.set_defaults(func=cmd_progress_set)

    for method, status in (("set-active", "active"), ("complete", "done"), ("block", "blocked"), ("skip", "skipped")):
        p = progress_sub.add_parser(method)
        p.add_argument("item_id")
        p.add_argument("--run", default=None, help="Agent run ID; defaults to ORCHESTRATION_AGENT_RUN_ID")
        p.add_argument("--message")
        p.set_defaults(func=cmd_progress_set, status=status)

    p = progress_sub.add_parser("current", help="Show the currently active checklist item for this run")
    p.add_argument("--run", default=None, help="Agent run ID; defaults to ORCHESTRATION_AGENT_RUN_ID")
    p.set_defaults(func=cmd_progress_current)

    p = progress_sub.add_parser("next", help="Complete the currently active item (if any) and activate <item_id>")
    p.add_argument("item_id")
    p.add_argument("--run", default=None, help="Agent run ID; defaults to ORCHESTRATION_AGENT_RUN_ID")
    p.add_argument("--message")
    p.set_defaults(func=cmd_progress_next)

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
    content_group = p.add_mutually_exclusive_group(required=True)
    content_group.add_argument("--content", help="Text content for text artifacts")
    content_group.add_argument("--content-base64", help="Base64-encoded binary content")
    content_group.add_argument("--source-file", help="Copy bytes from a local file")
    p.add_argument("--workstream", default=None, help="Optional workstream context for mounted artifact routing")
    p.set_defaults(func=cmd_artifact_create)

    p = artifact_sub.add_parser("read")
    p.add_argument("path")
    p.add_argument("--workstream", default=None, help="Optional workstream context for mounted artifact routing")
    p.set_defaults(func=cmd_artifact_read)

    p = artifact_sub.add_parser("list")
    p.add_argument("--prefix")
    p.add_argument("--workstream", default=None, help="Optional workstream context for mounted artifact routing")
    p.set_defaults(func=cmd_artifact_list)

    p = artifact_sub.add_parser("copytree")
    p.add_argument("source_prefix", help="Source artifact subtree path (relative to source artifacts root)")
    p.add_argument("--workstream", required=True, help="Destination workstream ID (must resolve to a mounted workspace)")
    p.add_argument("--source-base", default=None, help="Source workspace root (defaults to --base-dir)")
    p.add_argument("--overwrite", action="store_true", help="Overwrite conflicting destination files")
    p.add_argument("--dry-run", action="store_true", help="Preview copy actions without writing files")
    p.set_defaults(func=cmd_artifact_copytree)

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
