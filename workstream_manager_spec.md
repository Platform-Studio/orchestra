# Workstream Manager Spec
This document defines the Workstream Manager, which is a web app through which humans can visualize and manage the workstreams and tasks in the orchestration system.

This is a key part of the Platform way of building, as it allows for transparency, collaboration, and efficient management of the work being done by the various agents and humans involved in the process.

## Information Architecture
The Workstream Manager will follow the conventions of Trello:

- Workstream states represent columns that flow left to right
- Tasks are represented as cards that can be moved between columns (i.e. states) according to the allowed transitions defined in the workstream setup
- Tasks can have tags, descriptions, comments, and an audit trail of events (e.g. status changes, comments added, etc.)
- Clicking on a Task will show the details of the Task, including its description, tags, comments, and audit trail
- When an Agent has a lock on a Task, that should be shown on the Task card and Task details in the same way that Trello shows the avatars of people who have a card open, to indicate that an Agent is working on the Task.
- When a Task has a Task-specific schedule, that should be shown on the card with an appropriate icon, and the details should be shown in the Task details view.
- Workstream Triggers should be viewable in the Workstream details, showing which states have which triggers, and what those triggers do (e.g. run an agent, run a command, etc.)

Additionally, because the workstreams can be nested, the Workstream Manager should allow for viewing the workstreams in a hierarchical way, with the ability to drill down into child workstreams.

## UX Design
The UX design of the Workstream Manager should follow the conventions of Trello, with a clean and intuitive interface that allows users to easily visualize and manage the workstreams and tasks.

Follow Trello's design patterns and conventions as closely as possible.

The only addition to the standard Trello-like interface is that there should be a left sidebar that shows the hierarchy of workstreams, allowing users to navigate between different workstreams and see the parent-child relationships between them.

## Limited Edit Functionality
The current implementation of Workstream Manager will be almost exclusively read-only. The user will not be able to edit/move/delete tasks.

The only action the user can take is to pause and resume workstreams.

## User Stories
- As a user, I want to be able to see all the workstreams in a hierarchical view, so that I can understand how the workstreams are organized and related to each other.

- As a user, I want to be able to click on a workstream in the sidebar and see the details of that workstream, including its states, triggers, and tasks.

- As a user, I want to be able to see the tasks in each workstream laid out by state in columns, left-to-right, so that I can understand the status of the work being done in that workstream.

- As a user, I want to be able to see the tasks in each workstream, the state of the workstream (paused vs. active), and the task details (description, tags, comments, audit trail), so that I can understand what work is being done and the history of that work.

- As a user, I want to be able to pause and resume a workstream, so that I can control when work is being done in that workstream.

- As a user, I want to be able to see which Agent, if any, has a lock on a Task, so that I can understand when an Agent is working on a Task.

- As a user, I want to see the status of scheduling, so that I know if the scheduler is active, and when the last time it ran

- As a user, I want to see live data in the Workstream Manager, so that I can see the current state of the workstreams and tasks without needing to refresh the page.

## Implementation
It is important that the Workstream Manager uses the orchestration system's interface rather than directly accessing the data store, because the underlying data store may change, and the orchestration system's interface provides a layer of abstraction that allows for that flexibility. Additionally, using the orchestration system's interface ensures that all interactions with the workstreams and tasks go through the proper channels, including any necessary locking, triggering, and other logic that is handled by the orchestration system.