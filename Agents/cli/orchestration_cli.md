## Orchestration Framework CLI

Manage workstreams, tasks, locks, triggers, env settings, artifacts, and agents with the installed `orc` command.

All commands output **JSON** to stdout on success, and a JSON error object to stderr on failure. Exit code 0 indicates success, non-zero indicates failure.

### Prerequisites

- Python 3.12 or newer
- `pip install pyyaml` (already in project)
- For agent execution: Claude Code CLI (`claude`) installed and `ANTHROPIC_API_KEY` set in `.env`, or Cline CLI (`cline`) installed and authenticated with `cline auth` when using `x-runtime: cline`

### General Usage

```bash
orc <concept> <method> [arguments]

# Optionally specify a different workspace root (default: current directory)
orc --base-dir /path/to/workspace <concept> <method> [arguments]
```

`orchestra` and `python -m orchestration` are compatibility forms; use `orc` in documentation and automation.

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
orc workstream create --name "SDR Outreach"

# With description and parent
orc workstream create --name "Email Campaign" --description "Q2 email outreach" --parent PARENT_WS_ID

# With custom task states (JSON string)
orc workstream create --name "Pipeline" --states '{"To Do": ["In Progress", "Invalid"], "In Progress": ["Done", "Failed"], "Done": [], "Failed": ["To Do"], "Invalid": []}'

# With retry configuration
orc workstream create --name "Retry WS" --retry '{"max_retries": 5, "backoff": "exponential", "base_seconds": 30}'

# Descendants-only mounted workspace root
orc workstream create --name "career_pivot" --mounted-workspace-path "~/career_pivot"
```

Default task states if `--states` is omitted:
- `pending` → `in_progress`
- `in_progress` → `completed`, `failed`
- `completed` → (terminal)
- `failed` → `pending`

#### workstream list — List all workstreams

```bash
orc workstream list
```

#### workstream read — Read a single workstream

```bash
orc workstream read WORKSTREAM_ID
```

#### workstream find — Search workstreams by name or description

```bash
orc workstream find --query "SDR"
```

#### workstream tree — Pretty-print the workstream hierarchy

```bash
orc workstream tree
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
orc workstream descendants WORKSTREAM_ID

# Include the root workstream in the output
orc workstream descendants WORKSTREAM_ID --include-self
```

Returns JSON rows with:
- `id`
- `name`
- `parent_id`
- `depth` (1 = direct child, 2 = grandchild, etc.; 0 for root when `--include-self` is used)

This is the recommended command for director-style agents that need to traverse a workstream subtree.

#### workstream pause — Pause a workstream

```bash
orc workstream pause WORKSTREAM_ID
```

Paused workstreams are skipped by the scheduler — no triggers will fire.

#### workstream resume — Resume a paused workstream

```bash
orc workstream resume WORKSTREAM_ID
```

---

#### env set — Set a workstream-local env key

```bash
orc env set WORKSTREAM_ID OPENAI_API_KEY "sk-..."
```

Writes key/value pairs into `workstreams/{workstream_id}/.env`.

#### env unset — Mask a key at this workstream level

```bash
orc env unset WORKSTREAM_ID OPENAI_API_KEY
```

`unset` writes `KEY=` in the workstream `.env` file. This explicitly masks inherited values from parent workstreams and also masks process-level environment fallback.

#### env get — Resolve a key in a workstream/task context

```bash
# Resolve using a workstream context
orc env get OPENAI_API_KEY --workstream WORKSTREAM_ID

# Resolve using a task context (task -> owning workstream)
orc env get OPENAI_API_KEY --task TASK_ID
```

Resolution order:
1. Current workstream `.env`
2. Parent workstream `.env` files up the hierarchy
3. Process environment (`os.environ`)

If any level defines `KEY=`, resolution stops and returns no value for that key.

#### env list — List env keys for a context

```bash
# Effective inherited map (default)
orc env list --workstream WORKSTREAM_ID

# Task-context effective map
orc env list --task TASK_ID

# Only keys physically present in selected workstream .env
orc env list --workstream WORKSTREAM_ID --local

# Include process environment fallback keys in effective output
orc env list --workstream WORKSTREAM_ID --include-system
```

---

#### task create — Create a new task in a workstream

```bash
orc task create WORKSTREAM_ID --title "Contact John Doe"

