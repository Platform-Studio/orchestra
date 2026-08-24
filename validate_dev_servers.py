import subprocess
import time
import os
import requests

def get_pids():
    try:
        scheduler = subprocess.check_output(["pgrep", "-f", "orchestration.scheduler"]).decode().strip().split('\n')
        web = subprocess.check_output(["pgrep", "-f", "workstream_manager"]).decode().strip().split('\n')
        
        def get_details(pids):
            res = []
            for pid in pids:
                if not pid: continue
                ppid = subprocess.check_output(["ps", "-p", pid, "-o", "ppid="]).decode().strip()
                res.append((pid, ppid))
            return res
            
        return get_details(scheduler), get_details(web)
    except:
        return [], []

def main():
    with open("/tmp/supervisor.pid", "r") as f:
        supervisor_pid = f.read().strip()
    
    print(f"Supervisor PID: {supervisor_pid}")
    
    # Wait for children
    for _ in range(30):
        sched_info, web_info = get_pids()
        if sched_info and web_info:
            break
        time.sleep(1)
    else:
        print("Timeout waiting for children to start")
        return

    print(f"Initial - Scheduler: {sched_info}, Web: {web_info}")
    
    # Touch file
    print("Touching orchestration/tests/test_dev_servers.py")
    subprocess.run(["touch", "orchestration/tests/test_dev_servers.py"])
    
    old_sched_pids = [p[0] for p in sched_info]
    old_web_pids = [p[0] for p in web_info]
    
    # Wait for restart
    for _ in range(30):
        new_sched_info, new_web_info = get_pids()
        if new_sched_info and new_web_info:
            new_sched_pids = [p[0] for p in new_sched_info]
            new_web_pids = [p[0] for p in new_web_info]
            if set(new_sched_pids) != set(old_sched_pids) and set(new_web_pids) != set(old_web_pids):
                break
        time.sleep(1)
    else:
        print("Timeout waiting for restart")
        return

    print(f"Final - Scheduler: {new_sched_info}, Web: {new_web_info}")
    
    # Check status endpoint
    try:
        resp = requests.get("http://127.0.0.1:8080/api/scheduler/status")
        print(f"Status check: {resp.status_code}, {resp.text}")
    except Exception as e:
        print(f"Status check failed: {e}")

if __name__ == "__main__":
    main()
