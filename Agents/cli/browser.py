#!/usr/bin/env python3
"""
browser.py — Persistent browser automation CLI for agents.

Uses Playwright to drive Google Chrome with sessions that persist between
CLI invocations. A lightweight background HTTP server manages browser state.

Usage:
    browser.py open [URL]                           Open new session
    browser.py goto SESSION URL                     Navigate to URL
    browser.py status SESSION                       Get page load state
    browser.py html SESSION [--selector SEL]        Get page HTML
    browser.py text SESSION [--selector SEL]        Get visible text
    browser.py click SESSION SELECTOR               Click an element
    browser.py type SESSION SELECTOR TEXT            Enter text in a field
    browser.py press SESSION KEY                    Press a key (Enter, Tab, etc)
    browser.py select SESSION SELECTOR VALUE        Select dropdown option
    browser.py screenshot SESSION [--path FILE]     Take a screenshot
    browser.py get_console_logs SESSION             Get captured JS console output
    browser.py wait SESSION SELECTOR [--timeout MS] Wait for element
    browser.py eval SESSION EXPRESSION              Run JavaScript
    browser.py close SESSION                        Close a session
    browser.py sessions                             List active sessions
    browser.py cleanup-orphans [--force]           Terminate orphan Playwright Chromium processes
    browser.py auth-list                            List saved auth states
    browser.py auth-get SITE                        Get saved auth state for a site
    browser.py auth-save SESSION --site SITE        Save session auth state under a site key
    browser.py auth-delete SITE                     Delete saved auth state for a site
    browser.py server-stop                          Stop background server

The server auto-starts on first 'open' and persists until 'server-stop'.
Each session is an isolated browser context with its own cookies/storage.

Requirements:
    pip install --upgrade "playwright==1.59.0"
    python -m playwright install chromium
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid
from datetime import datetime, timezone
import tempfile
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import re

_TMPDIR = Path(tempfile.gettempdir())
SERVER_INFO = _TMPDIR / "browser_server.json"
SERVER_LOG = _TMPDIR / "browser_server.log"
DEFAULT_TIMEOUT = 30000  # ms
AUTH_DIR = Path("playwright/.auth")
AUTH_INDEX = AUTH_DIR / "index.json"
MAX_CONSOLE_LOGS = 200
PLAYWRIGHT_PROFILE_MARKER = "playwright_chromiumdev_profile"


def _normalize_site_key(site: str) -> str:
    text = str(site or "").strip().lower()
    if not text:
        raise ValueError("site is required")

    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.netloc or parsed.path).strip().lower()
    if host.startswith("www."):
        host = host[4:]

    candidate = host or text
    parts = [part for part in candidate.split(".") if part]
    if len(parts) == 2 and len(parts[1]) <= 4:
        candidate = parts[0]

    normalized = re.sub(r"[^a-z0-9._-]+", "-", candidate).strip("-._")
    if not normalized:
        raise ValueError(f"invalid site key: {site}")
    return normalized


def _ensure_auth_dir() -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)


def _load_auth_registry() -> dict:
    if not AUTH_INDEX.exists():
        return {"version": 1, "sites": {}}
    try:
        data = json.loads(AUTH_INDEX.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "sites": {}}

    if not isinstance(data, dict):
        return {"version": 1, "sites": {}}

    sites = data.get("sites")
    if not isinstance(sites, dict):
        sites = {}
    return {"version": 1, "sites": sites}


def _save_auth_registry(registry: dict) -> None:
    _ensure_auth_dir()
    AUTH_INDEX.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _default_auth_path(site_key: str) -> Path:
    return AUTH_DIR / f"{site_key}.json"


def _extract_playwright_orphan_pids(ps_output: str, exclude_pids=None) -> list[int]:
    excluded = set()
    for pid in (exclude_pids or []):
        try:
            excluded.add(int(pid))
        except (TypeError, ValueError):
            continue

    pids = []
    for raw_line in ps_output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid in excluded:
            continue
        cmdline = parts[1] if len(parts) > 1 else ""
        if PLAYWRIGHT_PROFILE_MARKER not in cmdline:
            continue
        pids.append(pid)

    return sorted(set(pids))


def _cleanup_playwright_orphan_processes(force=False) -> dict:
    """Terminate orphan Playwright Chromium processes, never personal Chrome."""
    found = []
    terminated = []
    term_failed = []

    # Multiple passes handle delayed child teardown after parent exit.
    for _ in range(3):
        try:
            ps_output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        except Exception as exc:
            return {"error": f"failed to inspect process list: {exc}"}

        current = _extract_playwright_orphan_pids(ps_output, exclude_pids=[os.getpid(), os.getppid()])
        if not current:
            break

        for pid in current:
            if pid not in found:
                found.append(pid)
            try:
                os.kill(pid, signal.SIGTERM)
                if pid not in terminated:
                    terminated.append(pid)
            except ProcessLookupError:
                continue
            except PermissionError:
                if pid not in term_failed:
                    term_failed.append(pid)

        time.sleep(0.2)

    try:
        remaining_output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
        remaining = _extract_playwright_orphan_pids(remaining_output, exclude_pids=[os.getpid(), os.getppid()])
    except Exception:
        remaining = []

    forced = []
    if force and remaining:
        for pid in list(remaining):
            try:
                os.kill(pid, signal.SIGKILL)
                forced.append(pid)
            except OSError:
                pass
        time.sleep(0.15)
        try:
            remaining_output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
            remaining = _extract_playwright_orphan_pids(remaining_output, exclude_pids=[os.getpid(), os.getppid()])
        except Exception:
            remaining = []

    return {
        "marker": PLAYWRIGHT_PROFILE_MARKER,
        "found": found,
        "terminated": terminated,
        "force_killed": forced,
        "term_failed": term_failed,
        "remaining": remaining,
    }


def _auth_registry_entry(site: str):
    site_key = _normalize_site_key(site)
    registry = _load_auth_registry()
    entry = registry["sites"].get(site_key)
    if not isinstance(entry, dict):
        return site_key, None

    path = entry.get("path")
    if not path or not Path(path).exists():
        return site_key, None
    return site_key, entry


def _auth_get(site: str) -> dict:
    site_key, entry = _auth_registry_entry(site)
    if entry is None:
        return {"site": site_key, "found": False}
    return {
        "site": site_key,
        "found": True,
        "path": entry["path"],
        "updated_at": entry.get("updated_at"),
        "account_label": entry.get("account_label"),
    }


def _auth_list() -> dict:
    registry = _load_auth_registry()
    entries = []
    live_sites = {}
    for site_key, entry in sorted(registry["sites"].items()):
        path = str(entry.get("path") or "")
        if not path or not Path(path).exists():
            continue
        live_sites[site_key] = entry
        entries.append({
            "site": site_key,
            "path": path,
            "updated_at": entry.get("updated_at"),
            "account_label": entry.get("account_label"),
        })

    if live_sites != registry["sites"]:
        registry["sites"] = live_sites
        _save_auth_registry(registry)

    return {"entries": entries}


def _auth_delete(site: str) -> dict:
    site_key = _normalize_site_key(site)
    registry = _load_auth_registry()
    entry = registry["sites"].pop(site_key, None)
    deleted_file = False
    if isinstance(entry, dict):
        path = entry.get("path")
        if path and Path(path).exists():
            Path(path).unlink()
            deleted_file = True
    _save_auth_registry(registry)
    return {"site": site_key, "deleted": entry is not None, "deleted_file": deleted_file}


def _auth_save(site: str, path: str, account_label: str = None) -> dict:
    site_key = _normalize_site_key(site)
    registry = _load_auth_registry()
    registry["sites"][site_key] = {
        "path": path,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "account_label": account_label,
    }
    _save_auth_registry(registry)
    return {
        "site": site_key,
        "path": path,
        "updated_at": registry["sites"][site_key]["updated_at"],
        "account_label": account_label,
    }


# ─────────────────────────────────────────────
#  Server — manages Playwright browser sessions
# ─────────────────────────────────────────────

class BrowserManager:
    """Holds the Playwright browser and all active sessions."""

    def __init__(self):
        self.pw = None
        self.browser = None
        self.sessions = {}
        self.headless = False

    def _ensure_runtime(self):
        if self.browser is not None:
            return

        from playwright.sync_api import sync_playwright

        if self.pw is None:
            self.pw = sync_playwright().start()

        stealth_args = [
            "--disable-blink-features=AutomationControlled",
        ]
        try:
            self.browser = self.pw.chromium.launch(channel="chrome", headless=self.headless, args=stealth_args)
        except Exception:
            self.browser = self.pw.chromium.launch(headless=self.headless, args=stealth_args)

    def _stop_runtime(self):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self.pw:
            try:
                self.pw.stop()
            except Exception:
                pass
            self.pw = None

    def start(self, headless=False):
        self.headless = headless
        self._ensure_runtime()

    def stop(self):
        for sid in list(self.sessions):
            try:
                self.sessions[sid]["context"].close()
            except Exception:
                pass
        self.sessions.clear()
        self._stop_runtime()

    def handle(self, cmd, params):
        fn = getattr(self, f"cmd_{cmd}", None)
        if not fn:
            return {"error": f"Unknown command: {cmd}"}
        try:
            return fn(params)
        except Exception as e:
            return {"error": str(e)}

    def _page(self, params):
        sid = params.get("session")
        if not sid or sid not in self.sessions:
            raise ValueError(f"Unknown session: {sid}")
        return self.sessions[sid]["page"]

    def _session(self, params):
        sid = params.get("session")
        if not sid or sid not in self.sessions:
            raise ValueError(f"Unknown session: {sid}")
        return self.sessions[sid]

    def _record_console_log(self, session_info, log_type, text):
        entries = session_info.setdefault("console_logs", [])
        seq = session_info.setdefault("console_next_seq", 1)
        entries.append({
            "seq": seq,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "type": log_type,
            "text": text,
        })
        session_info["console_next_seq"] = seq + 1
        if len(entries) > MAX_CONSOLE_LOGS:
            del entries[:-MAX_CONSOLE_LOGS]

    @staticmethod
    def _console_message_field(msg, field_name):
        """Read Playwright console-message fields across API variations.

        Depending on the installed Playwright build, fields like `type` and
        `text` can be exposed either as methods or as plain properties.
        """
        value = getattr(msg, field_name, None)
        if callable(value):
            value = value()
        return "" if value is None else str(value)

    def _attach_console_capture(self, page, session_info):
        page.on(
            "console",
            lambda msg: self._record_console_log(
                session_info,
                self._console_message_field(msg, "type"),
                self._console_message_field(msg, "text"),
            ),
        )
        page.on("pageerror", lambda err: self._record_console_log(session_info, "pageerror", str(err)))

    # ── Commands ──

    _STEALTH_INIT = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        delete window.__playwright;
        delete window.__pw_manual;
        window.chrome = { runtime: {}, };
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({state: Notification.permission})
                : originalQuery(parameters);
    """

    def cmd_open(self, params):
        self._ensure_runtime()
        sid = uuid.uuid4().hex[:8]
        ctx_kwargs = dict(
            viewport={"width": 1280, "height": 800},
        )
        auth_path = params.get("auth")
        if auth_path and Path(auth_path).exists():
            ctx_kwargs["storage_state"] = auth_path
        context = self.browser.new_context(**ctx_kwargs)
        context.add_init_script(self._STEALTH_INIT)
        page = context.new_page()
        session_info = {
            "context": context,
            "page": page,
            "created": datetime.now().isoformat(timespec="seconds"),
            "console_logs": [],
            "console_next_seq": 1,
        }
        self._attach_console_capture(page, session_info)
        target_url = params.get("url")
        if target_url:
            page.goto(target_url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        self.sessions[sid] = session_info
        return {"session": sid, "url": page.url, "title": page.title()}

    def cmd_goto(self, params):
        page = self._page(params)
        page.goto(params["url"], wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        return {"url": page.url, "title": page.title()}

    def cmd_status(self, params):
        page = self._page(params)
        state = page.evaluate("() => document.readyState")
        return {"url": page.url, "title": page.title(), "state": state}

    def cmd_html(self, params):
        page = self._page(params)
        sel = params.get("selector")
        if sel:
            el = page.query_selector(sel)
            if not el:
                return {"error": f"Selector not found: {sel}"}
            html = el.inner_html() if params.get("inner") else el.evaluate("e => e.outerHTML")
        else:
            html = page.content()
        return {"html": html}

    def cmd_text(self, params):
        page = self._page(params)
        sel = params.get("selector")
        if sel:
            el = page.query_selector(sel)
            if not el:
                return {"error": f"Selector not found: {sel}"}
            text = el.inner_text()
        else:
            text = page.inner_text("body")
        return {"text": text}

    def cmd_click(self, params):
        page = self._page(params)
        page.click(params["selector"], timeout=DEFAULT_TIMEOUT)
        return {"clicked": params["selector"], "url": page.url}

    def cmd_type(self, params):
        page = self._page(params)
        sel = params["selector"]
        text = params["text"]
        if params.get("keys"):
            delay = params.get("delay", 0)
            page.click(sel, timeout=DEFAULT_TIMEOUT)
            page.type(sel, text, delay=delay, timeout=DEFAULT_TIMEOUT)
        else:
            page.fill(sel, text, timeout=DEFAULT_TIMEOUT)
        return {"typed": len(text), "selector": sel}

    def cmd_press(self, params):
        page = self._page(params)
        page.keyboard.press(params["key"])
        return {"pressed": params["key"]}

    def cmd_select(self, params):
        page = self._page(params)
        page.select_option(params["selector"], params["value"], timeout=DEFAULT_TIMEOUT)
        return {"selected": params["value"], "selector": params["selector"]}

    def cmd_screenshot(self, params):
        page = self._page(params)
        path = params.get("path", str(_TMPDIR / f"browser_{params['session']}.png"))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=path, full_page=params.get("full_page", False))
        return {"path": path}

    def cmd_get_console_logs(self, params):
        session_info = self._session(params)
        logs = list(session_info.get("console_logs", []))
        since_seq = params.get("since_seq")
        if since_seq is not None:
            logs = [entry for entry in logs if entry.get("seq", 0) > since_seq]
        limit = params.get("limit")
        if limit is not None:
            logs = logs[-max(0, int(limit)):]
        result = {"session": params["session"], "logs": logs}
        if params.get("clear"):
            session_info["console_logs"] = []
        return result

    def cmd_wait(self, params):
        page = self._page(params)
        timeout = params.get("timeout", DEFAULT_TIMEOUT)
        page.wait_for_selector(params["selector"], timeout=timeout)
        return {"found": params["selector"]}

    def cmd_eval(self, params):
        page = self._page(params)
        result = page.evaluate(params["expression"])
        return {"result": result}

    def cmd_save_auth(self, params):
        sid = params.get("session")
        if sid not in self.sessions:
            return {"error": f"Unknown session: {sid}"}
        path = params.get("path", f"playwright/.auth/{sid}.json")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.sessions[sid]["context"].storage_state(path=path)
        return {"path": path, "session": sid}

    def cmd_close(self, params):
        sid = params.get("session")
        if sid not in self.sessions:
            return {"error": f"Unknown session: {sid}"}
        try:
            self.sessions[sid]["context"].close()
        except Exception:
            pass
        del self.sessions[sid]
        if not self.sessions:
            # Tear down Chromium/Playwright when the last session closes so
            # background browser processes do not linger with zero windows.
            self._stop_runtime()
        return {"closed": sid}

    def cmd_sessions(self, _params):
        out = []
        for sid, info in self.sessions.items():
            try:
                url = info["page"].url
                title = info["page"].title()
            except Exception:
                url = "?"
                title = "?"
            out.append({"session": sid, "url": url, "title": title, "created": info["created"]})
        return {"sessions": out}


# ── HTTP plumbing ──

manager = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"   # close connection after each response

    def log_message(self, _fmt, *_args):
        pass

    def do_GET(self):
        self._respond({"ok": True})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._respond({"error": "Invalid JSON"}, 400)
            return
        result = manager.handle(data.get("command", ""), data.get("params", {}))
        self._respond(result)

    def _respond(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(headless=False):
    global manager
    manager = BrowserManager()
    manager.start(headless=headless)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]

    SERVER_INFO.write_text(json.dumps({"port": port, "pid": os.getpid()}))
    print(f"Browser server on port {port} (PID {os.getpid()})", flush=True)

    def shutdown(_sig, _frame):
        manager.stop()
        SERVER_INFO.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    finally:
        manager.stop()
        SERVER_INFO.unlink(missing_ok=True)


# ─────────────────────────────────────────────
#  Client — thin wrapper that talks to server
# ─────────────────────────────────────────────

def get_server_port():
    if not SERVER_INFO.exists():
        return None
    try:
        info = json.loads(SERVER_INFO.read_text())
        urllib.request.urlopen(
            f"http://127.0.0.1:{info['port']}/", timeout=2
        )
        return info["port"]
    except (OSError, urllib.error.URLError, KeyError, json.JSONDecodeError):
        SERVER_INFO.unlink(missing_ok=True)
        return None


def ensure_server(headless=False):
    port = get_server_port()
    if port:
        return port
    cmd = [sys.executable, __file__, "_server"]
    if headless:
        cmd.append("--headless")
    with open(SERVER_LOG, "w") as log:
        subprocess.Popen(
            cmd, stdout=log, stderr=log,
            stdin=subprocess.DEVNULL, close_fds=True,
            start_new_session=True,
        )
    for _ in range(30):
        time.sleep(0.5)
        port = get_server_port()
        if port:
            return port
    print(f"Error: server failed to start. Check {SERVER_LOG}", file=sys.stderr)
    sys.exit(1)


def send(port, command, params):
    data = json.dumps({"command": command, "params": params}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read())
    except urllib.error.URLError as e:
        return {"error": f"Server connection failed: {e}"}


# ─────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(prog="browser", description="Persistent browser automation for agents")
    parser.add_argument("--json", action="store_true", help="Output raw JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("open", help="Open a new browser session")
    p.add_argument("url", nargs="?", help="URL to navigate to")
    p.add_argument("--headless", action="store_true", help="Headless mode (no visible window)")
    p.add_argument("--auth", help="Path to saved auth state JSON file")
    p.add_argument("--site", help="Saved auth site key to preload, e.g. linkedin")

    p = sub.add_parser("goto", help="Navigate to URL")
    p.add_argument("session", help="Session ID")
    p.add_argument("url", help="URL")

    p = sub.add_parser("status", help="Page load state")
    p.add_argument("session", help="Session ID")

    p = sub.add_parser("html", help="Get page HTML")
    p.add_argument("session", help="Session ID")
    p.add_argument("--selector", "-s", help="CSS selector for specific element")
    p.add_argument("--inner", action="store_true", help="Inner HTML only (with --selector)")

    p = sub.add_parser("text", help="Get visible text")
    p.add_argument("session", help="Session ID")
    p.add_argument("--selector", "-s", help="CSS selector for specific element")

    p = sub.add_parser("click", help="Click an element")
    p.add_argument("session", help="Session ID")
    p.add_argument("selector", help="CSS selector")

    p = sub.add_parser("type", help="Enter text in a field")
    p.add_argument("session", help="Session ID")
    p.add_argument("selector", help="CSS selector")
    p.add_argument("text", help="Text to enter")
    p.add_argument("--keys", action="store_true", help="Type key-by-key instead of fill (for autocomplete fields)")
    p.add_argument("--delay", type=int, default=0, help="Delay in ms between keystrokes (only with --keys)")

    p = sub.add_parser("press", help="Press a keyboard key")
    p.add_argument("session", help="Session ID")
    p.add_argument("key", help="Key name (Enter, Tab, Escape, ArrowDown, etc)")

    p = sub.add_parser("select", help="Select a dropdown option")
    p.add_argument("session", help="Session ID")
    p.add_argument("selector", help="CSS selector of <select>")
    p.add_argument("value", help="Option value to select")

    p = sub.add_parser("screenshot", help="Take a screenshot")
    p.add_argument("session", help="Session ID")
    p.add_argument("--path", help="Output file (default: /tmp/browser_SESSION.png)")
    p.add_argument("--full", action="store_true", help="Full page")

    p = sub.add_parser("get_console_logs", aliases=["get-console-logs"], help="Get captured JS console output")
    p.add_argument("session", help="Session ID")
    p.add_argument("--since-seq", type=int, help="Only return entries with seq greater than this value")
    p.add_argument("--limit", type=int, help="Return only the most recent N entries")
    p.add_argument("--clear", action="store_true", help="Clear stored logs after reading")

    p = sub.add_parser("wait", help="Wait for element to appear")
    p.add_argument("session", help="Session ID")
    p.add_argument("selector", help="CSS selector")
    p.add_argument("--timeout", type=int, default=30000, help="Timeout in ms (default: 30000)")

    p = sub.add_parser("eval", help="Run JavaScript on the page")
    p.add_argument("session", help="Session ID")
    p.add_argument("expression", help="JS expression to evaluate")

    p = sub.add_parser("save-auth", help="Save auth state (cookies/storage) to file")
    p.add_argument("session", help="Session ID")
    p.add_argument("--path", default="playwright/.auth/x_auth.json", help="Output file path")

    sub.add_parser("auth-list", help="List saved auth states")

    p = sub.add_parser("auth-get", help="Get saved auth state for a site")
    p.add_argument("site", help="Site key, e.g. linkedin")

    p = sub.add_parser("auth-save", help="Save auth state for a reusable site key")
    p.add_argument("session", help="Session ID")
    p.add_argument("--site", required=True, help="Site key, e.g. linkedin")
    p.add_argument("--account-label", help="Optional human label for the account")

    p = sub.add_parser("auth-delete", help="Delete saved auth state for a site")
    p.add_argument("site", help="Site key, e.g. linkedin")

    p = sub.add_parser("cleanup-orphans", aliases=["cleanup"], help="Terminate orphan Playwright Chromium processes")
    p.add_argument("--force", action="store_true", help="Also send SIGKILL to stubborn leftover processes")

    p = sub.add_parser("close", help="Close a session")
    p.add_argument("session", help="Session ID")

    sub.add_parser("sessions", help="List active sessions")
    sub.add_parser("server-stop", help="Stop the background server")

    p = sub.add_parser("_server")
    p.add_argument("--headless", action="store_true")

    args = parser.parse_args()

    # ── Internal: start server process ──
    if args.command == "_server":
        run_server(headless=args.headless)
        return

    if args.command == "auth-list":
        result = _auth_list()
        if args.json:
            print(json.dumps(result, indent=2))
            return
        entries = result.get("entries", [])
        if not entries:
            print("No saved auth states.")
            return
        for entry in entries:
            label = f" [{entry['account_label']}]" if entry.get("account_label") else ""
            print(f"{entry['site']}{label}: {entry['path']} ({entry.get('updated_at') or 'unknown'})")
        return

    if args.command == "auth-get":
        result = _auth_get(args.site)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        if not result.get("found"):
            print(f"No saved auth for {result['site']}.")
            return
        label = f" [{result['account_label']}]" if result.get("account_label") else ""
        print(f"{result['site']}{label}: {result['path']} ({result.get('updated_at') or 'unknown'})")
        return

    if args.command == "auth-delete":
        result = _auth_delete(args.site)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        if result.get("deleted"):
            print(f"Deleted auth for {result['site']}.")
        else:
            print(f"No saved auth for {result['site']}.")
        return

    if args.command in {"cleanup-orphans", "cleanup"}:
        result = _cleanup_playwright_orphan_processes(force=args.force)
        if args.json:
            print(json.dumps(result, indent=2))
            return
        if result.get("error"):
            print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)
        if result["remaining"]:
            print(
                "Cleanup partial: "
                f"found {len(result['found'])}, terminated {len(result['terminated'])}, "
                f"remaining {len(result['remaining'])}."
            )
        else:
            print(
                "Cleanup complete: "
                f"found {len(result['found'])}, terminated {len(result['terminated'])}."
            )
        return

    # ── server-stop ──
    if args.command == "server-stop":
        if not SERVER_INFO.exists():
            print("Server is not running.")
            cleanup = _cleanup_playwright_orphan_processes(force=False)
            if cleanup.get("terminated"):
                print(f"Cleaned {len(cleanup['terminated'])} orphan Playwright Chromium process(es).")
            return
        try:
            info = json.loads(SERVER_INFO.read_text())
            os.kill(info["pid"], signal.SIGTERM)
        except (OSError, KeyError):
            pass
        SERVER_INFO.unlink(missing_ok=True)
        time.sleep(0.2)
        cleanup = _cleanup_playwright_orphan_processes(force=False)
        print("Server stopped.")
        if cleanup.get("terminated"):
            print(f"Cleaned {len(cleanup['terminated'])} orphan Playwright Chromium process(es).")
        return

    # ── All other commands need a running server ──
    headless = getattr(args, "headless", False)
    port = ensure_server(headless=headless)

    # Build params from args
    params = {}
    cmd = args.command

    if cmd == "open":
        if args.url:
            params["url"] = args.url
        if args.auth:
            params["auth"] = args.auth
        elif args.site:
            auth_info = _auth_get(args.site)
            if auth_info.get("found"):
                params["auth"] = auth_info["path"]
                params["site"] = auth_info["site"]

    elif cmd == "goto":
        params["session"] = args.session
        params["url"] = args.url

    elif cmd == "status":
        params["session"] = args.session

    elif cmd == "html":
        params["session"] = args.session
        if args.selector:
            params["selector"] = args.selector
        if args.inner:
            params["inner"] = True

    elif cmd == "text":
        params["session"] = args.session
        if args.selector:
            params["selector"] = args.selector

    elif cmd == "click":
        params["session"] = args.session
        params["selector"] = args.selector

    elif cmd == "type":
        params["session"] = args.session
        params["selector"] = args.selector
        params["text"] = args.text
        if args.keys:
            params["keys"] = True
        if args.delay:
            params["delay"] = args.delay

    elif cmd == "press":
        params["session"] = args.session
        params["key"] = args.key

    elif cmd == "select":
        params["session"] = args.session
        params["selector"] = args.selector
        params["value"] = args.value

    elif cmd == "screenshot":
        params["session"] = args.session
        if args.path:
            params["path"] = args.path
        if args.full:
            params["full_page"] = True

    elif cmd in {"get_console_logs", "get-console-logs"}:
        cmd = "get_console_logs"
        params["session"] = args.session
        if args.since_seq is not None:
            params["since_seq"] = args.since_seq
        if args.limit is not None:
            params["limit"] = args.limit
        if args.clear:
            params["clear"] = True

    elif cmd == "wait":
        params["session"] = args.session
        params["selector"] = args.selector
        params["timeout"] = args.timeout

    elif cmd == "eval":
        params["session"] = args.session
        params["expression"] = args.expression

    elif cmd == "save-auth":
        cmd = "save_auth"
        params["session"] = args.session
        params["path"] = args.path

    elif cmd == "auth-save":
        cmd = "save_auth"
        site_key = _normalize_site_key(args.site)
        params["session"] = args.session
        params["path"] = str(_default_auth_path(site_key))

    elif cmd == "close":
        params["session"] = args.session

    # Send to server
    result = send(port, cmd, params)

    if args.command == "auth-save" and "error" not in result:
        result.update(_auth_save(args.site, result["path"], account_label=args.account_label))

    if args.command == "open" and "error" not in result and params.get("auth"):
        result["auth_path"] = params["auth"]
        if params.get("site"):
            result["site"] = params["site"]

    # ── Output ──
    if args.json:
        print(json.dumps(result, indent=2))
        return

    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        sys.exit(1)

    if cmd == "open":
        print(f"Session: {result['session']}")
        print(f"URL:     {result.get('url', '')}")
        print(f"Title:   {result.get('title', '')}")
        if result.get("auth_path"):
            loaded_for = f" ({result['site']})" if result.get("site") else ""
            print(f"Auth:    {result['auth_path']}{loaded_for}")

    elif cmd == "goto":
        print(f"URL:   {result['url']}")
        print(f"Title: {result['title']}")

    elif cmd == "status":
        print(f"URL:   {result['url']}")
        print(f"Title: {result['title']}")
        print(f"State: {result['state']}")

    elif cmd == "html":
        print(result["html"])

    elif cmd == "text":
        print(result["text"])

    elif cmd == "click":
        print(f"Clicked: {result['clicked']}")
        print(f"URL:     {result['url']}")

    elif cmd == "type":
        print(f"Typed {result['typed']} chars into {result['selector']}")

    elif cmd == "press":
        print(f"Pressed: {result['pressed']}")

    elif cmd == "select":
        print(f"Selected '{result['selected']}' in {result['selector']}")

    elif cmd == "screenshot":
        print(f"Screenshot: {result['path']}")

    elif cmd == "get_console_logs":
        logs = result.get("logs", [])
        if not logs:
            print("No console logs.")
        else:
            for entry in logs:
                print(f"[{entry.get('seq')}] {entry.get('timestamp')} {entry.get('type')}: {entry.get('text')}")

    elif cmd == "wait":
        print(f"Found: {result['found']}")

    elif cmd == "eval":
        r = result.get("result")
        if isinstance(r, (dict, list)):
            print(json.dumps(r, indent=2))
        else:
            print(f"Result: {r}")

    elif cmd == "save_auth":
        if args.command == "auth-save":
            label = f" [{result['account_label']}]" if result.get("account_label") else ""
            print(f"Auth saved for {result['site']}{label}: {result['path']}")
        else:
            print(f"Auth saved: {result['path']}")

    elif cmd == "close":
        print(f"Closed: {result['closed']}")

    elif cmd == "sessions":
        sessions = result.get("sessions", [])
        if not sessions:
            print("No active sessions.")
        else:
            for s in sessions:
                title = (s["title"] or "")[:50]
                url = (s["url"] or "")[:70]
                print(f"  {s['session']}  {url}  {title}  ({s['created']})")


if __name__ == "__main__":
    main()