# With description, tags, and retry override
orc task create WORKSTREAM_ID --title "Send proposal" --description "Draft and send the proposal doc" --tags "sales,urgent" --retry '{"max_retries": 5}'
```

The task's initial status will be the **first state** in the workstream's `task_states` map.

#### task read — Read one or more tasks by ID

```bash
# One task returns a single task object
orc task read TASK_ID

# Multiple tasks return an array in the requested order
orc task read TASK_ID_1 TASK_ID_2 TASK_ID_3

# Audit history is omitted by default; include it when needed for debugging or provenance
orc task read TASK_ID --include-audit
```

Tasks are found by ID across all workstreams — you don't need to specify the workstream. A single ID returns one object; multiple IDs return an array in the same order as the arguments.

#### task update — Update a task's status, description, or tags

```bash
# Change status (validated against workstream's state transition map)
orc task update TASK_ID --status "In Progress"

# Update description
orc task update TASK_ID --description "Updated scope"

# Update tags
orc task update TASK_ID --tags "high-priority,q2"
```

**Important:** Status transitions are validated. If you try an invalid transition (e.g. jumping from "To Do" directly to "Done" when only "To Do" → "In Progress" is allowed), the command will fail with code `INVALID_TRANSITION`.

#### task list — List tasks in a workstream

```bash
# All tasks
orc task list WORKSTREAM_ID

# Filter by status
orc task list WORKSTREAM_ID --status "pending"

# Filter by tags (comma-separated, all must match)
orc task list WORKSTREAM_ID --tags "urgent,sales"
```

Tasks are returned in board order for each state (rank-based ordering), not creation time.

#### task move-up — Move a task one position up in its current state

```bash
orc task move-up TASK_ID
```

This only reorders within the task's current status column.

#### task move-down — Move a task one position down in its current state

```bash
orc task move-down TASK_ID
```

This only reorders within the task's current status column.

#### task move-before — Move a task directly before another task

```bash
orc task move-before TASK_ID TARGET_TASK_ID
```

Both tasks must be in the same workstream and status column.

#### task move-after — Move a task directly after another task

```bash
orc task move-after TASK_ID TARGET_TASK_ID
```

Both tasks must be in the same workstream and status column.

#### task move-to-index — Move a task to a zero-based index within its current state

```bash
orc task move-to-index TASK_ID INDEX
```

This only reorders within the task's current status column. Index values outside bounds are clamped.

#### task comment — Add a comment to a task

```bash
orc task comment TASK_ID --message "Reached out via email, waiting for response"

# Recommended for agents: explicitly set author
orc task comment TASK_ID --message "Reached out via email, waiting for response" --author "LinkedIn SDR"
```

Notes:
- For agent workflows, always pass `--author` so comments are attributed correctly in the UI.
- Do not prefix comment bodies with dates like `[2026-04-21]`; timestamps are stored separately.

#### task attach — Attach an artifact to a task

```bash
orc task attach TASK_ID --path <artifact_path>
```

Adds an artifact path to the task's attachment list. After saving any artifact via the orchestration system, attach it to any Task(s) you were provided with so that other agents and human users can easily find your outputs. This applies to all artifact types — markdown documents, images (SVG, PNG, etc.), and any other files.

#### task detach — Remove an artifact attachment from a task

```bash
orc task detach TASK_ID --path <artifact_path>
```

---

#### task archive — Delete a task (and its lock)

```bash
orc task archive TASK_ID
```

#### task audit — View a task's audit trail

```bash
orc task audit TASK_ID
```

Returns an array of audit entries, each with `timestamp`, `type`, and `description`.

#### task clear-schedule — Remove a task's scheduled action

```bash
orc task clear-schedule TASK_ID
```

---

#### task pause — Pause automatic processing for one task

```bash
orc task pause TASK_ID
```

The task remains in its current workstream and state, but state-based and schedule-based triggers will not pick it up while it is paused. Pausing an already paused task is idempotent.

#### task resume — Resume automatic processing for one task

```bash
orc task resume TASK_ID
```

The task becomes eligible for automatic trigger pickup again. Resuming an already active task is idempotent.

---

#### lock acquire — Lock a task before working on it

```bash
orc lock acquire TASK_ID --agent my-agent-name

