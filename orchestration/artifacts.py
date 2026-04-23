"""Artifact operations."""

import os


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
