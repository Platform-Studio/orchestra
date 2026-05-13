## Orchestration Framework CLI

Manage workstreams, tasks, locks, triggers, env settings, artifacts, and agents from the command line using `orchestration/cli.py`.

All commands output **JSON** to stdout on success, and a JSON error object to stderr on failure. Exit code 0 indicates success, non-zero indicates failure.

### Prerequisites

- Python 3.x
- `pip install pyyaml` (already in project)
- For agent execution: Claude Code CLI (`claude`) installed and `ANTHROPIC_API_KEY` set in `.env`, or Cline CLI (`cline`) installed and authenticated with `cline auth` when using `x-runtime: cline`

### General Usage

```bash
python -m orchestration.cli <concept> <method> [arguments]

# Optionally specify a different workspace root (default: current directory)
python -m orchestration.cli --base-dir /path/to/workspace <concept> <method> [arguments]
```

### Output Format

Success:
```json
{"status": "ok", "data": { ... }}
```

Error:
```json
{"status": "error", "message": "Task is locked by agent sdr-worker-1", "code": "TASK_LOCKED"}
```

Error codes: `NOT_FOUND`, `INVALID_TRANSITION`, `TASK_LOCKED`, `RUNTIME_ERROR`, `INVALID_JSON`, `ERROR`

---

### Commands

#### workstream create — Create a new workstream

```bash
python -m orchestration.cli workstream create --name "SDR Outreach"

# With description and parent
python -m orchestration.cli workstream create --name "Email Campaign" --description "Q2 email outreach" --parent PARENT_WS_ID

# With custom task states (JSON string)
python -m orchestration.cli workstream create --name "Pipeline" --states '{"To Do": ["In Progress", "Invalid"], "In Progress": ["Done", "Failed"], "Done": [], "Failed": ["To Do"], "Invalid": []}'

# With retry configuration
python -m orchestration.cli workstream create --name "Retry WS" --retry '{"max_retries": 5, "backoff": "exponential", "base_seconds": 30}'

# Descendants-only mounted workspace root
python -m orchestration.cli workstream create --name "career_pivot" --mounted-workspace-path "~/career_pivot"
```

Default task states if `--states` is omitted:
- `pending` → `in_progress`
- `in_progress` → `completed`, `failed`
- `completed` → (terminal)
- `failed` → `pending`

#### workstream list — List all workstreams

```bash
python -m orchestration.cli workstream list
```

#### workstream read — Read a single workstream

```bash
python -m orchestration.cli workstream read WORKSTREAM_ID
```

#### workstream find — Search workstreams by name or description

```bash
python -m orchestration.cli workstream find --query "SDR"
```

#### workstream tree — Pretty-print the workstream hierarchy

```bash
python -m orchestration.cli workstream tree
```

Outputs a tree view like:
```
career_pivot (812b23b2-...)
└── outreach (2e144563-...)
    └── linkedin (9ef1b48c-...)
```

Note: This command prints plain text to stdout (not JSON).

#### workstream descendants — Return all descendant workstreams as JSON

```bash
python -m orchestration.cli workstream descendants WORKSTREAM_ID

# Include the root workstream in the output
python -m orchestration.cli workstream descendants WORKSTREAM_ID --include-self
```

Returns JSON rows with:
- `id`
- `name`
- `parent_id`
- `depth` (1 = direct child, 2 = grandchild, etc.; 0 for root when `--include-self` is used)

This is the recommended command for director-style agents that need to traverse a workstream subtree.

#### workstream pause — Pause a workstream

```bash
python -m orchestration.cli workstream pause WORKSTREAM_ID
```

Paused workstreams are skipped by the scheduler — no triggers will fire.

#### workstream resume — Resume a paused workstream

```bash
python -m orchestration.cli workstream resume WORKSTREAM_ID
```

---

#### env set — Set a workstream-local env key

```bash
python -m orchestration.cli env set WORKSTREAM_ID OPENAI_API_KEY "sk-..."
```

Writes key/value pairs into `workstreams/{workstream_id}/.env`.

#### env unset — Mask a key at this workstream level

```bash
python -m orchestration.cli env unset WORKSTREAM_ID OPENAI_API_KEY
```

`unset` writes `KEY=` in the workstream `.env` file. This explicitly masks inherited values from parent workstreams and also masks process-level environment fallback.

#### env get — Resolve a key in a workstream/task context