# With custom TTL (default: 900 seconds / 15 minutes)
orc lock acquire TASK_ID --agent my-agent-name --ttl 3600
```

**You MUST acquire a lock before modifying a task.** This prevents two agents from working on the same task simultaneously. If the task is already locked by another agent, the command fails with code `TASK_LOCKED`.

Locks automatically expire after the TTL. Expired locks are treated as unlocked.

#### lock release — Release a lock you hold

```bash
orc lock release TASK_ID --agent my-agent-name
```

Only the agent that acquired the lock can release it. Returns an error if a different agent tries to release.

#### lock status — Check if a task is locked

```bash
orc lock status TASK_ID
```

Returns `{"locked": false}` or `{"locked": true, "agent_id": "...", "acquired_at": "...", "expires_at": "..."}`.

---

#### trigger create — Add a trigger to a workstream

```bash
# State-based trigger: runs an agent when a task enters a state
orc trigger create WORKSTREAM_ID --on-state "pending" --action run_agent --agent sdr

# State-based trigger: runs a shell command
orc trigger create WORKSTREAM_ID --on-state "Done" --action run_command --command "echo Task {task_id} in {workstream_id} is done"

# Schedule-based trigger: runs every minute on matching tasks
orc trigger create WORKSTREAM_ID --on-schedule "* * * * *" --action run_command --command "echo tick"

# Email-based trigger: dispatches an agent when a new inbound thread arrives
orc trigger create WORKSTREAM_ID --on-email-recipient "build@mail.example.com" --on-email-event new_thread --action run_agent --agent email_triage_agent

# Schedule-based trigger with task filter. Filters are conjunctive:
# state/status must match and every listed tag must be present.
orc trigger create WORKSTREAM_ID --on-schedule "*/5 * * * *" --filter '{"status": "pending", "tags": ["batch"]}' --action run_agent --agent sdr

# Singular tag is also supported for one required tag.
orc trigger create WORKSTREAM_ID --on-schedule "0 * * * *" --filter '{"state": "Live", "tag": "requestor_notify"}' --action run_agent --agent review_agent
```

Template variables `{task_id}` and `{workstream_id}` are replaced in `run_command` commands. Email triggers also populate `{email_from}`, `{email_to}`, `{email_subject}`, `{email_date}`, `{email_body}`, `{email_storage_key}`, `{email_attachment_count}`, and `{email_attachments_json}`.

All triggers are evaluated by the scheduler on each tick (every 60 seconds). State-based triggers match tasks currently in the specified state and evaluate them in task order, dispatching only the first currently unlocked match per trigger evaluation. Schedule-based triggers match on cron expressions. Email-based `new_thread` triggers poll the configured recipient on each tick and dispatch the configured action once per unseen inbound thread as a standalone workstream-scoped run. For `run_agent`, the inbound email is appended to the trigger prompt so the agent can decide whether to create or update tasks.

Concurrency for `run_agent` triggers is controlled at the workstream level, per agent, with default `1` run per agent:

```yaml
agent_concurrency:
  default: 1
  overrides:
    example_agent: 2
```

This policy is configured in workstream YAML. Trigger-level concurrency flags are deprecated.

#### trigger list — List triggers on a workstream

```bash
orc trigger list WORKSTREAM_ID
```

#### trigger delete — Remove a trigger

```bash
orc trigger delete TRIGGER_ID
```

---

#### agent list — List available agent definitions

```bash
orc agent list
```

Lists all `.md` files in the `Agents/` directory, showing name, description, and agent type.

#### agent run — Run an agent against a task

```bash
orc agent run AGENT_NAME --task TASK_ID
```

Parses the agent's `.md` file and executes it via Claude Code CLI against the specified task. Requires `claude` CLI and `ANTHROPIC_API_KEY`.

---

#### scheduler run — Start the scheduler process

```bash
orc scheduler run
```

Runs the scheduler as a **foreground process** that ticks every 60 seconds. Each tick:
1. Fires past-due task-level schedules
2. Evaluates schedule-based triggers (cron matches)
3. Evaluates state-based triggers (tasks in matching state)

The scheduler is a standalone process, independent of the Workstream Manager web server. Stop with `Ctrl+C` or via `scheduler stop`.

#### scheduler stop — Stop the scheduler

```bash
orc scheduler stop
```

Sends SIGTERM to the running scheduler process (identified by PID stored in `scheduler_state.yaml`).

#### scheduler status — Check scheduler status

```bash
orc scheduler status
```

Returns `{"running": true, "pid": 12345, "last_tick_at": "..."}` or `{"running": false}`.

#### scheduler tick — Execute a single tick

```bash
orc scheduler tick
```

Runs one scheduler tick immediately (useful for testing or manual triggering).

---

#### audit log — View workspace audit trail

```bash
# Recent events
orc audit log

