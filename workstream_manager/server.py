#!/usr/bin/env python3
"""Workstream Manager — thin HTTP bridge to the orchestration modules.

Serves a static frontend and proxies API requests to orchestration Python
modules directly (no subprocess overhead).

Usage:
    python -m workstream_manager              # default port 8080
    python -m workstream_manager --port 9000  # custom port
    python -m workstream_manager --base-dir /path/to/orchestration-workspace
"""

import json
import os
import shutil
import subprocess
import sys
import base64
import mimetypes
import threading
import time
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs, unquote

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 32  # increase listen backlog from default 5
    allow_reuse_address = True

# Resolve paths
SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
AUDIO_FILE_PATH_ENV_VAR = "AUDIO_FILE_PATH"
API_REQUEST_LOG_ENV_VAR = "WORKSTREAM_MANAGER_LOG_API_REQUESTS"

# Ensure workspace is on the Python path so orchestration imports work
if SOURCE_DIR not in sys.path:
    sys.path.insert(0, SOURCE_DIR)

from orchestration.persistence import resolve_workstream_root


def _resolve_workspace_dir_default() -> str:
    """Resolve managed workspace root.

    Precedence:
    1) WORKSTREAM_MANAGER_BASE_DIR (explicit override for web server)
    2) WORKSTREAM_ROOT (global orchestration persistence root)
    3) SOURCE_DIR (repository root fallback)
    """
    raw_manager_base = (os.environ.get("WORKSTREAM_MANAGER_BASE_DIR") or "").strip()
    if raw_manager_base:
        return os.path.abspath(os.path.expanduser(raw_manager_base))
    return resolve_workstream_root(SOURCE_DIR)


WORKSPACE_DIR = _resolve_workspace_dir_default()

from orchestration.workstreams import (
    list_workstreams, read_workstream, create_workstream,
    find_workstreams, save_workstream, list_workstream_hierarchy_env,
    list_effective_workstream_env, resolve_workstream_workspace, resolve_workstream_state_root,
    resolve_workstream_artifact_root, resolve_workstream_child_state_root, set_workstream_context,
    read_workstream_agent_concurrency, set_workstream_agent_concurrency,
    get_workstream_tags, pause_workstream_states, resume_workstream_states, upsert_workstream_tag,
    get_workstream_code_mount_statuses,
)
from orchestration.tasks import (
    CorruptTaskError, create_task, read_task, read_task_from_workstream, update_task, list_tasks,
    list_board_tasks,
    comment_task, delete_task_comment, edit_task_comment, archive_task, get_audit, clear_schedule,
    move_task, duplicate_task, attach_to_task, detach_from_task,
    move_task_before, move_task_after, move_task_to_index,
    pause_task, resume_task,
    _task_path,
)
from orchestration.locks import (
    acquire_lock, release_lock, lock_status, lock_status_for_workstream,
    list_workstream_locks_for_workstream,
)
from orchestration.triggers import create_trigger, list_triggers, delete_trigger, pause_trigger, resume_trigger, run_trigger_now, get_active_triggers
from orchestration.scheduler import status as scheduler_status
from orchestration.artifacts import read_artifact, _resolve_artifact_root, _validate_path
from orchestration.agents import get_agent_run, get_global_sound_mute, kill_agent_run, list_active_agents, list_agent_runs, read_agent_run_context_file, retry_agent_run, set_global_sound_mute, tail_active_agent
from orchestration.progress import read_progress_summary


def set_workspace_dir(base_dir: str) -> None:
    global WORKSPACE_DIR
    WORKSPACE_DIR = os.path.abspath(base_dir)


def _subprocess_env() -> dict:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = SOURCE_DIR if not existing else SOURCE_DIR + os.pathsep + existing
    return env


def _ok(data):
    return 200, json.dumps({"status": "ok", "data": data}, indent=2, default=str)


def _err(msg, code="ERROR"):
    return 400, json.dumps({"status": "error", "message": msg, "code": code})


def _human_name() -> str:
    return (os.environ.get("HUMAN_NAME") or "").strip() or "Anonymous Human"


