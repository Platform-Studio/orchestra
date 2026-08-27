# Repository Instructions

## Scope

Orchestra is an open-source AI agent orchestration engine, CLI, and web Kanban app. 

Keep private agent definitions, company-specific workflows, credentials, and local paths out of this repository.

## Structure

- `orchestration/`: core Python APIs, CLI, scheduler, persistence, and agent runners.
- `workstream_manager/`: local web server and browser UI.
- `Agents/cli/`: public agent-facing CLI tools and their documentation.
- `examples/`: deterministic examples using synthetic data.
- `scripts/`: release, audit, and development utilities.
- `docs/`: security and release documentation.

Runtime data belongs under configured `WORKSTREAM_ROOT` and `ARTIFACT_ROOT` paths. Do not commit `.env`, workstreams, artifacts, logs, build output, or credentials.

## Setup And Validation

Orchestra requires Python 3.12 or newer.

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest -q
```

For release-related changes, also run:

```bash
python scripts/test_release_flow.py
python scripts/audit_public_repo.py
python scripts/audit_licenses.py
```

Use `orc` as the canonical installed command. `orchestra` and `python -m orchestration` are compatibility forms.

## Change Rules

- Preserve public CLI and persistence behavior unless the change intentionally includes migration and release notes.
- Keep changes focused and follow existing module patterns.
- Add tests for non-trivial behavior changes. Include end-to-end coverage when changing installation, persistence, scheduling, agent execution, or the web application.
- Keep examples deterministic and credential-free.
- Treat workstream YAML and artifacts as user data. Never delete or rewrite them during package installation or upgrade.
- Do not weaken localhost binding, path validation, secret handling, or command-execution safeguards without documenting the security impact.
- Do not add telemetry unless it is explicitly opt-in and documented.

See `CONTRIBUTING.md`, `SECURITY.md`, and `docs/security-model.md` for the human contribution and security policies.
