# Contributing To Orchestra

Thank you for improving Orchestra. Bug fixes, tests, documentation, examples, and focused feature contributions are welcome.

## Before Starting

Use a GitHub issue for bugs and concrete feature proposals. Use GitHub Discussions for questions and early ideas. For a large change, agree on the behavior and scope with a maintainer before investing substantial work.

Security vulnerabilities must follow `SECURITY.md` and must not be reported in a public issue.

## Development Setup

Orchestra requires Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

Optional integrations can be installed with `.[image]` and `.[browser]`. Browser development also requires `python -m playwright install chromium`.

Copy `.env.example` to `.env` only when testing an external agent runtime or provider integration. Never commit credentials or real user data.

## Making Changes

- Follow the patterns in the module you are changing.
- Add focused tests for new or changed behavior.
- Keep examples synthetic, deterministic, and usable without paid services.
- Preserve workstream, task, artifact, and `.env` data across upgrades.
- Update documentation when changing commands, configuration, persistence formats, or security behavior.
- Avoid unrelated refactoring in the same change.

Run the complete test suite before submitting:

```bash
python -m pytest -q
```

For packaging, persistence, or dependency changes, also run:

```bash
python scripts/test_release_flow.py
python scripts/audit_public_repo.py
python scripts/audit_licenses.py
```

CI repeats the tests on macOS, Windows, and Linux.

## Pull Requests

A pull request should explain the problem, the chosen behavior, material tradeoffs, and validation performed. Keep commits understandable, but maintainers may squash them when merging.

All checks must pass. A maintainer may request changes for correctness, compatibility, security, maintainability, or fit with the project direction.

Unless explicitly stated otherwise, contributions intentionally submitted for inclusion are provided under the Apache License 2.0 as described in section 5 of that license. Orchestra does not currently require a separate contributor license agreement.
