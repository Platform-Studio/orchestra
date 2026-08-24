"""Migration helpers for legacy mounted workstream layouts."""

from __future__ import annotations

import filecmp
import functools
import os
import shutil
from pathlib import Path

import yaml

from .models import Workstream
from .persistence import resolve_artifact_root, resolve_workstream_root
from .workstreams import read_workstream, resolve_workstream_artifact_root, resolve_workstream_state_root, save_workstream


IGNORED_ARTIFACT_RELATIVE_PATHS = {
    ".DS_Store",
    "logs/coder_learnings.md",
    "logs/user_stories.md",
}


def _resolve_path(path: str, anchor: str) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(anchor, expanded))


def _load_workstream_records_from_root(root: str) -> dict[str, dict]:
    ws_dir = Path(root) / "workstreams"
    if not ws_dir.exists():
        return {}

    result: dict[str, dict] = {}
    for path in sorted(ws_dir.rglob("*.yaml")):
        if path.parent.name != "workstreams":
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not data:
            continue
        result[data["id"]] = {
            "workstream": Workstream.from_dict(data),
            "state_root": str(path.parent.parent),
            "yaml_path": str(path),
        }
    return result


def _load_workstreams_from_root(root: str) -> dict[str, Workstream]:
    return {ws_id: entry["workstream"] for ws_id, entry in _load_workstream_records_from_root(root).items()}


def _collect_descendant_ids(by_id: dict[str, Workstream], root_id: str) -> list[str]:
    children: dict[str | None, list[str]] = {}
    for ws_id, ws in by_id.items():
        children.setdefault(ws.parent_id, []).append(ws_id)

    for values in children.values():
        values.sort()

    result: list[str] = []
    stack = list(reversed(children.get(root_id, [])))
    while stack:
        ws_id = stack.pop()
        result.append(ws_id)
        for child_id in reversed(children.get(ws_id, [])):
            stack.append(child_id)
    return result


def _compare_or_queue_copy(
    src: Path,
    dst: Path,
    *,
    planned: list[dict],
    conflicts: list[str],
    allow_overwrite: bool,
) -> None:
    rel = str(dst)
    if dst.exists():
        if src.is_file() and dst.is_file() and filecmp.cmp(src, dst, shallow=False):
            return
        if allow_overwrite:
            planned.append({"src": str(src), "dst": str(dst), "overwrite": True})
            return
        conflicts.append(rel)
        return
    planned.append({"src": str(src), "dst": str(dst), "overwrite": False})


def _plan_tree_copy(
    src_root: Path,
    dst_root: Path,
    *,
    planned: list[dict],
    conflicts: list[str],
    allow_overwrite: bool,
    ignore_relative_paths: set[str] | None = None,
    notes: list[str] | None = None,
) -> None:
    if not src_root.exists():
        return
    if src_root.is_file():
        _compare_or_queue_copy(
            src_root,
            dst_root,
            planned=planned,
            conflicts=conflicts,
            allow_overwrite=allow_overwrite,
        )
        return

    for src in sorted(src_root.rglob("*")):
        if src.is_dir():
            continue
        rel_src = src.relative_to(src_root).as_posix()
        if ignore_relative_paths and (rel_src in ignore_relative_paths or Path(rel_src).name in ignore_relative_paths):
            if notes is not None:
                notes.append(f"Skipped artifact during migration: {rel_src}")
            continue
        dst = dst_root / src.relative_to(src_root)
        _compare_or_queue_copy(
            src,
            dst,
            planned=planned,
            conflicts=conflicts,
            allow_overwrite=allow_overwrite,
        )


