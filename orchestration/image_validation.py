"""Utilities for validating image attachments before agent runs."""

import os
from typing import Any

from .artifacts import _resolve_artifact_root, _validate_path

_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".svg",
}


def _is_image_path(path: str) -> bool:
    return os.path.splitext(str(path or ""))[1].lower() in _IMAGE_EXTENSIONS


def _is_valid_svg(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(2048).lower()
    except OSError:
        return False
    return "<svg" in head


def _detect_raster_magic(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except OSError:
        return None

    if len(header) >= 8 and header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(header) >= 3 and header[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if len(header) >= 6 and (header.startswith(b"GIF87a") or header.startswith(b"GIF89a")):
        return "gif"
    return None


def validate_image_artifact(path: str, *, min_size_bytes: int = 64) -> str | None:
    """Return None when valid, otherwise a human-readable validation error."""
    ext = os.path.splitext(path)[1].lower()

    if not os.path.exists(path):
        return "file does not exist"
    if not os.path.isfile(path):
        return "path is not a regular file"

    size = os.path.getsize(path)
    if size <= 0:
        return "file is empty"
    if ext != ".svg" and size < min_size_bytes:
        return f"file is too small to be a real image ({size} bytes)"

    if ext == ".svg":
        return None if _is_valid_svg(path) else "invalid SVG payload"

    detected = _detect_raster_magic(path)
    if detected is None:
        return "unrecognized image header/magic bytes"

    expected = "jpeg" if ext in {".jpg", ".jpeg"} else ext.lstrip(".")
    if detected != expected:
        return f"header mismatch: extension expects {expected}, file is {detected}"

    return None


def validate_task_image_attachments(task: Any, *, base_dir: str) -> list[dict[str, str]]:
    """Validate image attachments on a task and return issue records.

    Each issue has keys: path, full_path, reason.
    """
    issues = []
    attachments = list(getattr(task, "attachments", []) or [])
    workstream_id = getattr(task, "workstream_id", None)

    for attachment_path in attachments:
        if not _is_image_path(attachment_path):
            continue

        root, rel_path = _resolve_artifact_root(
            str(attachment_path), base_dir=base_dir, workstream_id=workstream_id
        )
        full_path = _validate_path(root, rel_path)
        reason = validate_image_artifact(full_path)
        if reason:
            issues.append(
                {
                    "path": str(attachment_path),
                    "full_path": full_path,
                    "reason": reason,
                }
            )

    return issues
