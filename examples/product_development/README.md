# Product Development

This example installs an empty Product Development board for humans and AI agents to plan, implement, review, test, unblock, and deploy work together. It mounts the workstream to your existing product repository so agents read and modify the correct code.

The example does not include a sample application or tasks, and installation does not run an agent.

## Prepare Your Repository

Create or clone the product repository first. It should include its normal README and any contribution, agent, architecture, test, and deployment instructions agents must follow.

```bash
mkdir my-product
cd my-product
git init
cd ../orchestra
```

## Install the Example

From the Orchestra repository root:

```bash
./example.sh product_development --repository ../my-product
```

The default Orchestra state workspace is `./product-development-workspace`. To store Orchestra state elsewhere:

```bash
./example.sh product_development \
  --workspace ../my-orchestra-workspace \
  --repository ../my-product
```

`--workspace` stores workstreams, tasks, installed definitions, audit history, and agent-run metadata. `--repository` sets the Product Development workstream's `working_directory`, which is where its agents read and write code.

Both paths are resolved to absolute paths. The repository must already exist. Reinstalling with the same paths is safe and does not duplicate resources; using a different repository with an existing example workstream stops with an explanatory error.

## Installed Workflow

Installation creates `Examples > Product Development`, copies seven agent definitions and three shared skills into the state workspace, and configures:

```text
Backlog -> On Deck -> Implementation Plan -> In Progress -> Code Review
                                                      ^             |
                                                      +-------------+

Code Review -> Integration Test -> Deploy -> Live
```

Agents can send failed work backward for revision or move work to `Blocked`. The `unblocker` returns resolved work to its recorded owning lane.

## Add Your First Task

Start Orchestra against the installed state workspace:

```bash
./run-orchestra.sh ./product-development-workspace
```

In Workstream Manager:

1. Open `Examples > Product Development`.
2. Add a feature, bug, or engineering chore to `Backlog` with acceptance criteria.
3. Move it to `On Deck` when it is ready and prioritized.
4. Move it to `Implementation Plan` to start the automated delivery flow.

The state-based triggers are active. Moving a task into `Implementation Plan`, `In Progress`, `Code Review`, `Integration Test`, or `Deploy` may invoke your configured agent runtime and incur provider costs.

The hourly `backlog_maintainer` and 30-minute `unblocker` schedules install paused. Enable them from Workstream Manager or with `orc trigger resume <trigger-id>` after reviewing the workflow.

## Deployment Safety

`devops` deploys only through procedures documented in your repository. Before moving work to `Deploy`, document the target environment, required approvals, credentials, deployment command or pipeline, verification checks, and rollback procedure. Without that information, the agent moves the task to `Blocked` rather than improvising a deployment.