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
    browser.py wait SESSION SELECTOR [--timeout MS] Wait for element
    browser.py eval SESSION EXPRESSION              Run JavaScript
    browser.py close SESSION                        Close a session
    browser.py sessions                             List active sessions
    browser.py server-stop                          Stop background server

The server auto-starts on first 'open' and persists until 'server-stop'.
Each session is an isolated browser context with its own cookies/storage.

Requirements:
    pip install playwright
    playwright install chromium
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
import uuid
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SERVER_INFO = Path("/tmp/browser_server.json")
SERVER_LOG = Path("/tmp/browser_server.log")
DEFAULT_TIMEOUT = 30000  # ms


# ─────────────────────────────────────────────
#  Server — manages Playwright browser sessions
# ─────────────────────────────────────────────

class BrowserManager:
    """Holds the Playwright browser and all active sessions."""

    def __init__(self):
        self.pw = None
        self.browser = None
        self.sessions = {}

    def start(self, headless=False):
        from playwright.sync_api import sync_playwright
        self.pw = sync_playwright().start()
        try:
            self.browser = self.pw.chromium.launch(channel="chrome", headless=headless)
        except Exception:
            self.browser = self.pw.chromium.launch(headless=headless)

    def stop(self):
        for sid in list(self.sessions):
            try:
                self.sessions[sid]["context"].close()
            except Exception:
                pass
        self.sessions.clear()
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.pw:
            try:
                self.pw.stop()
            except Exception:
                pass

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

    # ── Commands ──

    def cmd_open(self, params):
        sid = uuid.uuid4().hex[:8]
        context = self.browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        url = params.get("url")
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=DEFAULT_TIMEOUT)
        self.sessions[sid] = {
            "context": context,
            "page": page,
            "created": datetime.now().isoformat(timespec="seconds"),
        }
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
            page.type(sel, text, timeout=DEFAULT_TIMEOUT)
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
        path = params.get("path", f"/tmp/browser_{params['session']}.png")
        page.screenshot(path=path, full_page=params.get("full_page", False))
        return {"path": path}

    def cmd_wait(self, params):
        page = self._page(params)
        timeout = params.get("timeout", DEFAULT_TIMEOUT)
        page.wait_for_selector(params["selector"], timeout=timeout)
        return {"found": params["selector"]}

    def cmd_eval(self, params):
        page = self._page(params)
        result = page.evaluate(params["expression"])
        return {"result": result}

    def cmd_close(self, params):
        sid = params.get("session")
        if sid not in self.sessions:
            return {"error": f"Unknown session: {sid}"}
        try:
            self.sessions[sid]["context"].close()
        except Exception:
            pass
        del self.sessions[sid]
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
        os.kill(info["pid"], 0)
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
        subprocess.Popen(cmd, stdout=log, stderr=log, start_new_session=True)
    for _ in range(30):
        time.sleep(0.5)
        port = get_server_port()
        if port:
            return port
    print("Error: server failed to start. Check /tmp/browser_server.log", file=sys.stderr)
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

    p = sub.add_parser("wait", help="Wait for element to appear")
    p.add_argument("session", help="Session ID")
    p.add_argument("selector", help="CSS selector")
    p.add_argument("--timeout", type=int, default=30000, help="Timeout in ms (default: 30000)")

    p = sub.add_parser("eval", help="Run JavaScript on the page")
    p.add_argument("session", help="Session ID")
    p.add_argument("expression", help="JS expression to evaluate")

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

    # ── server-stop ──
    if args.command == "server-stop":
        if not SERVER_INFO.exists():
            print("Server is not running.")
            return
        try:
            info = json.loads(SERVER_INFO.read_text())
            os.kill(info["pid"], signal.SIGTERM)
        except (OSError, KeyError):
            pass
        SERVER_INFO.unlink(missing_ok=True)
        print("Server stopped.")
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

    elif cmd == "wait":
        params["session"] = args.session
        params["selector"] = args.selector
        params["timeout"] = args.timeout

    elif cmd == "eval":
        params["session"] = args.session
        params["expression"] = args.expression

    elif cmd == "close":
        params["session"] = args.session

    # Send to server
    result = send(port, cmd, params)

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

    elif cmd == "wait":
        print(f"Found: {result['found']}")

    elif cmd == "eval":
        r = result.get("result")
        if isinstance(r, (dict, list)):
            print(json.dumps(r, indent=2))
        else:
            print(f"Result: {r}")

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
