"""Artifact operations."""

import os


def _artifacts_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "artifacts")


def _validate_path(artifacts_dir: str, path: str) -> str:
    """Validate and resolve artifact path, preventing path traversal."""
    full_path = os.path.normpath(os.path.join(artifacts_dir, path))
    if not full_path.startswith(os.path.normpath(artifacts_dir)):
        raise ValueError("Path traversal not allowed")
    return full_path


def create_artifact(path: str, content: str, base_dir: str = ".") -> dict:
    artifacts_dir = _artifacts_dir(base_dir)
    full_path = _validate_path(artifacts_dir, path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, "w") as f:
        f.write(content)
    return {"path": path, "created": True}


def read_artifact(path: str, base_dir: str = ".") -> str:
    artifacts_dir = _artifacts_dir(base_dir)
    full_path = _validate_path(artifacts_dir, path)
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"Artifact not found: {path}")
    with open(full_path) as f:
        return f.read()


def list_artifacts(prefix: str = None, base_dir: str = ".") -> list:
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
