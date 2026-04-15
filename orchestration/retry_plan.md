# Expired Lock Cleanup & Retry Plan

## Problem

When an agent process dies (scheduler killed, OOM, machine reboot, etc.), its lock file remains on disk. The lock eventually expires by TTL, but:
- Nothing detects the dead process
- Nothing cleans up the stale lock
- Nothing retries the failed work
- The task sits in `in_progress` limbo forever

## Current State

| Component | Status |
|-----------|--------|
| Lock TTL & expiry check | ✅ Exists (900s default, `Lock.is_expired()`) |
| Scheduler tick (60s) | ✅ Exists, already evaluates triggers each minute |
| `RetryConfig` model | ✅ Exists (`max_retries=3`, `backoff=exponential`, `base_seconds=60`) but **unused** |
| Task `"failed"` state | ✅ Exists in `DEFAULT_TASK_STATES` with transition `failed → pending` |
| PID tracking | ❌ Agent subprocess PIDs not stored anywhere |
| Dead process detection | ❌ Nothing checks for expired locks |
| Automatic retry | ❌ Not implemented |
| Corrupt task column in UI | ✅ Exists — similar pattern usable for failed tasks |

## How Agent Death Happens

```
Scheduler tick
  → trigger fires
    → run_agent() acquires lock (agent_id stored, no PID)
      → subprocess.run(claude ...) — BLOCKING
        → Process killed (SIGKILL / OOM / crash)
  → finally block never runs
  → lock file stays on disk with no living process behind it
  → lock expires after TTL, but task stuck in "in_progress" with no one working it
```

## Plan

### 1. Store PID in Lock

**Where:** `locks.py` → `acquire_lock()`
**What:** Add `pid: int` field to Lock model. Store `os.getpid()` (the scheduler/parent process) at acquisition time. The scheduler is the process that calls `subprocess.run()`, so if it dies, the subprocess is orphaned or also dead.

Also store the **subprocess PID** when available. Since `subprocess.run()` is blocking and doesn't expose PID, switch `run_agent()` to use `subprocess.Popen()` so we can:
1. Capture the PID immediately after spawn
2. Write it to the lock file
3. Then wait for completion (`.communicate(timeout=...)`)

Lock file gains two fields:
```yaml
agent_id: "my-agent"
pid: 12345           # scheduler process that owns the run
subprocess_pid: 12346 # the claude CLI process doing the work
acquired_at: "2026-04-15T10:00:00Z"
expires_at: "2026-04-15T10:15:00Z"
```

### 2. Add Expired Lock Scan to Scheduler Tick

**Where:** `scheduler.py` → `_tick()`
**What:** Add a fourth phase after existing trigger evaluation:

```
Phase 4: Expired Lock Cleanup
  for each workstream:
    for each task with a .lock file:
      lock = read lock file
      if lock.is_expired():
        → handle_expired_lock(workstream, task, lock)
```

This runs every 60 seconds, same as trigger evaluation. Expired locks are only detected after their TTL has passed, so worst case latency = TTL + 60s.

### 3. Handle Expired Lock

**Where:** New function in `locks.py` or `scheduler.py`
**What:** When an expired lock is found:

1. **Check if process is alive** — `os.kill(lock.subprocess_pid, 0)` (signal 0 = existence check)
2. **Kill if alive** — `os.kill(lock.subprocess_pid, signal.SIGTERM)`, wait 5s, then `SIGKILL` if still alive. Also kill `lock.pid` (parent) if different and alive.
3. **Remove lock file** — `os.unlink()` the `.lock` file
4. **Log** — write to both the task audit trail AND the scheduler log (see Logging section below)
5. **Decide retry** — call retry decision logic (step 4)

### 4. Retry Decision Logic

**Where:** New function, likely `tasks.py` or new `retry.py`
**What:**

Count previous failures from audit trail:
```python
failure_count = sum(1 for a in task.audit if a.type in ("lock_expired", "agent_failed"))
```

Get retry config (precedence: task → workstream → default):
```python
retry = task.retry or workstream.retry or RetryConfig()  # default: max_retries=3
```

**If `failure_count < retry.max_retries`:**
- Compute backoff delay:
  - `exponential`: `base_seconds * 2^failure_count` (60s, 120s, 240s)
  - `linear`: `base_seconds * (failure_count + 1)`
  - `fixed`: `base_seconds`
