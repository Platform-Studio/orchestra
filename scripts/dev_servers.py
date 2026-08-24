#!/usr/bin/env python3
"""Run scheduler + web server with automatic restart on code changes.

This avoids manual restarts while iterating on orchestration/workstream code.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from orchestration.persistence import resolve_artifact_root, resolve_workstream_root

WATCH_ROOTS = [
    BASE_DIR / "orchestration",
    BASE_DIR / "workstream_manager",
]

WATCH_FILES = [
    BASE_DIR / "pyproject.toml",
    BASE_DIR / ".env",
]

WATCH_EXTENSIONS = {
    ".py",
    ".html",
    ".js",
    ".css",
    ".yaml",
    ".yml",
    ".toml",
}

IGNORE_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
}

STARTUP_GRACE_SECONDS = 0.75


def _reload_dotenv() -> None:
    if load_dotenv is None:
        return
    load_dotenv(BASE_DIR / ".env", override=True)


def _orchestration_base_dir() -> Path:
    return Path(resolve_workstream_root(str(BASE_DIR))).resolve()


@dataclass
class ManagedProc:
    name: str
    cmd: list[str]
    log_path: Path
    popen: subprocess.Popen | None = None
    log_handle: object | None = None


def _tail_log(path: Path, lines: int = 20) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    chunks = text.rstrip().splitlines()
    if not chunks:
        return ""
    return "\n".join(chunks[-lines:])


def _orchestration_cli_json(py_executable: str, args: list[str]) -> dict:
    cmd = [py_executable, "-m", "orchestration.cli", "--base-dir", str(_orchestration_base_dir())] + args
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE_DIR))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"command failed: {' '.join(cmd)}")
    payload = json.loads(result.stdout)
    return payload.get("data", {})


def _log_dir() -> Path:
    return Path(resolve_artifact_root(str(BASE_DIR))) / "artifacts" / "logs"


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(BASE_DIR))
    except ValueError:
        return str(path)


def _stop_existing_scheduler(py_executable: str) -> None:
    try:
        status = _orchestration_cli_json(py_executable, ["scheduler", "status"])
    except Exception:
        return

    if not status.get("running"):
        return

    try:
        _orchestration_cli_json(py_executable, ["scheduler", "stop"])
    except Exception:
        pass

    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            status = _orchestration_cli_json(py_executable, ["scheduler", "status"])
        except Exception:
            return
        if not status.get("running"):
            return
        time.sleep(0.1)


def _ensure_process_started(proc: ManagedProc, grace_seconds: float = STARTUP_GRACE_SECONDS) -> None:
    if not proc.popen:
        raise RuntimeError(f"{proc.name} did not start")

    if proc.popen.poll() is not None:
        tail = _tail_log(proc.log_path)
        raise RuntimeError(
            f"{proc.name} exited immediately with code {proc.popen.returncode}."
            + (f"\nRecent log output:\n{tail}" if tail else "")
        )

    deadline = time.time() + grace_seconds
    while time.time() < deadline:
        if proc.popen.poll() is not None:
            tail = _tail_log(proc.log_path)
            raise RuntimeError(
                f"{proc.name} exited immediately with code {proc.popen.returncode}."
                + (f"\nRecent log output:\n{tail}" if tail else "")
            )
        time.sleep(0.05)


def iter_watched_files() -> list[Path]:
    files: list[Path] = []

    for root in WATCH_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part in IGNORE_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in WATCH_EXTENSIONS:
                files.append(path)

    for path in WATCH_FILES:
        if path.exists() and path.is_file():
            files.append(path)

    # Preserve deterministic ordering and de-duplicate.
    return sorted(set(files))


def snapshot() -> dict[str, int]:
    state: dict[str, int] = {}
    for path in iter_watched_files():
        try:
            state[str(path)] = path.stat().st_mtime_ns
        except OSError:
            continue
    return state


def describe_changes(before: dict[str, int], after: dict[str, int]) -> list[str]:
    changed: list[str] = []

    before_keys = set(before.keys())
    after_keys = set(after.keys())

    for p in sorted(after_keys - before_keys):
        changed.append(f"added: {p}")
    for p in sorted(before_keys - after_keys):
        changed.append(f"removed: {p}")
    for p in sorted(before_keys & after_keys):
        if before[p] != after[p]:
            changed.append(f"modified: {p}")

    return changed


def _kill_process_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def stop_managed(proc: ManagedProc, timeout: float = 10.0) -> None:
    if not proc.popen:
        return

    p = proc.popen
    if p.poll() is None:
        _kill_process_group(p.pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline and p.poll() is None:
            time.sleep(0.1)
        if p.poll() is None:
            _kill_process_group(p.pid, signal.SIGKILL)
            try:
                p.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass

    if proc.log_handle:
        try:
            proc.log_handle.close()
        except Exception:
            pass

    proc.popen = None
    proc.log_handle = None


def start_managed(proc: ManagedProc) -> None:
    if proc.name == "scheduler":
        _stop_existing_scheduler(proc.cmd[0])

    proc.log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(proc.log_path, "a", encoding="utf-8")
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_fh.write(f"\n[{stamp}] starting: {' '.join(proc.cmd)}\n")
    log_fh.flush()

    p = subprocess.Popen(
        proc.cmd,
        cwd=str(BASE_DIR),
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    proc.popen = p
    proc.log_handle = log_fh
    _ensure_process_started(proc)


def _build_processes(py_executable: str) -> list[ManagedProc]:
    orchestration_base_dir = _orchestration_base_dir()
    log_dir = _log_dir()
    return [
        ManagedProc(
            name="scheduler",
            cmd=[py_executable, "-m", "orchestration.cli", "--base-dir", str(orchestration_base_dir), "scheduler", "run"],
            log_path=log_dir / "scheduler.log",
        ),
        ManagedProc(
            name="web",
            cmd=[py_executable, "-m", "workstream_manager", "--port", "8080", "--base-dir", str(orchestration_base_dir)],
            log_path=log_dir / "workstream_manager.log",
        ),
    ]


def restart_all(processes: list[ManagedProc], py_executable: str, reason: str) -> list[ManagedProc]:
    _reload_dotenv()
    fresh_processes = _build_processes(py_executable)
    print(f"\n[dev-servers] restarting services ({reason})", flush=True)
    for proc in processes:
        stop_managed(proc)
    started: list[ManagedProc] = []
    try:
        for proc in fresh_processes:
            start_managed(proc)
            started.append(proc)
            pid = proc.popen.pid if proc.popen else "?"
            print(f"[dev-servers] {proc.name} pid={pid} log={_display_path(proc.log_path)}", flush=True)
    except Exception:
        for proc in started:
            stop_managed(proc)
        raise
    return fresh_processes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run scheduler + web server with auto-restart on file changes.")
    parser.add_argument("--interval", type=float, default=1.0, help="File scan interval in seconds (default: 1.0)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    py = str(BASE_DIR / ".venv" / "bin" / "python")
    if not Path(py).exists():
        print("[dev-servers] error: .venv Python not found. Create/activate .venv first.", file=sys.stderr)
        return 1

    _reload_dotenv()
    processes = _build_processes(py)

    try:
        processes = restart_all(processes, py, "initial start")
    except Exception as exc:
        print(f"[dev-servers] startup failed: {exc}", file=sys.stderr)
        return 1
    prev = snapshot()
    print(f"[dev-servers] watching {len(prev)} files; scan interval={args.interval:.2f}s", flush=True)

    try:
        while True:
            time.sleep(max(0.2, args.interval))
            now = snapshot()
            changes = describe_changes(prev, now)
            if changes:
                preview = "; ".join(changes[:3])
                more = f" (+{len(changes) - 3} more)" if len(changes) > 3 else ""
                try:
                    processes = restart_all(processes, py, f"file changes: {preview}{more}")
                except Exception as exc:
                    print(f"[dev-servers] restart failed: {exc}", file=sys.stderr)
                prev = now
    except KeyboardInterrupt:
        print("\n[dev-servers] stopping...", flush=True)
    finally:
        for proc in processes:
            stop_managed(proc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