# Filter by workstream
orc audit log --workstream WORKSTREAM_ID

# Filter by event type
orc audit log --type trigger_fired

# Limit results
orc audit log --limit 20
```

---

#### artifact create — Store a work product

```bash
orc artifact create --path "reports/q2_summary.md" --content "# Q2 Summary\n\nResults..."

# Optional mounted-routing context
orc artifact create --path "ideas/april.md" --content "..." --workstream WORKSTREAM_ID

# Save a raster image from base64 bytes
orc artifact create --path "assets/mockup.png" --content-base64 "iVBORw0KGgoAAA..." --workstream WORKSTREAM_ID

# Prefer source-file for large binary images
orc artifact create --path "assets/mockup.png" --source-file "/tmp/mockup.png" --workstream WORKSTREAM_ID
```

Important:
- `artifact create --content` is for text payloads. It writes string content to disk and is not suitable for arbitrary binary files.
- Raster image paths (`.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`) reject `--content` and must use `--content-base64` or `--source-file`.
- For binary outputs produced by external tools, persist the real binary bytes first, then attach the artifact path to the task with `task attach`.

#### artifact read — Read an artifact

```bash
orc artifact read "reports/q2_summary.md"

# Optional mounted-routing context
orc artifact read "ideas/april.md" --workstream WORKSTREAM_ID
```

Behavior:
- Text artifacts return `mode: "text"`, UTF-8 `content`, and `resolved_path`.
- Binary artifacts return `mode: "binary"`, `resolved_path`, byte count, and inferred `content_type`.
- Use `resolved_path` when a downstream tool needs to upload or otherwise open the binary file directly.

#### artifact list — List all artifacts

```bash
# All artifacts
orc artifact list

# Filter by path prefix
orc artifact list --prefix "reports/"
```

#### artifact copytree — Copy an artifact subtree into a mounted startup workspace

```bash
# Copy a research subtree from source workspace artifacts into the mounted destination
# artifacts root for a specific workstream.
orc artifact copytree "Research/example_project" \
  --workstream WORKSTREAM_ID \
  --source-base /path/to/source/workspace

# Preview without writing files
orc artifact copytree "Research/example_project" \
  --workstream WORKSTREAM_ID \
  --source-base /path/to/source/workspace \
  --dry-run

# Allow overwriting conflicting destination files
orc artifact copytree "Research/example_project" \
  --workstream WORKSTREAM_ID \
  --source-base /path/to/source/workspace \
  --overwrite
```

Notes:
- Destination workstream must resolve to a mounted workspace. If it does not, the command fails.
- This command is intended for one-time artifact snapshotting into project repositories.
- Source path is always interpreted relative to the source workspace `artifacts/` root.

---

### Typical Agent Workflow

When working on tasks from a workstream, follow this pattern:

```bash
# 1. Find your workstream
orc workstream find --query "SDR"

# 2. List available tasks
orc task list WORKSTREAM_ID --status "pending"

# 3. Lock a task before working on it
orc lock acquire TASK_ID --agent my-agent-name

# 4. Do your work...

# 5. Update the task status when done
orc task update TASK_ID --status "completed"

# 6. Add a comment about what was done
orc task comment TASK_ID --message "Completed outreach, got positive response"

# 7. Release the lock
orc lock release TASK_ID --agent my-agent-name
```

If your work fails:

```bash
# Update status to failed
orc task update TASK_ID --status "failed"

# Add a comment explaining what went wrong
orc task comment TASK_ID --message "LinkedIn profile not found"

# Release the lock
orc lock release TASK_ID --agent my-agent-name
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
