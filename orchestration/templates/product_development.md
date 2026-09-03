# Product Development Workstream

This template defines a compact product delivery process shared by humans and AI agents. Use it for feature, bug, and engineering chore tasks in a repository with documented development, test, and deployment procedures.

## Human and Agent Responsibilities

Humans create tasks in `Backlog`, define acceptance criteria, prioritize ready work, make consequential product decisions, provide credentials and approvals, and may pause or redirect work at any time.

Agents plan, implement, review, test, unblock, and deploy work in their assigned lanes. Before every state change, the agent adds a normal task comment recording what it did, relevant evidence, remaining risks, and why the next lane owns the work.

## States and Transitions

```text
Backlog -> On Deck -> Implementation Plan -> In Progress -> Code Review
              |              |                   ^             |
              |              |                   +-------------+
              |              |                                 |
              |              +-------> Blocked <---------------+
              |                            |
              +----------------------------+

Code Review -> Integration Test -> Deploy -> Live
      ^              |              |
      +--------------+--------------+
```

Use this exact transition map:

```json
{
  "Backlog": ["On Deck"],
  "On Deck": ["Backlog", "Implementation Plan", "Blocked"],
  "Blocked": ["On Deck", "Implementation Plan", "In Progress", "Code Review", "Integration Test", "Deploy"],
  "Implementation Plan": ["On Deck", "In Progress", "Blocked"],
  "In Progress": ["On Deck", "Code Review", "Blocked"],
  "Code Review": ["In Progress", "Integration Test", "Blocked"],
  "Integration Test": ["In Progress", "Code Review", "Deploy", "Blocked"],
  "Deploy": ["In Progress", "Integration Test", "Live", "Blocked"],
  "Live": []
}
```

- `Backlog`: New or unprioritized work. Humans move ready tasks to `On Deck`.
- `On Deck`: Prioritized work with sufficient context and acceptance criteria.
- `Blocked`: Work waiting on a dependency, access, approval, answer, or human decision.
- `Implementation Plan`: `implementation_planner` inspects the repository and adds an actionable plan.
- `In Progress`: `coder` implements the plan and runs focused tests.
- `Code Review`: `code_reviewer` approves the change or returns actionable findings.
- `Integration Test`: `integration_tester` runs all repository-defined pre-deployment checks.
- `Deploy`: `devops` follows the repository's documented deployment and verification process.
- `Live`: Successfully released work. This state is terminal.

Backward transitions are intentional. A failed review or test comment must state the problem, impact, required correction, and verification steps. A blocked-task comment must state the blocker, next action, likely owner, and lane to resume.

## State Triggers

Create each trigger with `task_selection` set to `first_unlocked`:

| State | Agent | Successful handoff |
|---|---|---|
| `Implementation Plan` | `implementation_planner` | `In Progress` |
| `In Progress` | `coder` | `Code Review` |
| `Code Review` | `code_reviewer` | `Integration Test` |
| `Integration Test` | `integration_tester` | `Deploy` |
| `Deploy` | `devops` | `Live` |

## Scheduled Triggers

- Hourly: run `backlog_maintainer`, filtered to `{"state":"On Deck"}`.
- Every 30 minutes: run `unblocker`, filtered to `{"state":"Blocked"}`.

Install these schedules paused so the user can review the workflow and runtime costs before enabling them.

## Repository Mount

Set the workstream's `working_directory` to the product repository. Orchestra supplies that resolved directory to each agent as the code workspace. The repository should document its architecture, contribution rules, test commands, deployment process, environments, approvals, smoke tests, and rollback procedure.

If safe instructions or required access are unavailable, agents move work to `Blocked` instead of guessing.