## Orchestration Framework CLI

Manage workstreams, tasks, locks, triggers, artifacts, and agents from the command line using `orchestration/cli.py`.

All commands output **JSON** to stdout on success, and a JSON error object to stderr on failure. Exit code 0 indicates success, non-zero indicates failure.

### Prerequisites

- Python 3.x
- `pip install pyyaml` (already in project)
- For agent execution: `pip install crewai` and `ANTHROPIC_API_KEY` set in `.env`

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

#### task comment — Add a comment to a task

```bash
python -m orchestration.cli task comment TASK_ID --message "Reached out via email, waiting for response"
```

#### task archive — Delete a task (and its lock)

```bash
python -m orchestration.cli task archive TASK_ID
```

#### task audit — View a task's audit trail

```bash
python -m orchestration.cli task audit TASK_ID
```

Returns an array of audit entries, each with `timestamp`, `type`, and `description`.

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
# Trigger that runs an agent when a task enters a state
python -m orchestration.cli trigger create WORKSTREAM_ID --on-state "pending" --action run_agent --agent sdr

# Trigger that runs a shell command
python -m orchestration.cli trigger create WORKSTREAM_ID --on-state "Done" --action run_command --command "echo Task {task_id} in {workstream_id} is done"
```

Template variables `{task_id}` and `{workstream_id}` are replaced in `run_command` commands.

Triggers fire automatically when a task enters the specified state (including on initial creation).

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

Parses the agent's `.md` file, creates a CrewAI agent, and executes it against the specified task. Requires CrewAI and `ANTHROPIC_API_KEY`.

---

#### artifact create — Store a work product

```bash
python -m orchestration.cli artifact create --path "reports/q2_summary.md" --content "# Q2 Summary\n\nResults..."
```

#### artifact read — Read an artifact

```bash
python -m orchestration.cli artifact read "reports/q2_summary.md"
```

#### artifact list — List all artifacts

```bash
# All artifacts
python -m orchestration.cli artifact list

# Filter by path prefix
python -m orchestration.cli artifact list --prefix "reports/"
```

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
    tasks/
      {task_id}.yaml            # Task data, audit trail
      {task_id}.yaml.lock       # Lock file (if locked)
artifacts/
  {path}/{filename}             # Artifact files
Agents/
  {agent_name}.md               # Agent definitions
```
