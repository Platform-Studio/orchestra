# Persistence and Runtime Abstraction Plan

## Goal
Create a clean abstraction between orchestration semantics and physical backends so we can:
- Persist state beyond local filesystem.
- Run agents in cloud environments.
- Keep the current local+git workflow intact during migration.

## Recommended Architecture
Treat GitHub as infrastructure primitives, not the domain model itself.

### 1. Core Interfaces
Define three provider interfaces:
- `StateStore`: workstreams, tasks, run state, locks, retries, audit.
- `ArtifactStore`: generated outputs, logs, and metadata pointers.
- `AgentRuntime`: executes agent runs.

Suggested implementations:
- `LocalFsStateStore` (current behavior)
- `GitHubRepoStateStore`
- `LocalFsArtifactStore`
- `GitHubArtifactStore`
- `LocalCliRuntime` (current Claude Code CLI path)
- `GitHubActionsRuntime` (cloud execution)

### 2. Canonical Data Model
Keep orchestration semantics canonical in our own structured model and files.

Use GitHub projections for collaboration/visibility, but avoid baking orchestration truth directly into GitHub-specific entities.

## Mapping Onto GitHub

### Workstreams and Tasks
Primary recommendation:
- Keep canonical workstream/task state in structured files in the repo (`yaml`/`json`) initially.
- Optionally project selected fields into GitHub Issues for human workflow.

Do **not** make Issues the authoritative task store at first.

### Artifacts
- Small text artifacts: commit to deterministic repo paths.
- Large/binary artifacts: use Release assets or external object storage; keep pointer metadata in repo.
- High-volume transient logs: store in Actions artifacts/object storage, with indexed manifest in repo.

### Agent Execution
- Use GitHub Actions as the baseline cloud runtime.
- Use Copilot on GitHub for coding/PR-centric tasks where it fits.
- Keep local runtime as a first-class fallback for latency, debugging, and offline ops.

## Why Issues Should Be a Projection (Not Canonical)
If Issues become canonical too early, likely pain points are:
- API and secondary rate-limit pressure.
- Awkward encoding of machine fields (retry policy, locks, dependency graph).
- Harder concurrency control and conflict handling.
- Eventual consistency + pagination overhead for scheduler loops.

A better pattern:
- Canonical state in files.
- Issues as projection for human collaboration.
- Narrow bi-directional sync on specific fields only (for example status/assignee), with explicit conflict policy.

## Key Gotchas to Plan For

1. Concurrency and locking
- Parallel updates can race.
- Use optimistic concurrency (`sha`/etag checks), retries, and jittered backoff.

2. GitHub API limits
- Burst scheduling can trigger secondary limits.
- Batch reads, cache, and apply global backoff.

3. Event ordering/duplication
- Webhooks are at-least-once and can arrive out of order.
- Require idempotency keys for transitions.

4. Permissions and secrets
- Use least-privilege tokens and scoped GitHub App permissions.
- Separate tokens/permissions for orchestration writes vs artifact publishing.

5. Cost and latency
- Actions startup latency impacts short jobs.
- Keep local runtime for low-latency loops.

6. Audit trail fragmentation
- Commits, issue timeline events, and Actions logs fragment history.
- Maintain a normalized run ledger in canonical state.

7. Git history noise
- Frequent state commits can clutter history.
- Consider dedicated state branch and/or snapshot strategy.

8. Hosted Workspace Manager security model
- Browser clients should not hold broad repo credentials.
- Add a backend API layer with OAuth/GitHub App auth and scoped access.

## Incremental Rollout Plan

### Phase 1: Interface Extraction (No Behavior Change)
- Introduce `StateStore`, `ArtifactStore`, `AgentRuntime` contracts.
- Keep current local filesystem and local runtime implementations.

### Phase 2: GitHub Persistence Adapters
- Implement `GitHubRepoStateStore` and `GitHubArtifactStore`.
- Continue canonical structured files in repo.

### Phase 3: Cloud Runtime
- Add `GitHubActionsRuntime`.
- Preserve local runtime as fallback and for development.

### Phase 4: Issues Projection
- Add optional task/workstream projection to Issues.
- Implement narrowly scoped sync + conflict resolution.

### Phase 5: Hosted Workspace Manager
- Deploy backend API for orchestration.
- Add auth/authorization for multi-user hosted access.

## Recommended Decisions (Current)
- Yes: GitHub as a physical backend.
- No (for now): GitHub Issues as canonical task database.
- Yes: GitHub Actions as default cloud execution substrate.
- Yes, selectively: Copilot on GitHub as specialized coding executor, not sole orchestrator runtime.

## Open Design Questions
- What fields, exactly, should sync bi-directionally between canonical task state and Issues?
- Should state commits go to main branch or dedicated state branch?
- What is the retention policy for logs/artifacts across storage tiers?
- Which runs must be hard real-time (local) vs acceptable with Actions startup latency?
