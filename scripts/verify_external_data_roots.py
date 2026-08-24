#!/usr/bin/env python3
"""Verify that Orchestra runtime data is stored outside the code repository.

The check resolves the configured workstream and artifact roots, confirms that
neither points at this repository, and reports legacy runtime files left behind
after migrating data to external directories.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

from orchestration.persistence import resolve_artifact_root, resolve_workstream_root


CANONICAL_WORKSTREAM_PATHS = (
    Path("workstreams"),
    Path("scheduler_state.yaml"),
    Path("workspace_audit.yaml"),
    Path(".orchestration"),
)

CANONICAL_ARTIFACT_PATHS = (Path("artifacts"),)

LEGACY_RUNTIME_GLOBS = (
    "workspace_audit_*.yaml",
    "dev_servers.log",
    "scheduler.log",
    "server.log",
    "workstream_manager.log",
)


@dataclass(frozen=True)
class CheckResult:
    label: str
    ok: bool
    details: str


@dataclass(frozen=True)
class VerificationReport:
    repo_root: Path
    workstream_root: Path
    artifact_root: Path
    checks: tuple[CheckResult, ...]
    warnings: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return all(check.ok for check in self.checks)


def _load_repo_env(repo_root: Path) -> None:
    env_path = repo_root / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def evaluate_repo_root(repo_root: Path) -> VerificationReport:
    repo_root = repo_root.resolve()
    _load_repo_env(repo_root)

    workstream_root = Path(resolve_workstream_root(str(repo_root)))
    artifact_root = Path(resolve_artifact_root(str(repo_root)))

    checks: list[CheckResult] = [
        CheckResult(
            label="workstream_root_external",
            ok=workstream_root != repo_root,
            details=f"resolved to {workstream_root}",
        ),
        CheckResult(
            label="artifact_root_external",
            ok=artifact_root != repo_root,
            details=f"resolved to {artifact_root}",
        ),
    ]

    for rel_path in CANONICAL_WORKSTREAM_PATHS:
        local_path = repo_root / rel_path
        checks.append(
            CheckResult(
                label=f"migrated:{rel_path}",
                ok=not local_path.exists(),
                details=f"local path {'absent' if not local_path.exists() else 'still exists'}: {local_path}",
            )
        )

    for rel_path in CANONICAL_ARTIFACT_PATHS:
        local_path = repo_root / rel_path
        checks.append(
            CheckResult(
                label=f"migrated:{rel_path}",
                ok=not local_path.exists(),
                details=f"local path {'absent' if not local_path.exists() else 'still exists'}: {local_path}",
            )
        )

    warnings: list[str] = []
    for pattern in LEGACY_RUNTIME_GLOBS:
        for path in sorted(repo_root.glob(pattern)):
            warnings.append(f"legacy runtime file still in repo root: {path}")

    return VerificationReport(
        repo_root=repo_root,
        workstream_root=workstream_root,
        artifact_root=artifact_root,
        checks=tuple(checks),
        warnings=tuple(warnings),
    )


def _print_report(report: VerificationReport) -> None:
    print(f"Repository root: {report.repo_root}")
    print(f"Resolved WORKSTREAM_ROOT: {report.workstream_root}")
    print(f"Resolved ARTIFACT_ROOT: {report.artifact_root}")
    print()
    print("Checks:")
    for check in report.checks:
        status = "PASS" if check.ok else "FAIL"
        print(f"  [{status}] {check.label} - {check.details}")

    if report.warnings:
        print()
        print("Warnings:")
        for warning in report.warnings:
            print(f"  [WARN] {warning}")


def main() -> int:
    repo_root = PROJECT_ROOT
    report = evaluate_repo_root(repo_root)
    _print_report(report)
    return 0 if report.is_valid else 1


if __name__ == "__main__":
    raise SystemExit(main())