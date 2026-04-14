"""Workspace-level audit trail.

Records events that aren't tied to a specific task: trigger fires,
scheduler start/stop, workstream pause/resume, etc.

Stored in workspace_audit.yaml at the workspace root, capped at
MAX_ENTRIES to prevent unbounded growth.
"""

import os
import yaml

from .models import now_iso

AUDIT_FILE = "workspace_audit.yaml"
MAX_ENTRIES = 500


def _audit_path(base_dir: str) -> str:
    return os.path.join(base_dir, AUDIT_FILE)


def _load_audit(base_dir: str) -> list:
    path = _audit_path(base_dir)
    if os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, list) else []
    return []


def _save_audit(entries: list, base_dir: str) -> None:
    path = _audit_path(base_dir)
    with open(path, "w") as f:
        yaml.dump(entries, f, default_flow_style=False, sort_keys=False)


def log_event(event_type: str, description: str, base_dir: str = ".", **extra) -> dict:
    """Append a workspace-level audit entry.

    Args:
        event_type: e.g. "scheduler_start", "trigger_fired", "workstream_paused"
        description: Human-readable description of what happened.
        base_dir: Workspace root.
        **extra: Additional key-value pairs to store (e.g. trigger_id, workstream_id).

    Returns:
        The audit entry dict.
    """
    entry = {
        "timestamp": now_iso(),
        "type": event_type,
        "description": description,
    }
    entry.update(extra)

    entries = _load_audit(base_dir)
    entries.append(entry)

    # Cap to last MAX_ENTRIES
    if len(entries) > MAX_ENTRIES:
        entries = entries[-MAX_ENTRIES:]

    _save_audit(entries, base_dir)
    return entry


def get_audit_log(base_dir: str = ".", limit: int = 50, workstream_id: str = None, event_type: str = None) -> list:
    """Return the most recent workspace audit entries.

    Args:
        base_dir: Workspace root.
        limit: Max entries to return (most recent first).
        workstream_id: If set, only return entries for this workstream
                       (plus global events like scheduler_start).
        event_type: If set, only return entries of this type.

    Returns:
        List of audit entry dicts, newest first.
    """
    entries = _load_audit(base_dir)
    if workstream_id:
        entries = [e for e in entries if e.get("workstream_id") == workstream_id
                   or "workstream_id" not in e]
    if event_type:
        entries = [e for e in entries if e.get("type") == event_type]
    return list(reversed(entries[-limit:]))
