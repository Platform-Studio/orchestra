"""Agent operations."""

import codecs
import errno
import json
import os
import glob
import pty
import re
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
from .persistence import resolve_workstream_root

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Maximum bytes of agent output to store in audit trail
MAX_AUDIT_OUTPUT = 10_000
DEFAULT_LEARNINGS_COMPACTION_THRESHOLD_BYTES = 20_000
LEARNINGS_COMPACTION_THRESHOLD_ENV_VAR = "ORCHESTRATION_LEARNINGS_COMPACTION_THRESHOLD_BYTES"
DEFAULT_AGENT_RUNTIME = "claude-code"
AGENT_RUNTIME_ENV_VAR = "ORCHESTRATION_AGENT_RUNTIME"
AUDIO_FILE_PATH_ENV_VAR = "AUDIO_FILE_PATH"
DEFAULT_AGENT_START_SOUND_ENV_VAR = "DEFAULT_AGENT_START_SOUND"
DEFAULT_AGENT_FINISHED_SOUND_ENV_VAR = "DEFAULT_AGENT_FINISHED_SOUND"
DEFAULT_AGENT_ERROR_SOUND_ENV_VAR = "DEFAULT_AGENT_ERROR_SOUND"
CLINE_CONFIG_DIR_ENV_VAR = "ORCHESTRATION_CLINE_CONFIG_DIR"
CLAUDE_CONFIG_DIR_ENV_VAR = "ORCHESTRATION_CLAUDE_CONFIG_DIR"
COPILOT_CONTEXT_DIRS_ENV_VAR = "ORCHESTRATION_COPILOT_CONTEXT_DIRS"
CLINE_DEFAULT_MODEL_ENV_VAR = "CLINE_DEFAULT_LLM"
CLINE_VERBOSE_ENV_VAR = "ORCHESTRATION_CLINE_VERBOSE"
COPILOT_DEFAULT_MODEL_ENV_VAR = "COPILOT_MODEL"
GLOBAL_SOUND_MUTE_FILENAME = "global_sound_muted"
AGENTS_DIR_ENV_VAR = "ORCHESTRATION_AGENTS_DIR"
SOURCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

CLINE_DEFAULT_MODEL = "deepseek/deepseek-v4-flash"
COPILOT_DEFAULT_MODEL = "auto"
CLINE_MODEL_LEVEL_DEFAULTS = {
    "high": "deepseek/deepseek-v4-pro",
    "medium": "deepseek/deepseek-v4-flash",
    "low": "deepseek/deepseek-v4-flash",
    "coding": None,
}
COPILOT_MODEL_LEVEL_DEFAULTS = {
    "high": COPILOT_DEFAULT_MODEL,
    "medium": COPILOT_DEFAULT_MODEL,
    "low": COPILOT_DEFAULT_MODEL,
    "coding": None,
}

_ACTIVE_AGENTS_LOCK = threading.Lock()

# Compile once for efficiency — matches complete and bare ANSI escape sequences.
_ANSI_ESCAPE_RE = re.compile(
    r'\x1b\[[0-9;]*[a-zA-Z]'       # CSI sequences: \x1b[0m, \x1b[1;32m, etc.
    r'|\x1b\][^\x07]*\x07'         # OSC sequences: \x1b]0;title\x07
    r'|\x1b[PX^_][^\x1b]*\x1b\\'   # DCS/SOS/PM/APC sequences
    r'|\x1b[()][AB012]'            # Character set sequences
    r'|\[[0-9;]+m'                 # Bare SGR fragments (when \x1b already stripped)
)


def _strip_ansi_escape_codes(text: str) -> str:
    """Remove ANSI terminal escape sequences and bare SGR fragments from output.

    Cline and other runtimes may output ANSI-styled text.  When stdout is not a
    TTY the leading ``\\x1b`` byte is sometimes stripped, leaving bare fragments
    like ``[0m``, ``[2m`` in the output.  This helper removes both complete and
    partial sequences so the output is clean for UI display and audit storage.
    """
    if not text:
        return text
    return _ANSI_ESCAPE_RE.sub('', text)


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


def _orchestration_cli_command(base_dir: str = None) -> str:
    """Return the Python command agents should use for the orchestration CLI."""
    command = f"{shlex.quote(sys.executable)} -m orchestration.cli"
    if base_dir:
        command += f" --base-dir {shlex.quote(os.path.abspath(base_dir))}"
    return command

def _audio_file_root(base_dir: str) -> str:
    """Resolve the root directory for agent sound assets."""
    override = str(os.getenv(AUDIO_FILE_PATH_ENV_VAR) or "").strip()
    if override:
        expanded = os.path.expanduser(override)
        if os.path.isabs(expanded):
            return os.path.abspath(expanded)
        return os.path.abspath(os.path.join(base_dir, expanded))
    return os.path.abspath(os.path.join(base_dir, "audio"))


def _resolve_agent_sound_file(audio_name: str, base_dir: str) -> str | None:
    """Resolve a configured sound file to an absolute on-disk path."""
    candidate_name = str(audio_name or "").strip()
    if not candidate_name:
        return None

    expanded = os.path.expanduser(candidate_name)
    if os.path.isabs(expanded):
        resolved = os.path.abspath(expanded)
    else:
        audio_root = _audio_file_root(base_dir)
        resolved = os.path.abspath(os.path.join(audio_root, expanded))
        try:
            if os.path.commonpath([resolved, audio_root]) != audio_root:
                return None
        except ValueError:
            return None

    if not os.path.isfile(resolved):
        return None
    return resolved


def _resolve_audio_player_command(audio_path: str) -> list[str] | None:
    """Return a best-effort local audio playback command."""
    for binary in ("afplay", "paplay", "aplay", "play"):
        resolved = shutil.which(binary)
        if resolved:
            return [resolved, audio_path]
    return None


def _configured_agent_sound_name(agent_def: dict, event: str) -> str | None:
    """Resolve the configured sound name for an event with header precedence."""
    env_var_by_event = {
        "start": DEFAULT_AGENT_START_SOUND_ENV_VAR,
        "finish": DEFAULT_AGENT_FINISHED_SOUND_ENV_VAR,
        "error": DEFAULT_AGENT_ERROR_SOUND_ENV_VAR,
    }
    sound_key = f"sound_{event}"
    defined_key = f"{sound_key}_defined"
    if agent_def.get(defined_key):
        raw_value = agent_def.get(sound_key)
    else:
        raw_value = os.getenv(env_var_by_event[event])
    candidate_name = str(raw_value or "").strip()
    return candidate_name or None


def _play_agent_sound(agent_def: dict, event: str, base_dir: str) -> str | None:
    """Play a configured agent sound without blocking the main run lifecycle."""
    if get_global_sound_mute(base_dir):
        return None

    audio_path = _resolve_agent_sound_file(_configured_agent_sound_name(agent_def, event), base_dir)
    if not audio_path:
        return None

    command = _resolve_audio_player_command(audio_path)
    if not command:
        return None

    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        return None
    return audio_path


