# Implementation Plan - Python
## Introduction
This is a plan for implementing the orchestration framework defined in `./spec.md` in Python using the local file system for data persistence.

This is just one possible implementation.

## Implementation Guidance
Review the `./implementation_guidance.md` file for guidance on how to implement the orchestration framework, including recommended data schemas and interfaces for some of the key concepts.

## Language
The implementation will be made in Python 3.x.

## Test Coverage
It is ESSENTIAL that you write tests for your implementation, to ensure that it works correctly and to prevent regressions as you iterate on the implementation.

After each significant change, run the tests to ensure that everything is still working as expected.

## Data Persistence
For data persistence, we will use the local file system, for simplicity and ease of development.

Files will be written relative to the root of the current project the agents are running in.

### Workstreams
Workstreams will be persisted as YAML files in a `workstreams` directory in the current project workspace. 

Each workstream will be a separate YAML file named with the workstream's unique ID (i.e. `workstreams/{workstream_id}.yaml`).

Example workstream YAML file:
```yaml
id: "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
name: "SDR Outreach"
description: "Manage outbound sales development leads"
parent_id: null
task_states:
  To Do: ["In Progress", "Invalid"]
  In Progress: ["Done", "Failed", "Invalid"]
  Failed: ["To Do", "Invalid"]
  Done: []
  Invalid: []
retry:
  max_retries: 3
  backoff: "exponential"
  base_seconds: 60
triggers:
  - id: "t1"
    on_state: "To Do"
    action: "run_agent"
    agent: "sdr"
  - id: "t2"
    on_state: "Done"
    action: "run_command"
    command: "python notify.py --channel slack --message 'Task completed'"
  - id: "t3"
    on_schedule: "0 * * * *"           # every hour
    filter:
      state: "waiting_for_reply"
    action: "run_agent"
    agent: "follow_up"
  - id: "t4"
    on_schedule: "0 0 * * *"           # daily
    filter:
      state: "Done"
      older_than_days: 30
    action: "run_agent"
    agent: "archiver"
```

### Tasks
Tasks will be persisted as YAML files in a subdirectory of the workstream's directory, named `tasks`. 

Each task will be a separate YAML file named with the task's unique ID (i.e. `workstreams/{workstream_id}/tasks/{task_id}.yaml`).

Task YAML files may include optional scheduling fields:
```yaml
scheduled_at: "2026-04-15T09:00:00Z"   # when the scheduled action should fire
scheduled_action:                        # uses the same format as trigger actions
  type: "run_agent"
  agent: "publisher"
```

When `scheduled_at` is set and the current time reaches or exceeds it (and the workstream is not paused), the system fires the `scheduled_action`. After firing, both fields are cleared from the task. This is a one-shot mechanism.

### Task Locks
Locks will be implemented as files in the same directory as the task they are locking, with the same name as the task file but with a `.lock` extension (i.e. `workstreams/{workstream_id}/tasks/{task_id}.yaml.lock`).

If the file exists and has not expired, the Task is locked. If the file does not exist, or exists but has expired, the Task is unlocked.

Lock files will contain the following YAML data:
```yaml
agent_id: "sdr-worker-1"       # identifier of the agent holding the lock
acquired_at: "2026-04-10T14:30:00Z"
expires_at: "2026-04-10T14:45:00Z"   # default TTL: 15 minutes
```

When acquiring a lock, the system will:
1. Check if a lock file exists and is not expired
2. If no valid lock exists, atomically create the lock file (using `os.open` with `O_CREAT | O_EXCL` for filesystem-level atomicity)
3. If a valid lock exists, return an error indicating the task is locked

When releasing a lock, the system will delete the lock file. Expired locks will be treated as unlocked and can be overwritten.

### Retry Strategy
The default retry strategy will be to retry a task up to 3 times with exponential backoff (i.e. wait 1 minute before the first retry, 2 minutes before the second retry, and 4 minutes before the third retry).

Workstream-level retry strategies can be defined in the workstream YAML file, and task-level retry strategies can be defined in the task YAML file, which will override the workstream-level strategy.

## Triggers
Triggers will be stored in the workstream YAML file as part of the workstream's data.

A trigger fires on one of two conditions:
- **State-based** (`on_state`): fires when a task enters a specific state. The system evaluates these when a task changes state.
- **Schedule-based** (`on_schedule`): fires on a recurring time-based schedule (cron expression), against tasks matching a filter. The system evaluates these on each scheduler tick (see "Scheduler" section below).

State-based trigger execution is synchronous by default — the state change completes, triggers are evaluated, and matched triggers are executed sequentially.