def _api_request_logging_enabled() -> bool:
    return (os.environ.get(API_REQUEST_LOG_ENV_VAR) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

_TASK_COUNT_CACHE = {}
_TASK_COUNT_CACHE_LOCK = threading.RLock()


def _truthy_param(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        return False
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _parse_state_names(params) -> list[str]:
    raw = params.get("states")
    if raw is None:
        raw = params.get("state")
    if raw is None:
        raise ValueError("Missing required parameter: state")
    if isinstance(raw, list):
        values = raw
    elif isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("["):
            values = json.loads(stripped)
        else:
            values = [part.strip() for part in stripped.split(",")]
    else:
        values = [raw]
    normalized = []
    seen = set()
    for value in values:
        state = str(value or "").strip()
        if not state or state in seen:
            continue
        seen.add(state)
        normalized.append(state)
    if not normalized:
        raise ValueError("At least one state is required")
    return normalized


def _serialize_board_tasks(tasks):
    task_dicts = []
    for task in tasks:
        task_dict = {
            "id": task.id,
            "workstream_id": task.workstream_id,
            "title": task.title,
            "status": task.status,
            "tags": task.tags or [],
        }
        if getattr(task, "scheduled_at", None) is not None:
            task_dict["scheduled_at"] = task.scheduled_at
        if getattr(task, "retry_count", 0):
            task_dict["retry_count"] = task.retry_count
        if getattr(task, "last_failure_at", None) is not None:
            task_dict["last_failure_at"] = task.last_failure_at
        if getattr(task, "paused", False):
            task_dict["paused"] = True
        if getattr(task, "token_usage", None) is not None:
            task_dict["token_usage"] = task.token_usage
        if getattr(task, "task_errors", None):
            task_dict["task_errors"] = task.task_errors
        if hasattr(task, "_parse_error"):
            task_dict["_error"] = task._parse_error
        task_dict["lock"] = {"locked": False}
        task_dicts.append(task_dict)
    return task_dicts


_POLL_SIDEBAR_CACHE_TTL_SECONDS = 5.0
_poll_sidebar_cache_lock = threading.Lock()
_poll_sidebar_cache = {
    "expires_at": 0.0,
    "data": None,
}


def _invalidate_poll_sidebar_cache() -> None:
    with _poll_sidebar_cache_lock:
        _poll_sidebar_cache["expires_at"] = 0.0
        _poll_sidebar_cache["data"] = None


def _build_poll_sidebar_snapshot() -> dict:
    wss = list_workstreams(base_dir=WORKSPACE_DIR, include_mount_status=False)
    counts = _task_counts_by_workstream(wss)
    active_run_summaries = []
    try:
        # Count only currently live runs from the active registry. This avoids
        # inflating the sidebar badge with stale historical metadata entries.
        _active_runs = list_active_agents(base_dir=WORKSPACE_DIR)
        active_run_summaries = [
            _serialize_agent_run_summary(run)
            for run in _active_runs
            if run.get("pid") is not None
        ]
        active_run_count = len(active_run_summaries)
    except Exception:
        active_run_count = 0
    return {
        "workstreams": [w.to_dict(include_transient=False) for w in wss],
        "counts": counts,
        "scheduler": scheduler_status(base_dir=WORKSPACE_DIR),
        "active_triggers": get_active_triggers(),
        "active_agent_runs": active_run_count,
        "active_agent_run_summaries": active_run_summaries,
    }


def _get_poll_sidebar_snapshot() -> dict:
    with _poll_sidebar_cache_lock:
        now = time.monotonic()
        cached = _poll_sidebar_cache["data"]
        if cached is not None and now < _poll_sidebar_cache["expires_at"]:
            return cached

        snapshot = _build_poll_sidebar_snapshot()
        _poll_sidebar_cache["data"] = snapshot
        _poll_sidebar_cache["expires_at"] = time.monotonic() + _POLL_SIDEBAR_CACHE_TTL_SECONDS
        return snapshot


def _task_storage_dir(workstream) -> str:
    ws_root = getattr(workstream, "_workspace_root", None)
    if not ws_root:
        try:
            ws_root = resolve_workstream_state_root(workstream.id, base_dir=WORKSPACE_DIR)
        except FileNotFoundError:
            ws_root = resolve_workstream_workspace(workstream.id, base_dir=WORKSPACE_DIR)
    return os.path.join(ws_root, "workstreams", workstream.id, "tasks")


def _workstream_storage_path(workstream) -> str:
    ws_root = getattr(workstream, "_workspace_root", None)
    if not ws_root:
        try:
            ws_root = resolve_workstream_state_root(workstream.id, base_dir=WORKSPACE_DIR)
        except FileNotFoundError:
            ws_root = resolve_workstream_workspace(workstream.id, base_dir=WORKSPACE_DIR)
    return os.path.join(ws_root, "workstreams", f"{workstream.id}.yaml")


def _file_fingerprint(path: str) -> tuple[int, int]:
    try:
        stat = os.stat(path)
        return stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return 0, 0


def _task_yaml_dir_fingerprint(tasks_dir: str) -> dict:
    count = 0
    latest_mtime_ns = 0
    total_size = 0
    try:
        with os.scandir(tasks_dir) as entries:
            for entry in entries:
                if not entry.name.endswith(".yaml"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    stat = entry.stat()
                except FileNotFoundError:
                    continue
                count += 1
                total_size += stat.st_size
                latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
    except (FileNotFoundError, NotADirectoryError):
        pass
    return {"count": count, "latest_mtime_ns": latest_mtime_ns, "total_size": total_size}


def _board_meta(workstream, task_count: int = None) -> dict:
    tasks_dir = _task_storage_dir(workstream)
    task_fp = _task_yaml_dir_fingerprint(tasks_dir)
    ws_mtime_ns, ws_size = _file_fingerprint(_workstream_storage_path(workstream))
    revision = ":".join(str(part) for part in (
        ws_mtime_ns,
        ws_size,
        task_fp["count"],
        task_fp["latest_mtime_ns"],
        task_fp["total_size"],
    ))
    return {
        "workstream": workstream.to_dict(),
        "revision": revision,
        "task_count": task_fp["count"] if task_count is None else task_count,
    }


def _count_task_files(tasks_dir: str) -> int:
    cache_key = os.path.abspath(tasks_dir)
    try:
        dir_stat = os.stat(tasks_dir)
    except FileNotFoundError:
        with _TASK_COUNT_CACHE_LOCK:
            _TASK_COUNT_CACHE.pop(cache_key, None)
        return 0

    with _TASK_COUNT_CACHE_LOCK:
        cached = _TASK_COUNT_CACHE.get(cache_key)
        if cached and cached["mtime_ns"] == dir_stat.st_mtime_ns:
            return cached["count"]

    try:
        with os.scandir(tasks_dir) as entries:
            count = sum(1 for entry in entries if entry.is_file() and entry.name.endswith(".yaml"))
    except FileNotFoundError:
        count = 0
    with _TASK_COUNT_CACHE_LOCK:
        _TASK_COUNT_CACHE[cache_key] = {"mtime_ns": dir_stat.st_mtime_ns, "count": count}
    return count


def _task_counts_by_workstream(workstreams) -> dict:
    return {
        workstream.id: _count_task_files(_task_storage_dir(workstream))
        for workstream in workstreams
    }


def _find_loaded_workstream(workstreams, ws_id: str):
    for workstream in workstreams:
        if workstream.id == ws_id:
            return workstream
    return read_workstream(ws_id, base_dir=WORKSPACE_DIR)


def _agent_run_status(run: dict) -> str:
    status = str((run or {}).get("status") or "").strip().lower()
    if status:
        return status
    return "completed" if (run or {}).get("ended_at") else "running"


def _sort_agent_runs_for_display(runs: list[dict]) -> list[dict]:
    ordered = sorted(runs, key=lambda run: str(run.get("started_at") or ""), reverse=True)
    ordered.sort(key=lambda run: 0 if _agent_run_status(run) == "running" else 1)
    return ordered


def _serialize_agent_run_summary(run: dict) -> dict:
    summary = {
        "run_id": run.get("run_id"),
        "agent": run.get("agent"),
        "agent_ref": run.get("agent_ref"),
        "workstream_id": run.get("workstream_id"),
        "workstream_path": run.get("workstream_path"),
        "task_ids": run.get("task_ids", []) or [],
        "tasks": run.get("tasks") or [],
        "started_at": run.get("started_at"),
        "ended_at": run.get("ended_at"),
        "status": _agent_run_status(run),
        "exit_code": run.get("exit_code"),
        "pid": run.get("pid"),
        "runtime": run.get("runtime"),
        "model": run.get("model"),
        "effort": run.get("effort"),
        "retried_from_run_id": run.get("retried_from_run_id"),
        "retried_to_run_ids": run.get("retried_to_run_ids") or [],
    }
    run_id = summary.get("run_id")
    if run_id:
        try:
            progress = read_progress_summary(run_id, base_dir=WORKSPACE_DIR)
            if progress:
                summary["progress_summary"] = progress
        except Exception:
            pass
    return summary


def _run_orchestration_cli(args: list[str]) -> dict:
    """Run orchestration CLI and return parsed JSON payload.

    Keeps the Workspace Manager decoupled from orchestration internals for
    feature-specific endpoints that can be served via CLI contracts.
    """
    cmd = [sys.executable, "-m", "orchestration.cli", "--base-dir", WORKSPACE_DIR] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=SOURCE_DIR,
        env=_subprocess_env(),
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "Orchestration CLI command failed"
        try:
            parsed_err = json.loads(result.stderr)
            message = parsed_err.get("message", message)
        except Exception:
            pass
        raise RuntimeError(message)

    payload = json.loads(result.stdout)
    if payload.get("status") != "ok":
        raise RuntimeError(payload.get("message", "Orchestration CLI returned an error"))
    return payload.get("data")


_TEXT_PREVIEW_EXTENSIONS = {
    ".md", ".txt", ".json", ".yaml", ".yml", ".py", ".js", ".ts", ".tsx", ".jsx",
    ".html", ".css", ".scss", ".sh", ".zsh", ".bash", ".toml", ".ini", ".cfg", ".csv",
    ".svg", ".xml", ".sql", ".log",
}

_TEXT_PREVIEW_MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".tsx": "text/typescript",
    ".jsx": "text/javascript",
    ".html": "text/html",
    ".css": "text/css",
    ".scss": "text/x-scss",
    ".sh": "text/x-shellscript",
    ".zsh": "text/x-shellscript",
    ".bash": "text/x-shellscript",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".csv": "text/csv",
    ".xml": "application/xml",
    ".sql": "application/sql",
    ".log": "text/plain",
}


def _build_artifact_preview(resolved_path: str) -> dict:
    mime_type, _ = mimetypes.guess_type(resolved_path)
    ext = os.path.splitext(resolved_path)[1].lower()
    if ext in _TEXT_PREVIEW_MIME_OVERRIDES:
        mime_type = _TEXT_PREVIEW_MIME_OVERRIDES[ext]
    else:
        mime_type = mime_type or "application/octet-stream"

    with open(resolved_path, "rb") as handle:
        raw = handle.read()

    if mime_type.startswith("image/"):
        return {
            "preview_type": "image",
            "mime_type": mime_type,
            "content": None,
            "data_url": f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}",
            "preview_error": None,
        }

    if ext in _TEXT_PREVIEW_EXTENSIONS or (b"\x00" not in raw and mime_type.startswith("text/")):
        return {
            "preview_type": "text",
            "mime_type": mime_type,
            "content": raw.decode("utf-8", errors="replace"),
            "data_url": None,
            "preview_error": None,
        }

    return {
        "preview_type": "unsupported",
        "mime_type": mime_type,
        "content": None,
        "data_url": None,
        "preview_error": f"Preview unavailable for {mime_type} files.",
    }


def _resolve_audio_root() -> str:
    override = str(os.environ.get(AUDIO_FILE_PATH_ENV_VAR) or "").strip()
    if override:
        expanded = os.path.expanduser(override)
        if os.path.isabs(expanded):
            return os.path.abspath(expanded)
        return os.path.abspath(os.path.join(WORKSPACE_DIR, expanded))
    return os.path.abspath(os.path.join(WORKSPACE_DIR, "audio"))


def _resolve_audio_path(request_path: str) -> str:
    relative = str(request_path or "").strip().lstrip("/")
    if not relative:
        raise FileNotFoundError("Audio file not found")

    audio_root = _resolve_audio_root()
    resolved = os.path.abspath(os.path.join(audio_root, relative))
    try:
        if os.path.commonpath([resolved, audio_root]) != audio_root:
            raise FileNotFoundError("Audio file not found")
    except ValueError:
        raise FileNotFoundError("Audio file not found")

    if not os.path.isfile(resolved):
        raise FileNotFoundError("Audio file not found")
    return resolved


# ── Route handlers ───────────────────────────────────────────────

def handle_board(parts, params):
    """Bulk endpoint: /api/board/<ws_id> — returns workstream + tasks for fast initial paint."""
    if not parts:
        return _err("Missing workstream ID")
    ws_id = parts[0]
    ws = read_workstream(ws_id, base_dir=WORKSPACE_DIR)
    if _truthy_param(params.get("meta")):
        meta = _board_meta(ws)
        if _truthy_param(params.get("locks")):
            meta["locks"] = list_workstream_locks_for_workstream(ws, base_dir=WORKSPACE_DIR)
        return _ok(meta)
    tasks = list_board_tasks(ws_id, base_dir=WORKSPACE_DIR)
    meta = _board_meta(ws, task_count=len(tasks))
    return _ok({
        "workstream": ws.to_dict(),
        "tasks": _serialize_board_tasks(tasks),
        "revision": meta["revision"],
        "task_count": meta["task_count"],
    })


def handle_workstream(method, parts, params):
    m = method
    if m == "list":
        wss = list_workstreams(base_dir=WORKSPACE_DIR, include_mount_status=False)
        return _ok([w.to_dict(include_transient=False) for w in wss])
    elif m == "code-status":
        requested_ids = params.get("ids")
        workstream_ids = requested_ids if isinstance(requested_ids, list) else None
        return _ok(get_workstream_code_mount_statuses(workstream_ids, base_dir=WORKSPACE_DIR))
    elif m == "counts":
        return _ok(dict(_get_poll_sidebar_snapshot()["counts"]))
    elif m == "read" and parts:
        ws = read_workstream(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(ws.to_dict())
    elif m == "context" and parts:
        ws = read_workstream(parts[0], base_dir=WORKSPACE_DIR)
        if params.get("_http_method") == "POST":
            updated = set_workstream_context(
                ws.id,
                context=params.get("context"),
                base_dir=WORKSPACE_DIR,
                updated_by=params.get("updated_by") or "Workspace Manager",
            )
            return _ok(updated.to_dict())
        return _ok({"id": ws.id, "name": ws.name, "context": ws.context})
    elif m == "concurrency" and parts:
        ws = read_workstream(parts[0], base_dir=WORKSPACE_DIR)
        if params.get("_http_method") == "POST":
            updated = set_workstream_agent_concurrency(
                ws.id,
                agent_concurrency=params.get("agent_concurrency"),
                base_dir=WORKSPACE_DIR,
                updated_by=params.get("updated_by") or "Workspace Manager",
            )
            return _ok(updated.to_dict())
        return _ok(read_workstream_agent_concurrency(ws.id, base_dir=WORKSPACE_DIR))
    elif m == "gettags" and parts:
        return _ok(get_workstream_tags(parts[0], base_dir=WORKSPACE_DIR))
    elif m == "upsert-tag" and parts:
        tag = upsert_workstream_tag(
            parts[0],
            params.get("name"),
            params.get("color"),
            base_dir=WORKSPACE_DIR,
        )
        return _ok(tag)
    elif m == "info" and parts:
        ws = read_workstream(parts[0], base_dir=WORKSPACE_DIR)
        levels = list_workstream_hierarchy_env(parts[0], base_dir=WORKSPACE_DIR)
        effective = list_effective_workstream_env(parts[0], base_dir=WORKSPACE_DIR, include_system=False)
        resolved_root = resolve_workstream_workspace(ws.id, base_dir=WORKSPACE_DIR)
        resolved_artifact_root = resolve_workstream_artifact_root(ws.id, base_dir=WORKSPACE_DIR)
        resolved_child_state_root = resolve_workstream_child_state_root(ws.id, base_dir=WORKSPACE_DIR)
        ws_data = ws.to_dict()
        return _ok({
            "id": ws.id,
            "name": ws.name,
            "task_states": ws.task_states,
            "agent_concurrency": ws.agent_concurrency,
            "working_directory": getattr(ws, "working_directory", None),
            "artifact_root": getattr(ws, "artifact_root", None),
            "child_workstream_root": getattr(ws, "child_workstream_root", None),
            "mount_available": ws_data.get("mount_available"),
            "resolved_workspace_path": resolved_root,
            "resolved_artifact_root": resolved_artifact_root,
            "resolved_artifact_directory": os.path.join(resolved_artifact_root, "artifacts") if resolved_artifact_root else None,
            "resolved_child_workstream_root": resolved_child_state_root,
            "effective_env": effective,
            "env_hierarchy": levels,
        })
    elif m == "find":
        wss = find_workstreams(params.get("query", ""), base_dir=WORKSPACE_DIR)
        return _ok([w.to_dict() for w in wss])
    elif m == "pause" and parts:
        ws = read_workstream(parts[0], base_dir=WORKSPACE_DIR)
        ws.paused = True
        save_workstream(ws, WORKSPACE_DIR)
        from orchestration.workspace_audit import log_event
        log_event("workstream_paused", f"Workstream '{ws.name}' paused", WORKSPACE_DIR, workstream_id=ws.id)
        return _ok(ws.to_dict())
    elif m == "resume" and parts:
        ws = read_workstream(parts[0], base_dir=WORKSPACE_DIR)
        ws.paused = False
        save_workstream(ws, WORKSPACE_DIR)
        from orchestration.workspace_audit import log_event
        log_event("workstream_resumed", f"Workstream '{ws.name}' resumed", WORKSPACE_DIR, workstream_id=ws.id)
        return _ok(ws.to_dict())
    elif m == "pause-columns" and parts:
        ws = pause_workstream_states(
            parts[0],
            _parse_state_names(params),
            base_dir=WORKSPACE_DIR,
            updated_by=params.get("updated_by") or "Workspace Manager",
        )
        return _ok(ws.to_dict())
    elif m == "resume-columns" and parts:
        ws = resume_workstream_states(
            parts[0],
            _parse_state_names(params),
            base_dir=WORKSPACE_DIR,
            updated_by=params.get("updated_by") or "Workspace Manager",
        )
        return _ok(ws.to_dict())
    elif m == "create":
        kwargs = {"name": params["name"], "base_dir": WORKSPACE_DIR}
        if "description" in params:
            kwargs["description"] = params["description"]
        if "context" in params:
            kwargs["context"] = params["context"]
        if "parent" in params:
            kwargs["parent_id"] = params["parent"]
        if "states" in params:
            kwargs["task_states"] = json.loads(params["states"])
        if "working_directory" in params:
            kwargs["working_directory"] = params["working_directory"]
        if "artifact_root" in params:
            kwargs["artifact_root"] = params["artifact_root"]
        if "child_workstream_root" in params:
            kwargs["child_workstream_root"] = params["child_workstream_root"]
        ws = create_workstream(**kwargs)
        return _ok(ws.to_dict())
    return _err(f"Unknown workstream method: {m}")


def handle_task(method, parts, params):
    m = method

    def _parse_bool(raw):
        if isinstance(raw, bool):
            return raw
        if raw is None:
            return False
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _parse_attachments(raw):
        if raw is None:
            return None
        if isinstance(raw, list):
            return [str(v).strip() for v in raw if str(v).strip()]
        if isinstance(raw, str):
            if not raw.strip():
                return []
            return [p.strip() for p in raw.split(",") if p.strip()]
        return [str(raw).strip()]

    def _parse_tags(raw):
        if raw is None:
            return None
        if isinstance(raw, list):
            return [str(v).strip() for v in raw if str(v).strip()]
        if isinstance(raw, str):
            if not raw.strip():
                return []
            return [tag.strip() for tag in raw.split(",") if tag.strip()]
        text = str(raw).strip()
        return [text] if text else []

    if m == "list" and parts:
        kwargs = {"workstream_id": parts[0], "base_dir": WORKSPACE_DIR}
        if "status" in params:
            kwargs["status"] = params["status"]
        if "tags" in params:
            kwargs["tags"] = _parse_tags(params.get("tags"))
        tasks = list_tasks(**kwargs)
        return _ok([t.to_dict() for t in tasks])
    elif m == "read" and parts:
        if params.get("workstream_id"):
            task = read_task_from_workstream(params["workstream_id"], parts[0], base_dir=WORKSPACE_DIR)
        else:
            task = read_task(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(task.to_dict())
    elif m == "open-file" and parts:
        task_id = parts[0]
        workstream_id = params.get("workstream_id") or None
        if not workstream_id:
            task = read_task(task_id, base_dir=WORKSPACE_DIR)
            workstream_id = task.workstream_id
        task_file_path = _task_path(WORKSPACE_DIR, workstream_id, task_id)
        if not os.path.exists(task_file_path):
            raise FileNotFoundError(f"Task file not found: {task_id}.yaml")
        launch = _open_file_in_vscode(task_file_path)
        return _ok({
            "opened": True,
            "task_id": task_id,
            "workstream_id": workstream_id,
            "path": task_file_path,
            "resolved_path": task_file_path,
            "method": launch["method"],
        })
    elif m == "create" and parts:
        kwargs = {"workstream_id": parts[0], "title": params["title"], "base_dir": WORKSPACE_DIR}
        if "description" in params:
            kwargs["description"] = params["description"]
        if "status" in params:
            kwargs["initial_status"] = params["status"]
        if "tags" in params:
            kwargs["tags"] = _parse_tags(params.get("tags"))
        if "scheduled-at" in params:
            kwargs["scheduled_at"] = params["scheduled-at"]
        if "scheduled-action" in params:
            kwargs["scheduled_action"] = json.loads(params["scheduled-action"])
        if "attachments" in params:
            kwargs["attachments"] = _parse_attachments(params.get("attachments"))
        task = create_task(**kwargs)
        return _ok(task.to_dict())
    elif m == "update" and parts:
        kwargs = {"task_id": parts[0], "base_dir": WORKSPACE_DIR}
        if "title" in params:
            kwargs["title"] = params["title"]
        if "status" in params:
            kwargs["status"] = params["status"]
        if "force" in params:
            kwargs["force"] = _parse_bool(params.get("force"))
        if "description" in params:
            kwargs["description"] = params["description"]
        if "tags" in params:
            kwargs["tags"] = _parse_tags(params.get("tags"))
        if "scheduled-at" in params:
            kwargs["scheduled_at"] = params["scheduled-at"]
        if "scheduled-action" in params:
            kwargs["scheduled_action"] = json.loads(params["scheduled-action"])
        if "attachments" in params:
            kwargs["attachments"] = _parse_attachments(params.get("attachments"))
        task = update_task(**kwargs)
        return _ok(task.to_dict())
    elif m == "attach" and parts:
        if "path" not in params:
            return _err("Missing required parameter: path")
        task = attach_to_task(parts[0], params["path"], base_dir=WORKSPACE_DIR)
        return _ok(task.to_dict())
    elif m == "detach" and parts:
        if "path" not in params:
            return _err("Missing required parameter: path")
        task = detach_from_task(parts[0], params["path"], base_dir=WORKSPACE_DIR)
        return _ok(task.to_dict())
    elif m == "comment" and parts:
        task = comment_task(
            parts[0],
            params["message"],
            author=params.get("author") or _human_name(),
            base_dir=WORKSPACE_DIR,
        )
        return _ok(task.to_dict())
    elif m == "edit-comment" and parts:
        if "index" not in params:
            return _err("Missing required parameter: index")
        if "message" not in params:
            return _err("Missing required parameter: message")
        task = edit_task_comment(
            parts[0],
            int(params["index"]),
            params["message"],
            author=params.get("author") or _human_name(),
            base_dir=WORKSPACE_DIR,
        )
        return _ok(task.to_dict())
    elif m == "delete-comment" and parts:
        if "index" not in params:
            return _err("Missing required parameter: index")
        task = delete_task_comment(
            parts[0],
            int(params["index"]),
            base_dir=WORKSPACE_DIR,
        )
        return _ok(task.to_dict())
    elif m == "archive" and parts:
        result = archive_task(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(result)
    elif m == "pause" and parts:
        task = pause_task(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(task.to_dict())
    elif m == "resume" and parts:
        task = resume_task(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(task.to_dict())
    elif m == "audit" and parts:
        audit = get_audit(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(audit)
    elif m == "clear-schedule" and parts:
        task = clear_schedule(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(task.to_dict())
    elif m == "move" and parts:
        if "workstream_id" not in params:
            return _err("Missing required parameter: workstream_id")
        kwargs = {
            "task_id": parts[0],
            "target_workstream_id": params["workstream_id"],
            "base_dir": WORKSPACE_DIR,
        }
        if "status" in params:
            kwargs["target_status"] = params["status"]
        task = move_task(**kwargs)
        return _ok(task.to_dict())
    elif m == "duplicate" and parts:
        kwargs = {"task_id": parts[0], "base_dir": WORKSPACE_DIR}
        if "workstream_id" in params:
            kwargs["target_workstream_id"] = params["workstream_id"]
        if "status" in params:
            kwargs["target_status"] = params["status"]
        task = duplicate_task(**kwargs)
        return _ok(task.to_dict())
    elif m == "reorder" and parts:
        task_id = parts[0]
        if "index" in params:
            task = move_task_to_index(task_id, int(params["index"]), base_dir=WORKSPACE_DIR)
            return _ok(task.to_dict())

        target_task_id = params.get("target_task_id") or params.get("target")
        if not target_task_id:
            return _err("Missing required parameter: target_task_id")

        position = (params.get("position") or "before").strip().lower()
        if position == "before":
            task = move_task_before(task_id, target_task_id, base_dir=WORKSPACE_DIR)
        elif position == "after":
            task = move_task_after(task_id, target_task_id, base_dir=WORKSPACE_DIR)
        else:
            return _err("Invalid position. Use 'before' or 'after'.")

        return _ok(task.to_dict())
    return _err(f"Unknown task method: {m}")


def handle_lock(method, parts, params):
    m = method
    if m == "status" and parts:
        if params.get("workstream_id"):
            lock = lock_status_for_workstream(params["workstream_id"], parts[0], base_dir=WORKSPACE_DIR)
        else:
            lock = lock_status(parts[0], base_dir=WORKSPACE_DIR)
        if lock is None:
            return _ok({"locked": False})
        data = lock.to_dict()
        data["locked"] = True
        return _ok(data)
    elif m == "list" and parts:
        ws = read_workstream(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(list_workstream_locks_for_workstream(ws, base_dir=WORKSPACE_DIR))
    elif m == "acquire" and parts:
        ttl = int(params["ttl"]) if "ttl" in params else None
        kwargs = {"task_id": parts[0], "agent_id": params["agent"], "base_dir": WORKSPACE_DIR}
        if ttl is not None:
            kwargs["ttl_seconds"] = ttl
        lock = acquire_lock(**kwargs)
        return _ok(lock.to_dict())
    elif m == "release" and parts:
        released = release_lock(parts[0], params["agent"], base_dir=WORKSPACE_DIR)
        return _ok({"released": released})
    return _err(f"Unknown lock method: {m}")


def handle_trigger(method, parts, params):
    m = method
    if m == "list" and parts:
        triggers = list_triggers(parts[0], base_dir=WORKSPACE_DIR)
        return _ok([t.to_dict() for t in triggers])
    elif m == "create" and parts:
        kwargs = {"workstream_id": parts[0], "action": params["action"], "base_dir": WORKSPACE_DIR}
        if "on-state" in params:
            kwargs["on_state"] = params["on-state"]
        if "on-schedule" in params:
            kwargs["on_schedule"] = params["on-schedule"]
        if "filter" in params:
            kwargs["filter"] = json.loads(params["filter"])
        if "agent" in params:
            kwargs["agent"] = params["agent"]
        if "command" in params:
            kwargs["command"] = params["command"]
        trigger = create_trigger(**kwargs)
        return _ok(trigger.to_dict())
    elif m == "delete" and parts:
        delete_trigger(parts[0], base_dir=WORKSPACE_DIR)
        return _ok({"deleted": True})
    elif m == "pause" and parts:
        trigger = pause_trigger(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(trigger.to_dict())
    elif m == "resume" and parts:
        trigger = resume_trigger(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(trigger.to_dict())
    elif m == "run-now" and parts:
        result = run_trigger_now(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(result)
    return _err(f"Unknown trigger method: {m}")


def handle_scheduler(method, parts, params):
    if method == "status":
        return _ok(scheduler_status(base_dir=WORKSPACE_DIR))
    return _err(f"Unknown scheduler method: {method}")


def handle_sound(method, parts, params):
    if method != "mute":
        return _err(f"Unknown sound method: {method}")

    if params.get("_http_method") == "POST":
        if "muted" not in params:
            return _err("Missing required parameter: muted", code="INVALID")

        raw = params.get("muted")
        if isinstance(raw, bool):
            muted = raw
        else:
            muted = str(raw).strip().lower() in {"1", "true", "yes", "on"}
        return _ok({"muted": set_global_sound_mute(muted, base_dir=WORKSPACE_DIR)})

    return _ok({"muted": get_global_sound_mute(base_dir=WORKSPACE_DIR)})


def handle_agent(method, parts, params):
    if method == "active":
        runs = list_active_agents(base_dir=WORKSPACE_DIR)
        return _ok(runs)
    if method == "runs":
        limit = min(int(params.get("limit", "25")), 100)
        runs = list_agent_runs(limit=limit, base_dir=WORKSPACE_DIR)
        summaries = [_serialize_agent_run_summary(run) for run in runs]
        return _ok(_sort_agent_runs_for_display(summaries))
    if method == "run" and parts:
        details = get_agent_run(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(details)
    if method == "context" and parts:
        details = read_agent_run_context_file(parts[0], params.get("path"), base_dir=WORKSPACE_DIR)
        return _ok(details)
    if method == "tail" and parts:
        run_id = parts[0]
        lines = int(params.get("lines", "200"))
        tail = tail_active_agent(run_id, lines=lines, base_dir=WORKSPACE_DIR)
        return _ok(tail)
    if method == "kill" and parts:
        result = kill_agent_run(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(result)
    return _err(f"Unknown agent method: {method}")


def handle_poll(method, parts, params):
    """Combined polling endpoint — returns workstreams, counts, scheduler, and optionally board metadata."""
    if method == "status":
        active_run_summaries = []
        try:
            _active_runs = list_active_agents(base_dir=WORKSPACE_DIR)
            active_run_summaries = [
                _serialize_agent_run_summary(run)
                for run in _active_runs
                if run.get("pid") is not None
            ]
            active_run_count = len(active_run_summaries)
        except Exception:
            active_run_count = 0
        return _ok({
            "scheduler": scheduler_status(base_dir=WORKSPACE_DIR),
            "active_triggers": get_active_triggers(),
            "active_agent_runs": active_run_count,
            "active_agent_run_summaries": active_run_summaries,
        })

    result = dict(_get_poll_sidebar_snapshot())
    # If a board ID is requested, include it
    ws_id = params.get("board") or (method if method != "all" else None)
    if ws_id:
        try:
            wss = list_workstreams(base_dir=WORKSPACE_DIR, include_mount_status=False)
            counts = result.get("counts", {})
            ws = _find_loaded_workstream(wss, ws_id)
            if _truthy_param(params.get("board_meta")):
                result["board_meta"] = _board_meta(ws, task_count=counts.get(ws.id))
                if not _truthy_param(params.get("skip_locks")):
                    result["board_locks"] = list_workstream_locks_for_workstream(ws, base_dir=WORKSPACE_DIR)
            else:
                tasks = list_tasks(ws_id, base_dir=WORKSPACE_DIR)
                meta = _board_meta(ws, task_count=len(tasks))
                result["board"] = {
                    "workstream": ws.to_dict(),
                    "tasks": _serialize_board_tasks(tasks),
                    "revision": meta["revision"],
                    "task_count": meta["task_count"],
                }
        except FileNotFoundError:
            pass
    return _ok(result)


def handle_audit(method, parts, params):
    from orchestration.workspace_audit import get_audit_log
    if method == "log":
        limit = int(params.get("limit", "50"))
        ws_id = params.get("workstream") or None
        event_type = params.get("type") or None
        entries = get_audit_log(base_dir=WORKSPACE_DIR, limit=limit, workstream_id=ws_id, event_type=event_type)
        return _ok(entries)
    return _err(f"Unknown audit method: {method}")


def handle_retry(method, parts, params):
    from orchestration.retry import manual_retry
    if method == "task" and parts:
        result = manual_retry(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(result)
    if method == "agent-run" and parts:
        allow_paused_workstream = str(params.get("allow_paused_workstream", "")).lower() in {"1", "true", "yes", "on"}
        result = retry_agent_run(
            parts[0],
            base_dir=WORKSPACE_DIR,
            allow_paused_workstream=allow_paused_workstream,
            background=True,
        )
        return _ok(result)
    return _err(f"Unknown retry method: {method}")


def _open_file_in_vscode(resolved_path: str) -> dict:
    launch_errors = []

    code_bin = shutil.which("code")
    if code_bin:
        try:
            subprocess.Popen([code_bin, "-r", resolved_path], cwd=WORKSPACE_DIR)
            return {"method": "code-cli"}
        except Exception as e:
            launch_errors.append(f"code-cli: {e}")

    # macOS fallback: ask Finder/LaunchServices to open file in VS Code.
    try:
        subprocess.Popen(["open", "-a", "Visual Studio Code", resolved_path], cwd=WORKSPACE_DIR)
        return {"method": "open-app"}
    except Exception as e:
        launch_errors.append(f"open-app: {e}")

    raise RuntimeError("Failed to open file in VS Code. " + " | ".join(launch_errors))


def handle_artifact(method, parts, params):
    m = method
    if m == "read":
        path = params.get("path")
        if not path:
            return _err("Missing required parameter: path", code="INVALID")

        workstream_id = params.get("workstream_id") or None
        artifacts_root, relative_path = _resolve_artifact_root(
            path,
            workstream_id=workstream_id,
        )
        resolved_path = _validate_path(artifacts_root, relative_path)
        if not os.path.exists(resolved_path):
            raise FileNotFoundError(f"Artifact not found: {path}")

        preview = _build_artifact_preview(resolved_path)
        return _ok({
            "path": path,
            "workstream_id": workstream_id,
            "resolved_path": resolved_path,
            "content": preview["content"],
            "preview_type": preview["preview_type"],
            "mime_type": preview["mime_type"],
            "data_url": preview["data_url"],
            "preview_error": preview["preview_error"],
        })
    if m == "open":
        path = params.get("path")
        if not path:
            return _err("Missing required parameter: path", code="INVALID")

        workstream_id = params.get("workstream_id") or None
        artifacts_root, relative_path = _resolve_artifact_root(
            path,
            base_dir=WORKSPACE_DIR,
            workstream_id=workstream_id,
        )
        resolved_path = _validate_path(artifacts_root, relative_path)
        if not os.path.exists(resolved_path):
            raise FileNotFoundError(f"Artifact not found: {path}")

        launch = _open_file_in_vscode(resolved_path)
        return _ok({
            "opened": True,
            "path": path,
            "workstream_id": workstream_id,
            "resolved_path": resolved_path,
            "method": launch["method"],
        })
    return _err(f"Unknown artifact method: {m}")


ROUTE_MAP = {
    "board": lambda m, p, q: handle_board(p, q),
    "poll": lambda m, p, q: handle_poll(m, p, q),
    "workstream": handle_workstream,
    "task": handle_task,
    "lock": handle_lock,
    "trigger": handle_trigger,
    "scheduler": handle_scheduler,
    "sound": handle_sound,
    "agent": handle_agent,
    "audit": handle_audit,
    "retry": handle_retry,
    "artifact": handle_artifact,
}


class Handler(SimpleHTTPRequestHandler):
    """Handles static files and /api/ routes."""

    protocol_version = "HTTP/1.1"  # keep-alive: browser reuses connections
    timeout = 30  # close idle keep-alive connections after 30s

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    @staticmethod
    def _is_app_route(path):
        parts = [segment for segment in str(path or "/").split("/") if segment]
        if not parts:
            return True
        return len(parts) == 2 and parts[0] in {"workstreams", "task"}

    def _send_json(self, code: int, body: str):
        encoded = body.encode()
        self._json_response = True
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers can cancel timed-out polling requests while the server is
            # writing the response. That is a client disconnect, not a server error.
            return
        finally:
            if hasattr(self, "_json_response"):
                delattr(self, "_json_response")

    def _send_binary_file(self, file_path: str) -> None:
        with open(file_path, "rb") as handle:
            raw = handle.read()
        mime_type, _ = mimetypes.guess_type(file_path)
        self.send_response(200)
        self.send_header("Content-Type", mime_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle_audio(self, parsed) -> bool:
        if not parsed.path.startswith("/audio/"):
            return False
        requested = unquote(parsed.path[len("/audio/"):])
        try:
            resolved = _resolve_audio_path(requested)
        except FileNotFoundError:
            self.send_error(404, "Audio file not found")
            return True
        self._send_binary_file(resolved)
        return True

    def _handle_api(self, http_method: str):
        parsed = urlparse(self.path)
        path = parsed.path

        if not path.startswith("/api/"):
            return False

        parts = [p for p in path[5:].split("/") if p]
        if len(parts) < 2:
            self._send_json(400, json.dumps({
                "status": "error", "message": "API path must be /api/<concept>/<method>/[id]"
            }))
            return True

        concept = parts[0]
        method = parts[1]
        positional = parts[2:]

        # Collect params from query string
        query = parse_qs(parsed.query, keep_blank_values=False)
        params = {k: v[0] for k, v in query.items()}
        params["_http_method"] = http_method

        # Merge POST body params
        if http_method == "POST":
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length > 0:
                body = self.rfile.read(content_length).decode()
                try:
                    payload = json.loads(body)
                    if isinstance(payload, dict):
                        params.update(payload)
                except json.JSONDecodeError:
                    self._send_json(400, json.dumps({
                        "status": "error", "message": "Invalid JSON body"
                    }))
                    return True

        handler = ROUTE_MAP.get(concept)
        if not handler:
            self._send_json(400, json.dumps({
                "status": "error", "message": f"Unknown concept: {concept}"
            }))
            return True

        try:
            # board handler has different signature
            if concept == "board":
                status_code, resp = handler(method, [method] + positional, params)
            else:
                status_code, resp = handler(method, positional, params)
            if http_method == "POST" and status_code < 400:
                _invalidate_poll_sidebar_cache()
            self._send_json(status_code, resp)
        except CorruptTaskError as e:
            self._send_json(422, json.dumps({"status": "error", "message": str(e), "code": "CORRUPT_TASK"}))
        except FileNotFoundError as e:
            self._send_json(404, json.dumps({"status": "error", "message": str(e), "code": "NOT_FOUND"}))
        except ValueError as e:
            self._send_json(400, json.dumps({"status": "error", "message": str(e), "code": "INVALID"}))
        except RuntimeError as e:
            self._send_json(409, json.dumps({"status": "error", "message": str(e), "code": "CONFLICT"}))
        except Exception as e:
            self._send_json(500, json.dumps({"status": "error", "message": str(e), "code": "ERROR"}))

        return True

    def do_GET(self):
        if self._handle_api("GET"):
            return
        parsed = urlparse(self.path)
        if self._handle_audio(parsed):
            return
        if self._is_app_route(parsed.path):
            self.path = "/"
        elif "." not in os.path.basename(parsed.path) and parsed.path != "/":
            self.path = "/"
        super().do_GET()

    def end_headers(self):
        # Prevent browser caching of static files so code changes take effect
        if not hasattr(self, '_json_response'):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    def do_POST(self):
        self._handle_api("POST")

    def log_message(self, format, *args):
        msg = format % args
        if "/api/" in msg and _api_request_logging_enabled():
            sys.stderr.write(f"[API] {msg}\n")


def run(base_dir: str, port: int = 8080, open_browser: bool = True) -> None:
    set_workspace_dir(base_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Workstream Manager running at {url}")
    print(f"Managing orchestration workspace: {WORKSPACE_DIR}")
    if open_browser:
        print(f"Opening Workstream Manager at {url}")
        try:
            if not webbrowser.open(url):
                print(f"WARNING: Could not open a browser. Open {url} manually.")
        except Exception as exc:
            print(f"WARNING: Could not open a browser: {exc}. Open {url} manually.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Workstream Manager server")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--no-open", action="store_true", help="Do not open a browser")
    parser.add_argument(
        "--base-dir",
        default=os.environ.get("WORKSTREAM_MANAGER_BASE_DIR", WORKSPACE_DIR),
        help="Orchestration workspace to manage (default: repository root or WORKSTREAM_MANAGER_BASE_DIR)",
    )
    args = parser.parse_args()
    run(args.base_dir, args.port, open_browser=not args.no_open)


if __name__ == "__main__":
    main()
