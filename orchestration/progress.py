"""Run-scoped agent progress checklist operations."""

import os
import re
from datetime import datetime, timezone
import yaml

from .persistence import resolve_workstream_root


VALID_STATUSES = {"pending", "active", "done", "blocked", "skipped"}
TERMINAL_STATUSES = {"done", "blocked", "skipped"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_dir(base_dir: str = ".") -> str:
    return os.path.join(resolve_workstream_root(base_dir), ".orchestration")


def progress_path(run_id: str, base_dir: str = ".") -> str:
    run = str(run_id or "").strip()
    if not run:
        raise ValueError("run_id is required")
    return os.path.join(_state_dir(base_dir), "agent_runs", run, "progress.yaml")


def _default_run_id() -> str | None:
    return os.getenv("ORCHESTRATION_AGENT_RUN_ID") or None


def _default_workstream_id() -> str | None:
    return os.getenv("ORCHESTRATION_AGENT_WORKSTREAM_ID") or None


def _default_agent_name() -> str | None:
    return os.getenv("ORCHESTRATION_AGENT_NAME") or None


def _default_task_ids() -> list[str]:
    raw = os.getenv("ORCHESTRATION_AGENT_TASK_IDS") or ""
    if not raw:
        return []
    try:
        loaded = yaml.safe_load(raw)
    except Exception:
        loaded = None
    if isinstance(loaded, list):
        return [str(item) for item in loaded if str(item or "").strip()]
    return [part.strip() for part in raw.split(",") if part.strip()]


def _slugify_item_id(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:48].strip("-") or fallback


def _normalize_item(raw, index: int, seen: set[str]) -> dict:
    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("title") or "").strip()
        item_id = str(raw.get("id") or "").strip()
        status = str(raw.get("status") or "pending").strip().lower()
    else:
        text = str(raw or "").strip()
        item_id = ""
        status = "pending"
    if not text:
        raise ValueError("Progress items require non-empty text")
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid progress status '{status}'")

    base_id = item_id or _slugify_item_id(text, f"item-{index + 1}")
    candidate = base_id
    suffix = 2
    while candidate in seen:
        candidate = f"{base_id}-{suffix}"
        suffix += 1
    seen.add(candidate)

    item = {
        "id": candidate,
        "text": text,
        "status": status,
    }
    for field in ("created_at", "started_at", "completed_at", "updated_at", "message"):
        value = raw.get(field) if isinstance(raw, dict) else None
        if value is not None:
            item[field] = value
    return item


def _normalize_items(items) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    seen = set()
    normalized = [_normalize_item(item, index, seen) for index, item in enumerate(items)]
    active_count = sum(1 for item in normalized if item.get("status") == "active")
    if active_count > 1:
        raise ValueError("Only one progress item can be active")
    return normalized


