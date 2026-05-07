#!/usr/bin/env python3
"""Workstream Manager — thin HTTP bridge to the orchestration modules.

Serves a static frontend and proxies API requests to orchestration Python
modules directly (no subprocess overhead).

Usage:
    python -m workstream_manager              # default port 8080
    python -m workstream_manager --port 9000  # custom port
"""

import json
import os
import shutil
import subprocess
import sys
import base64
import mimetypes
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    request_queue_size = 32  # increase listen backlog from default 5
    allow_reuse_address = True

# Resolve paths
WORKSPACE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Ensure workspace is on the Python path so orchestration imports work
if WORKSPACE_DIR not in sys.path:
    sys.path.insert(0, WORKSPACE_DIR)

from orchestration.workstreams import (
    list_workstreams, read_workstream, create_workstream,
    find_workstreams, save_workstream, list_workstream_hierarchy_env,
    list_effective_workstream_env, resolve_workstream_workspace, set_workstream_context,
)
from orchestration.tasks import (
    CorruptTaskError, create_task, read_task, update_task, list_tasks,
    comment_task, delete_task_comment, edit_task_comment, archive_task, get_audit, clear_schedule,
    move_task, duplicate_task, attach_to_task, detach_from_task,
    move_task_before, move_task_after, move_task_to_index,
)
from orchestration.triggers import create_trigger, list_triggers, delete_trigger, run_trigger_now, get_active_triggers
from orchestration.scheduler import status as scheduler_status
from orchestration.artifacts import read_artifact, _resolve_artifact_root, _validate_path
from orchestration.agents import list_active_agents, tail_active_agent, list_agent_runs, get_agent_run, kill_agent_run, retry_agent_run


def _ok(data):
    return 200, json.dumps({"status": "ok", "data": data}, indent=2, default=str)


def _err(msg, code="ERROR"):
    return 400, json.dumps({"status": "error", "message": msg, "code": code})


def _human_name() -> str:
    return (os.environ.get("HUMAN_NAME") or "").strip() or "Anonymous Human"


def _serialize_board_tasks(tasks):
    task_dicts = []
    for task in tasks:
        task_dict = task.to_dict()
        task_dict["lock"] = {"locked": False}
        task_dicts.append(task_dict)
    return task_dicts


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
        cwd=WORKSPACE_DIR,
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


# ── Route handlers ───────────────────────────────────────────────

def handle_board(parts, params):
    """Bulk endpoint: /api/board/<ws_id> — returns workstream + tasks for fast initial paint."""
    if not parts:
        return _err("Missing workstream ID")
    ws_id = parts[0]
    ws = read_workstream(ws_id, base_dir=WORKSPACE_DIR)
    tasks = list_tasks(ws_id, base_dir=WORKSPACE_DIR)
    return _ok({"workstream": ws.to_dict(), "tasks": _serialize_board_tasks(tasks)})


