# Orchestration Framework Specification
## Introduction
This specification defines the core concepts and interfaces for an agent orchestration framework. 

The goal is to have a clear and flexible structure that allows for different implementations of the underlying mechanics while maintaining a consistent way for agents to interact with the orchestration system.

This spec is intentionally implementation-agnostic, meaning it does not prescribe how the underlying mechanics should work (e.g. where data is persisted), or which language it should be implemented in, but rather defines the core concepts and interfaces that any implementation should adhere to.

In some cases, recommendations are made for the data schema but these are more about providing clarity versus being strict requirements. The key is that the data schema is well-defined and consistent within any given implementation, and that it captures all the necessary information for the agents to do their work and for the system to track progress.

Specific implementations will take this spec as input and build the underlying mechanics.

## Core Concepts
### Summary
- Agents
- Agent types: Worker, Manager, Director, Executive
- Workstreams
- Tasks
- Task Locks
- Triggers
- Retry Strategies
- Audits
- Artifacts
- Agent Runners

### Agents
`Agents` are the entities that perform work.

Agents are typically defined in a markdown file with a YAML header that includes metadata about the agent (e.g. name, description, type, etc.) and a body that describes the agent's behavior and how it interacts with the orchestration system.

### Agent Types
There are different types of agents based on their role in the orchestration system:
- `Worker` - does a task, often repeatedly. Unless otherwise stated, can be parallelized - e.g. an SDR agent where each agent is working through one lead at a time from a list.
- `Manager` - builds lists of tasks for workers to execute - e.g. writing a list of LinkedIn articles with a publishing calendar, building a list of targets.
- `Director` - manages the overall plan in a functional area, given a big picture objective. e.g. "we need to sign up as many customers as possible". Generates plans to achieve objectives. Creates progress reports.
- `Executive` - sets overall picture objectives. Owns whole projects. Monitors director’s progress reports and synthesizes them. Interfaces with humans to answer questions and take feedback.

### Workstreams
`Workstreams` are the containers of `Tasks`. Each `Workstream` contains 0-n `Tasks`.

`Workstreams` are hierarchical in that a `Workstream` can have a parent.

Conceptually, a `Workstream` is like a Kanban / Trello board or a Github project, and `Tasks` are like cards on the board or issues in the project.

`Workstreams` define the allowed states for their `Tasks` and the allowed transitions between those states.

Conceptually, a Task's state is like the column that a card is in on a Trello board.

The default states are "pending", "in_progress", and "completed", but a `Workstream` can define any states and transitions it wants.

### Tasks
`Tasks` represent units of work.

`Tasks` are attached to one and only one `Workstream`.

`Tasks` have a state.

`Tasks` have 0-n tags.

`Tasks` have a title (mandatory) and description (optional).

### Task Locks
To allow for parallelization, it's critical that a safe locking mechanism is provided for tasks so that two instances of an agent don't try to work on or update a task simultaneously.

A particular implementation of the orchestration framework will provide a way for agents to acquire and release locks on tasks, and to handle cases where a lock cannot be acquired (e.g. because another agent has it). The specific locking mechanism used will vary based on implementation.

### Triggers
`Triggers` are actions that fire when a `Task` enters a specific state, including when it is initially created.

`Triggers` are defined by the `Workstream`.

The primary use case for a `Trigger` is to run a specific `Agent` when a `Task` enters a specific state, e.g. run the "SDR Outreach Agent" when a `Task` enters the "pending" state in the "SDR Outreach" `Workstream`.

However, `Triggers` can also support a number of other use cases, including:
- sending a notification (e.g. send a Slack message to the sales channel when a `Task` enters the "completed" state in the "SDR Outreach" `Workstream`)
- running a script (e.g. run a Python script to update a CRM when a `Task` enters the "completed" state in the "SDR Outreach" `Workstream`)

### Retry Strategies
`Retry Strategies` define if and how `Tasks` should be retried on failure.

The system should implement sensible defaults for retrying tasks (e.g. exponential backoff with a maximum number of retries), but the `Workstream` and/or `Task` can override these defaults with specific retry strategies.

### Audits
Each `Task` has an audit trail that logs all significant events related to that task, including state changes, comments, and any other relevant actions.

### Artifacts
An `Artifact` represents any work product (output) generated by an `Agent`.

Other `Agents` can consume `Artifacts` as inputs to their work.

Most `Artifacts` will be markdown (.md) files but this specification is agnostic.

An `Artifact` has a name and path. The path and name combination should be globally unique across all `Artifacts` in the workspace.

### Agent Runners
`Agent Runners` encapsulate the execution of an agent.

They are the container that provides the environment in which the agent runs.

`Agent Runners` are responsible for:
- providing the agent with access to the orchestration system (e.g. to read and write `Tasks`, to acquire `Task Locks`, to read and write `Artifacts`, etc)
- handling error conditions and retries
- logging errors for debugging
- exposing the state to the outside world so that the rest of the framework can know if the agent is dead or not
- ensuring that any Task Locks the agent obtains are released when the agent is done or if it dies

The `Agent Runner` will typically update the `Task` to record retries, errors, etc but implementations may opt to maintain their 
own separate state.