```bash
# Resolve using a workstream context
python -m orchestration.cli env get OPENAI_API_KEY --workstream WORKSTREAM_ID

# Resolve using a task context (task -> owning workstream)
python -m orchestration.cli env get OPENAI_API_KEY --task TASK_ID
```

Resolution order:
1. Current workstream `.env`
2. Parent workstream `.env` files up the hierarchy
3. Process environment (`os.environ`)

If any level defines `KEY=`, resolution stops and returns no value for that key.

#### env list — List env keys for a context

```bash
# Effective inherited map (default)
python -m orchestration.cli env list --workstream WORKSTREAM_ID

# Task-context effective map
python -m orchestration.cli env list --task TASK_ID

# Only keys physically present in selected workstream .env
python -m orchestration.cli env list --workstream WORKSTREAM_ID --local

# Include process environment fallback keys in effective output
python -m orchestration.cli env list --workstream WORKSTREAM_ID --include-system
```

---

#### task create — Create a new task in a workstream

```bash
python -m orchestration.cli task create WORKSTREAM_ID --title "Contact John Doe"

# With description, tags, and retry override
python -m orchestration.cli task create WORKSTREAM_ID --title "Send proposal" --description "Draft and send the proposal doc" --tags "sales,urgent" --retry '{"max_retries": 5}'
```

The task's initial status will be the **first state** in the workstream's `task_states` map.

#### task read — Read a task by ID

```bash
python -m orchestration.cli task read TASK_ID
```

Tasks are found by ID across all workstreams — you don't need to specify the workstream.

#### task update — Update a task's status, description, or tags

```bash
# Change status (validated against workstream's state transition map)
python -m orchestration.cli task update TASK_ID --status "In Progress"

# Update description
python -m orchestration.cli task update TASK_ID --description "Updated scope"

# Update tags
python -m orchestration.cli task update TASK_ID --tags "high-priority,q2"
```

**Important:** Status transitions are validated. If you try an invalid transition (e.g. jumping from "To Do" directly to "Done" when only "To Do" → "In Progress" is allowed), the command will fail with code `INVALID_TRANSITION`.

#### task list — List tasks in a workstream

```bash
# All tasks
python -m orchestration.cli task list WORKSTREAM_ID

# Filter by status
python -m orchestration.cli task list WORKSTREAM_ID --status "pending"

# Filter by tags (comma-separated, all must match)
python -m orchestration.cli task list WORKSTREAM_ID --tags "urgent,sales"
```

Tasks are returned in board order for each state (rank-based ordering), not creation time.

#### task move-up — Move a task one position up in its current state

```bash
python -m orchestration.cli task move-up TASK_ID
```

This only reorders within the task's current status column.

#### task move-down — Move a task one position down in its current state

```bash
python -m orchestration.cli task move-down TASK_ID
```

This only reorders within the task's current status column.

#### task move-before — Move a task directly before another task

```bash
python -m orchestration.cli task move-before TASK_ID TARGET_TASK_ID
```

Both tasks must be in the same workstream and status column.

#### task move-after — Move a task directly after another task

```bash
python -m orchestration.cli task move-after TASK_ID TARGET_TASK_ID
```

Both tasks must be in the same workstream and status column.

#### task move-to-index — Move a task to a zero-based index within its current state

```bash
python -m orchestration.cli task move-to-index TASK_ID INDEX
```

This only reorders within the task's current status column. Index values outside bounds are clamped.

#### task comment — Add a comment to a task

```bash
python -m orchestration.cli task comment TASK_ID --message "Reached out via email, waiting for response"

# Recommended for agents: explicitly set author
python -m orchestration.cli task comment TASK_ID --message "Reached out via email, waiting for response" --author "LinkedIn SDR"
```

Notes:
- For agent workflows, always pass `--author` so comments are attributed correctly in the UI.
- Do not prefix comment bodies with dates like `[2026-04-21]`; timestamps are stored separately.

#### task attach — Attach an artifact to a task

```bash
python -m orchestration.cli task attach TASK_ID --path <artifact_path>
```

Adds an artifact path to the task's attachment list. After saving any artifact via the orchestration system, attach it to any Task(s) you were provided with so that other agents and human users can easily find your outputs. This applies to all artifact types — markdown documents, images (SVG, PNG, etc.), and any other files.

#### task detach — Remove an artifact attachment from a task

```bash
python -m orchestration.cli task detach TASK_ID --path <artifact_path>
```

---

#### task archive — Delete a task (and its lock)

```bash
python -m orchestration.cli task archive TASK_ID
```