- Set `task.scheduled_at = now + backoff_delay`
- Set `task.scheduled_action = original trigger's action` (copy from the trigger that fired it, or from the audit trail's `agent_started` entry)
- Transition task back to `pending`
- Audit: `type: "retry_scheduled"`, `description: "Retry 2/3 scheduled for 2026-04-15T10:18:00Z"`

**If `failure_count >= retry.max_retries`:**
- Transition task to `failed`
- Audit: `type: "max_retries_exceeded"`, `description: "Failed after 3 retries. Manual intervention required."`

### 5. Add `retry_count` Tracking to Task

Rather than counting audit entries each time (fragile), add a simple counter:

```yaml
retry_count: 2           # incremented each time we retry
last_failure_at: "..."   # ISO timestamp of most recent failure
```

This makes it cheap to check and resilient to audit trail edits. The audit trail remains the source of truth for *what happened*, but `retry_count` is the decision field.

Reset `retry_count` to 0 when a task successfully completes (in `run_agent()` success path).

### 6. Add `RetryConfig` to Workstream Model

Currently `RetryConfig` only exists on Task. Add it to Workstream too as a default:

```yaml
# workstream.yaml
retry:
  max_retries: 3
  backoff: exponential
  base_seconds: 60
```

Resolution order: `task.retry > workstream.retry > DEFAULT_RETRY_CONFIG`

### 7. Failed Task Display in Workstream Manager

**Where:** `workstream_manager/static/index.html`
**What:** Failed tasks should be visually distinct. Two options:

**Option A (recommended):** Failed tasks render in their normal column position (since "failed" is a real task state) but with error-style card treatment — red border/background similar to corrupt cards, plus a "retry" button.

**Option B:** Separate "failed" column like the corrupt column. But this is worse because "failed" is already a proper state in `task_states`, so it'll naturally get its own column.

**Additions:**
- `.card-failed` CSS class: red-tinted background, failure icon
- Show `retry_count` and `last_failure_at` on the card
- Add "Retry" button that resets state to `pending` and clears `retry_count`
- Show last failure reason from audit trail

### 8. Workstream-Level Retry Config in UI

**Where:** `workstream_manager/static/index.html` (workstream settings area)
**What:** Allow editing the workstream-level `RetryConfig` defaults. Simple form with three fields.

### 9. Logging Strategy

Every retry-related event writes to **two places**: the task's own audit trail, and the central scheduler log (`workspace_audit.yaml` via `workspace_audit.log_event()`). The task audit trail is the per-task record; the scheduler log is the central place to spot systemic patterns (e.g. "every agent is timing out" or "this workstream keeps failing").

We already use `workspace_audit.log_event()` for `scheduler_started`, `scheduler_stopped`, `trigger_fired`, and `trigger_skipped`. We add the following new event types:

#### Event Types

| Event | Task Audit (`task.audit`) | Scheduler Log (`workspace_audit.yaml`) |
|-------|---------------------------|----------------------------------------|
| Expired lock detected | `type: "lock_expired"` | `type: "lock_expired"` |
| Process killed | `type: "process_killed"` | `type: "process_killed"` |
| Retry scheduled | `type: "retry_scheduled"` | `type: "retry_scheduled"` |
| Max retries exceeded | `type: "max_retries_exceeded"` | `type: "max_retries_exceeded"` |
| Manual retry (UI button) | `type: "manual_retry"` | `type: "manual_retry"` |
| Lock cleanup (no process) | `type: "lock_cleaned"` | `type: "lock_cleaned"` |

#### Task Audit Trail Entries

```yaml
# Expired lock found, process killed
- timestamp: "2026-04-15T10:16:00Z"
  type: "lock_expired"
  description: "Lock held by my-agent expired after 900s."

- timestamp: "2026-04-15T10:16:01Z"
  type: "process_killed"
  description: "Killed subprocess 12346 (SIGTERM). Parent 12345 already dead."

# Retry scheduled
- timestamp: "2026-04-15T10:16:01Z"
  type: "retry_scheduled"
  description: "Retry 2/3 scheduled for 2026-04-15T10:20:01Z (backoff: 240s)"

# OR max retries hit
- timestamp: "2026-04-15T10:16:01Z"
  type: "max_retries_exceeded"
  description: "Failed after 3 retries. Task moved to failed state."
```

#### Scheduler Log Entries

The scheduler log includes identifying context so you can cross-reference without opening each task:

```yaml
# Expired lock
- timestamp: "2026-04-15T10:16:00Z"
  type: "lock_expired"
  description: "Lock expired for task 'Sort Chayote' (my-agent, 900s TTL)"
  task_id: "f58a6bd3-..."
  workstream_id: "f1beefd8-..."
  agent_id: "my-agent"
  subprocess_pid: 12346

# Process killed
- timestamp: "2026-04-15T10:16:01Z"
  type: "process_killed"
  description: "Killed subprocess 12346 for task 'Sort Chayote'"
  task_id: "f58a6bd3-..."
  workstream_id: "f1beefd8-..."
  signal: "SIGTERM"

# Retry scheduled
- timestamp: "2026-04-15T10:16:01Z"
  type: "retry_scheduled"
  description: "Retry 2/3 for task 'Sort Chayote' in 240s"
  task_id: "f58a6bd3-..."
  workstream_id: "f1beefd8-..."
  retry_count: 2
  max_retries: 3
  next_run_at: "2026-04-15T10:20:01Z"

# Permanently failed
- timestamp: "2026-04-15T10:16:01Z"
  type: "max_retries_exceeded"
  description: "Task 'Sort Chayote' failed after 3 retries"
  task_id: "f58a6bd3-..."
  workstream_id: "f1beefd8-..."
  retry_count: 3
```

#### Implementation

Every function that writes to `task.audit` also calls `log_event()`:

```python
from .workspace_audit import log_event

# In handle_expired_lock():
task.add_audit("lock_expired", f"Lock held by {lock.agent_id} expired after {ttl}s.")
log_event("lock_expired",
    f"Lock expired for task '{task.title}' ({lock.agent_id}, {ttl}s TTL)",
    base_dir,
    task_id=task.id, workstream_id=ws.id, agent_id=lock.agent_id,
    subprocess_pid=lock.subprocess_pid)
```

The `workspace_audit.yaml` is already capped at 500 entries, which at ~1 event per expired lock is plenty of history. If volume becomes a concern, we can increase `MAX_ENTRIES` or add rotation.

## Implementation Order

1. **Lock PID storage** — `models.py` + `locks.py` + `agents.py` (switch to Popen)
2. **Expired lock scan** — `scheduler.py` new phase in `_tick()`
3. **Process cleanup + logging** — `locks.py` kill logic + dual audit/scheduler log writes
4. **Retry decision** — `tasks.py` or new module
5. **Workstream retry config** — `models.py` + `workstreams.py`
6. **Failed task UI** — `index.html`
7. **Tests** — cover each component

Steps 1-4 are the critical path. Steps 5-7 are polish.

## Edge Cases

| Scenario | Handling |
|----------|----------|
| PID reuse (OS assigned same PID to new process) | Check `/proc/{pid}/cmdline` or `ps -p` for "claude" in command. Don't kill if it's not our process. |
| Lock expires but agent is still running fine (just slow) | This means TTL was too short. The agent should call a `renew_lock()` function periodically. **Future enhancement:** add lock renewal to agent system prompt instructions so long-running agents extend their own locks via CLI. |
| Scheduler itself dies and restarts | On restart, tick will find expired locks and clean up. No special handling needed — this is the normal path. |
| Two schedulers running | Already handled — `scheduler_state.yaml` records PID, and scheduler checks for existing instance at startup. |
| Task has no retry config and workstream has none | Falls back to `DEFAULT_RETRY_CONFIG` (3 retries, exponential, 60s base). |
| Agent fails fast (non-zero exit) vs. agent hangs | Fast failures are already caught by `run_agent()` try/except and logged as `agent_failed`. The expired-lock path handles hangs/crashes. Both feed into the same retry counter. |
| Manual retry after max retries | The "Retry" button in UI resets `retry_count` and transitions `failed → pending`. |

## Not In Scope (Future)

- **Lock renewal by agents** — agents calling `orchestration lock renew` to extend TTL mid-run. Important for long tasks but separate feature.
- **Dead letter queue** — moving permanently failed tasks somewhere for batch review. The "failed" state column serves this role for now.
- **Alerting** — notifications when tasks hit max retries. Could hook into the audit trail later.