def _agent_learning_prompt_section(agent_def: dict, workstream_id: str, base_dir: str = None) -> str:
    """Default learning behavior injected into agent task prompts."""
    learning_path = _agent_learning_artifact_name(agent_def)
    cli = _orchestration_cli_command(base_dir)
    return (
        "=== AGENT LEARNING (DEFAULT) ===\n"
        f"Before you start working, review the learnings artifact at '{learning_path}' in the orchestration system, if it exists.\n"
        f"- Try: {cli} artifact read '{learning_path}' --workstream {workstream_id}\n"
        "- If it does not exist, continue without failing.\n\n"
        f"When you are done working, append actionable learnings that will help you work faster and more efficiently to '{learning_path}' in the orchestration system (create it if it does not exist).\n"
        f"- Read current file first: {cli} artifact read '{learning_path}' --workstream {workstream_id}\n"
        f"- Save updated content: {cli} artifact create --path '{learning_path}' --content '<updated_markdown>' --workstream {workstream_id}\n"
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
    return os.path.join(resolve_workstream_root(base_dir), ".orchestration")


def _active_agents_path(base_dir: str) -> str:
    return os.path.join(_state_dir(base_dir), "active_agents.yaml")


def _agent_runs_dir(base_dir: str) -> str:
    return os.path.join(_state_dir(base_dir), "agent_runs")


def _agent_run_worktrees_dir(base_dir: str) -> str:
    return os.path.join(_state_dir(base_dir), "run_worktrees")


def _agent_run_context_dir(base_dir: str, run_id: str) -> str:
    return os.path.join(_agent_runs_dir(base_dir), run_id, "context")


def _agent_run_context_manifest_path(base_dir: str, run_id: str) -> str:
    return os.path.join(_agent_run_context_dir(base_dir, run_id), "context_manifest.json")


def _global_sound_mute_path(base_dir: str) -> str:
    return os.path.join(_state_dir(base_dir), GLOBAL_SOUND_MUTE_FILENAME)


def _agent_run_meta_path(base_dir: str, run_id: str) -> str:
    return os.path.join(_agent_runs_dir(base_dir), f"{run_id}.json")


def _ensure_state_dirs(base_dir: str) -> None:
    os.makedirs(_state_dir(base_dir), exist_ok=True)
    os.makedirs(_agent_runs_dir(base_dir), exist_ok=True)


def _provision_run_worktree(workspace_root: str, run_id: str, base_dir: str = ".") -> dict:
    """Create a detached per-run worktree and return its resolved paths."""
    source_workspace_root = os.path.abspath(os.path.expanduser(workspace_root))
    try:
        repo_root_result = subprocess.run(
            ["git", "-C", source_workspace_root, "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is required to provision an isolated agent worktree") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(
            f"Agent requested x-own-worktree, but WORKSPACE_ROOT is not inside a git repo: {stderr or source_workspace_root}"
        ) from exc

    repo_root = os.path.abspath(repo_root_result.stdout.strip())
    relative_workspace = os.path.relpath(source_workspace_root, repo_root)

    worktrees_dir = _agent_run_worktrees_dir(base_dir)
    os.makedirs(worktrees_dir, exist_ok=True)
    worktree_root = os.path.join(worktrees_dir, run_id)
    if os.path.exists(worktree_root):
        raise RuntimeError(f"Isolated worktree path already exists for run {run_id}: {worktree_root}")

    try:
        subprocess.run(
            ["git", "-C", repo_root, "worktree", "add", "--detach", worktree_root],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(worktree_root, ignore_errors=True)
        stderr = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(
            f"Failed to create isolated worktree for agent run {run_id}: {stderr or 'git worktree add failed'}"
        ) from exc

    effective_workspace_root = worktree_root
    if relative_workspace not in (".", ""):
        effective_workspace_root = os.path.join(worktree_root, relative_workspace)

    return {
        "repo_root": repo_root,
        "worktree_root": worktree_root,
        "workspace_root": effective_workspace_root,
    }


def _deprovision_run_worktree(worktree: dict | None, base_dir: str = ".") -> None:
    """Best-effort cleanup for a detached per-run worktree."""
    if not worktree:
        return

    worktree_root = os.path.abspath(str(worktree.get("worktree_root") or "").strip())
    repo_root = os.path.abspath(str(worktree.get("repo_root") or "").strip())
    if not worktree_root:
        return

    managed_root = os.path.abspath(_agent_run_worktrees_dir(base_dir))
    safe_to_delete = False
    try:
        safe_to_delete = os.path.commonpath([worktree_root, managed_root]) == managed_root
    except ValueError:
        safe_to_delete = False

    if repo_root and os.path.exists(worktree_root):
        try:
            subprocess.run(
                ["git", "-C", repo_root, "worktree", "remove", "--force", worktree_root],
                check=True,
                capture_output=True,
                text=True,
            )
        except Exception:
            pass

    if safe_to_delete and os.path.exists(worktree_root):
        shutil.rmtree(worktree_root, ignore_errors=True)


def get_global_sound_mute(base_dir: str) -> bool:
    path = _global_sound_mute_path(base_dir)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip().lower()
    except FileNotFoundError:
        return False
    return raw in {"1", "true", "yes", "on", "muted"}


def set_global_sound_mute(muted: bool, base_dir: str) -> bool:
    path = _global_sound_mute_path(base_dir)
    if muted:
        _ensure_state_dirs(base_dir)
        with open(path, "w", encoding="utf-8") as f:
            f.write("1\n")
        return True

    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    return False


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


def _candidate_context_roots(runtime: str) -> list[dict]:
    """Return provider-local roots to snapshot before and after a run."""
    runtime_norm = _normalize_agent_runtime(runtime)
    roots = []

    if runtime_norm == "cline":
        cline_home = _resolve_cline_config_dir() or os.path.expanduser("~/.cline")
        roots.append({
            "provider": "cline",
            "kind": "cline_tasks",
            "root": os.path.abspath(os.path.join(cline_home, "data", "tasks")),
        })
        return roots

    if runtime_norm == "claude-code":
        claude_home = os.getenv(CLAUDE_CONFIG_DIR_ENV_VAR) or os.path.expanduser("~/.claude")
        claude_home = os.path.abspath(os.path.expanduser(claude_home))
        roots.extend([
            {"provider": "claude-code", "kind": "claude_projects", "root": os.path.join(claude_home, "projects")},
            {"provider": "claude-code", "kind": "claude_home", "root": claude_home},
        ])
        return roots

    if runtime_norm == "copilot":
        raw_dirs = str(os.getenv(COPILOT_CONTEXT_DIRS_ENV_VAR) or "").strip()
        if raw_dirs:
            parts = []
            for chunk in raw_dirs.split(os.pathsep):
                parts.extend(chunk.split(","))
            for idx, raw in enumerate(parts):
                candidate = str(raw or "").strip()
                if candidate:
                    roots.append({
                        "provider": "copilot",
                        "kind": f"configured_{idx + 1}",
                        "root": os.path.abspath(os.path.expanduser(candidate)),
                    })
        return roots

    return roots


def _context_file_snapshot(root: str) -> dict:
    """Return a lightweight file snapshot for a provider context root."""
    snapshot = {}
    if not root or not os.path.isdir(root):
        return snapshot

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {"node_modules", ".git", "__pycache__"}]
        for filename in filenames:
            path = os.path.join(dirpath, filename)
            try:
                if not os.path.isfile(path):
                    continue
                stat = os.stat(path)
            except OSError:
                continue
            snapshot[os.path.abspath(path)] = {
                "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))),
                "size": int(stat.st_size),
            }
    return snapshot


def _snapshot_provider_context(runtime: str) -> dict:
    """Snapshot candidate provider files before the subprocess starts."""
    sources = []
    for source in _candidate_context_roots(runtime):
        root = source["root"]
        exists = os.path.isdir(root)
        sources.append({
            **source,
            "exists": exists,
            "files": _context_file_snapshot(root) if exists else {},
        })
    return {
        "runtime": _normalize_agent_runtime(runtime),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "sources": sources,
    }


def _changed_context_files(source: dict) -> list[str]:
    """Return files that appeared or changed after the pre-run snapshot."""
    root = source.get("root")
    before = source.get("files") or {}
    after = _context_file_snapshot(root)
    changed = []
    for path, stat in after.items():
        previous = before.get(path)
        if previous is None or previous.get("mtime_ns") != stat.get("mtime_ns") or previous.get("size") != stat.get("size"):
            changed.append(path)
    return changed


def _expand_cline_task_files(root: str, changed_files: list[str]) -> list[str]:
    """When any Cline task file changes, copy the whole task folder."""
    task_dirs = set()
    root_abs = os.path.abspath(root)
    for path in changed_files:
        try:
            rel = os.path.relpath(path, root_abs)
        except ValueError:
            continue
        parts = rel.split(os.sep)
        if parts and parts[0] not in {"", ".", ".."}:
            task_dirs.add(os.path.join(root_abs, parts[0]))

    expanded = set(changed_files)
    for task_dir in task_dirs:
        if not os.path.isdir(task_dir):
            continue
        for dirpath, _dirnames, filenames in os.walk(task_dir):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                if os.path.isfile(path):
                    expanded.add(os.path.abspath(path))
    return sorted(expanded)


def _copy_context_file(source_root: str, source_path: str, dest_root: str, source_index: int) -> dict:
    source_root_abs = os.path.abspath(source_root)
    source_path_abs = os.path.abspath(source_path)
    rel = os.path.relpath(source_path_abs, source_root_abs)
    if rel.startswith(".."):
        raise ValueError("context file is outside source root")

    copied_rel = os.path.join(f"source_{source_index}", rel)
    dest_path = os.path.join(dest_root, copied_rel)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    shutil.copy2(source_path_abs, dest_path)
    stat = os.stat(dest_path)
    return {
        "original_path": source_path_abs,
        "copied_path": copied_rel,
        "bytes": int(stat.st_size),
        "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _write_context_manifest(base_dir: str, run_id: str, manifest: dict) -> str:
    context_dir = _agent_run_context_dir(base_dir, run_id)
    os.makedirs(context_dir, exist_ok=True)
    path = _agent_run_context_manifest_path(base_dir, run_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return os.path.relpath(path, base_dir)


def _capture_provider_context(
    base_dir: str,
    run_id: str,
    runtime: str,
    pre_snapshot: dict | None,
) -> dict:
    """Copy provider-native context artifacts into the central run store."""
    runtime_norm = _normalize_agent_runtime(runtime)
    context_dir = _agent_run_context_dir(base_dir, run_id)
    manifest = {
        "version": 1,
        "run_id": run_id,
        "runtime": runtime_norm,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "storage": {
            "context_dir": os.path.relpath(context_dir, base_dir),
            "central": True,
        },
        "sources": [],
        "files": [],
        "warnings": [],
        "foundation_context": {
            "prompt_in_run_metadata": True,
            "system_prompt_in_run_metadata": True,
            "command_line_in_run_metadata": True,
            "stdout_log_in_run_metadata": True,
            "cli_calls_in_workspace_audit": True,
        },
    }

    sources = (pre_snapshot or {}).get("sources") or _snapshot_provider_context(runtime_norm).get("sources", [])
    os.makedirs(context_dir, exist_ok=True)

    for idx, source in enumerate(sources):
        root = source.get("root")
        source_summary = {
            "provider": source.get("provider") or runtime_norm,
            "kind": source.get("kind"),
            "root": root,
            "exists": bool(root and os.path.isdir(root)),
            "status": "ok",
            "file_count": 0,
            "confidence": "best_effort",
        }
        if not source_summary["exists"]:
            source_summary["status"] = "missing"
            warning = f"Provider context root not found: {root}"
            source_summary["warning"] = warning
            manifest["warnings"].append(warning)
            manifest["sources"].append(source_summary)
            continue

        try:
            changed = _changed_context_files(source)
            if runtime_norm == "cline" and source.get("kind") == "cline_tasks":
                changed = _expand_cline_task_files(root, changed)
            for path in sorted(set(changed)):
                try:
                    copied = _copy_context_file(root, path, context_dir, idx)
                except Exception as exc:
                    manifest["warnings"].append(f"Failed to copy context file {path}: {exc}")
                    continue
                entry = {
                    "provider": source_summary["provider"],
                    "kind": source_summary["kind"],
                    **copied,
                }
                manifest["files"].append(entry)
            source_summary["file_count"] = len([
                f for f in manifest["files"]
                if f.get("provider") == source_summary["provider"] and f.get("kind") == source_summary["kind"]
            ])
            if source_summary["file_count"] == 0:
                source_summary["status"] = "no_changed_files"
        except Exception as exc:
            source_summary["status"] = "error"
            source_summary["warning"] = str(exc)
            manifest["warnings"].append(f"Provider context capture failed for {root}: {exc}")
        manifest["sources"].append(source_summary)

    manifest_rel = _write_context_manifest(base_dir, run_id, manifest)
    return {
        "manifest_path": manifest_rel,
        "context_dir": manifest["storage"]["context_dir"],
        "file_count": len(manifest["files"]),
        "warnings": list(manifest["warnings"]),
        "sources": [
            {
                "provider": s.get("provider"),
                "kind": s.get("kind"),
                "status": s.get("status"),
                "file_count": s.get("file_count", 0),
            }
            for s in manifest["sources"]
        ],
    }


def _load_context_manifest(base_dir: str, run_id: str) -> dict:
    path = _agent_run_context_manifest_path(base_dir, run_id)
    if not os.path.exists(path):
        return {
            "version": 1,
            "run_id": run_id,
            "runtime": None,
            "storage": {
                "context_dir": os.path.relpath(_agent_run_context_dir(base_dir, run_id), base_dir),
                "central": True,
            },
            "sources": [],
            "files": [],
            "warnings": ["No provider context manifest was captured for this run."],
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _stringify_context_content(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item.get("text") or ""))
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                return _stringify_context_content(value.get(key))
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _normalize_cline_json(payload, *, source_file: str) -> tuple[list[dict], list[dict]]:
    messages = []
    events = []
    if not isinstance(payload, list):
        return messages, events

    basename = os.path.basename(source_file)
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        role = item.get("role") or item.get("say") or item.get("type") or "event"
        text = _stringify_context_content(item.get("content", item.get("text", item.get("message"))))
        ts = item.get("ts") or item.get("timestamp")
        common = {
            "provider": "cline",
            "source_file": source_file,
            "index": index,
            "timestamp": ts,
            "type": item.get("type") or item.get("say") or basename,
            "text": text,
            "raw": item,
        }
        if basename == "api_conversation_history.json" and (item.get("role") or text):
            messages.append({**common, "role": str(role)})
        else:
            events.append(common)
            if text and (item.get("say") in {"user_feedback", "text", "reasoning", "thinking"}):
                messages.append({**common, "role": str(role)})
    return messages, events


def _normalize_jsonl(path: str, *, provider: str, source_file: str) -> tuple[list[dict], list[dict]]:
    messages = []
    events = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for index, line in enumerate(f):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                events.append({
                    "provider": provider,
                    "source_file": source_file,
                    "index": index,
                    "type": "text",
                    "text": stripped,
                    "raw": stripped,
                })
                continue
            role = item.get("role") or item.get("type") or "event"
            text = _stringify_context_content(item.get("content", item.get("text", item.get("message"))))
            row = {
                "provider": provider,
                "source_file": source_file,
                "index": index,
                "timestamp": item.get("timestamp") or item.get("ts"),
                "type": item.get("type") or "jsonl",
                "text": text,
                "raw": item,
            }
            if item.get("role") or text:
                messages.append({**row, "role": str(role)})
            else:
                events.append(row)
    return messages, events


def _normalize_provider_context(base_dir: str, run_id: str, manifest: dict) -> dict:
    context_dir = _agent_run_context_dir(base_dir, run_id)
    messages = []
    events = []
    warnings = []
    for file_entry in manifest.get("files") or []:
        rel = file_entry.get("copied_path")
        provider = file_entry.get("provider") or manifest.get("runtime") or "unknown"
        if not rel:
            continue
        path = os.path.abspath(os.path.join(context_dir, rel))
        try:
            if os.path.commonpath([path, os.path.abspath(context_dir)]) != os.path.abspath(context_dir):
                continue
        except ValueError:
            continue
        if not os.path.exists(path):
            continue
        try:
            basename = os.path.basename(path)
            if provider == "cline" and basename.endswith(".json"):
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    payload = json.load(f)
                new_messages, new_events = _normalize_cline_json(payload, source_file=rel)
                messages.extend(new_messages)
                events.extend(new_events)
            elif path.endswith(".jsonl"):
                new_messages, new_events = _normalize_jsonl(path, provider=provider, source_file=rel)
                messages.extend(new_messages)
                events.extend(new_events)
        except Exception as exc:
            warnings.append(f"Failed to normalize {rel}: {exc}")
    return {
        "manifest": manifest,
        "messages": messages,
        "events": events,
        "files": list(manifest.get("files") or []),
        "warnings": list(manifest.get("warnings") or []) + warnings,
    }


def get_agent_run_context(run_id: str, base_dir: str = ".") -> dict:
    manifest = _load_context_manifest(base_dir, run_id)
    return _normalize_provider_context(base_dir, run_id, manifest)


def read_agent_run_context_file(run_id: str, relative_path: str, base_dir: str = ".") -> dict:
    rel = str(relative_path or "").strip()
    if not rel:
        raise ValueError("Missing context file path")
    context_dir = os.path.abspath(_agent_run_context_dir(base_dir, run_id))
    candidate = os.path.abspath(os.path.join(context_dir, rel))
    try:
        if os.path.commonpath([candidate, context_dir]) != context_dir:
            raise ValueError("Context file path escapes run context directory")
    except ValueError:
        raise ValueError("Context file path escapes run context directory") from None
    if not os.path.isfile(candidate):
        raise FileNotFoundError(f"Context file not found: {relative_path}")
    with open(candidate, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()
    return {
        "run_id": run_id,
        "path": rel,
        "content": content,
        "bytes": os.path.getsize(candidate),
    }


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


def count_active_agent_runs(workstream_id: str, agent_ref: str, base_dir: str = ".", concurrency_state: str = None) -> int:
    """Count active runs for a specific agent within a workstream."""
    if not workstream_id or not agent_ref:
        return 0

    target_keys = _agent_identity_keys(agent_ref, base_dir)
    if not target_keys:
        return 0

    with _ACTIVE_AGENTS_LOCK:
        runs = _prune_dead_active_runs(base_dir)

    count = 0
    target_state = str(concurrency_state or "").strip().lower()
    for run in runs:
        if str(run.get("workstream_id") or "") != str(workstream_id):
            continue
        if target_state:
            run_state = str(run.get("concurrency_state") or "").strip().lower()
            if run_state != target_state:
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
                output = _strip_ansi_escape_codes(f.read())

    from .workspace_audit import get_audit_log
    cli_calls = get_audit_log(base_dir=base_dir, limit=2000, event_type="orchestration_cli_call")
    cli_calls = [e for e in cli_calls if e.get("run_id") == run_id]
    cli_calls = sorted(cli_calls, key=lambda e: e.get("timestamp", ""))

    retry_info = _get_run_retry_info(run, base_dir)
    interruption_reason = _get_run_interruption_reason(run, base_dir)
    context_capture = get_agent_run_context(run_id, base_dir=base_dir)

    return {
        "run": run,
        "output": output,
        "cli_calls": cli_calls,
        "retry": retry_info,
        "interruption_reason": interruption_reason,
        "context_capture": context_capture,
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

    tail_text = _strip_ansi_escape_codes("".join(all_lines[-lines:]))
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


def _get_model(runtime: str = DEFAULT_AGENT_RUNTIME) -> str:
    """Return the default model name for the selected runtime."""
    runtime_norm = _normalize_agent_runtime(runtime)
    if runtime_norm == "cline":
        return _normalize_model_name(os.getenv(CLINE_DEFAULT_MODEL_ENV_VAR, CLINE_DEFAULT_MODEL))
    if runtime_norm == "copilot":
        return _normalize_model_name(os.getenv(COPILOT_DEFAULT_MODEL_ENV_VAR, COPILOT_DEFAULT_MODEL))

    return _normalize_model_name(os.getenv("DEFAULT_LLM", "sonnet"))


def _normalize_agent_role(raw: str) -> str:
    """Normalize canonical agent role names while preserving legacy aliases."""
    role = str(raw or "").strip().lower().replace("_", "-")
    if not role:
        return "worker"

    aliases = {
        "exec": "executive",
        "executive": "executive",
        "director": "director",
        "manager": "manager",
        "worker": "worker",
    }
    normalized = aliases.get(role)
    if normalized:
        return normalized

    raise ValueError(
        f"Invalid x-role '{raw}'. Expected one of: executive, exec, director, manager, worker"
    )


def _model_from_level(level: str, runtime: str = DEFAULT_AGENT_RUNTIME) -> str:
    """Map x-model-level to runtime-aware model aliases."""
    level_norm = str(level or "").strip().lower()
    runtime_norm = _normalize_agent_runtime(runtime)
    env_key_by_level = {
        "high": "HIGH_LLM",
        "medium": "MEDIUM_LLM",
        "low": "LOW_LLM",
        "coding": "CODING_LLM",
    }
    if runtime_norm == "cline":
        env_key_by_level = {
            "high": "CLINE_HIGH_LLM",
            "medium": "CLINE_MEDIUM_LLM",
            "low": "CLINE_LOW_LLM",
            "coding": "CLINE_CODING_LLM",
        }
    elif runtime_norm == "copilot":
        env_key_by_level = {
            "high": "COPILOT_HIGH_LLM",
            "medium": "COPILOT_MEDIUM_LLM",
            "low": "COPILOT_LOW_LLM",
            "coding": "COPILOT_CODING_LLM",
        }

    env_key = env_key_by_level.get(level_norm)
    if not env_key:
        raise ValueError(f"Invalid x-model-level '{level}'. Expected one of: high, medium, low, coding")

    env_value = os.getenv(env_key)
    if not env_value:
        if runtime_norm == "cline":
            if level_norm == "coding":
                default_runtime_model = _normalize_model_name(os.getenv(CLINE_DEFAULT_MODEL_ENV_VAR, CLINE_DEFAULT_MODEL))
                if default_runtime_model:
                    return default_runtime_model
            default_model = _normalize_model_name(CLINE_MODEL_LEVEL_DEFAULTS.get(level_norm))
            if default_model:
                return default_model
        if runtime_norm == "copilot":
            default_model = _normalize_model_name(COPILOT_MODEL_LEVEL_DEFAULTS.get(level_norm))
            if default_model:
                return default_model
        raise ValueError(f"x-model-level '{level_norm}' requires env var {env_key} to be set")

    model = _normalize_model_name(env_value)
    if not model:
        raise ValueError(f"Env var {env_key} is empty")
    return model


def _resolve_agent_model(agent_def: dict, runtime: str = DEFAULT_AGENT_RUNTIME) -> str:
    """Resolve model with precedence: x-model > x-model-level > runtime default."""
    explicit_model = _normalize_model_name(agent_def.get("model"))
    if explicit_model:
        return explicit_model

    level = agent_def.get("model_level")
    if str(level or "").strip():
        return _model_from_level(level, runtime=runtime)

    return _get_model(runtime=runtime)


def _normalize_agent_effort(raw, *, source_label: str = "x-effort") -> str | None:
    """Validate and normalize effort values from headers or env vars."""
    effort = str(raw or "").strip().lower()
    if not effort:
        return None
    allowed = {"low", "medium", "high", "xhigh", "max"}
    if effort not in allowed:
        raise ValueError(
            f"Invalid {source_label} '{raw}'. Expected one of: low, medium, high, xhigh, max"
        )
    return effort


def _effort_env_var_for_level(level: str, runtime: str = DEFAULT_AGENT_RUNTIME) -> str:
    """Map model level to runtime-aware effort env var names."""
    level_norm = str(level or "").strip().lower()
    runtime_norm = _normalize_agent_runtime(runtime)
    env_key_by_level = {
        "high": "HIGH_EFFORT",
        "medium": "MEDIUM_EFFORT",
        "low": "LOW_EFFORT",
        "coding": "CODING_EFFORT",
    }
    if runtime_norm == "cline":
        env_key_by_level = {
            "high": "CLINE_HIGH_EFFORT",
            "medium": "CLINE_MEDIUM_EFFORT",
            "low": "CLINE_LOW_EFFORT",
            "coding": "CLINE_CODING_EFFORT",
        }
    elif runtime_norm == "copilot":
        env_key_by_level = {
            "high": "COPILOT_HIGH_EFFORT",
            "medium": "COPILOT_MEDIUM_EFFORT",
            "low": "COPILOT_LOW_EFFORT",
            "coding": "COPILOT_CODING_EFFORT",
        }

    env_key = env_key_by_level.get(level_norm)
    if not env_key:
        raise ValueError(f"Invalid x-model-level '{level}'. Expected one of: high, medium, low, coding")
    return env_key


def _default_effort_env_var(runtime: str = DEFAULT_AGENT_RUNTIME) -> str:
    """Return runtime-aware default effort env var name."""
    runtime_norm = _normalize_agent_runtime(runtime)
    if runtime_norm == "cline":
        return "CLINE_DEFAULT_EFFORT"
    if runtime_norm == "copilot":
        return "COPILOT_DEFAULT_EFFORT"
    return "DEFAULT_EFFORT"


def _resolve_agent_effort(agent_def: dict, runtime: str = DEFAULT_AGENT_RUNTIME):
    """Resolve effort with precedence: x-effort > level env > runtime default env."""
    explicit_effort = _normalize_agent_effort(agent_def.get("effort"), source_label="x-effort")
    if explicit_effort:
        return explicit_effort

    level = agent_def.get("model_level")
    if str(level or "").strip():
        env_key = _effort_env_var_for_level(level, runtime=runtime)
        env_effort = _normalize_agent_effort(os.getenv(env_key), source_label=f"env var {env_key}")
        if env_effort:
            return env_effort

    default_env_key = _default_effort_env_var(runtime=runtime)
    return _normalize_agent_effort(os.getenv(default_env_key), source_label=f"env var {default_env_key}")


def _normalize_agent_runtime(raw: str) -> str:
    """Normalize supported agent runtime identifiers."""
    runtime = str(raw or "").strip().lower().replace("_", "-")
    if not runtime:
        return DEFAULT_AGENT_RUNTIME

    aliases = {
        "claude": "claude-code",
        "claude-code": "claude-code",
        "cline": "cline",
        "copilot": "copilot",
    }
    normalized = aliases.get(runtime)
    if normalized:
        return normalized

    raise ValueError(
        f"Invalid x-runtime '{raw}'. Expected one of: claude-code, claude, cline, copilot"
    )


def _resolve_agent_runtime(agent_def: dict) -> str:
    """Resolve runtime with precedence: x-runtime > ORCHESTRATION_AGENT_RUNTIME > default."""
    explicit_runtime = str(agent_def.get("runtime") or "").strip()
    if explicit_runtime:
        return _normalize_agent_runtime(explicit_runtime)

    env_runtime = str(os.getenv(AGENT_RUNTIME_ENV_VAR, "") or "").strip()
    if env_runtime:
        return _normalize_agent_runtime(env_runtime)

    return DEFAULT_AGENT_RUNTIME


def _resolve_runtime_executable(runtime: str) -> str:
    """Resolve the executable path for the selected agent runtime."""
    runtime_norm = _normalize_agent_runtime(runtime)
    if runtime_norm == "cline":
        runtime_path = shutil.which("cline")
        if runtime_path:
            return runtime_path
        raise RuntimeError(
            "Cline CLI not found. Install it with `npm install -g cline` and authenticate via `cline auth`."
        )

    if runtime_norm == "copilot":
        runtime_path = shutil.which("copilot")
        if runtime_path:
            return runtime_path

        gh_path = shutil.which("gh")
        if gh_path:
            return gh_path

        raise RuntimeError(
            "GitHub Copilot CLI not found. Install the `copilot` CLI or ensure `gh` with the `gh copilot` extension is available and authenticated."
        )

    runtime_path = shutil.which("claude")
    if runtime_path:
        return runtime_path

    raise RuntimeError(
        "Claude Code CLI not found. Install it from https://docs.anthropic.com/en/docs/claude-code"
    )


def _build_cline_prompt(task_prompt: str, system_prompt: str) -> str:
    """Compose a single prompt for Cline, which lacks a separate system prompt flag."""
    return (
        "=== SYSTEM INSTRUCTIONS ===\n"
        f"{system_prompt}\n\n"
        "=== TASK ===\n"
        f"{task_prompt}"
    )


def _cline_thinking_level(effort: str | None) -> str | None:
    """Map orchestration effort hints onto Cline's explicit thinking levels."""
    normalized = str(effort or "").strip().lower()
    if not normalized:
        return None
    mapping = {
        "low": None,
        "medium": None,
        "high": "high",
        "xhigh": "xhigh",
        "max": "xhigh",
    }
    return mapping.get(normalized)


def _build_copilot_prompt(task_prompt: str, system_prompt: str) -> str:
    """Compose a single prompt for Copilot CLI prompt mode."""
    return (
        "=== SYSTEM INSTRUCTIONS ===\n"
        f"{system_prompt}\n\n"
        "=== TASK ===\n"
        f"{task_prompt}"
    )


def _copilot_effort_value(effort: str | None) -> str | None:
    """Map orchestration effort hints onto Copilot's supported values."""
    normalized = str(effort or "").strip().lower()
    if not normalized:
        return None
    if normalized in {"low", "medium", "high"}:
        return normalized
    if normalized in {"xhigh", "max"}:
        return "high"
    return None


def _resolve_cline_config_dir() -> str | None:
    """Return a valid Cline home directory override, if one is configured.

    Cline's --config flag expects the Cline home directory and appends data/
    internally. Accept either ~/.cline or ~/.cline/data from operators, but
    normalize the latter back to ~/.cline so auth and task history resolve.
    """
    raw = str(os.getenv(CLINE_CONFIG_DIR_ENV_VAR, "") or "").strip()
    if not raw:
        return None
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(raw)))
    if not os.path.isdir(path):
        return None

    # A common misconfiguration is pointing at the Node/npm bin directory where
    # the `cline` executable lives. Cline may create a partial `data/` tree there,
    # but it is still not the intended config root and will bypass the user's
    # real authenticated state.
    def _looks_like_node_bin_dir(candidate: str) -> bool:
        binary_markers = ("node", "npm", "npx", "corepack")
        return os.path.basename(candidate) == "bin" and sum(
            1 for name in binary_markers if os.path.exists(os.path.join(candidate, name))
        ) >= 2

    if _looks_like_node_bin_dir(path):
        return None

    data_dir_markers = ("settings", "state", "workspaces", "tasks")
    file_markers = ("globalState.json", "secrets.json")
    path_looks_like_data_dir = (
        os.path.basename(path) == "data" and (
            any(os.path.isdir(os.path.join(path, name)) for name in data_dir_markers)
            or any(os.path.isfile(os.path.join(path, name)) for name in file_markers)
        )
    )
    if path_looks_like_data_dir:
        parent = os.path.dirname(path)
        if parent and not _looks_like_node_bin_dir(parent):
            return parent
        return None

    if any(os.path.isfile(os.path.join(path, name)) for name in file_markers):
        parent = os.path.dirname(path)
        if parent and not _looks_like_node_bin_dir(parent):
            return parent
        return None

    if os.path.isdir(os.path.join(path, "data")) or not os.listdir(path):
        return path

    return None


def _cline_verbose_enabled() -> bool:
    """Return whether Cline should stream verbose reasoning/progress logs."""
    return _coerce_bool(os.getenv(CLINE_VERBOSE_ENV_VAR), default=False)


def _coerce_timeout_seconds(value) -> int:
    """Normalize timeout configuration to a positive integer number of seconds."""
    try:
        timeout_seconds = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid agent timeout '{value}'. Expected a positive integer number of seconds")
    if timeout_seconds <= 0:
        raise ValueError(f"Invalid agent timeout '{value}'. Expected a positive integer number of seconds")
    return timeout_seconds


def _runtime_process_timeout_seconds(runtime: str, runtime_timeout_seconds: int) -> int:
    """Return the parent subprocess timeout for a runtime invocation."""
    if _normalize_agent_runtime(runtime) != "cline":
        return runtime_timeout_seconds
    # Cline has its own --timeout and needs room to flush output, persist state,
    # and exit after its internal task timeout fires.
    grace = max(30, min(300, int(runtime_timeout_seconds * 0.10)))
    return runtime_timeout_seconds + grace


def _terminate_process_group(proc, grace_seconds: float = 10.0) -> None:
    """Terminate a detached subprocess session, escalating to SIGKILL if needed."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass

    try:
        proc.wait(timeout=grace_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    except AttributeError:
        pass

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass

    try:
        proc.wait(timeout=grace_seconds)
    except Exception:
        pass


def _should_stream_output_via_pty(runtime: str) -> bool:
    """Use a PTY for runtimes whose incremental output is otherwise buffered."""
    return _normalize_agent_runtime(runtime) == "claude-code"


def _start_pty_output_pump(master_fd: int, log_file, stats: dict) -> threading.Thread:
    """Mirror PTY output into the run log while tracking first/last output timing."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def _pump() -> None:
        try:
            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    stats["output_capture_error"] = str(exc)
                    break

                if not chunk:
                    break

                now = datetime.now(timezone.utc).isoformat()
                if not stats.get("first_output_at"):
                    stats["first_output_at"] = now
                stats["last_output_at"] = now
                stats["output_bytes"] = int(stats.get("output_bytes") or 0) + len(chunk)

                text = decoder.decode(chunk)
                if text:
                    log_file.write(text)
                    log_file.flush()

            tail = decoder.decode(b"", final=True)
            if tail:
                log_file.write(tail)
                log_file.flush()
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass

    thread = threading.Thread(target=_pump, daemon=True)
    thread.start()
    return thread


def _runtime_reported_timeout(runtime: str, output: str, returncode: int) -> bool:
    """Detect runtime-native timeout failures that exit before our parent timeout."""
    if returncode == 0 or _normalize_agent_runtime(runtime) != "cline":
        return False
    normalized_output = str(output or "").lower()
    return "error: timeout" in normalized_output or (
        "timeout" in normalized_output and '"message"' in normalized_output
    )


def _runtime_reported_failure(runtime: str, output: str) -> bool:
    """Detect fatal runtime-generated errors surfaced in command output."""
    if _normalize_agent_runtime(runtime) != "copilot":
        return False

    for line in str(output or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Execution failed:"):
            return True
        if stripped.startswith("Authorization error"):
            return True
    return False


def _build_runtime_command(
    runtime: str,
    runtime_path: str,
    task_prompt: str,
    system_prompt: str,
    model: str,
    effort,
    timeout_seconds: int,
    task_cwd: str,
):
    """Build the subprocess command and effective prompt for the selected runtime."""
    runtime_norm = _normalize_agent_runtime(runtime)

    if runtime_norm == "cline":
        effective_prompt = _build_cline_prompt(task_prompt, system_prompt)
        cmd = [
            runtime_path,
            "-c",
            task_cwd,
            "-m",
            model,
            "--timeout",
            str(timeout_seconds),
        ]
        cmd.extend(["--auto-approve", "true"])
        if _cline_verbose_enabled():
            cmd.append("--verbose")
        cline_config_dir = _resolve_cline_config_dir()
        if cline_config_dir:
            cmd.extend(["--config", cline_config_dir])
        thinking_level = _cline_thinking_level(effort)
        if thinking_level:
            cmd.extend(["--thinking", thinking_level])
        cmd.append(effective_prompt)
        return cmd, effective_prompt

    if runtime_norm == "copilot":
        effective_prompt = _build_copilot_prompt(task_prompt, system_prompt)
        cmd = [runtime_path]
        if os.path.basename(runtime_path) == "gh":
            cmd.append("copilot")
        cmd.extend(
            [
                "-p", effective_prompt,
                "--model", model,
                "--output-format", "text",
                "--silent",
                "--allow-all",
                "--no-ask-user",
            ]
        )
        copilot_effort = _copilot_effort_value(effort)
        if copilot_effort:
            cmd.extend(["--effort", copilot_effort])
        return cmd, effective_prompt

    cmd = [
        runtime_path,
        "-p", task_prompt,
        "--append-system-prompt", system_prompt,
        "--model", model,
        "--output-format", "text",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    if effort:
        cmd.extend(["--effort", effort])
    return cmd, task_prompt


def _agents_dir(base_dir: str) -> str:
    override = os.getenv(AGENTS_DIR_ENV_VAR)
    if override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(override)))

    workspace_agents_dir = os.path.join(base_dir, "Agents")
    if os.path.isdir(workspace_agents_dir):
        return workspace_agents_dir

    source_agents_dir = os.path.join(SOURCE_DIR, "Agents")
    if os.path.isdir(source_agents_dir):
        return source_agents_dir

    return workspace_agents_dir


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
        "agent_type": _normalize_agent_role(header.get("x-role", header.get("x-agent-type", "worker"))),
        "tools": header.get("x-tools", []),
        "learning_enabled": _coerce_bool(header.get("x-learning", True), default=True),
        "own_worktree": _coerce_bool(header.get("x-own-worktree", False), default=False),
        "timeout": header.get("x-timeout"),
        "model": header.get("x-model"),
        "model_level": header.get("x-model-level"),
        "effort": header.get("x-effort"),
        "runtime": header.get("x-runtime"),
        "sound_start": header.get("x-sound-start"),
        "sound_start_defined": "x-sound-start" in header,
        "sound_finish": header.get("x-sound-finish"),
        "sound_finish_defined": "x-sound-finish" in header,
        "sound_error": header.get("x-sound-error"),
        "sound_error_defined": "x-sound-error" in header,
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
    cli = _orchestration_cli_command(base_dir)

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
        f"Use `{cli} <command>` to manage tasks, workstreams, artifacts, etc.\n"
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
        "- `artifact create --path '<path>' --content '<content>' --workstream '<workstream_id>'` — save a text artifact\n"
        "- `artifact create --path '<image_path>' --source-file '<local_file>' --workstream '<workstream_id>'` — save a binary image artifact from a local file\n"
        "- `artifact create --path '<image_path>' --content-base64 '<base64>' --workstream '<workstream_id>'` — save a binary image artifact when you only have base64 bytes\n"
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


def _classify_run_outcome(returncode: int, timeout_expired: bool, runtime_failed: bool = False) -> str:
    """Map subprocess result to persisted run status."""
    if timeout_expired:
        return "timeout"
    if runtime_failed:
        return "failed"
    if returncode == 0:
        return "completed"

    # Treat termination signals as killed so UI/operator intent is preserved.
    if returncode in (-signal.SIGTERM, 128 + signal.SIGTERM, -signal.SIGKILL, 128 + signal.SIGKILL):
        return "killed"

    return "failed"


def run_agent(agent_name: str, task_ids: list = None, workstream_id: str = None, prompt: str = None, timeout: int = None, base_dir: str = ".", allow_paused_workstream: bool = False, retried_from_run_id: str = None, _run_id: str = None, concurrency_state: str = None) -> dict:
    """Run an agent via the configured local runtime against 0-N tasks.

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
        concurrency_state: Optional state bucket used for agent concurrency.
    """
    if task_ids is None:
        task_ids = []

    agent_file = _resolve_agent_file(agent_name, base_dir)
    agent_def = _parse_agent_md(agent_file)
    runtime = _resolve_agent_runtime(agent_def)
    runtime_path = _resolve_runtime_executable(runtime)

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
            _play_agent_sound(agent_def, "error", base_dir)
            raise RuntimeError(f"Workstream '{ws.name}' is paused")

    # Preflight: block obviously invalid image attachments before invoking the runtime.
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

        _play_agent_sound(agent_def, "error", base_dir)
        raise RuntimeError(
            "Invalid image attachments detected; refusing to start agent run.\n"
            + "\n".join(details)
        )

    # Write task IDs to a temp file for the agent to reference (always, even for single task)
    task_file_path = None
    run_id = _run_id or str(uuid.uuid4())
    log_path_rel = None
    run_meta = None
    provider_context_snapshot = None
    owned_worktree = None
    fd, task_file_path = tempfile.mkstemp(suffix=".json", prefix="agent_tasks_")
    with os.fdopen(fd, "w") as f:
        json.dump({"task_ids": task_ids, "workstream_id": workstream_id}, f)

    try:
        # Build system prompt from agent definition + tool docs
        system_prompt = _build_system_prompt(agent_def, base_dir)
        cli = _orchestration_cli_command(base_dir)

        # Compute path context for prompt injection
        orchestration_root = os.path.abspath(base_dir)
        workspace_root = orchestration_root
        if ws:
            try:
                workspace_root = os.path.abspath(
                    resolve_workstream_workspace(ws.id, base_dir=base_dir)
                )
            except Exception:
                workspace_root = orchestration_root
        if agent_def.get("own_worktree"):
            owned_worktree = _provision_run_worktree(workspace_root, run_id, base_dir=base_dir)
            workspace_root = owned_worktree["workspace_root"]
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
                    f"1. Run: {cli} task read {task.id}\n"
                    f"2. Read the full task payload (description, comments, audit, attachments).\n"
                    f"3. For each attachment/path referenced in the task payload, run: {cli} artifact read \"<artifact_path>\" --workstream {ws.id}\n"
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
                    f"1. Run: {cli} task list {ws.id} or read each task ID from the Task IDs file.\n"
                    f"2. For each task ID, run: {cli} task read <task_id>\n"
                    f"3. Read the full task payload for each task (description, comments, audit, attachments).\n"
                    f"4. For each attachment/path referenced by any task payload, run: {cli} artifact read \"<artifact_path>\" --workstream {ws.id}\n"
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
                f"2. Before you begin work on any specific task, acquire a lock through the orchestration system: {cli} lock acquire <task_id> --agent \"<agent_name>\"\n"
                "3. If a task is already locked or lock acquisition fails, skip that task.\n"
                f"4. While you hold a task lock, complete the needed work, then release it when finished: {cli} lock release <task_id> --agent \"<agent_name>\"\n"
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
        model = _resolve_agent_model(agent_def, runtime=runtime)
        effort = _resolve_agent_effort(agent_def, runtime=runtime)
        effective_timeout = _coerce_timeout_seconds(
            timeout or agent_def.get("timeout") or DEFAULT_AGENT_TIMEOUT
        )

        # Initialize run metadata
        _ensure_state_dirs(base_dir)
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
        if owned_worktree:
            env["ORCHESTRATION_AGENT_WORKTREE_ROOT"] = owned_worktree["worktree_root"]
        if workstream_id:
            env["ORCHESTRATION_AGENT_WORKSTREAM_ID"] = str(workstream_id)
        if runtime == "copilot":
            # Copilot CLI authenticates with GitHub tokens/session state. Do not
            # forward an ambient OpenAI key into the child process.
            env.pop("OPENAI_API_KEY", None)
        abs_base = os.path.abspath(base_dir)
        if runtime == "cline" and workspace_root != abs_base:
            existing_pythonpath = str(env.get("PYTHONPATH", "") or "")
            python_paths = [SOURCE_DIR, abs_base]
            if existing_pythonpath:
                python_paths.append(existing_pythonpath)
            env["PYTHONPATH"] = os.pathsep.join(python_paths)

        task_titles = [{"id": t.id, "title": t.title} for t in tasks]
        run_meta = {
            "run_id": run_id,
            "agent": agent_def["name"],
            "agent_ref": agent_name,
            "runtime": runtime,
            "model": model,
            "effort": effort,
            "workstream_id": workstream_id,
            "workstream_path": _workstream_path(base_dir, workstream_id),
            "workspace_root": workspace_root,
            "concurrency_state": concurrency_state,
            "task_ids": list(task_ids),
            "tasks": task_titles,
            "prompt": task_prompt,
            "system_prompt": system_prompt,
            "log_path": log_path_rel,
            "own_worktree": bool(owned_worktree),
            "started_at": started_at,
            "ended_at": None,
            "status": "running",
            "exit_code": None,
            "retried_from_run_id": retried_from_run_id,
            "retried_to_run_ids": [],
        }
        if owned_worktree:
            run_meta["worktree_root"] = owned_worktree["worktree_root"]
        _write_run_meta(base_dir, run_id, run_meta)

        process_cwd = workspace_root if owned_worktree else abs_base
        task_cwd = workspace_root if runtime == "cline" else process_cwd
        cmd, effective_prompt = _build_runtime_command(
            runtime=runtime,
            runtime_path=runtime_path,
            task_prompt=task_prompt,
            system_prompt=system_prompt,
            model=model,
            effort=effort,
            timeout_seconds=effective_timeout,
            task_cwd=task_cwd,
        )
        process_timeout = _runtime_process_timeout_seconds(runtime, effective_timeout)
        command_line = shlex.join(cmd)

        run_meta["command_line"] = command_line
        run_meta["effective_prompt"] = effective_prompt
        _write_run_meta(base_dir, run_id, run_meta)
        provider_context_snapshot = _snapshot_provider_context(runtime)

        # Use a file-backed log so the Workspace Manager can live-tail active agent output.
        timeout_expired = False
        use_pty_output = _should_stream_output_via_pty(runtime)
        stream_stats = {
            "output_bytes": 0,
            "first_output_at": None,
            "last_output_at": None,
            "output_capture_error": None,
        }
        pty_master_fd = None
        pty_pump_thread = None
        with open(log_path, "w", encoding="utf-8") as log_file:
            pty_slave_fd = None
            popen_kwargs = {
                "stderr": subprocess.STDOUT,
                "cwd": process_cwd,
                "env": env,
                # Detach the agent process from the scheduler's process group.
                # This prevents scheduler restarts/stops from accidentally terminating
                # in-flight agent runs that should continue independently.
                "start_new_session": True,
            }
            if use_pty_output:
                pty_master_fd, pty_slave_fd = pty.openpty()
                popen_kwargs["stdout"] = pty_slave_fd
            else:
                popen_kwargs["stdout"] = log_file
                popen_kwargs["text"] = True

            try:
                proc = subprocess.Popen(cmd, **popen_kwargs)
            finally:
                if pty_slave_fd is not None:
                    try:
                        os.close(pty_slave_fd)
                    except OSError:
                        pass

            if use_pty_output and pty_master_fd is not None:
                pty_pump_thread = _start_pty_output_pump(pty_master_fd, log_file, stream_stats)

            _play_agent_sound(agent_def, "start", base_dir)

            active_run = {
                "run_id": run_id,
                "agent": agent_def["name"],
                "agent_ref": agent_name,
                "runtime": runtime,
                "model": model,
                "effort": effort,
                "workstream_id": workstream_id,
                "concurrency_state": concurrency_state,
                "task_ids": list(task_ids),
                "pid": proc.pid,
                "started_at": started_at,
                "log_path": log_path_rel,
            }
            _register_active_agent(base_dir, active_run)

            # Acquire a process-level lock so standalone (non-task) agents can be
            # auto-detected and cleaned up if they hang past their TTL.
            from .locks import acquire_process_lock
            _process_lock_ttl = process_timeout + 300
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
                proc.communicate(timeout=process_timeout)
            except subprocess.TimeoutExpired:
                timeout_expired = True
                _terminate_process_group(proc)
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    pass
            finally:
                _unregister_active_agent(base_dir, run_id)
                from .locks import release_process_lock
                try:
                    release_process_lock(run_id, base_dir=base_dir)
                except Exception:
                    pass
                if pty_pump_thread is not None:
                    pty_pump_thread.join(timeout=5)
                    if pty_pump_thread.is_alive() and pty_master_fd is not None:
                        try:
                            os.close(pty_master_fd)
                        except OSError:
                            pass
                        pty_pump_thread.join(timeout=1)

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
        final_status = _classify_run_outcome(
            returncode,
            timeout_expired or _runtime_reported_timeout(runtime, output, returncode),
            runtime_failed=_runtime_reported_failure(runtime, output),
        )
        output_bytes = int(stream_stats.get("output_bytes") or 0)
        if output_bytes == 0:
            try:
                output_bytes = os.path.getsize(log_path)
            except OSError:
                output_bytes = 0
        run_meta["output_bytes"] = output_bytes
        run_meta["first_output_at"] = stream_stats.get("first_output_at")
        run_meta["last_output_at"] = stream_stats.get("last_output_at")
        if stream_stats.get("output_capture_error"):
            run_meta["output_capture_error"] = stream_stats.get("output_capture_error")
        else:
            run_meta.pop("output_capture_error", None)
        run_meta["ended_at"] = datetime.now(timezone.utc).isoformat()
        run_meta["status"] = final_status
        run_meta["exit_code"] = returncode
        try:
            run_meta["context_capture"] = _capture_provider_context(
                base_dir,
                run_id,
                runtime,
                provider_context_snapshot,
            )
        except Exception as exc:
            run_meta["context_capture"] = {
                "manifest_path": None,
                "context_dir": os.path.relpath(_agent_run_context_dir(base_dir, run_id), base_dir),
                "file_count": 0,
                "warnings": [f"Provider context capture failed: {exc}"],
                "sources": [],
            }
        _write_run_meta(base_dir, run_id, run_meta)

        # Log agent output to audit trail for each task
        stripped_output = _strip_ansi_escape_codes(output)
        for tid in task_ids:
            t = read_task(tid, base_dir)  # Re-read in case agent modified it
            if final_status == "completed":
                audit_output = stripped_output[:MAX_AUDIT_OUTPUT]
                if len(stripped_output) > MAX_AUDIT_OUTPUT:
                    audit_output += f"\n... (truncated, {len(stripped_output)} total chars)"
                t.add_audit("agent_completed", f"Agent '{agent_def['name']}' completed.\n\nOutput:\n{audit_output}")
                # Reset retry count on success
                t.retry_count = 0
                t.last_failure_at = None
            else:
                error_msg = stripped_output[:MAX_AUDIT_OUTPUT]
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

        _play_agent_sound(agent_def, "finish", base_dir)
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

    except Exception:
        _play_agent_sound(agent_def, "error", base_dir)
        raise

    finally:
        if owned_worktree is not None:
            _deprovision_run_worktree(owned_worktree, base_dir=base_dir)
        # Clean up temp file
        if task_file_path and os.path.exists(task_file_path):
            os.remove(task_file_path)
