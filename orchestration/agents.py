"""Agent operations."""

import json
import os
import glob
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
import yaml
from .image_validation import validate_task_image_attachments

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Maximum bytes of agent output to store in audit trail
MAX_AUDIT_OUTPUT = 10_000
DEFAULT_LEARNINGS_COMPACTION_THRESHOLD_BYTES = 20_000
LEARNINGS_COMPACTION_THRESHOLD_ENV_VAR = "ORCHESTRATION_LEARNINGS_COMPACTION_THRESHOLD_BYTES"

_ACTIVE_AGENTS_LOCK = threading.Lock()


def _coerce_bool(value, default: bool = True) -> bool:
    """Convert YAML/frontmatter truthy values to a strict bool."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return bool(value)


def _agent_learning_artifact_name(agent_def: dict) -> str:
    """Return a stable learnings artifact filename for an agent."""
    stem = os.path.splitext(os.path.basename(agent_def["file"]))[0]
    return f"{stem}_learnings.md"


def _agent_learning_prompt_section(agent_def: dict, workstream_id: str) -> str:
    """Default learning behavior injected into agent task prompts."""
    learning_path = _agent_learning_artifact_name(agent_def)
    return (
        "=== AGENT LEARNING (DEFAULT) ===\n"
        f"Before you start working, review the learnings artifact at '{learning_path}' in the orchestration system, if it exists.\n"
        f"- Try: python -m orchestration.cli artifact read '{learning_path}' --workstream {workstream_id}\n"
        "- If it does not exist, continue without failing.\n\n"
        f"When you are done working, append actionable learnings that will help you work faster and more efficiently to '{learning_path}' in the orchestration system (create it if it does not exist).\n"
        f"- Read current file first: python -m orchestration.cli artifact read '{learning_path}' --workstream {workstream_id}\n"
        f"- Save updated content: python -m orchestration.cli artifact create --path '{learning_path}' --content '<updated_markdown>' --workstream {workstream_id}\n"
        "- Keep entries concise and practical. Do not include secrets, tokens, passwords, or personal data."
    )


def _learnings_compaction_threshold_bytes() -> int:
    """Resolve learnings compaction threshold from env with safe fallback."""
    raw = os.getenv(LEARNINGS_COMPACTION_THRESHOLD_ENV_VAR)
    if not raw:
        return DEFAULT_LEARNINGS_COMPACTION_THRESHOLD_BYTES
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    return DEFAULT_LEARNINGS_COMPACTION_THRESHOLD_BYTES


def _build_compacted_learnings_content(content: str) -> str:
    """Create a concise, deduplicated learnings document.

    We intentionally keep this deterministic and model-free so compaction is
    fast, reproducible, and safe to run automatically after an agent run.
    """
    items = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- ") or stripped.startswith("* "):
            item = stripped[2:].strip()
        else:
            item = stripped
        if len(item) < 6:
            continue
        items.append(item)

    deduped_recent = []
    seen = set()
    for item in reversed(items):
        key = " ".join(item.lower().split())
        if key in seen:
            continue
        seen.add(key)
        deduped_recent.append(item)

    deduped_recent.reverse()
    # Keep the most recent learnings within a bounded list.
    kept = deduped_recent[-80:]

    compacted_lines = [
        "# Agent Learnings (Compacted)",
        "",
        f"Compacted at {datetime.now(timezone.utc).isoformat()}.",
        "",
        "## Key Learnings",
    ]
    compacted_lines.extend([f"- {item}" for item in kept])
    return "\n".join(compacted_lines).rstrip() + "\n"


def _compact_learnings_artifact_if_needed(agent_def: dict, workstream_id: str, base_dir: str) -> dict:
    """Compact an agent learnings artifact when it exceeds the threshold.

    Returns a small diagnostics dict. Failures are returned as data so callers can
    treat this as best-effort and avoid impacting agent run outcomes.
    """
    if not workstream_id or not agent_def.get("learning_enabled", True):
        return {"checked": False, "reason": "disabled_or_no_workstream"}

    from .artifacts import create_artifact, read_artifact

    learning_path = _agent_learning_artifact_name(agent_def)
    try:
        content = read_artifact(learning_path, base_dir=base_dir, workstream_id=workstream_id)
    except FileNotFoundError:
        return {"checked": True, "compacted": False, "reason": "missing"}
    except Exception as exc:
        return {"checked": True, "compacted": False, "reason": f"read_error: {exc}"}

    current_size = len(content.encode("utf-8"))
    threshold = _learnings_compaction_threshold_bytes()
    if current_size <= threshold:
        return {
            "checked": True,
            "compacted": False,
            "reason": "below_threshold",
            "size": current_size,
            "threshold": threshold,
        }

    compacted = _build_compacted_learnings_content(content)
    archive_path = (
        f"{os.path.splitext(learning_path)[0]}"
        f"_archive_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.md"
    )
    try:
        create_artifact(archive_path, content, base_dir=base_dir, workstream_id=workstream_id)
        create_artifact(learning_path, compacted, base_dir=base_dir, workstream_id=workstream_id)
    except Exception as exc:
        return {"checked": True, "compacted": False, "reason": f"write_error: {exc}"}

    return {
        "checked": True,
        "compacted": True,
        "size": current_size,
        "threshold": threshold,
        "archive_path": archive_path,
    }


def _is_pid_alive(pid) -> bool:
    """Best-effort process existence check for active run bookkeeping."""
    if pid is None:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _finalize_expired_active_run(base_dir: str, run: dict) -> None:
    """Kill an orphaned active run and mark its metadata as timed out."""
    run_id = run.get("run_id")
    if not run_id:
        return

    candidate_pids = set()
    for pid in _find_run_pids(run_id):
        try:
            candidate_pids.add(int(pid))
        except (TypeError, ValueError):
            continue

    direct_pid = run.get("pid")
    try:
        if direct_pid is not None:
            candidate_pids.add(int(direct_pid))
    except (TypeError, ValueError):
        pass

    for pid in sorted(candidate_pids):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    ended_at = datetime.now(timezone.utc).isoformat()
    meta_path = _agent_run_meta_path(base_dir, run_id)
    if os.path.exists(meta_path):
        meta = _read_run_meta(meta_path)
    else:
        meta = {
            "run_id": run_id,
            "agent": run.get("agent"),
            "agent_ref": run.get("agent_ref"),
            "workstream_id": run.get("workstream_id"),
            "workstream_path": _workstream_path(base_dir, run.get("workstream_id")),
            "task_ids": list(run.get("task_ids") or []),
            "tasks": [],
            "prompt": "",
            "system_prompt": "",
            "command_line": "",
            "log_path": run.get("log_path"),
            "started_at": run.get("started_at") or ended_at,
            "ended_at": None,
            "status": "running",
            "exit_code": None,
            "retried_from_run_id": None,
            "retried_to_run_ids": [],
        }

    if meta.get("status") not in ("completed", "failed", "timeout", "killed"):
        meta["status"] = "timeout"
        meta["exit_code"] = -9
        meta["ended_at"] = ended_at
        _write_run_meta(base_dir, run_id, meta)

    from .locks import release_process_lock
    try:
        release_process_lock(run_id, base_dir=base_dir)
    except Exception:
        pass


def _prune_dead_active_runs(base_dir: str, runs: list = None) -> list:
    """Drop stale active-agent entries whose PIDs are no longer alive or whose process lock has expired."""
    if runs is None:
        runs = _read_active_agents(base_dir)

    from .locks import process_lock_status

    live_runs = []
    changed = False
    for run in runs:
        pid = run.get("pid")
        run_id = run.get("run_id")

        # If PID is dead, prune immediately.
        if pid is not None and not _is_pid_alive(pid):
            changed = True
            continue

        # If there's a process lock for this run and it has expired, prune too.
        # This catches agents that are alive but have run past their allowed TTL.
        if run_id is not None:
            lock = process_lock_status(run_id, base_dir)
            # lock is None means either no lock exists (pre-feature runs) or it expired.
            # Only prune if a lock *file* exists and has expired — not if it was never created.
            from .locks import _process_lock_path
            lock_path = _process_lock_path(run_id, base_dir)
            if os.path.exists(lock_path) and lock is None:
                # Lock file exists but is expired — this run has exceeded its TTL.
                _finalize_expired_active_run(base_dir, run)
                changed = True
                continue

        # Runs without a pid are kept for backward compatibility.
        live_runs.append(run)

    if changed:
        _write_active_agents(base_dir, live_runs)
    return live_runs


def _compact_json(value) -> str:
    """Compact JSON for safe env var transport."""
    return json.dumps(value, separators=(",", ":"))


def _state_dir(base_dir: str) -> str:
    return os.path.join(base_dir, ".orchestration")


def _active_agents_path(base_dir: str) -> str:
    return os.path.join(_state_dir(base_dir), "active_agents.yaml")


def _agent_runs_dir(base_dir: str) -> str:
    return os.path.join(_state_dir(base_dir), "agent_runs")


def _agent_run_meta_path(base_dir: str, run_id: str) -> str:
    return os.path.join(_agent_runs_dir(base_dir), f"{run_id}.json")


def _ensure_state_dirs(base_dir: str) -> None:
    os.makedirs(_state_dir(base_dir), exist_ok=True)
    os.makedirs(_agent_runs_dir(base_dir), exist_ok=True)


def _write_run_meta(base_dir: str, run_id: str, data: dict) -> None:
    _ensure_state_dirs(base_dir)
    path = _agent_run_meta_path(base_dir, run_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _read_run_meta(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _read_run_meta_by_id(base_dir: str, run_id: str) -> dict:
    path = _agent_run_meta_path(base_dir, run_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Agent run '{run_id}' not found")
    return _read_run_meta(path)


def _append_retry_child(base_dir: str, run_id: str, child_run_id: str) -> None:
    """Record retry lineage on the original run metadata."""
    meta = _read_run_meta_by_id(base_dir, run_id)
    existing = list(meta.get("retried_to_run_ids") or [])
    if child_run_id not in existing:
        existing.append(child_run_id)
        meta["retried_to_run_ids"] = existing
        _write_run_meta(base_dir, run_id, meta)


def _workstream_path(base_dir: str, ws_id: str) -> str:
    if not ws_id:
        return ""
    from .workstreams import list_workstreams

    by_id = {ws.id: ws for ws in list_workstreams(base_dir=base_dir)}
    ws = by_id.get(ws_id)
    if ws is None:
        return ws_id

    names = []
    current = ws
    visited = set()
    while current and current.id not in visited:
        visited.add(current.id)
        names.append(current.name)
        if not current.parent_id:
            break
        current = by_id.get(current.parent_id)
    return " / ".join(reversed(names))


def _read_active_agents(base_dir: str) -> list:
    path = _active_agents_path(base_dir)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        raw = yaml.safe_load(f)

    # Canonical format: {"runs": [...]}
    if isinstance(raw, dict):
        runs = raw.get("runs")
        if isinstance(runs, list):
            return runs
        return []

    # Backward/accidental format support: bare list at file root.
    if isinstance(raw, list):
        return raw

    return []


def _write_active_agents(base_dir: str, runs: list) -> None:
    _ensure_state_dirs(base_dir)
    path = _active_agents_path(base_dir)
    payload = {"runs": runs}
    with open(path, "w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)


def _register_active_agent(base_dir: str, run: dict) -> None:
    with _ACTIVE_AGENTS_LOCK:
        runs = _read_active_agents(base_dir)
        runs = [r for r in runs if r.get("run_id") != run.get("run_id")]
        runs.append(run)
        _write_active_agents(base_dir, runs)


def _unregister_active_agent(base_dir: str, run_id: str) -> None:
    with _ACTIVE_AGENTS_LOCK:
        runs = _read_active_agents(base_dir)
        runs = [r for r in runs if r.get("run_id") != run_id]
        _write_active_agents(base_dir, runs)


def list_active_agents(base_dir: str = ".") -> list:
    with _ACTIVE_AGENTS_LOCK:
        runs = _prune_dead_active_runs(base_dir)
    return sorted(runs, key=lambda r: r.get("started_at", ""), reverse=True)


def _agent_identity_keys(agent_ref: str, base_dir: str = ".") -> set:
    """Return case-insensitive identity keys for an agent reference."""
    raw = str(agent_ref or "").strip()
    if not raw:
        return set()

    keys = set()
    keys.add(raw.lower())

    base_name = os.path.basename(raw)
    keys.add(base_name.lower())
    if base_name.lower().endswith(".md"):
        keys.add(base_name[:-3].lower())

    try:
        resolved = _resolve_agent_file(raw, base_dir)
        resolved_name = os.path.basename(resolved)
        keys.add(resolved_name.lower())
        if resolved_name.lower().endswith(".md"):
            keys.add(resolved_name[:-3].lower())
        agent_def = _parse_agent_md(resolved)
        friendly = str(agent_def.get("name") or "").strip()
        if friendly:
            keys.add(friendly.lower())
    except Exception:
        pass

    return {k for k in keys if k}


def count_active_agent_runs(workstream_id: str, agent_ref: str, base_dir: str = ".") -> int:
    """Count active runs for a specific agent within a workstream."""
    if not workstream_id or not agent_ref:
        return 0

    target_keys = _agent_identity_keys(agent_ref, base_dir)
    if not target_keys:
        return 0

    with _ACTIVE_AGENTS_LOCK:
        runs = _prune_dead_active_runs(base_dir)

    count = 0
    for run in runs:
        if str(run.get("workstream_id") or "") != str(workstream_id):
            continue

        run_keys = set()
        for candidate in (run.get("agent"), run.get("agent_ref")):
            text = str(candidate or "").strip()
            if not text:
                continue
            run_keys.add(text.lower())
            bname = os.path.basename(text)
            run_keys.add(bname.lower())
            if bname.lower().endswith(".md"):
                run_keys.add(bname[:-3].lower())

        if run_keys & target_keys:
            count += 1

    return count


def list_agent_runs(limit: int = 100, base_dir: str = ".") -> list:
    """Return active + recent completed runs, sorted by start time desc."""
    if limit <= 0:
        raise ValueError("limit must be > 0")

    _ensure_state_dirs(base_dir)
    meta_paths = sorted(glob.glob(os.path.join(_agent_runs_dir(base_dir), "*.json")))
    runs = []
    for path in meta_paths:
        try:
            meta = _read_run_meta(path)
            runs.append(meta)
        except Exception:
            continue

    with _ACTIVE_AGENTS_LOCK:
        active_runs = _prune_dead_active_runs(base_dir)
        active = {r.get("run_id"): r for r in active_runs}

    from .tasks import read_task

    # Merge active runtime fields into persisted metadata.
    seen_ids = {r.get("run_id") for r in runs}
    for run in runs:
        rid = run.get("run_id")
        live = active.get(rid)
        if live:
            run["status"] = "running"
            run["pid"] = live.get("pid")
            run["log_path"] = live.get("log_path", run.get("log_path"))
        elif run.get("status") == "running":
            # Metadata can be left behind after crashes/restarts. If a run is not
            # in live active state anymore, report it as stale instead of running.
            run["status"] = "stale"

    # Include active runs even if metadata file does not exist yet.
    for rid, live in active.items():
        if rid in seen_ids:
            continue
        live_task_ids = list(live.get("task_ids") or [])
        live_tasks = []
        for tid in live_task_ids:
            try:
                t = read_task(tid, base_dir=base_dir)
                live_tasks.append({"id": t.id, "title": t.title})
            except Exception:
                continue
        runs.append({
            "run_id": rid,
            "agent": live.get("agent"),
            "agent_ref": live.get("agent_ref"),
            "workstream_id": live.get("workstream_id"),
            "workstream_path": _workstream_path(base_dir, live.get("workstream_id")),
            "task_ids": live_task_ids,
            "tasks": live_tasks,
            "prompt": "",
            "system_prompt": "",
            "command_line": "",
            "log_path": live.get("log_path"),
            "started_at": live.get("started_at"),
            "ended_at": None,
            "status": "running",
            "exit_code": None,
            "pid": live.get("pid"),
            "retried_from_run_id": None,
            "retried_to_run_ids": [],
        })

    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs[:limit]


def get_agent_run(run_id: str, base_dir: str = ".") -> dict:
    """Return full details for a single run including output and CLI calls."""
    try:
        run = _read_run_meta_by_id(base_dir, run_id)
    except FileNotFoundError:
        run = {
            "run_id": run_id,
            "agent": None,
            "agent_ref": None,
            "workstream_id": None,
            "workstream_path": "",
            "task_ids": [],
            "tasks": [],
            "prompt": "",
            "system_prompt": "",
            "command_line": "",
            "log_path": None,
            "started_at": None,
            "ended_at": None,
            "status": "running",
            "exit_code": None,
            "retried_from_run_id": None,
            "retried_to_run_ids": [],
        }

    with _ACTIVE_AGENTS_LOCK:
        active = _prune_dead_active_runs(base_dir)
    live = next((r for r in active if r.get("run_id") == run_id), None)
    if live:
        run["status"] = "running"
        run["agent"] = run.get("agent") or live.get("agent")
        run["agent_ref"] = run.get("agent_ref") or live.get("agent_ref")
        run["workstream_id"] = run.get("workstream_id") or live.get("workstream_id")
        run["workstream_path"] = run.get("workstream_path") or _workstream_path(base_dir, run.get("workstream_id"))
        run["task_ids"] = run.get("task_ids") or list(live.get("task_ids") or [])
        run["started_at"] = run.get("started_at") or live.get("started_at")
        run["pid"] = live.get("pid")
        run["log_path"] = live.get("log_path", run.get("log_path"))
        if not run.get("tasks") and run.get("task_ids"):
            from .tasks import read_task
            task_titles = []
            for tid in run.get("task_ids"):
                try:
                    t = read_task(tid, base_dir=base_dir)
                    task_titles.append({"id": t.id, "title": t.title})
                except Exception:
                    continue
            run["tasks"] = task_titles
    elif run.get("status") == "running":
        # Keep detail view consistent with list view for orphaned runs whose
        # metadata was left in a running state after interruption/restart.
        run["status"] = "stale"
    elif not run.get("started_at"):
        raise FileNotFoundError(f"Agent run '{run_id}' not found")

    log_path = run.get("log_path")
    output = ""
    if log_path:
        abs_log_path = log_path if os.path.isabs(log_path) else os.path.join(base_dir, log_path)
        if os.path.exists(abs_log_path):
            with open(abs_log_path, "r", encoding="utf-8", errors="replace") as f:
                output = f.read()

    from .workspace_audit import get_audit_log
    cli_calls = get_audit_log(base_dir=base_dir, limit=2000, event_type="orchestration_cli_call")
    cli_calls = [e for e in cli_calls if e.get("run_id") == run_id]
    cli_calls = sorted(cli_calls, key=lambda e: e.get("timestamp", ""))

    retry_info = _get_run_retry_info(run, base_dir)
    interruption_reason = _get_run_interruption_reason(run, base_dir)

    return {
        "run": run,
        "output": output,
        "cli_calls": cli_calls,
        "retry": retry_info,
        "interruption_reason": interruption_reason,
    }


def _parse_iso_timestamp(value: str):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _get_run_interruption_reason(run: dict, base_dir: str) -> str | None:
    """Infer a user-facing reason for stale/orphaned runs when possible."""
    if str(run.get("status") or "").lower() != "stale":
        return None

    started_at = _parse_iso_timestamp(run.get("started_at"))
    log_path = run.get("log_path")
    log_empty = False
    if log_path:
        abs_log_path = log_path if os.path.isabs(log_path) else os.path.join(base_dir, log_path)
        if os.path.exists(abs_log_path):
            try:
                log_empty = os.path.getsize(abs_log_path) == 0
            except OSError:
                log_empty = False

    task_ids = list(run.get("task_ids") or [])
    if len(task_ids) == 1:
        from .tasks import read_task

        try:
            task = read_task(task_ids[0], base_dir=base_dir)
        except Exception:
            task = None

        if task is not None:
            relevant_audit = []
            for entry in task.audit:
                entry_ts = _parse_iso_timestamp(getattr(entry, "timestamp", None))
                if started_at and entry_ts and entry_ts < started_at:
                    continue
                relevant_audit.append(entry)

            for entry in reversed(relevant_audit):
                if entry.type == "lock_expired":
                    return "This run appears to have been interrupted and later had its task lock expire. Retry scheduling likely happened via expired-lock cleanup."
                if entry.type == "process_killed":
                    return "This run appears to have been interrupted and its process was later killed during lock cleanup."
                if entry.type == "retry_scheduled":
                    return "This run appears to have been interrupted. The linked task was automatically scheduled for retry by expired-lock cleanup."
                if entry.type == "max_retries_exceeded":
                    return "This run appears to have been interrupted, and the linked task later exhausted its automatic retries."

            if any(entry.type == "agent_started" for entry in relevant_audit):
                if log_empty:
                    return "The agent started, but no output was ever written and no completion or failure audit was recorded. The process likely exited or the app restarted before cleanup finished."
                return "The agent started, but no completion or failure audit was recorded. The process likely exited or was interrupted before cleanup finished."

    if log_empty:
        return "No output was captured for this stale run. The process likely exited or was interrupted before it wrote anything to its log."

    return "This run was left in a running state, but no live process still exists for it. It was likely interrupted by a restart or unexpected process exit."


def _get_run_retry_info(run: dict, base_dir: str) -> dict:
    """Return retry diagnostics for an agent run detail payload.

    Notes:
    - Automatic retry logic is currently task-centric and triggered from
      expired lock cleanup in orchestration.retry.
    - Standalone runs (no task_ids) therefore have no retry schedule.
    """
    task_ids = list(run.get("task_ids") or [])
    workstream_id = run.get("workstream_id")

    if not task_ids:
        return {
            "eligible": False,
            "reason": "Standalone run (no task binding)",
            "retry_count": None,
            "max_retries": None,
            "retries_remaining": None,
            "next_retry_at": None,
            "last_failure_at": None,
            "policy": "Automatic retries currently apply to task-bound lock-expiry failures.",
        }

    if len(task_ids) != 1:
        return {
            "eligible": False,
            "reason": f"Multi-task run ({len(task_ids)} tasks)",
            "retry_count": None,
            "max_retries": None,
            "retries_remaining": None,
            "next_retry_at": None,
            "last_failure_at": None,
            "policy": "Retry diagnostics are currently shown for single task runs.",
        }

    from .tasks import read_task
    from .workstreams import read_workstream, resolve_workstream_workspace
    from .retry import DEFAULT_RETRY_CONFIG

    task_id = task_ids[0]
    try:
        task = read_task(task_id, base_dir=base_dir)
    except Exception as e:
        return {
            "eligible": False,
            "reason": f"Task unavailable: {e}",
            "task_id": task_id,
            "retry_count": None,
            "max_retries": None,
            "retries_remaining": None,
            "next_retry_at": None,
            "last_failure_at": None,
            "policy": "Retry diagnostics require readable task state.",
        }

    ws = None
    try:
        ws = read_workstream(workstream_id or task.workstream_id, base_dir=base_dir)
    except Exception:
        ws = None

    retry_cfg = task.retry or (ws.retry if ws and ws.retry else DEFAULT_RETRY_CONFIG)
    retry_count = int(getattr(task, "retry_count", 0) or 0)
    max_retries = int(getattr(retry_cfg, "max_retries", 3) or 3)
    retries_remaining = max(0, max_retries - retry_count)

    # A retry is considered scheduled when a run_agent scheduled_action is present.
    scheduled_action = getattr(task, "scheduled_action", None) or {}
    next_retry_at = None
    if task.scheduled_at and isinstance(scheduled_action, dict):
        if scheduled_action.get("type") == "run_agent":
            next_retry_at = task.scheduled_at

    return {
        "eligible": True,
        "task_id": task.id,
        "task_status": task.status,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "retries_remaining": retries_remaining,
        "backoff": getattr(retry_cfg, "backoff", "exponential"),
        "base_seconds": getattr(retry_cfg, "base_seconds", 60),
        "next_retry_at": next_retry_at,
        "last_failure_at": getattr(task, "last_failure_at", None),
        "policy": "Automatic retries are scheduled by expired-lock cleanup.",
    }


def retry_agent_run(run_id: str, base_dir: str = ".", allow_paused_workstream: bool = False, background: bool = False) -> dict:
    """Manually retry a recorded agent run using its stored execution context.

    When background=True, the agent is dispatched on a daemon thread and the
    pre-generated run_id is returned immediately.  Synchronous validation
    (paused workstream, missing agent) still happens on the calling thread so
    errors are surfaced before returning.
    """
    run = _read_run_meta_by_id(base_dir, run_id)

    agent_ref = str(run.get("agent_ref") or run.get("agent") or "").strip()
    if not agent_ref:
        raise ValueError(f"Agent run '{run_id}' does not record an agent reference")

    task_ids = list(run.get("task_ids") or [])
    workstream_id = run.get("workstream_id") or None

    # --- Synchronous preflight so callers get errors immediately ---
    # Only the paused-workstream check is done here because it drives an
    # interactive "retry anyway?" dialog in the UI. Other errors (missing
    # agent file, invalid images) surface through the background thread.
    if workstream_id:
        from .workstreams import read_workstream
        ws = read_workstream(workstream_id, base_dir)
        if ws.paused and not allow_paused_workstream:
            raise RuntimeError(f"Workstream '{ws.name}' is paused")

    new_run_id = str(uuid.uuid4())

    if background:
        import threading

        def _run():
            try:
                result = run_agent(
                    agent_ref,
                    task_ids=task_ids,
                    workstream_id=workstream_id,
                    base_dir=base_dir,
                    allow_paused_workstream=allow_paused_workstream,
                    retried_from_run_id=run_id,
                    _run_id=new_run_id,
                )
                _append_retry_child(base_dir, run_id, result["run_id"])
            except Exception as e:
                import sys
                print(f"[retry_agent_run] background error for run {new_run_id}: {e}", file=sys.stderr)

        t = threading.Thread(target=_run, daemon=True, name=f"retry-{new_run_id[:8]}")
        t.start()
        return {"run_id": new_run_id, "agent": agent_ref, "workstream_id": workstream_id, "task_ids": task_ids, "retried_from_run_id": run_id}

    result = run_agent(
        agent_ref,
        task_ids=task_ids,
        workstream_id=workstream_id,
        base_dir=base_dir,
        allow_paused_workstream=allow_paused_workstream,
        retried_from_run_id=run_id,
    )
    _append_retry_child(base_dir, run_id, result["run_id"])
    result["retried_from_run_id"] = run_id
    return result


def tail_active_agent(run_id: str, lines: int = 200, base_dir: str = ".") -> dict:
    if lines <= 0:
        raise ValueError("lines must be > 0")

    with _ACTIVE_AGENTS_LOCK:
        runs = _read_active_agents(base_dir)
    run = next((r for r in runs if r.get("run_id") == run_id), None)
    if run is None:
        raise FileNotFoundError(f"Active agent run '{run_id}' not found")

    log_path = run.get("log_path")
    if not log_path:
        raise FileNotFoundError(f"No log path available for run '{run_id}'")

    abs_log_path = log_path if os.path.isabs(log_path) else os.path.join(base_dir, log_path)
    if not os.path.exists(abs_log_path):
        raise FileNotFoundError(f"Log file not found for run '{run_id}'")

    with open(abs_log_path, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()

    tail_text = "".join(all_lines[-lines:])
    return {
        "run": run,
        "tail": tail_text,
        "line_count": min(lines, len(all_lines)),
    }


def _find_run_pids(run_id: str) -> list:
    """Find live process IDs for an agent run via env marker."""
    marker = f"ORCHESTRATION_AGENT_RUN_ID={run_id}"
    try:
        output = subprocess.check_output(
            ["ps", "eww", "-axo", "pid=,command="],
            text=True,
            errors="replace",
        )
    except Exception:
        return []

    pids = []
    for line in output.splitlines():
        line = line.strip()
        if not line or marker not in line:
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        try:
            pids.append(int(parts[0]))
        except ValueError:
            continue
    return sorted(set(pids))


def kill_agent_run(run_id: str, base_dir: str = ".", grace_seconds: float = 1.0) -> dict:
    """Terminate a running agent run and mark it as killed.

    If metadata already indicates a terminal status, preserve it and return
    without overwriting failure reason.
    """
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be >= 0")

    with _ACTIVE_AGENTS_LOCK:
        active_runs = _read_active_agents(base_dir)
        active = next((r for r in active_runs if r.get("run_id") == run_id), None)

    meta = None
    meta_path = _agent_run_meta_path(base_dir, run_id)
    if os.path.exists(meta_path):
        meta = _read_run_meta(meta_path)

    if active is None and meta is None:
        raise FileNotFoundError(f"Agent run '{run_id}' not found")

    previous_status = (meta or {}).get("status") or ("running" if active else None)
    if previous_status in ("completed", "failed", "timeout", "killed"):
        return {
            "run_id": run_id,
            "killed": False,
            "message": f"Run already finished with status '{previous_status}'",
            "previous_status": previous_status,
            "status": previous_status,
            "pids": [],
        }

    pids = _find_run_pids(run_id)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass

    if pids and grace_seconds > 0:
        time.sleep(grace_seconds)

    for pid in pids:
        try:
            os.kill(pid, 0)
        except OSError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass

    ended_at = datetime.now(timezone.utc).isoformat()
    if meta is None:
        meta = {
            "run_id": run_id,
            "agent": active.get("agent") if active else None,
            "agent_ref": active.get("agent_ref") if active else None,
            "workstream_id": active.get("workstream_id") if active else None,
            "workstream_path": _workstream_path(base_dir, active.get("workstream_id") if active else None),
            "task_ids": list(active.get("task_ids") or []) if active else [],
            "tasks": [],
            "prompt": "",
            "system_prompt": "",
            "log_path": active.get("log_path") if active else None,
            "started_at": active.get("started_at") if active else ended_at,
        }

    # Preserve non-running terminal state if one appeared mid-flight.
    if meta.get("status") in ("completed", "failed", "timeout", "killed"):
        final_status = meta.get("status")
    else:
        meta["status"] = "killed"
        meta["exit_code"] = -15
        meta["ended_at"] = ended_at
        final_status = "killed"
        _write_run_meta(base_dir, run_id, meta)

    _unregister_active_agent(base_dir, run_id)

    return {
        "run_id": run_id,
        "killed": final_status == "killed",
        "previous_status": previous_status,
        "status": final_status,
        "pids": pids,
    }


def _normalize_model_name(raw: str) -> str:
    """Normalize model names accepted by claude CLI.

    Supports legacy values prefixed with 'anthropic/'.
    """
    model = str(raw or "").strip()
    if model.startswith("anthropic/"):
        model = model[len("anthropic/"):]
    return model


def _get_model() -> str:
    """Return the default model name for Claude Code CLI.

    Uses DEFAULT_LLM and falls back to 'sonnet'.
    """
    return _normalize_model_name(os.getenv("DEFAULT_LLM", "sonnet"))


def _model_from_level(level: str) -> str:
    """Map x-model-level to env-configured model aliases."""
    level_norm = str(level or "").strip().lower()
    env_key_by_level = {
        "high": "HIGH_LLM",
        "medium": "MEDIUM_LLM",
        "low": "LOW_LLM",
    }
    env_key = env_key_by_level.get(level_norm)
    if not env_key:
        raise ValueError(f"Invalid x-model-level '{level}'. Expected one of: high, medium, low")

    env_value = os.getenv(env_key)
    if not env_value:
        raise ValueError(f"x-model-level '{level_norm}' requires env var {env_key} to be set")

    model = _normalize_model_name(env_value)
    if not model:
        raise ValueError(f"Env var {env_key} is empty")
    return model


def _resolve_agent_model(agent_def: dict) -> str:
    """Resolve model with precedence: x-model > x-model-level > DEFAULT_LLM."""
    explicit_model = _normalize_model_name(agent_def.get("model"))
    if explicit_model:
        return explicit_model

    level = agent_def.get("model_level")
    if str(level or "").strip():
        return _model_from_level(level)

    return _get_model()


def _resolve_agent_effort(agent_def: dict):
    """Return validated effort value or None when not set."""
    raw = agent_def.get("effort")
    effort = str(raw or "").strip().lower()
    if not effort:
        return None

    allowed = {"low", "medium", "high", "xhigh", "max"}
    if effort not in allowed:
        raise ValueError(
            f"Invalid x-effort '{raw}'. Expected one of: low, medium, high, xhigh, max"
        )
    return effort


def _agents_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "Agents")


def _cli_dir(base_dir: str) -> str:
    return os.path.join(_agents_dir(base_dir), "cli")


def _resolve_agent_file(agent_ref: str, base_dir: str) -> str:
    """Resolve an agent reference to an absolute file path.

    Accepts:
      - bare name:        "sorter"
      - filename:         "sorter.md"
      - relative path:    "Agents/sorter.md"
            - header name:      "Sorter" (from YAML frontmatter `name:`)
    """
    agents_dir = _agents_dir(base_dir)

    # If it looks like a path (has separator or starts with Agents/)
    if os.sep in agent_ref or agent_ref.startswith("Agents/"):
        candidate = os.path.join(base_dir, agent_ref)
        if os.path.exists(candidate):
            return candidate

    # Strip .md if present for bare-name lookup
    bare = agent_ref
    if bare.endswith(".md"):
        bare = bare[:-3]
    # Strip leading Agents/ or Agents\ prefix
    for prefix in ("Agents/", "Agents\\"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]

    candidate = os.path.join(agents_dir, f"{bare}.md")
    if os.path.exists(candidate):
        return candidate

    # Normalize spaces to underscores (agent names use spaces, filenames use underscores)
    bare_underscore = bare.replace(" ", "_")
    candidate = os.path.join(agents_dir, f"{bare_underscore}.md")
    if os.path.exists(candidate):
        return candidate

    # Case-insensitive fallback (try both space and underscore variants)
    if os.path.exists(agents_dir):
        for fname in os.listdir(agents_dir):
            if fname.lower() == f"{bare.lower()}.md" or fname.lower() == f"{bare_underscore.lower()}.md":
                return os.path.join(agents_dir, fname)

        # Fall back to matching the human-readable name declared in frontmatter.
        # This keeps triggers stable even when filenames use a different slug.
        for fname in sorted(os.listdir(agents_dir)):
            if not fname.endswith(".md"):
                continue
            path = os.path.join(agents_dir, fname)
            try:
                agent = _parse_agent_md(path)
            except Exception:
                continue
            agent_name = str(agent.get("name", "") or "").strip()
            if agent_name and agent_name.lower() == agent_ref.strip().lower():
                return path

    raise FileNotFoundError(f"Agent '{agent_ref}' not found in {agents_dir}")


def _parse_agent_md(path: str) -> dict:
    """Parse an agent .md file, extracting YAML header and body."""
    with open(path) as f:
        content = f.read()

    header = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            header = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()

    return {
        "name": header.get("name", os.path.splitext(os.path.basename(path))[0]),
        "description": header.get("description", ""),
        "agent_type": header.get("x-agent-type", "worker"),
        "tools": header.get("x-tools", []),
        "learning_enabled": _coerce_bool(header.get("x-learning", True), default=True),
        "timeout": header.get("x-timeout"),
        "model": header.get("x-model"),
        "model_level": header.get("x-model-level"),
        "effort": header.get("x-effort"),
        "file": path,
        "body": body,
    }


def list_agents(base_dir: str = ".") -> list:
    agents_dir = _agents_dir(base_dir)
    if not os.path.exists(agents_dir):
        return []
    result = []
    for path in sorted(glob.glob(os.path.join(agents_dir, "*.md"))):
        try:
            agent = _parse_agent_md(path)
            result.append({
                "name": agent["name"],
                "description": agent["description"],
                "agent_type": agent["agent_type"],
                "file": os.path.relpath(path, base_dir),
            })
        except Exception:
            continue
    return result


# ── Dynamic CLI tool discovery ───────────────────────────────────────

def discover_cli_tools(base_dir: str = ".") -> dict:
    """Discover available CLI tools from Agents/cli/.

    Returns a dict mapping tool name to {"py": path, "md": path, "description": str}.
    Only includes tools that have both a .py and a matching .md file.
    """
    cli = _cli_dir(base_dir)
    if not os.path.isdir(cli):
        return {}

    tools = {}
    for py_path in sorted(glob.glob(os.path.join(cli, "*.py"))):
        name = os.path.splitext(os.path.basename(py_path))[0]
        md_path = os.path.join(cli, f"{name}.md")
        if os.path.exists(md_path):
            with open(md_path) as f:
                description = f.read()
            tools[name] = {
                "py": py_path,
                "md": md_path,
                "description": description,
            }
    return tools


def _build_system_prompt(agent_def: dict, base_dir: str) -> str:
    """Build the system prompt from the agent body and available tools."""
    parts = [agent_def["body"]]

    # List available CLI tools so the agent knows what it can run via bash
    requested = agent_def.get("tools", [])
    if requested:
        available = discover_cli_tools(base_dir)
        tool_docs = []
        for tool_name in requested:
            if tool_name in available:
                tool_docs.append(f"### {tool_name}\n```\npython3 {available[tool_name]['py']} <args>\n```\n{available[tool_name]['description']}")
        if tool_docs:
            parts.append("\n\n## Available CLI Tools\n" + "\n\n".join(tool_docs))

    # Always document the orchestration CLI
    parts.append(
        "\n\n## Orchestration CLI\n"
        "Use `python -m orchestration.cli <command>` to manage tasks, workstreams, artifacts, etc.\n"
        "Key commands:\n"
        "- `workstream find --query '<name>'` — find a workstream by name\n"
        "- `workstream read <workstream_id>` — read workstream details\n"
        "- `workstream context <workstream_id>` — read the current workstream operating context\n"
        "- `workstream update-context <workstream_id> --content '<text>'` — update the workstream operating context\n"
        "- `workstream descendants <workstream_id>` — list all descendant workstreams as JSON\n"
        "- `task create <workstream_id> --title '<title>' --description '<desc>'` — create a task\n"
        "- `task update <task_id> --status <new_status>` — transition a task\n"
        "- `task comment <task_id> --message '<msg>' --author '<agent_name>'` — add a comment with author\n"
        "- `task attach <task_id> --path '<artifact_path>'` — attach an artifact to a task (REQUIRED after creating any artifact)\n"
        "- `task detach <task_id> --path '<artifact_path>'` — remove an artifact attachment from a task\n"
        "- `task list <workstream_id>` — list tasks in a workstream\n"
        "- `artifact create --path '<path>' --content '<content>' --workstream '<workstream_id>'` — save an artifact\n"
        "- `artifact read '<path>' --workstream '<workstream_id>'` — read an artifact\n"
        "- `artifact list --workstream '<workstream_id>'` — list artifacts in the workstream root\n"
        "- `artifact list --prefix '<prefix>' --workstream '<workstream_id>'` — list artifacts under a path\n"
        "  (When running under orchestration, `--workstream` is auto-inferred from run context.)\n"
        "\nFull CLI reference: Agents/cli/orchestration_cli.md\n"
    )

    return "\n".join(parts)


def _read_task_attachments_for_prompt(task, base_dir: str, max_chars_per_attachment: int = 12000) -> str:
    """Return a prompt section with attachment paths and their artifact contents.

    Attachments are artifact paths stored on the task. Their contents are injected
    directly into the agent prompt so task context does not depend on separate reads.
    """
    attachments = list(getattr(task, "attachments", []) or [])
    if not attachments:
        return ""

    from .artifacts import read_artifact

    lines = ["Task attachments (artifact paths + inlined content):"]
    for path in attachments:
        lines.append(f"- Path: {path}")
        try:
            content = read_artifact(path, base_dir=base_dir, workstream_id=task.workstream_id)
            clipped = content[:max_chars_per_attachment]
            if len(content) > max_chars_per_attachment:
                clipped += f"\n... (truncated, {len(content)} total chars)"
            lines.append("  Content:")
            lines.append("```")
            lines.append(clipped)
            lines.append("```")
        except Exception as exc:
            lines.append(f"  Content unavailable: {exc}")
    return "\n".join(lines)


def _workstream_context_prompt_section(ws) -> str:
    context = str(getattr(ws, "context", "") or "").strip()
    if not context:
        return ""
    return "Workstream Operating Context:\n" + context


def _should_inline_attachments(ws) -> bool:
    return bool(getattr(ws, "inline_attachments", False))


# Default agent execution timeout in seconds (30 minutes)
DEFAULT_AGENT_TIMEOUT = 1800


def _classify_run_outcome(returncode: int, timeout_expired: bool) -> str:
    """Map subprocess result to persisted run status."""
    if returncode == 0:
        return "completed"
    if timeout_expired:
        return "timeout"

    # Treat termination signals as killed so UI/operator intent is preserved.
    if returncode in (-signal.SIGTERM, 128 + signal.SIGTERM, -signal.SIGKILL, 128 + signal.SIGKILL):
        return "killed"

    return "failed"


def run_agent(agent_name: str, task_ids: list = None, workstream_id: str = None, prompt: str = None, timeout: int = None, base_dir: str = ".", allow_paused_workstream: bool = False, retried_from_run_id: str = None, _run_id: str = None) -> dict:
    """Run an agent via Claude Code CLI against 0-N tasks.

    Callers are responsible for locking/unlocking tasks. This function
    does not acquire or release locks.

    Task IDs are written to a temporary JSON file and passed to the agent
    via the prompt so the agent knows which tasks to work on.

    Args:
        agent_name: Agent reference (bare name, filename, or path).
        task_ids: List of task IDs to process. May be None or empty.
        workstream_id: Workstream context. Inferred from first task if not provided.
        prompt: Optional custom prompt from trigger, appended to the task prompt.
        timeout: Execution timeout in seconds. Overrides agent x-timeout. Defaults to DEFAULT_AGENT_TIMEOUT.
        base_dir: Workspace root.
        allow_paused_workstream: When true, allow manual execution even if the workstream is paused.
        retried_from_run_id: Optional originating run id when this run is a manual retry/replay.
    """
    if task_ids is None:
        task_ids = []

    # Ensure claude CLI is available
    claude_path = shutil.which("claude")
    if not claude_path:
        raise RuntimeError("Claude Code CLI not found. Install it from https://docs.anthropic.com/en/docs/claude-code")

    agent_file = _resolve_agent_file(agent_name, base_dir)
    agent_def = _parse_agent_md(agent_file)

    from .tasks import read_task, _save_task
    from .workstreams import read_workstream, resolve_workstream_workspace

    # Load tasks and resolve workstream context
    tasks = []
    ws = None
    for tid in task_ids:
        tasks.append(read_task(tid, base_dir))

    if tasks and not workstream_id:
        workstream_id = tasks[0].workstream_id
    if workstream_id:
        ws = read_workstream(workstream_id, base_dir)
        if ws.paused and not allow_paused_workstream:
            raise RuntimeError(f"Workstream '{ws.name}' is paused")

    # Preflight: block obviously invalid image attachments before invoking Claude.
    invalid_images = []
    for t in tasks:
        issues = validate_task_image_attachments(t, base_dir=base_dir)
        for issue in issues:
            invalid_images.append((t, issue))

    if invalid_images:
        details = []
        by_task = {}
        for task_obj, issue in invalid_images:
            by_task.setdefault(task_obj.id, {"task": task_obj, "issues": []})
            by_task[task_obj.id]["issues"].append(issue)

        for item in by_task.values():
            task_obj = item["task"]
            for issue in item["issues"]:
                details.append(
                    f"- task {task_obj.id} ({task_obj.title}): {issue['path']} -> {issue['reason']}"
                )
            task_obj.add_audit(
                "agent_failed",
                "Agent preflight failed due to invalid image attachments. "
                "Fix or detach invalid image files before retry."
            )
            _save_task(task_obj, base_dir)

        raise RuntimeError(
            "Invalid image attachments detected; refusing to start agent run.\n"
            + "\n".join(details)
        )

    # Write task IDs to a temp file for the agent to reference (always, even for single task)
    task_file_path = None
    run_id = None
    log_path_rel = None
    run_meta = None
    fd, task_file_path = tempfile.mkstemp(suffix=".json", prefix="agent_tasks_")
    with os.fdopen(fd, "w") as f:
        json.dump({"task_ids": task_ids, "workstream_id": workstream_id}, f)

    try:
        # Build system prompt from agent definition + tool docs
        system_prompt = _build_system_prompt(agent_def, base_dir)

        # Compute path context for prompt injection
        orchestration_root = os.path.abspath(base_dir)
        workspace_root = orchestration_root
        if ws:
            # For descendants under a mounted workstream, mounted_workspace_path is
            # often set on an ancestor. Resolve the workspace that actually stores
            # this workstream's files first.
            try:
                workspace_root = os.path.abspath(
                    resolve_workstream_workspace(ws.id, base_dir=base_dir)
                )
            except Exception:
                workspace_root = orchestration_root

            # If the current workstream itself is mounted, descendants should be
            # developed in the mounted target path rather than the YAML storage root.
            if ws.mounted_workspace_path:
                _expanded = os.path.expanduser(ws.mounted_workspace_path)
                workspace_root = (
                    os.path.abspath(_expanded)
                    if os.path.isabs(_expanded)
                    else os.path.abspath(os.path.join(orchestration_root, _expanded))
                )
        _path_context = (
            f"ORCHESTRATION_ROOT: {orchestration_root} "
            f"(orchestration CLI, artifacts, agent instructions)\n"
            f"WORKSPACE_ROOT: {workspace_root} "
            f"(product source code, tests, configs for this workstream)\n"
            "MANDATORY: Write product code only under WORKSPACE_ROOT. "
            "Do not write product code under ORCHESTRATION_ROOT unless both paths are identical.\n\n"
        )

        # Build task prompt based on context available
        if tasks and ws:
            if len(tasks) == 1:
                task = tasks[0]
                valid_transitions = ws.task_states.get(task.status, [])
                task_prompt = (
                    f"You are working on task '{task.title}' (ID: {task.id}) "
                    f"in workstream '{ws.name}' (ID: {ws.id}).\n"
                    f"Current status: {task.status}\n"
                    f"Valid next states: {valid_transitions}\n\n"
                    f"Task IDs file: {task_file_path}\n\n"
                    + _path_context +
                    f"Execution contract:\n"
                    f"1. Run: python -m orchestration.cli task read {task.id}\n"
                    f"2. Read the full task payload (description, comments, audit, attachments).\n"
                    f"3. For each attachment/path referenced in the task payload, run: python -m orchestration.cli artifact read \"<artifact_path>\" --workstream {ws.id}\n"
                    f"4. Only begin implementation/triage after completing steps 1 to 3.\n"
                    f"5. Before finishing, post a task comment summarizing what you changed and why.\n"
                    f"6. If your role owns state movement, transition the task to the next valid state based on outcome.\n\n"
                    f"Role-specific objective:\n"
                    f"Follow your agent instructions and complete this task.\n\n"
                    f"Completion requirements:\n"
                    f"1. Explicitly state: done, blocked, or needs follow-up.\n"
                    f"2. If blocked, include blocker details and exact dependency.\n"
                    f"3. If done, include verification evidence (tests/build/commands run)."
                )
                context_section = _workstream_context_prompt_section(ws)
                if context_section:
                    task_prompt += f"\n\n{context_section}"
            else:
                task_lines = []
                for t in tasks:
                    transitions = ws.task_states.get(t.status, [])
                    task_lines.append(
                        f"- '{t.title}' (ID: {t.id}, status: {t.status}, "
                        f"valid next: {transitions}, attachments: {len(getattr(t, 'attachments', []) or [])})"
                    )
                task_prompt = (
                    f"You are working on {len(tasks)} tasks "
                    f"in workstream '{ws.name}' (ID: {ws.id}).\n\n"
                    f"Tasks:\n" + "\n".join(task_lines) + "\n\n"
                    f"Task IDs file: {task_file_path}\n\n"
                    + _path_context +
                    f"Execution contract:\n"
                    f"1. Run: python -m orchestration.cli task list {ws.id} or read each task ID from the Task IDs file.\n"
                    f"2. For each task ID, run: python -m orchestration.cli task read <task_id>\n"
                    f"3. Read the full task payload for each task (description, comments, audit, attachments).\n"
                    f"4. For each attachment/path referenced by any task payload, run: python -m orchestration.cli artifact read \"<artifact_path>\" --workstream {ws.id}\n"
                    f"5. Only begin implementation/triage after completing steps 1 to 4.\n"
                    f"6. Before finishing, post task comments summarizing what you changed and why.\n"
                    f"7. If your role owns state movement, transition each task to the next valid state based on outcome.\n\n"
                    f"Role-specific objective:\n"
                    f"Follow your agent instructions and complete these tasks.\n\n"
                    f"Completion requirements:\n"
                    f"1. Explicitly state for each task: done, blocked, or needs follow-up.\n"
                    f"2. If blocked, include blocker details and exact dependency.\n"
                    f"3. If done, include verification evidence (tests/build/commands run)."
                )
                context_section = _workstream_context_prompt_section(ws)
                if context_section:
                    task_prompt += f"\n\n{context_section}"
        elif ws:
            states = list(ws.task_states.keys())
            task_prompt = (
                f"You are running standalone in workstream '{ws.name}' (ID: {ws.id}).\n"
                f"Available states for tasks on this workstream: {states}\n\n"
                + _path_context +
                "Execution contract:\n"
                "1. You may inspect tasks in this workstream and decide which ones to work on.\n"
                "2. Before you begin work on any specific task, acquire a lock through the orchestration system: python -m orchestration.cli lock acquire <task_id> --agent \"<agent_name>\"\n"
                "3. If a task is already locked or lock acquisition fails, skip that task.\n"
                "4. While you hold a task lock, complete the needed work, then release it when finished: python -m orchestration.cli lock release <task_id> --agent \"<agent_name>\"\n"
                "5. Do not modify a task unless you successfully acquired its lock first.\n\n"
                f"Follow your instructions now."
            )
            context_section = _workstream_context_prompt_section(ws)
            if context_section:
                task_prompt += f"\n\n{context_section}"
        else:
            task_prompt = (
                f"You are running standalone with no specific workstream or task.\n"
                f"ORCHESTRATION_ROOT: {orchestration_root}\n\n"
                f"Follow your instructions now."
            )

        # Append custom trigger prompt if provided
        if prompt:
            task_prompt += f"\n\nAdditional instructions:\n{prompt}"

        # Log agent_started to audit trail for each task
        for t in tasks:
            t.add_audit("agent_started", f"Agent '{agent_def['name']}' started processing")
            _save_task(t, base_dir)

        # Resolve model and effort for CLI and for run metadata
        model = _resolve_agent_model(agent_def)
        effort = _resolve_agent_effort(agent_def)

        # Initialize run metadata
        _ensure_state_dirs(base_dir)
        run_id = _run_id or str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        log_path = os.path.join(_agent_runs_dir(base_dir), f"{run_id}.log")
        log_path_rel = os.path.relpath(log_path, base_dir)

        # Prepare environment for subprocess
        env = os.environ.copy()
        env["ORCHESTRATION_AGENT_NAME"] = agent_def["name"]
        env["ORCHESTRATION_AGENT_RUN_ID"] = run_id if run_id else ""
        env["ORCHESTRATION_AGENT_TASK_IDS"] = _compact_json(task_ids)
        env["ORCHESTRATION_ROOT"] = orchestration_root
        env["WORKSPACE_ROOT"] = workspace_root
        if workstream_id:
            env["ORCHESTRATION_AGENT_WORKSTREAM_ID"] = str(workstream_id)
        abs_base = os.path.abspath(base_dir)

        task_titles = [{"id": t.id, "title": t.title} for t in tasks]
        run_meta = {
            "run_id": run_id,
            "agent": agent_def["name"],
            "agent_ref": agent_name,
            "model": model,
            "effort": effort,
            "workstream_id": workstream_id,
            "workstream_path": _workstream_path(base_dir, workstream_id),
            "task_ids": list(task_ids),
            "tasks": task_titles,
            "prompt": task_prompt,
            "system_prompt": system_prompt,
            "log_path": log_path_rel,
            "started_at": started_at,
            "ended_at": None,
            "status": "running",
            "exit_code": None,
            "retried_from_run_id": retried_from_run_id,
            "retried_to_run_ids": [],
        }
        _write_run_meta(base_dir, run_id, run_meta)

        # Build claude CLI command
        cmd = [
            claude_path,
            "-p", task_prompt,
            "--append-system-prompt", system_prompt,
            "--model", model,
            "--output-format", "text",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if effort:
            cmd.extend(["--effort", effort])
        command_line = shlex.join(cmd)

        run_meta["command_line"] = command_line
        _write_run_meta(base_dir, run_id, run_meta)

        # Use a file-backed log so the Workspace Manager can live-tail active agent output.
        timeout_expired = False
        with open(log_path, "w", encoding="utf-8") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=abs_base,
                env=env,
                # Detach the agent process from the scheduler's process group.
                # This prevents scheduler restarts/stops from accidentally terminating
                # in-flight agent runs that should continue independently.
                start_new_session=True,
            )

            active_run = {
                "run_id": run_id,
                "agent": agent_def["name"],
                "agent_ref": agent_name,
                "model": model,
                "effort": effort,
                "workstream_id": workstream_id,
                "task_ids": list(task_ids),
                "pid": proc.pid,
                "started_at": started_at,
                "log_path": log_path_rel,
            }
            _register_active_agent(base_dir, active_run)

            # Acquire a process-level lock so standalone (non-task) agents can be
            # auto-detected and cleaned up if they hang past their TTL.
            from .locks import acquire_process_lock
            _process_lock_ttl = (timeout or agent_def.get("timeout") or DEFAULT_AGENT_TIMEOUT) + 300
            try:
                acquire_process_lock(
                    run_id,
                    agent_id=agent_def["name"],
                    pid=proc.pid,
                    ttl_seconds=int(_process_lock_ttl),
                    base_dir=base_dir,
                )
            except Exception:
                pass  # Non-fatal; process lock is best-effort

            # Store subprocess PID in lock files for dead-process detection
            from .locks import update_lock_pid
            for tid in task_ids:
                update_lock_pid(tid, proc.pid, base_dir=base_dir)

            try:
                # Resolve timeout: caller override > agent x-timeout > default
                effective_timeout = timeout or agent_def.get("timeout") or DEFAULT_AGENT_TIMEOUT
                proc.communicate(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                timeout_expired = True
                proc.kill()
                proc.communicate()
            finally:
                _unregister_active_agent(base_dir, run_id)
                from .locks import release_process_lock
                try:
                    release_process_lock(run_id, base_dir=base_dir)
                except Exception:
                    pass

        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            output = f.read().strip()
        returncode = proc.returncode

        if run_meta is None:
            run_meta = {
                "run_id": run_id,
                "agent": agent_def["name"],
                "agent_ref": agent_name,
                "model": model,
                "effort": effort,
                "workstream_id": workstream_id,
                "workstream_path": _workstream_path(base_dir, workstream_id),
                "task_ids": list(task_ids),
                "tasks": [{"id": t.id, "title": t.title} for t in tasks],
                "prompt": task_prompt,
                "system_prompt": system_prompt,
                "command_line": command_line,
                "log_path": log_path_rel,
                "started_at": started_at,
                "retried_from_run_id": retried_from_run_id,
                "retried_to_run_ids": [],
            }
        final_status = _classify_run_outcome(returncode, timeout_expired)
        run_meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        run_meta["status"] = final_status
        run_meta["exit_code"] = returncode
        _write_run_meta(base_dir, run_id, run_meta)

        # Log agent output to audit trail for each task
        for tid in task_ids:
            t = read_task(tid, base_dir)  # Re-read in case agent modified it
            if final_status == "completed":
                audit_output = output[:MAX_AUDIT_OUTPUT]
                if len(output) > MAX_AUDIT_OUTPUT:
                    audit_output += f"\n... (truncated, {len(output)} total chars)"
                t.add_audit("agent_completed", f"Agent '{agent_def['name']}' completed.\n\nOutput:\n{audit_output}")
                # Reset retry count on success
                t.retry_count = 0
                t.last_failure_at = None
            else:
                error_msg = output[:MAX_AUDIT_OUTPUT]
                if final_status == "timeout":
                    t.add_audit("agent_failed", f"Agent '{agent_def['name']}' timed out.\n\nError:\n{error_msg}")
                elif final_status == "killed":
                    t.add_audit("agent_failed", f"Agent '{agent_def['name']}' was killed.\n\nError:\n{error_msg}")
                else:
                    t.add_audit("agent_failed", f"Agent '{agent_def['name']}' failed (exit {returncode}).\n\nError:\n{error_msg}")
            _save_task(t, base_dir)

        # Best-effort learnings compaction to keep artifacts concise over time.
        _compact_learnings_artifact_if_needed(agent_def, workstream_id, base_dir)

        # Best-effort lock cleanup for this agent/task set.
        # This handles stale lock edge cases when scheduler/thread lifecycles are interrupted.
        from .locks import release_lock
        for tid in task_ids:
            try:
                release_lock(tid, agent_id=agent_def["name"], base_dir=base_dir)
            except Exception:
                # Ignore ownership mismatches/missing locks; callers may also release locks.
                pass

        if final_status != "completed":
            if final_status == "timeout":
                raise RuntimeError(f"Agent '{agent_def['name']}' timed out after {effective_timeout} seconds")
            if final_status == "killed":
                raise RuntimeError(f"Agent '{agent_def['name']}' was killed (exit {returncode}): {output}")
            raise RuntimeError(f"Agent '{agent_def['name']}' failed (exit {returncode}): {output}")

        response = {
            "agent": agent_def["name"],
            "result": output,
            "run_id": run_id,
            "log_path": log_path_rel,
            "retried_from_run_id": retried_from_run_id,
        }
        if task_ids:
            response["task_ids"] = task_ids
        if ws:
            response["workstream_id"] = ws.id
        return response

    finally:
        # Clean up temp file
        if task_file_path and os.path.exists(task_file_path):
            os.remove(task_file_path)
