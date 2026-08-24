import os
import shutil
import tempfile
import yaml
from datetime import datetime, timezone, timedelta
from orchestration.workstreams import create_workstream
from orchestration.tasks import create_task, read_task, update_task, _save_task
from orchestration.scheduler import tick
from orchestration.locks import find_expired_locks, _lock_path_for_task

# 1. Setup workspace
workspace = tempfile.mkdtemp(prefix="debug_tick_")
try:
    # 2. Setup WS and Task
    states = {"todo": ["in_progress"], "in_progress": ["done"], "done": []}
    ws = create_workstream(name="Test WS", task_states=states, base_dir=workspace)
    task = create_task(ws.id, title="Test Task", base_dir=workspace)
    
    # Mirroring test: status='in_progress', scheduled_action='retry'
    task.status = "in_progress"
    task.scheduled_action = "retry"
    _save_task(task, workspace)

    # 3. Create expired lock
    lock_path = _lock_path_for_task(task.id, base_dir=workspace)
    now = datetime.now(timezone.utc)
    expired_at = (now - timedelta(minutes=10)).isoformat()
    
    lock_data = {
        "agent_id": "test-agent",
        "acquired_at": (now - timedelta(minutes=20)).isoformat(),
        "expires_at": expired_at,
        "pid": 1234
    }
    with open(lock_path, "w") as f:
        yaml.dump(lock_data, f)

    # 4. Before tick
    print("--- Before Tick ---")
    expired_before = find_expired_locks(workspace)
    print(f"Expired locks count: {len(expired_before)}")
    if expired_before:
        print(f"Expired lock task_id: {expired_before[0]['task_id']}")

    # 5. Tick
    results = tick(workspace)
    print("\n--- After Tick ---")
    print(f"Tick results: {results}")

    # 6. After tick state
    expired_after = find_expired_locks(workspace)
    print(f"Expired locks count: {len(expired_after)}")
    
    updated_task = read_task(task.id, base_dir=workspace)
    print(f"Task status: {updated_task.status}")
    print(f"Task scheduled_at: {updated_task.scheduled_at}")
    print(f"Task scheduled_action: {updated_task.scheduled_action}")
    print(f"Task retry_count: {updated_task.retry_count}")

finally:
    shutil.rmtree(workspace)
