"""Persistence root resolution helpers.

Supports URI-based persistence roots so state and artifacts can move off the
repository without changing the code workspace location.
"""

import os
import ntpath
from urllib.parse import unquote, urlparse


WORKSTREAM_ROOT_ENV = "WORKSTREAM_ROOT"
ARTIFACT_ROOT_ENV = "ARTIFACT_ROOT"
ARTIFACT_ROOT_LEGACY_ENV = "ARTICACT_ROOT"


def _default_root(base_dir: str) -> str:
    return os.path.abspath(os.path.expanduser(base_dir))


def _resolve_file_target(raw: str, *, env_name: str) -> str:
    parsed = urlparse(raw)
    windows_drive, _ = ntpath.splitdrive(raw)
    if parsed.scheme not in ("", "file") and not windows_drive:
        raise ValueError(f"{env_name} uses unsupported URI scheme '{parsed.scheme}'")

    if parsed.scheme == "" or windows_drive:
        candidate = raw
    else:
        if parsed.netloc not in ("", "localhost"):
            raise ValueError(f"{env_name} file URI host must be empty or localhost")
        candidate = parsed.path or ""
        if not candidate and parsed.netloc:
            candidate = parsed.netloc

    candidate = unquote(candidate).strip()
    if not candidate:
        raise ValueError(f"{env_name} must not be empty")

    expanded = os.path.expanduser(candidate)
    return os.path.abspath(expanded)


def _resolve_root(raw_value: str | None, *, env_name: str, default_base_dir: str) -> str:
    if raw_value is None or str(raw_value).strip() == "":
        return _default_root(default_base_dir)
    return _resolve_file_target(str(raw_value).strip(), env_name=env_name)


def resolve_workstream_root(base_dir: str = ".") -> str:
    """Return the root directory that persists workstreams, tasks, locks, and audits."""
    return _resolve_root(
        os.getenv(WORKSTREAM_ROOT_ENV),
        env_name=WORKSTREAM_ROOT_ENV,
        default_base_dir=base_dir,
    )


def resolve_artifact_root(base_dir: str = ".") -> str:
    """Return the root directory that persists orchestration artifacts."""
    raw_value = os.getenv(ARTIFACT_ROOT_ENV)
    if raw_value is None:
        raw_value = os.getenv(ARTIFACT_ROOT_LEGACY_ENV)
    return _resolve_root(raw_value, env_name=ARTIFACT_ROOT_ENV, default_base_dir=base_dir)