#### task audit — View a task's audit trail

```bash
python -m orchestration.cli task audit TASK_ID
```

Returns an array of audit entries, each with `timestamp`, `type`, and `description`.

#### task clear-schedule — Remove a task's scheduled action

```bash
python -m orchestration.cli task clear-schedule TASK_ID
```

---

#### lock acquire — Lock a task before working on it

```bash
python -m orchestration.cli lock acquire TASK_ID --agent my-agent-name

# With custom TTL (default: 900 seconds / 15 minutes)
python -m orchestration.cli lock acquire TASK_ID --agent my-agent-name --ttl 3600
```

**You MUST acquire a lock before modifying a task.** This prevents two agents from working on the same task simultaneously. If the task is already locked by another agent, the command fails with code `TASK_LOCKED`.

Locks automatically expire after the TTL. Expired locks are treated as unlocked.

#### lock release — Release a lock you hold

```bash
python -m orchestration.cli lock release TASK_ID --agent my-agent-name
```

Only the agent that acquired the lock can release it. Returns an error if a different agent tries to release.

#### lock status — Check if a task is locked

```bash
python -m orchestration.cli lock status TASK_ID
```

Returns `{"locked": false}` or `{"locked": true, "agent_id": "...", "acquired_at": "...", "expires_at": "..."}`.

---

#### trigger create — Add a trigger to a workstream

```bash
# State-based trigger: runs an agent when a task enters a state
python -m orchestration.cli trigger create WORKSTREAM_ID --on-state "pending" --action run_agent --agent sdr

# State-based trigger: runs a shell command
python -m orchestration.cli trigger create WORKSTREAM_ID --on-state "Done" --action run_command --command "echo Task {task_id} in {workstream_id} is done"

# Schedule-based trigger: runs every minute on matching tasks
python -m orchestration.cli trigger create WORKSTREAM_ID --on-schedule "* * * * *" --action run_command --command "echo tick"

# Schedule-based trigger with task filter
python -m orchestration.cli trigger create WORKSTREAM_ID --on-schedule "*/5 * * * *" --filter '{"status": "pending", "tags": ["batch"]}' --action run_agent --agent sdr
```

Template variables `{task_id}` and `{workstream_id}` are replaced in `run_command` commands.

All triggers are evaluated by the scheduler on each tick (every 60 seconds). State-based triggers match tasks currently in the specified state and evaluate them in task order, dispatching only the first currently unlocked match per trigger evaluation. Schedule-based triggers match on cron expressions.

Concurrency for `run_agent` triggers is controlled at the workstream level, per agent, with default `1` run per agent:

```yaml
agent_concurrency:
  default: 1
  overrides:
    kanban_ninja: 2
```

This policy is configured in workstream YAML. Trigger-level concurrency flags are deprecated.

#### trigger list — List triggers on a workstream

```bash
python -m orchestration.cli trigger list WORKSTREAM_ID
```

#### trigger delete — Remove a trigger

```bash
python -m orchestration.cli trigger delete TRIGGER_ID
```

---

#### agent list — List available agent definitions

```bash
python -m orchestration.cli agent list
```

Lists all `.md` files in the `Agents/` directory, showing name, description, and agent type.

#### agent run — Run an agent against a task

```bash
python -m orchestration.cli agent run AGENT_NAME --task TASK_ID
```

Parses the agent's `.md` file and executes it via Claude Code CLI against the specified task. Requires `claude` CLI and `ANTHROPIC_API_KEY`.

---

#### scheduler run — Start the scheduler process

```bash
python -m orchestration.cli scheduler run
```

Runs the scheduler as a **foreground process** that ticks every 60 seconds. Each tick:
1. Fires past-due task-level schedules
2. Evaluates schedule-based triggers (cron matches)
3. Evaluates state-based triggers (tasks in matching state)

The scheduler is a standalone process, independent of the Workstream Manager web server. Stop with `Ctrl+C` or via `scheduler stop`.

#### scheduler stop — Stop the scheduler

```bash
python -m orchestration.cli scheduler stop
```

Sends SIGTERM to the running scheduler process (identified by PID stored in `scheduler_state.yaml`).

#### scheduler status — Check scheduler status

```bash
python -m orchestration.cli scheduler status
```

Returns `{"running": true, "pid": 12345, "last_tick_at": "..."}` or `{"running": false}`.

#### scheduler tick — Execute a single tick

```bash
python -m orchestration.cli scheduler tick
```

Runs one scheduler tick immediately (useful for testing or manual triggering).

