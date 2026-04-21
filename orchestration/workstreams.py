"""Workstream operations."""

import os
import re
import yaml

from .models import Workstream, RetryConfig, new_id


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ws_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "workstreams")


def _ws_path(base_dir: str, ws_id: str) -> str:
    return os.path.join(_ws_dir(base_dir), f"{ws_id}.yaml")


def _ws_env_path(base_dir: str, ws_id: str) -> str:
    return os.path.join(_ws_dir(base_dir), ws_id, ".env")


def _validate_env_key(key: str) -> None:
    if not ENV_KEY_PATTERN.match(key):
        raise ValueError(f"Invalid env key '{key}'. Expected [A-Za-z_][A-Za-z0-9_]*")


def _parse_env_line(line: str):
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].strip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return key, value


def read_workstream_env(ws_id: str, base_dir: str = ".") -> dict:
    """Return key/value pairs from a workstream-local .env file."""
    env_path = _ws_env_path(base_dir, ws_id)
    if not os.path.exists(env_path):
        return {}
    data = {}
    with open(env_path) as f:
        for raw_line in f:
            parsed = _parse_env_line(raw_line)
            if not parsed:
                continue
            key, value = parsed
            _validate_env_key(key)
            data[key] = value
    return data


def write_workstream_env(ws_id: str, values: dict, base_dir: str = ".") -> None:
    """Write key/value pairs to a workstream-local .env file."""
    read_workstream(ws_id, base_dir=base_dir)
    ws_data_dir = os.path.join(_ws_dir(base_dir), ws_id)
    os.makedirs(ws_data_dir, exist_ok=True)
    env_path = _ws_env_path(base_dir, ws_id)
    with open(env_path, "w") as f:
        for key in sorted(values):
            _validate_env_key(key)
            f.write(f"{key}={values[key]}\n")


def set_workstream_env_key(ws_id: str, key: str, value: str, base_dir: str = ".") -> dict:
    _validate_env_key(key)
    if value is None:
        raise ValueError("value is required")
    data = read_workstream_env(ws_id, base_dir=base_dir)
    data[key] = str(value)
    write_workstream_env(ws_id, data, base_dir=base_dir)
    return {"workstream_id": ws_id, "key": key, "value": data[key]}


def unset_workstream_env_key(ws_id: str, key: str, base_dir: str = ".") -> dict:
    """Mask a key at this workstream level by writing KEY= in .env."""
    _validate_env_key(key)
    data = read_workstream_env(ws_id, base_dir=base_dir)
    data[key] = ""
    write_workstream_env(ws_id, data, base_dir=base_dir)
    return {"workstream_id": ws_id, "key": key, "masked": True}


def _workstream_ancestry(ws_id: str, base_dir: str = ".") -> list:
    by_id = {ws.id: ws for ws in list_workstreams(base_dir=base_dir)}
    if ws_id not in by_id:
        raise FileNotFoundError(f"Workstream {ws_id} not found")

    lineage = []
    seen = set()
    current = by_id[ws_id]
    while current is not None:
        if current.id in seen:
            raise RuntimeError(f"Cycle detected in workstream hierarchy at {current.id}")
        seen.add(current.id)
        lineage.append(current.id)
        if not current.parent_id:
            current = None
        else:
            current = by_id.get(current.parent_id)
            if current is None:
                raise FileNotFoundError(f"Parent workstream {lineage[-1]} not found")
    return lineage


def list_workstream_hierarchy_env(ws_id: str, base_dir: str = ".") -> list:
    """Return hierarchy levels with local .env content (root to selected workstream)."""
    by_id = {ws.id: ws for ws in list_workstreams(base_dir=base_dir)}
    if ws_id not in by_id:
        raise FileNotFoundError(f"Workstream {ws_id} not found")

    levels = []
    lineage = list(reversed(_workstream_ancestry(ws_id, base_dir=base_dir)))
    for ws_level_id in lineage:
        ws = by_id[ws_level_id]
        env_path = _ws_env_path(base_dir, ws_level_id)
        has_env_file = os.path.exists(env_path)
        if not has_env_file:
            continue
        local_env = read_workstream_env(ws_level_id, base_dir=base_dir)
        levels.append({
            "id": ws.id,
            "name": ws.name,
            "parent_id": ws.parent_id,
            "env": local_env,
        })
    return levels


def resolve_workstream_env_key(ws_id: str, key: str, base_dir: str = "."):
    """Resolve a key from child to parent .env files, then process env.

    If a level contains KEY=, the key is explicitly masked and resolution stops.
    """
    _validate_env_key(key)
    for current_ws_id in _workstream_ancestry(ws_id, base_dir=base_dir):
        local = read_workstream_env(current_ws_id, base_dir=base_dir)
        if key in local:
            value = local[key]
            if value == "":
                return None
            return value
    return os.environ.get(key)


def resolve_env_key(key: str, workstream_id: str = None, task_id: str = None, base_dir: str = "."):
    """Resolve an env key for a workstream or task context."""
    if bool(workstream_id) == bool(task_id):
        raise ValueError("Provide exactly one of workstream_id or task_id")
    if task_id:
        from .tasks import read_task
        task = read_task(task_id, base_dir=base_dir)
        workstream_id = task.workstream_id
    return resolve_workstream_env_key(workstream_id, key, base_dir=base_dir)


def list_effective_workstream_env(ws_id: str, base_dir: str = ".", include_system: bool = False) -> dict:
    """Return effective env map for a workstream context."""
    effective = {}
    masked = set()
    # Root first so nearest child can override.
    lineage = list(reversed(_workstream_ancestry(ws_id, base_dir=base_dir)))
    for current_ws_id in lineage:
        local = read_workstream_env(current_ws_id, base_dir=base_dir)
        for key, value in local.items():
            if value == "":
                effective.pop(key, None)
                masked.add(key)
            else:
                effective[key] = value
                if key in masked:
                    masked.remove(key)

    if include_system:
        for key, value in os.environ.items():
            if key not in effective and key not in masked:
                effective[key] = value

    return dict(sorted(effective.items()))


def list_effective_env(workstream_id: str = None, task_id: str = None, base_dir: str = ".", include_system: bool = False) -> dict:
    """List effective env map for workstream or task context."""
    if bool(workstream_id) == bool(task_id):
        raise ValueError("Provide exactly one of workstream_id or task_id")
    if task_id:
        from .tasks import read_task
        task = read_task(task_id, base_dir=base_dir)
        workstream_id = task.workstream_id
    return list_effective_workstream_env(workstream_id, base_dir=base_dir, include_system=include_system)


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