Schedule-based triggers include a `filter` that selects which tasks the action applies to. Supported filter fields:
- `state` (string) — match tasks in this state
- `tags` (array of strings) — match tasks with any of these tags
- `older_than_days` (integer) — match tasks whose `created_at` is older than this many days

When a schedule-based trigger fires, the system queries for all tasks in the workstream matching the filter and executes the action for each matched task.

Schedule-based triggers do not fire if the workstream is paused.

Both trigger types support two action types:
- `run_agent` — invokes the named agent via CrewAI, passing the task ID as input: `python orchestration.py agent run <agent_name> --task <task_id>`
- `run_command` — executes an arbitrary shell command, with `{task_id}` and `{workstream_id}` available as template variables

## Audits
The audit trail for each task will be stored as part of the task's YAML file. Each time a significant event occurs (e.g. state change, comment added), an entry will be added to the audit trail with a timestamp, type, and description of the event.

## Agent Definitions
Agents will be defined in .md files in an `Agents` directory in the current project workspace.

## Artifacts
Artifacts will be stored in an `artifacts` directory in the current project workspace. Each artifact will be a separate file named with the artifact's unique name (i.e. `artifacts/{artifact_path}/{artifact_name}`).

## Agent Runners
CrewAI will be used as the agent runner framework for this implementation.

See https://github.com/crewaiinc/crewai for more details on how to define agents and run them with CrewAI.

### Agent Definition Mapping
Agent `.md` files in the `Agents/` directory will be parsed to create CrewAI `Agent` objects:
- The YAML header fields `name`, `description`, and `x-agent-type` map to CrewAI's `role`, `goal`, and `backstory` respectively
- The body of the `.md` file is passed as additional instructions in the agent's `backstory`
- Tools are provisioned dynamically — see "Dynamic Tool Discovery" below

### Agent Name Resolution
The agent runner must accept agent references in multiple forms, since triggers, CLI users, and other agents may refer to agents differently:
- **Bare name**: `"sorter"` → resolves to `Agents/sorter.md`
- **Filename**: `"sorter.md"` → resolves to `Agents/sorter.md`
- **Relative path**: `"Agents/sorter.md"` → resolves relative to the workspace root

Resolution order:
1. If the reference contains a path separator or starts with `Agents/`, try it as a relative path from the workspace root
2. Otherwise, strip `.md` suffix and any `Agents/` prefix, then look for `Agents/{bare_name}.md`
3. Fall back to case-insensitive filename match in `Agents/`
4. Raise `FileNotFoundError` if no match

### Dynamic Tool Discovery
Tools are provisioned for agents dynamically, based on what's available in `Agents/cli/` and what the agent declares in its YAML header.

#### CLI Tool Convention
Each CLI tool in `Agents/cli/` consists of a **paired** `.py` and `.md` file:
- `Agents/cli/browser.py` — the executable script
- `Agents/cli/browser.md` — usage documentation (passed to the LLM as the tool description)

A `.py` file without a matching `.md` file is **ignored** (not registered as a tool). This is intentional — the `.md` file serves as the LLM-readable usage guide and is required.

#### Tool Discovery Process
1. Scan `Agents/cli/` for all `.py` files
2. For each `.py` file, check if a matching `.md` file exists
3. If both exist, register the tool using the stem as the tool name (e.g. `browser.py` + `browser.md` → tool name `browser`)
4. Read the `.md` file content as the tool's description (truncated to 4000 chars for LLM context limits)

#### Agent Tool Declarations (`x-tools`)
Agents declare which CLI tools they need via the `x-tools` field in their YAML header:

```yaml
---
name: My Agent
description: Does things
x-tools: [browser, trello]
---
```

#### Tool Provisioning (`build_tools_for_agent`)
When building the tool list for an agent:
1. **Always included**: `FileReadTool` (from crewai_tools) — lets agents read files in the workspace
2. **Always included**: `orchestration` — a built-in tool wrapping `python -m orchestration.cli`, with its description sourced from `Agents/cli/orchestration_cli.md` if that file exists
3. **Per-agent opt-in**: Each name in `x-tools` is matched against discovered CLI tools. If found, a CrewAI `BaseTool` wrapper is created for it

If an agent declares `x-tools: [orchestration]`, it's a no-op since orchestration is always included.

#### CrewAI BaseTool Wrappers
Each CLI tool wrapper is a CrewAI `BaseTool` subclass that:
- Accepts a single string argument (the command-line arguments to pass after `python3 <script>.py`)
- Runs the command via `subprocess.run` with `shell=True`, `capture_output=True`, 120-second timeout
- Returns stdout on success, or a formatted error string on failure
- Uses a **closure-based class definition** (not `type()`) to avoid Pydantic private-attribute issues:

