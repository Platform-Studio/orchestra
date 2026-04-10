"""Workstream operations."""

import os
import yaml

from .models import Workstream, RetryConfig, new_id


def _ws_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "workstreams")


def _ws_path(base_dir: str, ws_id: str) -> str:
    return os.path.join(_ws_dir(base_dir), f"{ws_id}.yaml")


def create_workstream(
    name: str,
    description: str = None,
    parent_id: str = None,
    task_states: dict = None,
    retry: dict = None,
    base_dir: str = ".",
) -> Workstream:
    ws_id = new_id()
    ws = Workstream(id=ws_id, name=name, description=description, parent_id=parent_id)
    if task_states:
        ws.task_states = task_states
    if retry:
        ws.retry = RetryConfig.from_dict(retry)

    # Create directories
    os.makedirs(_ws_dir(base_dir), exist_ok=True)
    tasks_dir = os.path.join(_ws_dir(base_dir), ws_id, "tasks")
    os.makedirs(tasks_dir, exist_ok=True)

    # Save workstream YAML
    save_workstream(ws, base_dir)
    return ws


def list_workstreams(base_dir: str = ".") -> list:
    ws_dir = _ws_dir(base_dir)
    if not os.path.exists(ws_dir):
        return []
    result = []
    for fname in sorted(os.listdir(ws_dir)):
        if fname.endswith(".yaml"):
            path = os.path.join(ws_dir, fname)
            with open(path) as f:
                data = yaml.safe_load(f)
            if data:
                result.append(Workstream.from_dict(data))
    return result


def read_workstream(ws_id: str, base_dir: str = ".") -> Workstream:
    path = _ws_path(base_dir, ws_id)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Workstream {ws_id} not found")
    with open(path) as f:
        data = yaml.safe_load(f)
    return Workstream.from_dict(data)


def find_workstreams(query: str, base_dir: str = ".") -> list:
    query_lower = query.lower()
    result = []
    for ws in list_workstreams(base_dir):
        if query_lower in ws.name.lower():
            result.append(ws)
        elif ws.description and query_lower in ws.description.lower():
            result.append(ws)
    return result


def save_workstream(ws: Workstream, base_dir: str = ".") -> None:
    """Save a workstream to its YAML file."""
    os.makedirs(_ws_dir(base_dir), exist_ok=True)
    with open(_ws_path(base_dir, ws.id), "w") as f:
        yaml.dump(ws.to_dict(), f, default_flow_style=False, sort_keys=False)