def handle_workstream(method, parts, params):
    m = method
    if m == "list":
        wss = list_workstreams(base_dir=WORKSPACE_DIR)
        return _ok([w.to_dict() for w in wss])
    elif m == "counts":
        wss = list_workstreams(base_dir=WORKSPACE_DIR)
        counts = {}
        for ws in wss:
            counts[ws.id] = len(list_tasks(ws.id, base_dir=WORKSPACE_DIR))
        return _ok(counts)
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
    elif m == "info" and parts:
        ws = read_workstream(parts[0], base_dir=WORKSPACE_DIR)
        levels = list_workstream_hierarchy_env(parts[0], base_dir=WORKSPACE_DIR)
        effective = list_effective_workstream_env(parts[0], base_dir=WORKSPACE_DIR, include_system=False)
        resolved_root = resolve_workstream_workspace(ws.id, base_dir=WORKSPACE_DIR)
        if ws.mounted_workspace_path:
            expanded = os.path.expanduser(ws.mounted_workspace_path)
            if os.path.isabs(expanded):
                resolved_root = os.path.abspath(expanded)
            else:
                resolved_root = os.path.abspath(os.path.join(resolved_root, expanded))
        return _ok({
            "id": ws.id,
            "name": ws.name,
            "task_states": ws.task_states,
            "mounted_workspace_path": ws.mounted_workspace_path,
            "resolved_workspace_path": resolved_root,
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

    if m == "list" and parts:
        kwargs = {"workstream_id": parts[0], "base_dir": WORKSPACE_DIR}
        if "status" in params:
            kwargs["status"] = params["status"]
        if "tags" in params:
            kwargs["tags"] = [t.strip() for t in params["tags"].split(",")]
        tasks = list_tasks(**kwargs)
        return _ok([t.to_dict() for t in tasks])
    elif m == "read" and parts:
        task = read_task(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(task.to_dict())
    elif m == "create" and parts:
        kwargs = {"workstream_id": parts[0], "title": params["title"], "base_dir": WORKSPACE_DIR}
        if "description" in params:
            kwargs["description"] = params["description"]
        if "tags" in params:
            kwargs["tags"] = [t.strip() for t in params["tags"].split(",")]
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
        if "status" in params:
            kwargs["status"] = params["status"]
        if "force" in params:
            kwargs["force"] = _parse_bool(params.get("force"))
        if "description" in params:
            kwargs["description"] = params["description"]
        if "tags" in params:
            kwargs["tags"] = [t.strip() for t in params["tags"].split(",")]
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
        return _ok(_run_orchestration_cli(["lock", "status", parts[0]]))
    elif m == "list" and parts:
        return _ok(_run_orchestration_cli(["lock", "list", parts[0]]))
    elif m == "acquire" and parts:
        cmd = ["lock", "acquire", parts[0], "--agent", params["agent"]]
        if "ttl" in params:
            cmd.extend(["--ttl", str(params["ttl"])])
        return _ok(_run_orchestration_cli(cmd))
    elif m == "release" and parts:
        return _ok(_run_orchestration_cli(["lock", "release", parts[0], "--agent", params["agent"]]))
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
    elif m == "run-now" and parts:
        result = run_trigger_now(parts[0], base_dir=WORKSPACE_DIR)
        return _ok(result)
    return _err(f"Unknown trigger method: {m}")


def handle_scheduler(method, parts, params):
    if method == "status":
        return _ok(scheduler_status(base_dir=WORKSPACE_DIR))
    return _err(f"Unknown scheduler method: {method}")


def handle_agent(method, parts, params):
    if method == "active":
        runs = list_active_agents(base_dir=WORKSPACE_DIR)
        return _ok(runs)
    if method == "runs":
        limit = int(params.get("limit", "100"))
        runs = list_agent_runs(limit=limit, base_dir=WORKSPACE_DIR)
        return _ok(runs)
    if method == "run" and parts:
        details = get_agent_run(parts[0], base_dir=WORKSPACE_DIR)
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
    wss = list_workstreams(base_dir=WORKSPACE_DIR)
    counts = {}
    for ws in wss:
        counts[ws.id] = len(list_tasks(ws.id, base_dir=WORKSPACE_DIR))
    try:
        # Count only currently live runs from the active registry. This avoids
        # inflating the sidebar badge with stale historical metadata entries.
        _active_runs = list_active_agents(base_dir=WORKSPACE_DIR)
        active_run_count = sum(1 for r in _active_runs if r.get("pid") is not None)
    except Exception:
        active_run_count = 0
    result = {
        "workstreams": [w.to_dict() for w in wss],
        "counts": counts,
        "scheduler": scheduler_status(base_dir=WORKSPACE_DIR),
        "active_triggers": get_active_triggers(),
        "active_agent_runs": active_run_count,
    }
    # If a board ID is requested, include it
    ws_id = params.get("board") or (method if method != "all" else None)
    if ws_id:
        try:
            ws = read_workstream(ws_id, base_dir=WORKSPACE_DIR)
            tasks = list_tasks(ws_id, base_dir=WORKSPACE_DIR)
            result["board"] = {"workstream": ws.to_dict(), "tasks": _serialize_board_tasks(tasks)}
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


def handle_artifact(method, parts, params):
    m = method
    if m == "read":
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

        launch_errors = []

        code_bin = shutil.which("code")
        if code_bin:
            try:
                subprocess.Popen([code_bin, "-r", resolved_path], cwd=WORKSPACE_DIR)
                return _ok({
                    "opened": True,
                    "path": path,
                    "workstream_id": workstream_id,
                    "resolved_path": resolved_path,
                    "method": "code-cli",
                })
            except Exception as e:
                launch_errors.append(f"code-cli: {e}")

        # macOS fallback: ask Finder/LaunchServices to open file in VS Code.
        try:
            subprocess.Popen(["open", "-a", "Visual Studio Code", resolved_path], cwd=WORKSPACE_DIR)
            return _ok({
                "opened": True,
                "path": path,
                "workstream_id": workstream_id,
                "resolved_path": resolved_path,
                "method": "open-app",
            })
        except Exception as e:
            launch_errors.append(f"open-app: {e}")

        raise RuntimeError("Failed to open file in VS Code. " + " | ".join(launch_errors))
    return _err(f"Unknown artifact method: {m}")


ROUTE_MAP = {
    "board": lambda m, p, q: handle_board(p, q),
    "poll": lambda m, p, q: handle_poll(m, p, q),
    "workstream": handle_workstream,
    "task": handle_task,
    "lock": handle_lock,
    "trigger": handle_trigger,
    "scheduler": handle_scheduler,
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
        if "." not in os.path.basename(parsed.path) and parsed.path != "/":
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
        if "/api/" in msg:
            sys.stderr.write(f"[API] {msg}\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Workstream Manager server")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Workstream Manager running at http://localhost:{args.port}")
    print("Note: Start the scheduler separately via: python -m orchestration.cli scheduler run")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
