# Path Resolution Rules in the Orchestration System

This document describes the current implementation.

## Core Concepts
There are 3 separate concepts:

1. Where workstream state* is persisted
2. Where artifacts are persisted
3. Where agents execute and where code changes land

These concepts are intentionally separate. A workstream can resolve all 3 to different locations.

*"workstream state" includes workstreams themselves, tasks, scheduler state, locks, and audit data.

## Terms

- Workstream root: the root directory used for workstream persistence. Workstream files are stored under a `workstreams/` directory inside this root.
- Workstream YAML path: the `.yaml` file for a specific workstream.
- Workstream home directory: the directory reserved for a workstream's local files, including its `tasks/` directory and the default root for its children.
- Artifact root: the root directory used for artifact persistence. Artifact files are stored under an `artifacts/` directory inside this root.
- Working directory: the directory where agents execute and where code changes should land.

## Workstream State Persistence

### Base rule

- By default, the workstream root is the current repo.
- If `WORKSTREAM_ROOT` is set, the workstream root becomes that path instead.
- Workstream YAML files are always stored under `workstreams/` inside the resolved workstream root.

In other words:

- default top-level workstream path: `./workstreams/<workstream_id>.yaml`
- with `WORKSTREAM_ROOT=/tmp/shared_state`: `/tmp/shared_state/workstreams/<workstream_id>.yaml`

### Exact layout

For a workstream persisted in workstream root `<workstream_root>`:

- workstream YAML path: `<workstream_root>/workstreams/<workstream_id>.yaml`
- workstream home directory: `<workstream_root>/workstreams/<workstream_id>/`
- task directory for that workstream: `<workstream_root>/workstreams/<workstream_id>/tasks/`

For a task `<task_id>` that belongs to workstream `<workstream_id>` persisted in `<workstream_root>`:

- task YAML path: `<workstream_root>/workstreams/<workstream_id>/tasks/<task_id>.yaml`

### Child workstream placement

- Every workstream resolves a child workstream root.
- By default, a workstream's child workstream root is its own workstream home directory.
- Direct children are persisted using that child workstream root as their workstream root.

That means:

- parent workstream YAML: `./workstreams/1234.yaml`
- parent workstream home directory: `./workstreams/1234/`
- default child workstream root for `1234`: `./workstreams/1234`
- direct child `5678` YAML: `./workstreams/1234/workstreams/5678.yaml`

Grandchildren continue the same pattern:

- child `5678` home directory: `./workstreams/1234/workstreams/5678/`
- default child workstream root for `5678`: `./workstreams/1234/workstreams/5678`
- grandchild `9999` YAML: `./workstreams/1234/workstreams/5678/workstreams/9999.yaml`

### child_workstream_root override

- A workstream can set `child_workstream_root`.
- This changes where that workstream's direct children are persisted.
- The value is interpreted as a workstream root, not as the final `workstreams/` directory.
- Child workstream YAML files are therefore still written under `workstreams/` inside the overridden root.

Example:

- workstream `1234` sets `child_workstream_root: ~/other_state`
- direct child `5678` persists at `~/other_state/workstreams/5678.yaml`
- tasks for `5678` persist at `~/other_state/workstreams/5678/tasks/<task_id>.yaml`
- the default child workstream root for `5678` then becomes `~/other_state/workstreams/5678`

## Artifact Persistence

### Base rule

- By default, the artifact root is the current repo.
- If `ARTIFACT_ROOT` is set, the artifact root becomes that path instead.
- Artifact files are always stored under `artifacts/` inside the resolved artifact root.

In other words:

- default artifact directory: `./artifacts/`
- with `ARTIFACT_ROOT=/tmp/shared_artifacts`: `/tmp/shared_artifacts/artifacts/`

### Exact layout

Artifact paths are logical relative paths inside the artifact directory.

Examples:

- artifact path `logs/devops_log.md` resolves to `./artifacts/logs/devops_log.md` by default
- artifact path `/logs/devops_log.md` resolves to the same location; leading `/` is stripped from the logical artifact path

### artifact_root workstream override

- A workstream can set `artifact_root`.
- In the current implementation, this is inherited by descendants.
- A descendant may override it by setting its own `artifact_root`.
- Descendants without their own override continue using the nearest ancestor's resolved `artifact_root`.

Example:

- workspace default artifact directory: `./artifacts/`
- workstream `1234` sets `artifact_root: ~/special_artifacts`
- artifact created in the context of `1234` at logical path `reports/a.md` persists at `~/special_artifacts/artifacts/reports/a.md`
- direct child `5678` without its own `artifact_root` also persists `reports/b.md` at `~/special_artifacts/artifacts/reports/b.md`

### Independence from workstream state

- Artifact placement is independent from workstream state placement.
- Changing `WORKSTREAM_ROOT` does not change artifact placement.
- Changing `child_workstream_root` does not change artifact placement.
- Changing `working_directory` does not change artifact placement.

## Agent Execution And Code Changes

### Base rule

- By default, the working directory is the current repo.
- A workstream can set `working_directory`.
- `working_directory` inherits downward to descendants unless a descendant sets its own `working_directory`.

Example:

- workstream `1234` sets `working_directory: ~/other_repo`
- agents running in the context of `1234` execute in `~/other_repo`
- descendants of `1234` also execute in `~/other_repo`, unless they override it again

### Independence from state and artifacts