def migrate_artifact_root_home(
    workstream_id: str,
    *,
    base_dir: str = ".",
    dry_run: bool = True,
    target_root: str | None = None,
    archive_conflicts: bool = False,
) -> dict:
    workspace_root = os.path.abspath(base_dir)
    root_ws = read_workstream(workstream_id, base_dir=workspace_root)
    if root_ws.artifact_root is None:
        raise ValueError(f"Workstream {workstream_id} does not have an explicit artifact_root to migrate")

    source_root = resolve_workstream_artifact_root(workstream_id, base_dir=workspace_root)
    if not os.path.isdir(source_root):
        current_state_root = resolve_workstream_state_root(workstream_id, base_dir=workspace_root)
        current_workstream_home = os.path.join(current_state_root, "workstreams", workstream_id)
        if os.path.isdir(current_workstream_home):
            source_root = current_workstream_home
        else:
            raise FileNotFoundError(f"Artifact root not found: {source_root}")

    default_root = resolve_artifact_root(workspace_root)
    target_root_abs = _resolve_path(target_root, workspace_root) if target_root else default_root

    planned_artifact_files: list[dict] = []
    conflicts: list[str] = []
    notes: list[str] = []
    source_artifacts_root = Path(source_root) / "artifacts"
    destination_artifacts_root = Path(target_root_abs) / "artifacts"
    archive_root = destination_artifacts_root / "_migration_conflicts" / workstream_id

    for src in sorted(source_artifacts_root.rglob("*")):
        if src.is_dir():
            continue
        rel_src = src.relative_to(source_artifacts_root).as_posix()
        if rel_src in IGNORED_ARTIFACT_RELATIVE_PATHS or src.name in IGNORED_ARTIFACT_RELATIVE_PATHS:
            notes.append(f"Skipped artifact during migration: {rel_src}")
            continue

        dst = destination_artifacts_root / rel_src
        if dst.exists():
            if src.is_file() and dst.is_file() and filecmp.cmp(src, dst, shallow=False):
                continue
            if archive_conflicts:
                archived_dst = archive_root / rel_src
                if archived_dst.exists() and src.is_file() and archived_dst.is_file() and filecmp.cmp(src, archived_dst, shallow=False):
                    continue
                if archived_dst.exists():
                    conflicts.append(str(archived_dst))
                    continue
                planned_artifact_files.append({
                    "src": str(src),
                    "dst": str(archived_dst),
                    "overwrite": False,
                    "archived_conflict": True,
                })
                notes.append(f"Archived conflicting artifact: {rel_src} -> _migration_conflicts/{workstream_id}/{rel_src}")
                continue
            conflicts.append(str(dst))
            continue

        planned_artifact_files.append({
            "src": str(src),
            "dst": str(dst),
            "overwrite": False,
        })

    result = {
        "workstream_id": workstream_id,
        "source_artifact_root": source_root,
        "planned_artifact_file_count": len(planned_artifact_files),
        "conflicts": conflicts,
        "notes": notes,
        "dry_run": dry_run,
        "archive_conflicts": archive_conflicts,
        "root_update": {
            "artifact_root": None if os.path.normpath(target_root_abs) == os.path.normpath(default_root) else target_root_abs,
        },
    }

    if dry_run:
        return result

    if conflicts:
        preview = ", ".join(conflicts[:5])
        extra = "" if len(conflicts) <= 5 else f" (+{len(conflicts) - 5} more)"
        raise FileExistsError(f"Artifact migration has {len(conflicts)} conflict(s): {preview}{extra}")

    for entry in planned_artifact_files:
        src = Path(entry["src"])
        dst = Path(entry["dst"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    root_ws.artifact_root = None if os.path.normpath(target_root_abs) == os.path.normpath(default_root) else target_root_abs
    save_workstream(root_ws, base_dir=workspace_root)
    result["applied"] = True
    return result


def migrate_child_workstream_layout(*, base_dir: str = ".", dry_run: bool = True) -> dict:
    workspace_root = os.path.abspath(base_dir)
    state_root = resolve_workstream_root(workspace_root)
    by_id = _load_workstreams_from_root(state_root)

    children: dict[str | None, list[str]] = {}
    for ws_id, ws in by_id.items():
        children.setdefault(ws.parent_id, []).append(ws_id)
    for values in children.values():
        values.sort()

    @functools.lru_cache(maxsize=None)
    def _target_state_root(ws_id: str) -> str:
        ws = by_id[ws_id]
        if not ws.parent_id or ws.parent_id not in by_id:
            return state_root

        parent = by_id[ws.parent_id]
        parent_root = _target_state_root(parent.id)
        configured_child_root = parent.child_workstream_root
        if configured_child_root:
            return _resolve_path(configured_child_root, parent_root)
        return os.path.join(parent_root, "workstreams", parent.id)

    planned_workstream_files: list[dict] = []
    conflicts: list[str] = []
    moved_workstream_ids: list[str] = []

    def _depth(ws_id: str) -> int:
        depth = 0
        current = by_id[ws_id]
        while current.parent_id and current.parent_id in by_id:
            depth += 1
            current = by_id[current.parent_id]
        return depth

    for ws_id in sorted(by_id, key=lambda item: (_depth(item), item)):
        ws = by_id[ws_id]
        if not ws.parent_id or ws.parent_id not in by_id:
            continue

        source_yaml = Path(state_root) / "workstreams" / f"{ws_id}.yaml"
        source_dir = Path(state_root) / "workstreams" / ws_id
        target_root = Path(_target_state_root(ws_id))
        target_yaml = target_root / "workstreams" / f"{ws_id}.yaml"
        target_dir = target_root / "workstreams" / ws_id

        if source_yaml.resolve() == target_yaml.resolve():
            continue

        moved_workstream_ids.append(ws_id)
        _plan_tree_copy(
            source_yaml,
            target_yaml,
            planned=planned_workstream_files,
            conflicts=conflicts,
            allow_overwrite=False,
        )
        _plan_tree_copy(
            source_dir,
            target_dir,
            planned=planned_workstream_files,
            conflicts=conflicts,
            allow_overwrite=False,
        )

    result = {
        "state_root": state_root,
        "moved_workstream_ids": moved_workstream_ids,
        "planned_workstream_file_count": len(planned_workstream_files),
        "conflicts": conflicts,
        "dry_run": dry_run,
    }

    if dry_run:
        return result

    if conflicts:
        preview = ", ".join(conflicts[:5])
        extra = "" if len(conflicts) <= 5 else f" (+{len(conflicts) - 5} more)"
        raise FileExistsError(f"Migration has {len(conflicts)} conflict(s): {preview}{extra}")

    for entry in planned_workstream_files:
        src = Path(entry["src"])
        dst = Path(entry["dst"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    for ws_id in moved_workstream_ids:
        source_yaml = Path(state_root) / "workstreams" / f"{ws_id}.yaml"
        source_dir = Path(state_root) / "workstreams" / ws_id
        if source_yaml.exists():
            source_yaml.unlink()
        if source_dir.exists():
            shutil.rmtree(source_dir)

    result["applied"] = True
    return result