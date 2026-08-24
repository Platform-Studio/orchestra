import os
import shutil
import tempfile
import yaml
from datetime import datetime, timezone, timedelta
from orchestration.workstreams import create_workstream, list_workstreams
from orchestration.tasks import create_task, _find_task_file, _tasks_dir
from orchestration.locks import find_expired_locks, _lock_path_for_task
from orchestration.models import Lock

# 1. Create a temp workspace
workspace = tempfile.mkdtemp(prefix="debug_workspace_")
print(f"Workspace: {workspace}")

try:
    # 2. Create one workstream and one task
    states = {"To Do": ["Done"], "Done": []}
    ws = create_workstream(name="Debug WS", task_states=states, base_dir=workspace)
    task = create_task(ws.id, title="Debug Task", base_dir=workspace)
    
    # 3. Write an expired lock
    lock_path = _lock_path_for_task(task.id, base_dir=workspace)
    now = datetime.now(timezone.utc)
    expired_at = (now - timedelta(hours=1)).isoformat()
    acquired_at = (now - timedelta(hours=2)).isoformat()
    
    lock_data = {
        "agent_id": "debug_agent",
        "acquired_at": acquired_at,
        "expires_at": expired_at,
        "pid": 1234
    }
    
    with open(lock_path, "w") as f:
        yaml.dump(lock_data, f)
        
    # 4. Prints
    res = _find_task_file(task.id, workspace)
    task_file_path = res[1] if res else "Not found"
    
    print(f"Task file path: {task_file_path}")
    print(f"Lock path: {lock_path}")
    print(f"Lock path exists: {os.path.exists(lock_path)}")
    
    ws_ids = [w.id for w in list_workstreams(base_dir=workspace)]
    print(f"List workstreams ids: {ws_ids}")
    
    tasks_dir = _tasks_dir(workspace, ws.id)
    print(f"Tasks dir: {tasks_dir}")
    print(f"Tasks dir exists: {os.path.isdir(tasks_dir)}")
    print(f"Files in tasks dir: {os.listdir(tasks_dir)}")
    
    expired = find_expired_locks(workspace)
    print(f"Expired locks: {[{'task_id': e['task_id'], 'lock_path': e['lock_path']} for e in expired]}")

finally:
    shutil.rmtree(workspace)