def _write_progress(progress: dict, base_dir: str = ".") -> dict:
    path = progress_path(progress.get("run_id"), base_dir=base_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(progress, handle, sort_keys=False, default_flow_style=False)
    return progress


def read_progress(run_id: str = None, base_dir: str = ".") -> dict | None:
    resolved_run_id = run_id or _default_run_id()
    path = progress_path(resolved_run_id, base_dir=base_dir)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Progress file for run '{resolved_run_id}' must contain a YAML mapping")
    data.setdefault("run_id", resolved_run_id)
    data.setdefault("items", [])
    data.setdefault("events", [])
    return data


def init_progress(
    items: list,
    *,
    run_id: str = None,
    workstream_id: str = None,
    task_ids: list[str] = None,
    agent: str = None,
    base_dir: str = ".",
) -> dict:
    resolved_run_id = run_id or _default_run_id()
    if not resolved_run_id:
        raise ValueError("run_id is required outside an agent run")
    now = _now_iso()
    normalized_items = _normalize_items(items)
    for item in normalized_items:
        item.setdefault("created_at", now)
        item.setdefault("updated_at", now)
        if item["status"] == "active":
            item.setdefault("started_at", now)
        if item["status"] in TERMINAL_STATUSES:
            item.setdefault("completed_at", now)

    progress = {
        "version": 1,
        "run_id": resolved_run_id,
        "workstream_id": workstream_id or _default_workstream_id(),
        "task_ids": list(task_ids if task_ids is not None else _default_task_ids()),
        "agent": agent or _default_agent_name(),
        "status": "active",
        "current_item_id": next((item["id"] for item in normalized_items if item["status"] == "active"), None),
        "created_at": now,
        "updated_at": now,
        "items": normalized_items,
        "events": [{"timestamp": now, "type": "created", "message": "Progress checklist created"}],
    }
    return _write_progress(progress, base_dir=base_dir)


def add_progress_items(
    items: list,
    *,
    run_id: str = None,
    base_dir: str = ".",
) -> dict:
    resolved_run_id = run_id or _default_run_id()
    if not resolved_run_id:
        raise ValueError("run_id is required outside an agent run")
    progress = read_progress(resolved_run_id, base_dir=base_dir)
    if progress is None:
        raise FileNotFoundError(f"Progress checklist for run '{resolved_run_id}' not found")

    now = _now_iso()
    seen = {str(item.get("id") or "") for item in progress.get("items", []) if item.get("id")}
    existing_count = len(progress.get("items", []))
    normalized_items = [_normalize_item(item, existing_count + index, seen) for index, item in enumerate(items)]
    active_count = sum(1 for item in normalized_items if item.get("status") == "active")
    if active_count > 1:
        raise ValueError("Only one progress item can be active")

    for item in normalized_items:
        item.setdefault("created_at", now)
        item.setdefault("updated_at", now)
        if item["status"] == "active":
            item.setdefault("started_at", now)
            for existing in progress.get("items", []):
                if existing.get("status") == "active":
                    existing["status"] = "pending"
                    existing["updated_at"] = now
            progress["current_item_id"] = item["id"]
        if item["status"] in TERMINAL_STATUSES:
            item.setdefault("completed_at", now)
        progress.setdefault("items", []).append(item)
        progress.setdefault("events", []).append({
            "timestamp": now,
            "type": "item_added",
            "item_id": item["id"],
            "message": item.get("text"),
        })

    progress["updated_at"] = now
    return _write_progress(progress, base_dir=base_dir)


def update_progress_item(
    item_id: str,
    status: str,
    *,
    run_id: str = None,
    message: str = None,
    base_dir: str = ".",
) -> dict:
    resolved_run_id = run_id or _default_run_id()
    if not resolved_run_id:
        raise ValueError("run_id is required outside an agent run")
    next_status = str(status or "").strip().lower()
    if next_status not in VALID_STATUSES:
        raise ValueError(f"Invalid progress status '{next_status}'")
    progress = read_progress(resolved_run_id, base_dir=base_dir)
    if progress is None:
        raise FileNotFoundError(f"Progress checklist for run '{resolved_run_id}' not found")

    target_id = str(item_id or "").strip()
    now = _now_iso()
    found = None
    for item in progress.get("items", []):
        if item.get("id") == target_id:
            found = item
            break
    if found is None:
        raise FileNotFoundError(f"Progress item '{target_id}' not found")

    if next_status == "active":
        for item in progress.get("items", []):
            if item.get("status") == "active" and item.get("id") != target_id:
                item["status"] = "pending"
                item["updated_at"] = now
        found.setdefault("started_at", now)
        progress["current_item_id"] = target_id
    elif progress.get("current_item_id") == target_id:
        progress["current_item_id"] = None

    found["status"] = next_status
    found["updated_at"] = now
    if next_status in TERMINAL_STATUSES:
        found["completed_at"] = now
    if message:
        found["message"] = str(message)

    progress["updated_at"] = now
    progress.setdefault("events", []).append({
        "timestamp": now,
        "type": f"item_{next_status}",
        "item_id": target_id,
        "message": message or found.get("text"),
    })
    return _write_progress(progress, base_dir=base_dir)


def summarize_progress(progress: dict | None) -> dict | None:
    if not progress:
        return None
    items = list(progress.get("items") or [])
    counts = {status: 0 for status in sorted(VALID_STATUSES)}
    for item in items:
        status = str(item.get("status") or "pending").lower()
        counts[status] = counts.get(status, 0) + 1
    active = next((item for item in items if item.get("status") == "active"), None)
    inferred = False
    current = active
    if current is None:
        current = next((item for item in items if item.get("status") == "pending"), None)
        inferred = current is not None
    total = len(items)
    done = counts.get("done", 0)
    return {
        "run_id": progress.get("run_id"),
        "status": progress.get("status") or "active",
        "total": total,
        "done": done,
        "counts": counts,
        "current_item_id": current.get("id") if current else None,
        "current_item": current,
        "inferred_current": inferred,
        "items": items,
        "updated_at": progress.get("updated_at"),
    }


def read_progress_summary(run_id: str = None, base_dir: str = ".") -> dict | None:
    return summarize_progress(read_progress(run_id, base_dir=base_dir))