```python
def _make_cli_tool(tool_name, py_path, description, base_dir):
    from crewai.tools import BaseTool

    class CLITool(BaseTool):
        name: str = tool_name
        description: str = f"Run the {tool_name} CLI tool..."

        def _run(self, command: str) -> str:
            return _run_cli_tool(abs_py, command, abs_base)

    CLITool.__name__ = f"CLITool_{tool_name}"
    CLITool.__qualname__ = f"CLITool_{tool_name}"
    return CLITool()
```

The `__name__`/`__qualname__` override prevents CrewAI from deduplicating tools that share the same class name.

The orchestration tool follows the same pattern but runs `python -m orchestration.cli <args>` with a 30-second timeout.

#### Adding a New Tool
To make a new tool available to agents:
1. Create `Agents/cli/foo.py` (the executable)
2. Create `Agents/cli/foo.md` (usage docs for the LLM)
3. Add `foo` to the `x-tools` list in any agent that should use it

No code changes to the framework are needed.

### Agent Execution
When an agent is run (either via CLI or trigger), the system will:
1. Resolve the agent reference to an absolute file path (see "Agent Name Resolution")
2. Parse the agent's `.md` file to extract YAML header and body
3. Load the orchestration task and its workstream for context
4. Build the tool list via `build_tools_for_agent`
5. Create a CrewAI `Agent` with role=name, goal=description, backstory=body, tools, llm, verbose=False, allow_delegation=False
6. Build a rich task description including: task title, ID, workstream name/ID, current status, valid next states, and instructions for using the orchestration tool
7. Create a single-agent `Crew` and call `kickoff()`
8. **Do NOT write to the task file after kickoff** — the agent updates the task via CLI during execution. The runner only re-reads the task to get the final state for its return value. This avoids a race condition where the runner's post-kickoff write would overwrite or corrupt changes the agent already made.
9. Return `{"agent": name, "task_id": id, "result": str(result)}`
10. Release any locks held by the agent

## LLM
The default LLM will be `anthropic/claude-opus-4-6`, but this can be overridden by setting the `CREWAI_LLM` environment variable to a different model string.

