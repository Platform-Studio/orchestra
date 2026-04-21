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
import sys
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
    list_effective_workstream_env, resolve_workstream_workspace,
)
from orchestration.tasks import (
    create_task, read_task, update_task, list_tasks,
    comment_task, archive_task, get_audit, clear_schedule,
)
from orchestration.locks import acquire_lock, release_lock, lock_status
from orchestration.triggers import create_trigger, list_triggers, delete_trigger, run_trigger_now, get_active_triggers
from orchestration.scheduler import status as scheduler_status


def _ok(data):
    return 200, json.dumps({"status": "ok", "data": data}, indent=2, default=str)


def _err(msg, code="ERROR"):
    return 400, json.dumps({"status": "error", "message": msg, "code": code})


# ── Route handlers ───────────────────────────────────────────────

def handle_board(parts, params):
    """Bulk endpoint: /api/board/<ws_id> — returns workstream + tasks + locks in one call."""
    if not parts:
        return _err("Missing workstream ID")
    ws_id = parts[0]
    ws = read_workstream(ws_id, base_dir=WORKSPACE_DIR)
    tasks = list_tasks(ws_id, base_dir=WORKSPACE_DIR)
    task_dicts = []
    for t in tasks:
        td = t.to_dict()
        if td.get("_error"):
            td["lock"] = {"locked": False}
            task_dicts.append(td)
            continue
        lock = lock_status(t.id, base_dir=WORKSPACE_DIR)
        if lock and not lock.is_expired():
            td["lock"] = lock.to_dict()
            td["lock"]["locked"] = True
        else:
            td["lock"] = {"locked": False}
        task_dicts.append(td)
    return _ok({"workstream": ws.to_dict(), "tasks": task_dicts})


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
        if "parent" in params:
            kwargs["parent_id"] = params["parent"]
        if "states" in params:
            kwargs["task_states"] = json.loads(params["states"])
        ws = create_workstream(**kwargs)
        return _ok(ws.to_dict())
    return _err(f"Unknown workstream method: {m}")


def handle_task(method, parts, params):
    m = method
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
        task = create_task(**kwargs)
        return _ok(task.to_dict())
    elif m == "update" and parts:
        kwargs = {"task_id": parts[0], "base_dir": WORKSPACE_DIR}
        if "status" in params:
            kwargs["status"] = params["status"]
        if "description" in params:
            kwargs["description"] = params["description"]
        if "tags" in params:
            kwargs["tags"] = [t.strip() for t in params["tags"].split(",")]
        if "scheduled-at" in params:
            kwargs["scheduled_at"] = params["scheduled-at"]
        if "scheduled-action" in params:
            kwargs["scheduled_action"] = json.loads(params["scheduled-action"])
        task = update_task(**kwargs)
        return _ok(task.to_dict())
    elif m == "comment" and parts:
        task = comment_task(
            parts[0],
            params["message"],
            author=params.get("author"),
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
    return _err(f"Unknown task method: {m}")


def handle_lock(method, parts, params):
    m = method
    if m == "status" and parts:
        lock = lock_status(parts[0], base_dir=WORKSPACE_DIR)
        if lock is None or lock.is_expired():
            return _ok({"locked": False})
        d = lock.to_dict()
        d["locked"] = True
        return _ok(d)
    elif m == "acquire" and parts:
        kwargs = {"task_id": parts[0], "agent_id": params["agent"], "base_dir": WORKSPACE_DIR}
        if "ttl" in params:
            kwargs["ttl_seconds"] = int(params["ttl"])
        lock = acquire_lock(**kwargs)
        return _ok(lock.to_dict())
    elif m == "release" and parts:
        result = release_lock(parts[0], params["agent"], base_dir=WORKSPACE_DIR)
        return _ok({"released": result})
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


def handle_poll(method, parts, params):
    """Combined polling endpoint — returns workstreams, counts, scheduler, and optionally board in one call."""
    wss = list_workstreams(base_dir=WORKSPACE_DIR)
    counts = {}
    for ws in wss:
        counts[ws.id] = len(list_tasks(ws.id, base_dir=WORKSPACE_DIR))
    result = {
        "workstreams": [w.to_dict() for w in wss],
        "counts": counts,
        "scheduler": scheduler_status(base_dir=WORKSPACE_DIR),
        "active_triggers": get_active_triggers(),
    }
    # If a board ID is requested, include it
    ws_id = params.get("board") or (method if method != "all" else None)
    if ws_id:
        try:
            ws = read_workstream(ws_id, base_dir=WORKSPACE_DIR)
            tasks = list_tasks(ws_id, base_dir=WORKSPACE_DIR)
            task_dicts = []
            for t in tasks:
                td = t.to_dict()
                if td.get("_error"):
                    td["lock"] = {"locked": False}
                    task_dicts.append(td)
                    continue
                lock = lock_status(t.id, base_dir=WORKSPACE_DIR)
                if lock and not lock.is_expired():
                    td["lock"] = lock.to_dict()
                    td["lock"]["locked"] = True
                else:
                    td["lock"] = {"locked": False}
                task_dicts.append(td)
            result["board"] = {"workstream": ws.to_dict(), "tasks": task_dicts}
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
    return _err(f"Unknown retry method: {method}")


ROUTE_MAP = {
    "board": lambda m, p, q: handle_board(p, q),
    "poll": lambda m, p, q: handle_poll(m, p, q),
    "workstream": handle_workstream,
    "task": handle_task,
    "lock": handle_lock,
    "trigger": handle_trigger,
    "scheduler": handle_scheduler,
    "audit": handle_audit,
    "retry": handle_retry,
}


class Handler(SimpleHTTPRequestHandler):
    """Handles static files and /api/ routes."""

    protocol_version = "HTTP/1.1"  # keep-alive: browser reuses connections
    timeout = 30  # close idle keep-alive connections after 30s

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def _send_json(self, code: int, body: str):
        encoded = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(encoded)
        return  # don't close — let keep-alive reuse the connection
        self.wfile.write(body.encode())

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
