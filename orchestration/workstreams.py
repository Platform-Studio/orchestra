"""Workstream operations."""

import os
import re
import yaml

from .models import Workstream, RetryConfig, new_id


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _abs_base_dir(base_dir: str) -> str:
    return os.path.abspath(os.path.expanduser(base_dir))


def _ws_dir(base_dir: str) -> str:
    return os.path.join(_abs_base_dir(base_dir), "workstreams")


def _ws_path(base_dir: str, ws_id: str) -> str:
    return os.path.join(_ws_dir(base_dir), f"{ws_id}.yaml")


def _ws_env_path(base_dir: str, ws_id: str) -> str:
    return os.path.join(_ws_dir(base_dir), ws_id, ".env")


def _normalize_mounted_workspace_path(path: str, current_workspace_root: str) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(current_workspace_root, expanded))


def _set_workspace_root(ws: Workstream, workspace_root: str) -> Workstream:
    setattr(ws, "_workspace_root", workspace_root)
    return ws


def _workspace_root_for(ws: Workstream, fallback_base_dir: str) -> str:
    return getattr(ws, "_workspace_root", _abs_base_dir(fallback_base_dir))


def _list_local_workstreams(workspace_root: str) -> list:
    ws_dir = _ws_dir(workspace_root)
    if not os.path.exists(ws_dir):
        return []
    result = []
    for fname in sorted(os.listdir(ws_dir)):
        if not fname.endswith(".yaml"):
            continue
        path = os.path.join(ws_dir, fname)
        with open(path) as f:
            data = yaml.safe_load(f)
        if data:
            ws = Workstream.from_dict(data)
            result.append(_set_workspace_root(ws, workspace_root))
    return result


def _collect_effective_workstreams(base_dir: str) -> dict:
    base_root = _abs_base_dir(base_dir)
    by_id = {}
    ws_cache = {}
    visiting_mounts = set()

    def get_workspace_workstreams(workspace_root: str):
        if workspace_root not in ws_cache:
            ws_cache[workspace_root] = _list_local_workstreams(workspace_root)
        return ws_cache[workspace_root]

    def add_descendants_from_workspace(parent_id: str, workspace_root: str):
        workspace_workstreams = get_workspace_workstreams(workspace_root)
        children = [w for w in workspace_workstreams if w.parent_id == parent_id]
        children.sort(key=lambda w: (w.name.lower(), w.id))
        for child in children:
            if child.id not in by_id:
                by_id[child.id] = child
            add_descendants_from_workspace(child.id, workspace_root)
            add_mounted_children(child)

    def add_mounted_children(ws: Workstream):
        mount_path = ws.mounted_workspace_path
        if not mount_path:
            return
        workspace_root = _workspace_root_for(ws, base_root)
        target_root = _normalize_mounted_workspace_path(mount_path, workspace_root)
        key = (ws.id, target_root)
        if key in visiting_mounts:
            return
        visiting_mounts.add(key)
        try:
            add_descendants_from_workspace(ws.id, target_root)
        finally:
            visiting_mounts.remove(key)

    for ws in get_workspace_workstreams(base_root):
        by_id[ws.id] = ws

    # Mounts are descendants-only: only children/descendants are sourced from target roots.
    for ws in list(by_id.values()):
        add_mounted_children(ws)

    return by_id


def workstream_workspace_index(base_dir: str = ".") -> dict:
    """Return map of workstream ID -> workspace root containing its files."""
    by_id = _collect_effective_workstreams(base_dir)
    return {ws_id: _workspace_root_for(ws, base_dir) for ws_id, ws in by_id.items()}


def resolve_workstream_workspace(ws_id: str, base_dir: str = ".") -> str:
    """Resolve which workspace root stores this workstream's YAML/task files."""
    local_path = _ws_path(base_dir, ws_id)
    if os.path.exists(local_path):
        return _abs_base_dir(base_dir)
    idx = workstream_workspace_index(base_dir)
    root = idx.get(ws_id)
    if root is None:
        raise FileNotFoundError(f"Workstream {ws_id} not found")
    return root


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
    ws_root = resolve_workstream_workspace(ws_id, base_dir=base_dir)
    env_path = _ws_env_path(ws_root, ws_id)
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
    ws_root = resolve_workstream_workspace(ws_id, base_dir=base_dir)
    ws_data_dir = os.path.join(_ws_dir(ws_root), ws_id)
    os.makedirs(ws_data_dir, exist_ok=True)
    env_path = _ws_env_path(ws_root, ws_id)
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
        ws_root = resolve_workstream_workspace(ws_level_id, base_dir=base_dir)
        env_path = _ws_env_path(ws_root, ws_level_id)
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
    mounted_workspace_path: str = None,
    base_dir: str = ".",
) -> Workstream:
    target_base_dir = _abs_base_dir(base_dir)
    if parent_id:
        parent = read_workstream(parent_id, base_dir=base_dir)
        if parent.mounted_workspace_path:
            parent_root = _workspace_root_for(parent, base_dir)
            target_base_dir = _normalize_mounted_workspace_path(parent.mounted_workspace_path, parent_root)

    ws_id = new_id()
    ws = Workstream(
        id=ws_id,
        name=name,
        description=description,
        parent_id=parent_id,
        mounted_workspace_path=mounted_workspace_path,
    )
    if task_states:
        ws.task_states = task_states
    if retry:
        ws.retry = RetryConfig.from_dict(retry)

    # Create directories
    os.makedirs(_ws_dir(target_base_dir), exist_ok=True)
    tasks_dir = os.path.join(_ws_dir(target_base_dir), ws_id, "tasks")
    os.makedirs(tasks_dir, exist_ok=True)

    # Save workstream YAML
    save_workstream(ws, target_base_dir)
    return ws


def list_workstreams(base_dir: str = ".") -> list:
    by_id = _collect_effective_workstreams(base_dir)
    return sorted(by_id.values(), key=lambda w: (w.name.lower(), w.id))


def read_workstream(ws_id: str, base_dir: str = ".") -> Workstream:
    path = _ws_path(base_dir, ws_id)
    if os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f)
        return _set_workspace_root(Workstream.from_dict(data), _abs_base_dir(base_dir))

    by_id = _collect_effective_workstreams(base_dir)
    ws = by_id.get(ws_id)
    if ws is None:
        raise FileNotFoundError(f"Workstream {ws_id} not found")
    return ws


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
    try:
        target_root = resolve_workstream_workspace(ws.id, base_dir=base_dir)
    except FileNotFoundError:
        target_root = _abs_base_dir(base_dir)

    os.makedirs(_ws_dir(target_root), exist_ok=True)
    with open(_ws_path(target_root, ws.id), "w") as f:
        yaml.dump(ws.to_dict(), f, default_flow_style=False, sort_keys=False)
