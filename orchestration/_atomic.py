"""Crash-safe file writers used across the orchestration package.

Why this module exists
----------------------
Several writers in the orchestration package used to open the destination
file directly with ``open(path, "w")`` (or a file descriptor returned from
``os.open``) and then dump content into it. That pattern is **not** safe
against a process kill mid-write: the destination is truncated to zero
bytes by ``open(path, "w")`` *before* the dump even starts, so a
``SIGKILL``, OOM kill, or laptop restart between the open and the dump
finishing leaves a zero-byte or partial file on disk. If the file holds
load-bearing state (a task definition, the active-agents index, the
scheduler's last tick, a workstream's ``.env``), the orchestrator then
fails to parse it on the next read and reports the file as corrupted —
the exact pattern we observed in June 2026 on task
``5bf75407-9c46-4332-8494-ca7c08d0f3ae`` and ``fe815e75-1efe-4ce5-a5b0-f31c25e97323``.

The fix is the standard "write to a tempfile in the same directory, flush
and fsync, then atomically rename" pattern. ``os.replace`` is atomic on
POSIX and on Windows when both paths are on the same filesystem, so any
mid-write kill leaves the *previous* good copy in place and either
nothing else (best case) or an orphaned ``.tmp`` (recoverable on the next
read).
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Callable, Optional

import yaml


def _atomic_write(
    path: str,
    writer: Callable[[Any], None],
    *,
    encoding: str = "utf-8",
) -> None:
    """Write ``path`` atomically by routing ``writer`` through a tempfile.

    ``writer`` is called with an open file object as its single argument
    and is expected to fully populate the file before returning. The temp
    file is created in the same directory as the destination so that
    ``os.replace`` is a same-filesystem rename (atomic).

    A ``BaseException`` during the write (including ``KeyboardInterrupt``
    and ``SystemExit`` from a hard-killed agent run) is propagated *after*
    best-effort cleanup of the temp file, so the destination directory
    is left tidy even on crashes.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    # Use a generic ".atomic." prefix so a previous crash-orphaned temp
    # file is recognizable as a partial write (e.g. ".atomic.abc123.tmp").
    fd, temp_path = tempfile.mkstemp(prefix=".atomic.", suffix=".tmp", dir=directory)
    try:
        # ``os.fdopen`` takes ownership of ``fd`` and closes it on exit.
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            writer(handle)
            try:
                handle.flush()
                os.fsync(handle.fileno())
            except OSError:
                # Some filesystems (rare) don't support fsync. The flush
                # already pushed the data to the OS; we'll accept the
                # weaker guarantee rather than failing the whole write.
                pass
        os.replace(temp_path, path)
    except BaseException:
        # Catch BaseException (not just Exception) so KeyboardInterrupt /
        # SystemExit still clean up. The destination is untouched because
        # we haven't called os.replace yet.
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def atomic_write_yaml(
    path: str,
    data: Any,
    *,
    default_flow_style: bool = False,
    sort_keys: bool = False,
    encoding: str = "utf-8",
) -> None:
    """Write a YAML document to ``path`` atomically.

    Mirrors the call signature of ``yaml.dump`` so it can be a drop-in
    replacement in any site that currently does::

        with open(path, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    """
    def _write(handle: Any) -> None:
        yaml.dump(
            data,
            handle,
            default_flow_style=default_flow_style,
            sort_keys=sort_keys,
        )
    _atomic_write(path, _write, encoding=encoding)


def atomic_write_text(
    path: str,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Write a string to ``path`` atomically.

    Used for plain-text state files (e.g. workstream ``.env``) that need
    the same crash-safety as the YAML writers.
    """
    def _write(handle: Any) -> None:
        handle.write(text)
    _atomic_write(path, _write, encoding=encoding)


def atomic_write_json(
    path: str,
    data: Any,
    *,
    indent: Optional[int] = 2,
    ensure_ascii: bool = True,
    encoding: str = "utf-8",
) -> None:
    """Write a JSON document to ``path`` atomically.

    Mirrors ``json.dump`` so it can be a drop-in replacement for
    ``with open(path, "w") as f: json.dump(data, f, indent=2)``.
    """
    def _write(handle: Any) -> None:
        json.dump(data, handle, indent=indent, ensure_ascii=ensure_ascii)
    _atomic_write(path, _write, encoding=encoding)


__all__ = ["atomic_write_json", "atomic_write_text", "atomic_write_yaml"]
