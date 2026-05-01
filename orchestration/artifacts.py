"""Artifact operations."""

import filecmp
import os
import shutil


def _normalize_name(value: str) -> str:
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _artifacts_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "artifacts")


def _resolve_artifact_root(path: str, base_dir: str = ".", workstream_id: str = None) -> tuple:
    """Resolve (artifacts_root, relative_path) with mounted workspace awareness.

    If workstream_id is provided, the artifact root is the workspace root that owns
    that workstream plus `/artifacts`.
    Without workstream_id, a best-effort mapping is applied: if the logical path
    contains a mounted workstream node name, the path remainder after that node is
    rooted at the mounted workspace's `/artifacts` directory.
    """
    from .workstreams import read_workstream, _workspace_root_for, list_workstreams, _normalize_mounted_workspace_path

    rel_path = path.lstrip("/")
    base_abs = os.path.abspath(base_dir)

    if workstream_id:
        ws = read_workstream(workstream_id, base_dir=base_dir)
        ws_root = _workspace_root_for(ws, base_dir)
        return _artifacts_dir(ws_root), rel_path

    # Best-effort path-based mounted node mapping.
    parts = [p for p in rel_path.split("/") if p]
    if not parts:
        return _artifacts_dir(base_abs), rel_path

    by_norm_name = {}
    for ws in list_workstreams(base_dir=base_dir):
        norm_name = _normalize_name(ws.name)
        if norm_name not in by_norm_name:
            by_norm_name[norm_name] = []
        by_norm_name[norm_name].append(ws)

    for idx, part in enumerate(parts):
        candidates = by_norm_name.get(_normalize_name(part), [])
        for ws in candidates:
            if not ws.mounted_workspace_path:
                continue
            ws_root = _workspace_root_for(ws, base_dir)
            target_workspace = _normalize_mounted_workspace_path(ws.mounted_workspace_path, ws_root)
            suffix = "/".join(parts[idx + 1:])
            return _artifacts_dir(target_workspace), suffix

    return _artifacts_dir(base_abs), rel_path


def _validate_path(artifacts_dir: str, path: str) -> str:
    """Validate and resolve artifact path, preventing path traversal."""
    full_path = os.path.normpath(os.path.join(artifacts_dir, path))
    if not full_path.startswith(os.path.normpath(artifacts_dir)):
        raise ValueError("Path traversal not allowed")
    return full_path


def create_artifact(path: str, content: str, base_dir: str = ".", workstream_id: str = None) -> dict:
    artifacts_dir, rel_path = _resolve_artifact_root(path, base_dir=base_dir, workstream_id=workstream_id)
    full_path = _validate_path(artifacts_dir, rel_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w") as f:
        f.write(content)
    return {"path": path, "created": True}


def read_artifact(path: str, base_dir: str = ".", workstream_id: str = None) -> str:
    artifacts_dir, rel_path = _resolve_artifact_root(path, base_dir=base_dir, workstream_id=workstream_id)
    full_path = _validate_path(artifacts_dir, rel_path)
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"Artifact not found: {path}")
    with open(full_path) as f:
        return f.read()


def list_artifacts(prefix: str = None, base_dir: str = ".", workstream_id: str = None) -> list:
    if workstream_id:
        artifacts_dir, _ = _resolve_artifact_root("", base_dir=base_dir, workstream_id=workstream_id)
    else:
        artifacts_dir = _artifacts_dir(base_dir)
    if not os.path.exists(artifacts_dir):
        return []
    result = []
    for root, _dirs, files in os.walk(artifacts_dir):
        for fname in sorted(files):
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, artifacts_dir)
            if prefix is None or rel.startswith(prefix):
                result.append(rel)
    return sorted(result)


def copy_artifact_tree(
    source_prefix: str,
    *,
    base_dir: str = ".",
    workstream_id: str,
    source_base_dir: str = None,
    overwrite: bool = False,
    dry_run: bool = False,
) -> dict:
    """Copy an artifact subtree from a source workspace into a mounted destination workspace.

    The destination is resolved from the provided workstream context and must be
    mounted outside the current workspace root. Existing files with different
    contents are treated as conflicts unless ``overwrite=True``.
    """
    from .workstreams import resolve_workstream_workspace

    if not workstream_id:
        raise ValueError("workstream_id is required for copytree")

    source_rel = source_prefix.lstrip("/")
    if not source_rel:
        raise ValueError("source_prefix must not be empty")

    base_abs = os.path.abspath(base_dir)
    destination_workspace = resolve_workstream_workspace(workstream_id, base_dir=base_dir)
    if os.path.abspath(destination_workspace) == os.path.abspath(base_abs):
        raise ValueError(
            f"Workstream {workstream_id} is not mounted to a separate workspace; mount it before running artifact copytree"
        )

    source_root_base = os.path.abspath(source_base_dir) if source_base_dir else base_abs
    source_root = _artifacts_dir(source_root_base)
    destination_root, _ = _resolve_artifact_root("", base_dir=base_dir, workstream_id=workstream_id)

    source_tree = _validate_path(source_root, source_rel)
    if not os.path.exists(source_tree):
        raise FileNotFoundError(f"Artifact source subtree not found: {source_prefix}")
    if not os.path.isdir(source_tree):
        raise ValueError(f"Artifact source must be a directory tree: {source_prefix}")

    destination_tree = _validate_path(destination_root, source_rel)

    planned = []
    conflicts = []
    same_content = []
    for root, _dirs, files in os.walk(source_tree):
        for fname in files:
            src_file = os.path.join(root, fname)
            rel_file = os.path.relpath(src_file, source_tree)
            dst_file = os.path.join(destination_tree, rel_file)
            if os.path.exists(dst_file):
                if filecmp.cmp(src_file, dst_file, shallow=False):
                    same_content.append(rel_file)
                    continue
                if not overwrite:
                    conflicts.append(rel_file)
                    continue
                planned.append((src_file, dst_file, "overwrite"))
            else:
                planned.append((src_file, dst_file, "new"))

    if conflicts:
        conflict_preview = ", ".join(conflicts[:5])
        more = "" if len(conflicts) <= 5 else f" (+{len(conflicts) - 5} more)"
        raise FileExistsError(
            f"artifact copytree would overwrite {len(conflicts)} existing file(s). "
            f"Use overwrite mode to proceed. Conflicts: {conflict_preview}{more}"
        )

    copied = []
    overwritten = []
    if not dry_run:
        for src_file, dst_file, mode in planned:
            os.makedirs(os.path.dirname(dst_file), exist_ok=True)
            shutil.copy2(src_file, dst_file)
            if mode == "overwrite":
                overwritten.append(os.path.relpath(dst_file, destination_tree))
            else:
                copied.append(os.path.relpath(dst_file, destination_tree))

    return {
        "source_prefix": source_rel,
        "source_artifacts_root": source_root,
        "destination_artifacts_root": destination_root,
        "destination_workstream_id": workstream_id,
        "dry_run": dry_run,
        "overwrite": overwrite,
        "copied": [] if dry_run else sorted(copied),
        "overwritten": [] if dry_run else sorted(overwritten),
        "skipped_same": sorted(same_content),
        "copy_count": sum(1 for _ in planned if _[2] == "new"),
        "overwrite_count": sum(1 for _ in planned if _[2] == "overwrite"),
        "skipped_same_count": len(same_content),
    }
