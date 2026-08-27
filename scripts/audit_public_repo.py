#!/usr/bin/env python3
"""Fail when tracked files contain internal paths or private identifiers."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SELF_PATH = Path(__file__).resolve()


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str


def _private_rule(label: str, *parts: str) -> tuple[str, re.Pattern[str]]:
    return label, re.compile(re.escape("".join(parts)), re.IGNORECASE)


RULES = (
    ("macOS home path", re.compile(r"/Users/[A-Za-z0-9._-]+")),
    ("Linux home path", re.compile(r"/home/[A-Za-z0-9._-]+")),
    ("Windows home path", re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+", re.IGNORECASE)),
    _private_rule("private repository name", "found", "ation"),
    _private_rule("private data-root name", "jb_", "workstreams"),
    _private_rule("private user name", "Jere", "my"),
    _private_rule("internal email domain", "platform", "stud.io"),
    _private_rule("internal Mailgun domain", "hire", "scout.us"),
    _private_rule("private organization name", "Platform Venture ", "Studio"),
    _private_rule("private investment directory", "Direct ", "Investments"),
    _private_rule("private research directory", "Unmet ", "Needs"),
    _private_rule("private thesis directory", "The", "ses"),
    _private_rule("private agent name", "kanban_", "ninja"),
    _private_rule("private agent name", "product_feedback_", "loopback"),
    _private_rule("private agent name", "startup_", "vendor"),
)


def scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for label, pattern in RULES:
            if pattern.search(line):
                findings.append(Finding(path=path, line=line_number, rule=label))
    return findings


def tracked_files(repo_root: Path = REPO_ROOT) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return [repo_root / raw.decode("utf-8") for raw in result.stdout.split(b"\0") if raw]


def audit_repo(repo_root: Path = REPO_ROOT) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    skipped_binary: list[str] = []
    for path in tracked_files(repo_root):
        if path.resolve() == SELF_PATH:
            continue
        relative_path = path.relative_to(repo_root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped_binary.append(relative_path)
            continue
        findings.extend(scan_text(relative_path, text))
    return findings, skipped_binary


def main() -> int:
    findings, skipped_binary = audit_repo()
    for finding in findings:
        print(f"{finding.path}:{finding.line}: {finding.rule}")
    if skipped_binary:
        print("Binary files skipped: " + ", ".join(skipped_binary))
    if findings:
        print(f"Public repository audit failed with {len(findings)} finding(s).")
        return 1
    print("Public repository audit passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())