# Guidance for Implementations
The orchestration system described in `./spec.md` can be implemented in a number of ways, and the specific implementation will depend on the needs and constraints of the project.

However, these are some general guidelines for implementations:

### Agents
Agents are defined in .md files.

The .md file should have a YAML header with the following fields:
- name (string)
- description (string)
- x-agent-type (string, one of "worker", "manager", "director", "executive")

The body of the .md file should describe the agent's behavior and how it interacts with the orchestration system.

### Workstreams
#### Data Schema
`Workstreams` will likely have the following attributes:
- Unique ID (uuid)
- Parent ID (uuid, optional)
- Name (string)
- Description (text, optional)
- Task state map (dictionary defining each state and the allowed transitions, e.g. {"To Do": ["In Progress", "Invalid"], "In Progress": ["Done", "Invalid"], "Done": [], "Invalid": []})
- Retry setup (optional, defines if and how tasks in the workstream should be retried on failure), e.g. an array of retry times in seconds, or a string referencing a known retry strategy (e.g. "exponential_backoff")
- 0-n `Tasks`
- 0-n `Triggers` (each defining a state that triggers and action and the action - e.g. a command line to run or an agent to run)

#### Methods / Tools
`Workstream` methods/tools might include:
- create `Workstream` (name, description, task_states, retry setup)
- index `Workstreams`
- find `Workstream` (query)
- read `Workstream` (id)
- get `Tasks` (state filter, tags filter, etc)
- index `Triggers` (list of triggers on a workstream)
- create `Trigger` (state, action)
- delete `Trigger` (id)

#### Persistence
`Workstreams` might be persisted in:
- local file system (good for local dev and running things locally). Can be combined with Git for version control and history (but not live updates or locking)
- Github projects (a natural fit for software development, and where the humans involved are engineers)
- Trello boards (great for human collaboration)
- database (good for production, allows for better querying and indexing)

### Tasks
#### Data Schema
`Tasks` will likely have the following attributes:
- Unique ID (uuid)
- Workstream ID (uuid)
- Title (string)
- Description (text, optional)
- Status (string, allowed values depend on workstream)
- Creator (string, optional, typically the name of the agent or human who created the task)
- Tags (array of strings, optional)
- Retry setup (optional, overrides workstream retry setup if present, and system defaults if not)
- Comments (array of strings, optional)
- Audit trail (array of 0-n events, each event could be a dictionary with fields like timestamp, type, description)

#### Methods / Tools
`Task` methods/tools might include:
- create `Task`
- read `Task`
- update `Task`
- archive `Task`

#### Persistence
`Tasks` might be persisted in:
- local file system (good for local dev and running things locally)
- Github issues (a natural fit for software development, and where the humans involved are engineers)
- Trello cards (great for human collaboration)
- database (good for production, allows for better querying and indexing)

### Task Locks
`Task Locks` might be implemented as file locks (if using local file system for persistence), using row locks in a database, or as a specific field in the task data (e.g. "locked_by" and "locked_at" fields) if using a database or other storage mechanism.

### Triggers
#### Data Schema
`Triggers` will likely have the following attributes:
- Unique ID (uuid)
- Workstream ID (uuid)
- State (string, the state that triggers the action when a task enters it)
- Action (dictionary defining the action to take, e.g. {"type": "run_agent", "agent_name": "SDR Outreach Agent"}, or {"type": "send_notification", "channel": "slack-sales", "message": "A task has been completed in the SDR Outreach workstream!"}, or {"type": "run_script", "script_path": "scripts/update_crm.py"})

### Artifacts
#### Persistence
`Artifacts` might be persisted in:
- local file system (good for local dev and running things locally)
- Github repositories (a natural fit for software development, and where the humans involved are engineers)
- cloud storage (e.g. AWS S3, good for production and large files)