## Environment Variables
The implementation must load environment variables from a `.env` file at startup using `python-dotenv` (with a graceful fallback if the package isn't installed). This is critical because agents invoked via triggers run as subprocesses and need API keys to be available.

```python
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
```

The following environment variables will be used for configuration:
    ANTHROPIC_API_KEY   - Required. Your Anthropic API key.
    SERPER_API_KEY      - Optional. Enables web search via Serper.dev.
    CREWAI_LLM         - Optional. Override the LLM model string (default: anthropic/claude-opus-4-6).

## Interface
The implementation will be based on a Command Line Interface (CLI) that agents can run to perform actions such as creating workstreams, creating tasks, updating task status, etc.

The structure of command line calls will be as follows:

```
python orchestration.py <concept> <method> [arguments]
```

Where:
- `<concept>` is one of `workstream`, `task`, `lock`, `trigger`, `artifact`, `agent`, `scheduler`
- `<method>` is a method defined for that concept (see below)
- `[arguments]` are the arguments required for that method

### CLI Output
All CLI commands will output JSON to stdout on success, and a JSON error object to stderr on failure. Exit code 0 indicates success, non-zero indicates failure.

Success example:
```json
{"status": "ok", "data": {"id": "abc-123", "title": "Send outreach email"}}
```

Error example:
```json
{"status": "error", "message": "Task is locked by agent sdr-worker-1", "code": "TASK_LOCKED"}
```

### CLI Commands

#### Workstream
- `workstream create --name <name> [--description <desc>] [--parent <id>] [--states <json>] [--retry <json>]`
- `workstream list`
- `workstream read <id>`
- `workstream find --query <query>`
- `workstream tree` — pretty-print the hierarchy of all workstreams as an indented tree

#### Task
- `task create <workstream_id> --title <title> [--description <desc>] [--tags <tags>] [--retry <json>] [--scheduled-at <datetime>] [--scheduled-action <json>]`
- `task read <task_id>`
- `task update <task_id> [--status <status>] [--description <desc>] [--tags <tags>] [--scheduled-at <datetime>] [--scheduled-action <json>]`
- `task clear-schedule <task_id>` — clear the `scheduled_at` and `scheduled_action` fields

When `task create` or `task update` is called with `--scheduled-at`, the system will automatically call `Scheduler.ensure_started()` (see "Scheduler" section below) to ensure the scheduler cron job is running.
- `task list <workstream_id> [--status <status>] [--tags <tags>]`
- `task comment <task_id> --message <message>`
- `task archive <task_id>`
- `task audit <task_id>` — display the audit trail

State transitions are validated against the workstream's `task_states` map. Invalid transitions will be rejected with an error.

#### Lock
- `lock acquire <task_id> --agent <agent_id> [--ttl <seconds>]`
- `lock release <task_id> --agent <agent_id>`
- `lock status <task_id>`

#### Trigger
- `trigger list <workstream_id>`
- `trigger create <workstream_id> --on-state <state> --action <type> [--agent <name>] [--command <cmd>]` — create a state-based trigger
- `trigger create <workstream_id> --on-schedule <cron> --filter <json> --action <type> [--agent <name>] [--command <cmd>]` — create a schedule-based trigger
- `trigger delete <trigger_id>`

When `trigger create` is called with `--on-schedule`, the system will automatically call `Scheduler.ensure_started()` (see "Scheduler" section below) to ensure the scheduler cron job is running.

#### Agent
- `agent list` — list available agent definitions
- `agent run <agent_name> --task <task_id>` — run an agent against a specific task

#### Artifact
- `artifact create --path <path> --content <content_or_file>`
- `artifact read <path>`
- `artifact list [--prefix <prefix>]`

#### Scheduler
- `scheduler start` — ensure the scheduler cron job is running (idempotent: if already running, responds with a message saying so)
- `scheduler stop` — remove the scheduler cron job (idempotent: if already stopped, responds with a message saying so)
- `scheduler status` — report whether the scheduler is currently running and when the last tick occurred
- `scheduler tick` — execute one scheduler tick immediately (used by the cron job; can also be called manually for testing)

## Scheduler
Schedule-based triggers and task-level schedules require a periodic process to evaluate them.

### Lifecycle: start, stop, tick
The scheduler is managed via three CLI commands:

- **`scheduler start`** — installs a system cron job (via `crontab` on macOS/Linux) that runs `python orchestration.py scheduler tick` every minute. The cron entry will be tagged with a unique comment (e.g. `# orchestration-scheduler:<workspace_path>`) so it can be identified. If a cron job with that tag already exists, the command does nothing and responds: `{"status": "ok", "message": "Scheduler is already running"}`.

- **`scheduler stop`** — removes the tagged cron entry. If no matching cron entry exists, the command does nothing and responds: `{"status": "ok", "message": "Scheduler is not running"}`.

- **`scheduler status`** — checks whether the tagged cron entry exists and reports the `last_tick_at` from `scheduler_state.yaml` if available.

- **`scheduler tick`** — executes one evaluation cycle. This is what the cron job calls. It can also be called manually for testing.

### Auto-Start
The scheduler is automatically started when needed. The system provides a `Scheduler.ensure_started()` method that is called internally by:
- `trigger create` when the trigger has `on_schedule`
- `task create` or `task update` when `scheduled_at` is set

`Scheduler.ensure_started()` simply checks if the cron job exists and creates it if not — the same logic as `scheduler start`.

This means users and agents don't need to remember to start the scheduler manually — it activates automatically the first time a schedule is created.

### Tick Behavior
When `scheduler tick` runs, it will:
1. Load all workstreams that are not paused
2. **Task-level schedules**: For each workstream, find all tasks where `scheduled_at` is set and has passed. Fire the `scheduled_action` for each, then clear the `scheduled_at` and `scheduled_action` fields on the task. Add an audit entry.
3. **Schedule-based triggers**: For each workstream, evaluate each trigger that has `on_schedule`. If the cron expression has matched at any point since the last tick, query for tasks matching the trigger's `filter` and fire the action for each matched task.
4. Update `last_tick_at` in `scheduler_state.yaml`.

The tick command is stateless apart from `scheduler_state.yaml` — it determines what to fire based on the current time and the data on disk. To avoid duplicate firings of schedule-based triggers, the system stores a `last_tick_at` timestamp in `scheduler_state.yaml` in the workspace root. On each tick, it evaluates whether each cron expression has matched at any point between `last_tick_at` and now.

The cron job runs every minute, matching the minimum cron granularity.

## No Daemon / Background Process
This implementation does not include a long-running daemon. Instead, agents directly invoke CLI commands to perform actions, state-based triggers fire synchronously during state changes, and time-based concerns are handled by the system cron job installed via `scheduler start`. The cron job simply calls `scheduler tick` every minute — the tick process runs, evaluates, and exits.