- Working directory resolution is independent from state persistence.
- Working directory resolution is independent from artifact persistence.

This means a workstream can:

- persist its state under one root
- persist artifacts under another root
- run agents in a third directory

## Environment Variable Resolution

Environment variables resolve in layers.

### Workstream-local .env files

- Each workstream can have a local `.env` file at:
  - `<state_root>/workstreams/<workstream_id>/.env`
- These layers are applied from root ancestor to leaf workstream.
- A descendant value overrides an ancestor value.
- Setting `KEY=` at a lower level masks the key completely.

### Working-directory .env files

- If a workstream sets `working_directory`, the `.env` at `<working_directory>/.env` is included as a layer for that workstream.
- These working-directory layers participate in normal inheritance for descendants.

### System environment fallback

- If a key is not provided by any workstream-local or working-directory layer, resolution falls back to the process environment.
- In practice, the process environment may itself include values loaded from a repo-level `.env`, depending on how the orchestration process was started.

## Examples

### Example 1: Default layout

```
current-repo/
├── workstreams/
│   ├── 1234.yaml
│   └── 1234/
│       ├── tasks/
│       └── workstreams/
│           ├── 5678.yaml
│           └── 5678/
│               ├── .env
│               ├── tasks/
│               │   └── abcd.yaml
│               └── workstreams/
├── artifacts/
│   └── logs/
│       └── devops_log.md
└── .env
```

Resolved paths:

- workstream `1234`: `./workstreams/1234.yaml`
- child workstream `5678`: `./workstreams/1234/workstreams/5678.yaml`
- task `abcd` on workstream `5678`: `./workstreams/1234/workstreams/5678/tasks/abcd.yaml`
- artifact `logs/devops_log.md`: `./artifacts/logs/devops_log.md`

### Example 2: child_workstream_root override

If workstream `1234` sets `child_workstream_root: ~/shared_state`:

```
~/shared_state/
└── workstreams/
    ├── 5678.yaml
    └── 5678/
        ├── tasks/
        │   └── abcd.yaml
        └── workstreams/
```

Resolved paths:

- direct child `5678`: `~/shared_state/workstreams/5678.yaml`
- task `abcd` on `5678`: `~/shared_state/workstreams/5678/tasks/abcd.yaml`
- grandchildren of `5678` default under `~/shared_state/workstreams/5678`

### Example 3: artifact_root is inherited by descendants

If workstream `1234` sets `artifact_root: ~/special_artifacts` and child `5678` does not set one:

```
current-repo/


~/special_artifacts/
└── artifacts/
    └── reports/
    ├── child.md
    └── parent.md
```

Resolved paths:

- artifact written in workstream `1234` as `reports/parent.md`: `~/special_artifacts/artifacts/reports/parent.md`
- artifact written in child `5678` as `reports/child.md`: `~/special_artifacts/artifacts/reports/child.md`

### Example 4: working_directory override with separate state and artifacts

If workstream `5678` sets `working_directory: ~/other_repo`:

```
current-repo/
├── workstreams/
│   ├── 1234.yaml
│   └── 1234/
│       └── workstreams/
│           ├── 5678.yaml
│           └── 5678/
│               └── tasks/
├── artifacts/
│   └── logs/
│       └── devops_log.md
└── .env

~/other_repo/
└── .env
```

Resolved behavior:

- workstream `5678` state still lives under the current repo's workstream root
- artifacts for `5678` still live under the current repo's artifact root unless `artifact_root` is also set
- agents for `5678` execute in `~/other_repo`
- `~/other_repo/.env` becomes part of env resolution for `5678` and its descendants

## Agent Prompt Variables

When an agent runs in a workstream context, the orchestration system injects directory context in 2 places:

1. As prompt variables in the task prompt text
2. As environment variables for the agent subprocess

### Prompt text

The task prompt includes:

- `ORCHESTRATION_ROOT`: the foundation repo / orchestration workspace root
- `WORKSPACE_ROOT`: the resolved working directory for the selected workstream context

The prompt explicitly tells the agent to write product code under `WORKSPACE_ROOT`, not `ORCHESTRATION_ROOT`, unless both paths are the same.

### Environment variables

The agent subprocess receives these directory variables:

- `ORCHESTRATION_ROOT`: the foundation repo / orchestration workspace root
- `WORKSPACE_ROOT`: the resolved working directory for the selected workstream context

It also receives related orchestration metadata:

- `ORCHESTRATION_AGENT_NAME`
- `ORCHESTRATION_AGENT_RUN_ID`
- `ORCHESTRATION_AGENT_TASK_IDS`
- `ORCHESTRATION_AGENT_WORKSTREAM_ID` when a workstream context exists

### Variables not currently injected

The current implementation does not inject dedicated prompt or environment variables for:

- resolved workstream root
- resolved artifact root
- resolved child workstream root

## Resolving At Runtime

At runtime, the orchestration system resolves the effective workstream root, artifact root, and working directory based on:

1. workspace-level defaults and environment variables
2. the selected workstream's own fields
3. inherited fields from ancestors where the implementation supports inheritance

Many orchestration CLI commands accept `--workstream-id` because the same logical command may need different resolved paths depending on which workstream context it is operating in.

## Current Inheritance Summary

- `working_directory`: inherited by descendants
- `child_workstream_root`: affects where direct children are persisted, and then normal default child placement continues from those children
- `artifact_root`: inherited by descendants unless a descendant overrides it
