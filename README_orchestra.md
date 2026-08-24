# Orchestra

[Orchestra](https://github.com/Platform-Studio/orchestra) is an open-source framework for coordinating autonomous agents across repeatable workflows. It combines a standalone orchestration CLI with a web-based Workstream Manager for creating tasks, assigning agents, following progress, and reviewing a complete audit trail.

Orchestra is model- and task-agnostic. It can coordinate coding, research, operations, marketing, sales, or any other work that can be represented as tasks moving through a workflow.

## Features

- Hierarchical workstreams with configurable task states
- State-based and scheduled agent triggers
- Configurable agent concurrency and task locking
- Agent execution through Claude Code, Cline, or GitHub Copilot CLI
- Markdown agent definitions with YAML frontmatter
- Per-workstream context shared across agent runs
- Comments, attachments, errors, progress checklists, and audit history
- Filesystem-based persistence with independently configurable state and artifact roots
- Optional per-task token and cost tracking through Beans Proxy
- A Kanban-style Workstream Manager for humans

## Components

### Orchestration CLI

The CLI is the primary interface for managing workstreams, tasks, agents, triggers, locks, artifacts, progress, and the scheduler. It can be used directly by humans, scripts, or agents.

```bash
python -m orchestration --help
python -m orchestration workstream list
python -m orchestration agent list
```

### Workstream Manager

The Workstream Manager is a web application built on the same orchestration data. It provides Kanban boards, task details, agent-run status, audit history, attachments, and operational controls.

```bash
python -m workstream_manager
```

### Scheduler

The scheduler evaluates time-based and state-based triggers and starts eligible agents. It runs as a separate foreground process:

```bash
python -m orchestration scheduler run
```

## Core Concepts

- **Tasks** are the basic units of work. A task has a title, description, state, tags, comments, attachments, errors, progress, and an audit trail.
- **Workstreams** contain tasks and define the allowed task states and transitions. Workstreams can be nested to represent larger systems of work.
- **Agents** are Markdown definitions that describe a role and configure how that role is executed.
- **Triggers** start agents in response to task state changes or schedules.
- **Artifacts** are documents, images, and other files consumed or produced by agents. Agents can manage them through the CLI so storage location remains independent of the code workspace.
- **Workstream context** is shared operating context available to every agent in a workstream.
- **Agent runners** translate Orchestra's task and workstream context into commands for supported agent runtimes.
- **Locks** prevent multiple agents from working on the same task at the same time.

## Repository Layout

```text
/
|-- Agents/                 # Public agent definitions and CLI tools
|   |-- skills/             # Public skill definitions
|   `-- cli/                # Agent-facing CLI tools and documentation
|-- orchestration/          # Orchestration framework and CLI
|-- workstream_manager/     # Workstream Manager server and web application
|-- examples/               # Example workflows and synthetic example data
|-- scripts/                # Validation and maintenance utilities
|-- audio/                  # Optional event sounds
|-- workstreams/            # Runtime state; created during setup and ignored by Git
`-- artifacts/              # Runtime artifacts; created during setup and ignored by Git
```

## Installation From Source

The packaged installer described in the project roadmap is not yet available. For the current source checkout:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Configure at least one supported agent runtime:

- Claude Code CLI: install `claude` and authenticate it.
- Cline CLI: install `cline` and authenticate it.
- GitHub Copilot CLI: install `copilot`, or authenticate `gh` and use `gh copilot`.

Select the default runtime in `.env`:

```dotenv
ORCHESTRATION_AGENT_RUNTIME=copilot
```

Run the scheduler and Workstream Manager together with automatic restart during development:

```bash
.venv/bin/python dev_servers.py
```

Or run them separately:

```bash
python -m orchestration scheduler run
python -m workstream_manager
```

## Extension Contracts

Orchestra is designed so public and private definitions can coexist without copying files into the framework repository. The contracts below describe the current extension boundaries. Where a broader adapter API is planned but not implemented, that is stated explicitly.

### Agent Definitions

An agent is a Markdown file whose body contains its instructions. Optional YAML frontmatter configures execution:

```markdown
---
name: Code Reviewer
description: Reviews changes for correctness and risk
x-role: worker
x-runtime: copilot
x-model-level: coding
x-effort: high
x-timeout: 1800
x-learning: true
x-progress-checklist: true
x-own-worktree: true
x-tools:
  - browser
---

Review the assigned task and its implementation.
```

Supported headers include:

| Header | Purpose |
|---|---|
| `name` | Human-readable agent name |
| `description` | Short description used in listings |
| `x-role` | `worker`, `manager`, `director`, or `executive`; `exec` is accepted as an alias |
| `x-runtime` | `claude-code`, `cline`, or `copilot`; `claude` aliases `claude-code` |
| `x-model` | Exact model identifier; overrides `x-model-level` |
| `x-model-level` | Logical `high`, `medium`, `low`, or `coding` model tier |
| `x-effort` | Runtime effort hint: `low`, `medium`, `high`, `xhigh`, or `max` |
| `x-timeout` | Maximum run time in seconds |
| `x-learning` | Enables or disables accumulated agent learnings |
| `x-progress-checklist` | Enables a visible run checklist |
| `x-own-worktree` | Gives the run an isolated Git worktree |
| `x-tools` | Names of documented CLI tools requested by the agent |
| `x-sound-start`, `x-sound-finish`, `x-sound-error` | Optional event sound names |

Agent references can use a filename, a relative path, or the human-readable frontmatter name. Definitions are resolved in this precedence order:

1. The normal project or built-in `Agents/` directory
2. Directories in `ORCHESTRA_AGENT_PATHS`, in configured order

Later configured directories take precedence over earlier directories. Multiple paths use the platform path separator: `:` on macOS and Linux, and `;` on Windows.

```dotenv
ORCHESTRA_AGENT_PATHS=/path/to/team-agents:/path/to/private-agents
```

`ORCHESTRATION_AGENTS_DIR` remains available as a legacy single-directory override.
The former `ORKESTRA_AGENT_PATHS` spelling is also accepted as a compatibility alias when `ORCHESTRA_AGENT_PATHS` is unset.

### Skills

Skills are Markdown resources that give agents reusable procedures, constraints, or domain knowledge. Project skills conventionally live under `Agents/skills/`.

External skill directories can be supplied through `ORCHESTRA_SKILL_PATHS`:

```dotenv
ORCHESTRA_SKILL_PATHS=/path/to/shared-skills:/path/to/private-skills
```

When this variable is set, Orchestra includes those locations in the agent's initialization instructions. Skill selection and loading are performed by the agent runtime; Orchestra does not currently parse skills into an internal registry.
The former `ORKESTRA_SKILL_PATHS` spelling remains a compatibility alias.

### Agent-Facing CLI Tools

A discoverable CLI tool in `Agents/cli/` consists of a Python implementation and matching Markdown documentation with the same base name:

```text
Agents/cli/browser.py
Agents/cli/browser.md
```

An agent requests tools with `x-tools`. For matching project-local pairs, Orchestra adds the command path and Markdown documentation to the agent's system prompt.

External CLI directories can be supplied through `ORCHESTRA_CLI_PATHS`:

```dotenv
ORCHESTRA_CLI_PATHS=/path/to/shared-cli:/path/to/private-cli
```

Orchestra currently tells the initialized agent where these external tools are located. Automatic discovery and prompt injection of external tool pairs is not yet implemented.
The former `ORKESTRA_CLI_PATHS` spelling remains a compatibility alias.

### Persistence

The current persistence implementation stores workstreams, tasks, locks, scheduler state, audits, run metadata, and artifacts on the filesystem.

Two process-level settings route data independently:

```dotenv
WORKSTREAM_ROOT=file:/absolute/path/to/state
ARTIFACT_ROOT=file:/absolute/path/to/artifacts
```

- `WORKSTREAM_ROOT` controls workstream YAML, task YAML, locks, scheduler state, audits, and agent-run state.
- `ARTIFACT_ROOT` controls artifact storage.
- Both accept absolute paths or local `file:` URIs.
- Other URI schemes are currently rejected.

A workstream can also define:

- `working_directory` for the code or project an agent should modify
- `child_workstream_root` for descendant workstream state
- `artifact_root` for artifacts in that workstream subtree

Keep orchestration state and artifacts outside repositories where agents create branches or worktrees. Otherwise, changing branches can produce conflicting or apparently missing task state.

Database, GitHub, and other persistence backends are planned extension points, but there is not yet a public persistence-adapter interface. That interface should preserve the existing task, workstream, locking, audit, and artifact behavior.

### Agent Runners

The runner contract turns an agent definition plus task/workstream context into an isolated runtime process. Current runners support Claude Code, Cline, and GitHub Copilot CLI.

Runtime selection precedence is:

1. Agent `x-runtime`
2. `ORCHESTRATION_AGENT_RUNTIME`
3. `claude-code`

Model selection precedence is:

1. Agent `x-model`
2. Agent `x-model-level` mapped through runtime-specific environment settings
3. Runtime default

Effort selection precedence is:

1. Agent `x-effort`
2. Level-specific effort environment setting
3. Runtime default effort setting
4. No explicit effort

Before starting a runtime, Orchestra provides:

- Agent instructions and requested tool documentation
- Task and workstream identifiers and context
- Valid task-state transitions
- Attachment locations and, when configured, inline attachment content
- Orchestration and product workspace roots
- An execution contract for reading tasks, posting results, and updating state

During and after execution, the runner records run metadata, streams output to a log, updates the audit trail, captures supported runtime context, tracks progress, releases locks, and classifies the result as completed, failed, killed, or timed out.

A future runner adapter should preserve this lifecycle while translating it to another model or agent harness.

## Data and Working Directories

Orchestra separates four locations:

1. **Orchestra repository:** framework code, public definitions, and CLI tools.
2. **Orchestration state:** workstreams, tasks, locks, scheduler state, and run metadata.
3. **Artifacts:** files read and produced by agents.
4. **Working directory:** the product or repository an agent modifies.

This separation allows agents to work in one repository while orchestration state and artifacts remain stable elsewhere.

## Environment Hierarchy

A root `.env` provides process defaults. Workstreams can also contain `.env` files. Child workstreams inherit their ancestors' environment settings and can override or mask individual values.

This makes it possible to share common runtime settings while keeping project-specific credentials and configuration at the appropriate workstream level. Never commit populated `.env` files.

## Token and Cost Tracking

Orchestra can optionally route supported agent calls through [Beans Proxy](https://github.com/platform-studio/beans-proxy) to record token usage by task. When enabled, task totals are stored with run metadata and displayed by the Workstream Manager.

```dotenv
BEANS_PROXY=true
BEANS_PROXY_HOST=127.0.0.1
BEANS_PROXY_PORT=8000
```

## Development

Run the complete test suite with:

```bash
python -m pytest -q
```

Tests use `./.test/pytest` and clear external persistence-root settings so test workspaces cannot write into live orchestration data.

During development, run the scheduler and server with automatic restart:

```bash
.venv/bin/python dev_servers.py
```

Logs are written under `ARTIFACT_ROOT/artifacts/logs/`, or `./artifacts/logs/` when `ARTIFACT_ROOT` is unset.

## Project Status

The existing public Orchestra repository is being expanded with this orchestration framework and Workstream Manager. Installation, packaging, examples, contribution guidance, security documentation, and the formal adapter APIs are still being completed.