---

#### audit log — View workspace audit trail

```bash
# Recent events
python -m orchestration.cli audit log

# Filter by workstream
python -m orchestration.cli audit log --workstream WORKSTREAM_ID

# Filter by event type
python -m orchestration.cli audit log --type trigger_fired

# Limit results
python -m orchestration.cli audit log --limit 20
```

---

#### artifact create — Store a work product

```bash
python -m orchestration.cli artifact create --path "reports/q2_summary.md" --content "# Q2 Summary\n\nResults..."

# Optional mounted-routing context
python -m orchestration.cli artifact create --path "ideas/april.md" --content "..." --workstream WORKSTREAM_ID

# Save a raster image from base64 bytes
python -m orchestration.cli artifact create --path "assets/mockup.png" --content-base64 "iVBORw0KGgoAAA..." --workstream WORKSTREAM_ID

# Prefer source-file for large binary images
python -m orchestration.cli artifact create --path "assets/mockup.png" --source-file "/tmp/mockup.png" --workstream WORKSTREAM_ID
```

Important:
- `artifact create --content` is for text payloads. It writes string content to disk and is not suitable for arbitrary binary files.
- Raster image paths (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) reject `--content` and must use `--content-base64` or `--source-file`.
- For binary outputs produced by external tools, persist the real binary bytes first, then attach the artifact path to the task with `task attach`.

#### artifact read — Read an artifact

```bash
python -m orchestration.cli artifact read "reports/q2_summary.md"

# Optional mounted-routing context
python -m orchestration.cli artifact read "ideas/april.md" --workstream WORKSTREAM_ID
```

#### artifact list — List all artifacts

```bash
# All artifacts
python -m orchestration.cli artifact list

# Filter by path prefix
python -m orchestration.cli artifact list --prefix "reports/"
```

#### artifact copytree — Copy an artifact subtree into a mounted startup workspace

```bash
# Copy a Stage 2 subtree from source workspace artifacts into the mounted destination
# artifacts root for a specific workstream.
python -m orchestration.cli artifact copytree "Stage 2 Research/2026/04/calendly_plus" \
  --workstream WORKSTREAM_ID \
  --source-base /path/to/source/workspace

# Preview without writing files
python -m orchestration.cli artifact copytree "Stage 2 Research/2026/04/calendly_plus" \
  --workstream WORKSTREAM_ID \
  --source-base /path/to/source/workspace \
  --dry-run

# Allow overwriting conflicting destination files
python -m orchestration.cli artifact copytree "Stage 2 Research/2026/04/calendly_plus" \
  --workstream WORKSTREAM_ID \
  --source-base /path/to/source/workspace \
  --overwrite
```

Notes:
- Destination workstream must resolve to a mounted workspace. If it does not, the command fails.
- This command is intended for one-time Stage 3 setup artifact snapshotting into startup repos.
- Source path is always interpreted relative to the source workspace `artifacts/` root.

---

### Typical Agent Workflow

When working on tasks from a workstream, follow this pattern:

```bash
# 1. Find your workstream
python -m orchestration.cli workstream find --query "SDR"

# 2. List available tasks
python -m orchestration.cli task list WORKSTREAM_ID --status "pending"

# 3. Lock a task before working on it
python -m orchestration.cli lock acquire TASK_ID --agent my-agent-name

# 4. Do your work...

# 5. Update the task status when done
python -m orchestration.cli task update TASK_ID --status "completed"

# 6. Add a comment about what was done
python -m orchestration.cli task comment TASK_ID --message "Completed outreach, got positive response"

# 7. Release the lock
python -m orchestration.cli lock release TASK_ID --agent my-agent-name
```

If your work fails:

```bash
# Update status to failed
python -m orchestration.cli task update TASK_ID --status "failed"

# Add a comment explaining what went wrong
python -m orchestration.cli task comment TASK_ID --message "LinkedIn profile not found"

# Release the lock
python -m orchestration.cli lock release TASK_ID --agent my-agent-name
```

### Data Storage

All data is stored as YAML files on the local filesystem:

```
workstreams/
  {workstream_id}.yaml          # Workstream config, triggers
  {workstream_id}/
    .env                        # Workstream-local env keys (gitignored)
    tasks/
      {task_id}.yaml            # Task data, audit trail
      {task_id}.yaml.lock       # Lock file (if locked)
artifacts/
  {path}/{filename}             # Artifact files
Agents/
  {agent_name}.md               # Agent definitions
```
