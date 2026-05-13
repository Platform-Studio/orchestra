"""Workstream operations."""

import os
import re
import yaml

from .models import Workstream, RetryConfig, new_id, normalize_agent_concurrency_policy
from .persistence import resolve_artifact_root, resolve_workstream_root


ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
TAG_COLOR_PALETTE = (
    "#61bd4f", "#f2d600", "#ff9f1a", "#eb5a46", "#c377e0",
    "#0079bf", "#00c2e0", "#51e898", "#ff78cb", "#344563",
    "#b3bac5", "#055a8c", "#89609e", "#cd8313", "#4bbf6b",
)


def _abs_base_dir(base_dir: str) -> str:
    return os.path.abspath(os.path.expanduser(base_dir))


def _state_base_dir(base_dir: str) -> str:
    return resolve_workstream_root(base_dir)


def _local_ws_dir(state_root: str) -> str:
    return os.path.join(_abs_base_dir(state_root), "workstreams")


def _ws_dir(base_dir: str) -> str:
    return _local_ws_dir(_state_base_dir(base_dir))


def _ws_path(base_dir: str, ws_id: str) -> str:
    return os.path.join(_local_ws_dir(base_dir), f"{ws_id}.yaml")


def _ws_env_path(base_dir: str, ws_id: str) -> str:
    return os.path.join(_local_ws_dir(base_dir), ws_id, ".env")


def _workstream_home_dir(state_root: str, ws_id: str) -> str:
    return os.path.join(_local_ws_dir(state_root), ws_id)


def _resolve_root_path(path: str, anchor_root: str) -> str:
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(anchor_root, expanded))


def _normalize_tag_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip())


def _default_tag_color(name: str) -> str:
    normalized = _normalize_tag_name(name)
    if not normalized:
        return TAG_COLOR_PALETTE[0]
    idx = sum(ord(ch) for ch in normalized) % len(TAG_COLOR_PALETTE)
    return TAG_COLOR_PALETTE[idx]


def _normalize_tag_color(name: str, color: str = None) -> str:
    candidate = str(color or "").strip().lower()
    if candidate in TAG_COLOR_PALETTE:
        return candidate
    return _default_tag_color(name)


def _normalize_tag_definitions(raw_definitions) -> list[dict]:
    normalized = []
    seen = set()
    for raw in raw_definitions or []:
        if isinstance(raw, dict):
            name = _normalize_tag_name(raw.get("name"))
            color = _normalize_tag_color(name, raw.get("color"))
        else:
            name = _normalize_tag_name(raw)
            color = _normalize_tag_color(name)
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append({"name": name, "color": color})
    return normalized


def _set_workspace_root(ws: Workstream, workspace_root: str) -> Workstream:
    setattr(ws, "_workspace_root", workspace_root)
    return ws


def _workspace_root_for(ws: Workstream, fallback_base_dir: str) -> str:
    return getattr(ws, "_workspace_root", _state_base_dir(fallback_base_dir))


def _configured_working_directory(ws: Workstream) -> str | None:
    return ws.working_directory


def _configured_artifact_root(ws: Workstream) -> str | None:
    return ws.artifact_root


def _configured_child_workstream_root(ws: Workstream) -> str | None:
    return ws.child_workstream_root


def _list_local_workstreams(workspace_root: str) -> list:
    ws_dir = _local_ws_dir(workspace_root)
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


def _write_workstream_to_root(ws: Workstream, state_root: str) -> None:
    os.makedirs(_local_ws_dir(state_root), exist_ok=True)
    with open(_ws_path(state_root, ws.id), "w") as f:
        yaml.dump(ws.to_dict(include_transient=False), f, default_flow_style=False, sort_keys=False)


def _collect_effective_workstreams(base_dir: str, *, include_mount_status: bool = True) -> dict:
    state_root = _state_base_dir(base_dir)
    default_working_directory = _abs_base_dir(base_dir)
    default_artifact_root = resolve_artifact_root(base_dir)
    cache = {}
    visible = {}
    active = set()

    def _index_for_root(root: str) -> dict:
        normalized_root = _abs_base_dir(root)
        if normalized_root in cache:
            return cache[normalized_root]

        by_parent = {}
        for ws in _list_local_workstreams(normalized_root):
            by_parent.setdefault(ws.parent_id, []).append(ws)
        for children in by_parent.values():
            children.sort(key=lambda item: (item.name.lower(), item.id))
        cache[normalized_root] = {"by_parent": by_parent}
        return cache[normalized_root]

    def _visit(ws: Workstream, effective_working_directory: str, effective_artifact_root: str) -> None:
        if ws.id in active:
            raise RuntimeError(f"Cycle detected in workstream hierarchy at {ws.id}")
        if ws.id in visible:
            existing_root = _workspace_root_for(visible[ws.id], state_root)
            current_root = _workspace_root_for(ws, state_root)
            if existing_root != current_root:
                raise RuntimeError(f"Duplicate workstream ID {ws.id} found in multiple state roots")
            return

        active.add(ws.id)
        ws_state_root = _workspace_root_for(ws, state_root)

        working_directory = effective_working_directory
        configured_working_directory = _configured_working_directory(ws)
        if configured_working_directory is not None:
            working_directory = _resolve_root_path(configured_working_directory, ws_state_root)
            if include_mount_status:
                setattr(ws, "_mount_available", os.path.isdir(working_directory))

        artifact_root = effective_artifact_root
        configured_artifact_root = _configured_artifact_root(ws)
        if configured_artifact_root is not None:
            artifact_root = _resolve_root_path(configured_artifact_root, ws_state_root)

        child_state_root = _workstream_home_dir(ws_state_root, ws.id)
        configured_child_root = _configured_child_workstream_root(ws)
        if configured_child_root is not None:
            child_state_root = _resolve_root_path(configured_child_root, ws_state_root)

        setattr(ws, "_resolved_workspace_path", working_directory)
        setattr(ws, "_resolved_artifact_root", artifact_root)
        setattr(ws, "_resolved_child_workstream_root", child_state_root)
        visible[ws.id] = ws

        child_index = _index_for_root(child_state_root)
        for child in child_index["by_parent"].get(ws.id, []):
            _visit(child, working_directory, artifact_root)

        active.remove(ws.id)

    root_index = _index_for_root(state_root)
    for root_ws in root_index["by_parent"].get(None, []):
        _visit(root_ws, default_working_directory, default_artifact_root)

    return visible


def workstream_workspace_index(base_dir: str = ".") -> dict:
    """Return map of visible workstream ID -> persistence root containing its files."""
    by_id = _collect_effective_workstreams(base_dir)
    return {ws_id: _workspace_root_for(ws, base_dir) for ws_id, ws in by_id.items()}


def resolve_workstream_state_root(ws_id: str, base_dir: str = ".") -> str:
    """Resolve where a workstream's persisted YAML/task files live."""
    idx = workstream_workspace_index(base_dir)
    root = idx.get(ws_id)
    if root is not None:
        return root
    raise FileNotFoundError(f"Workstream {ws_id} not found")


def resolve_workstream_child_state_root(ws_id: str, base_dir: str = ".") -> str:
    ws = read_workstream(ws_id, base_dir=base_dir)
    resolved = getattr(ws, "_resolved_child_workstream_root", None)
    if resolved is not None:
        return resolved
    return resolve_workstream_state_root(ws_id, base_dir=base_dir)


def resolve_workstream_artifact_root(ws_id: str, base_dir: str = ".") -> str:
    ws = read_workstream(ws_id, base_dir=base_dir)
    resolved = getattr(ws, "_resolved_artifact_root", None)
    if resolved is not None:
        return resolved
    return resolve_artifact_root(base_dir)


def resolve_workstream_workspace(ws_id: str, base_dir: str = ".") -> str:
    """Resolve the effective code workspace root for a workstream."""
    ws = read_workstream(ws_id, base_dir=base_dir)
    resolved = getattr(ws, "_resolved_workspace_path", None)
    if resolved is not None:
        return resolved
    return _abs_base_dir(base_dir)


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


def _read_env_file(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    data = {}
    with open(path) as f:
        for raw_line in f:
            parsed = _parse_env_line(raw_line)
            if not parsed:
                continue
            key, value = parsed
            _validate_env_key(key)
            data[key] = value
    return data


def _env_layers_for_workstream(ws_id: str, base_dir: str = ".") -> list:
    """Return env layers from root -> selected workstream.

    Layers include:
    - workstream-local `.env` files
        - working-directory root `.env` files for nodes in the lineage,
            including the selected workstream itself when it defines one
    """
    by_id = {ws.id: ws for ws in list_workstreams(base_dir=base_dir)}
    if ws_id not in by_id:
        raise FileNotFoundError(f"Workstream {ws_id} not found")

    layers = []
    lineage = list(reversed(_workstream_ancestry(ws_id, base_dir=base_dir)))
    for ws_level_id in lineage:
        ws = by_id[ws_level_id]

        ws_state_root = resolve_workstream_state_root(ws_level_id, base_dir=base_dir)
        local_env_path = _ws_env_path(ws_state_root, ws_level_id)
        if os.path.exists(local_env_path):
            layers.append({
                "kind": "workstream",
                "id": ws.id,
                "name": ws.name,
                "parent_id": ws.parent_id,
                "path": local_env_path,
                "env": _read_env_file(local_env_path),
            })

        configured_working_directory = _configured_working_directory(ws)
        if configured_working_directory is not None:
            working_root = getattr(ws, "_resolved_workspace_path", None)
            if working_root is None:
                current_ws_root = _workspace_root_for(ws, base_dir)
                working_root = _resolve_root_path(configured_working_directory, current_ws_root)
            mounted_env_path = os.path.join(working_root, ".env")
            if os.path.exists(mounted_env_path):
                layers.append({
                    "kind": "working-directory-root",
                    "id": ws.id,
                    "name": f"{ws.name} working directory root",
                    "parent_id": ws.id,
                    "path": mounted_env_path,
                    "env": _read_env_file(mounted_env_path),
                })

    return layers


def read_workstream_env(ws_id: str, base_dir: str = ".") -> dict:
    """Return key/value pairs from a workstream-local .env file."""
    ws_root = resolve_workstream_state_root(ws_id, base_dir=base_dir)
    env_path = _ws_env_path(ws_root, ws_id)
    return _read_env_file(env_path)


def write_workstream_env(ws_id: str, values: dict, base_dir: str = ".") -> None:
    """Write key/value pairs to a workstream-local .env file."""
    ws_root = resolve_workstream_state_root(ws_id, base_dir=base_dir)
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
    layers = _env_layers_for_workstream(ws_id, base_dir=base_dir)
    return [{
        "id": layer["id"],
        "name": layer["name"],
        "parent_id": layer["parent_id"],
        "kind": layer["kind"],
        "path": layer["path"],
        "env": layer["env"],
    } for layer in layers]


def resolve_workstream_env_key(ws_id: str, key: str, base_dir: str = "."):
    """Resolve a key from child to parent .env files, then process env.

    If a level contains KEY=, the key is explicitly masked and resolution stops.
    """
    _validate_env_key(key)
    for layer in reversed(_env_layers_for_workstream(ws_id, base_dir=base_dir)):
        local = layer["env"]
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
    for layer in _env_layers_for_workstream(ws_id, base_dir=base_dir):
        local = layer["env"]
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
    context: str = None,
    parent_id: str = None,
    task_states: dict = None,
    retry: dict = None,
    working_directory: str = None,
    artifact_root: str = None,
    child_workstream_root: str = None,
    base_dir: str = ".",
) -> Workstream:
    target_base_dir = _state_base_dir(base_dir)
    if parent_id:
        target_base_dir = resolve_workstream_child_state_root(parent_id, base_dir=base_dir)

    ws_id = new_id()
    ws = Workstream(
        id=ws_id,
        name=name,
        description=description,
        context=context,
        parent_id=parent_id,
        working_directory=working_directory,
        artifact_root=artifact_root,
        child_workstream_root=child_workstream_root,
    )
    if task_states:
        ws.task_states = task_states
    if retry:
        ws.retry = RetryConfig.from_dict(retry)

    # Create directories
    os.makedirs(_local_ws_dir(target_base_dir), exist_ok=True)
    tasks_dir = os.path.join(_local_ws_dir(target_base_dir), ws_id, "tasks")
    os.makedirs(tasks_dir, exist_ok=True)

    # Save workstream YAML
    _write_workstream_to_root(ws, target_base_dir)
    return ws


def list_workstreams(base_dir: str = ".", *, include_mount_status: bool = True) -> list:
    by_id = _collect_effective_workstreams(base_dir, include_mount_status=include_mount_status)
    return sorted(by_id.values(), key=lambda w: (w.name.lower(), w.id))


def read_workstream(ws_id: str, base_dir: str = ".") -> Workstream:
    by_id = _collect_effective_workstreams(base_dir)
    ws = by_id.get(ws_id)
    if ws is None:
        raise FileNotFoundError(f"Workstream {ws_id} not found")
    return ws


def get_workstream_code_mount_statuses(workstream_ids: list[str] | None = None, base_dir: str = ".") -> dict[str, dict]:
    by_id = _collect_effective_workstreams(base_dir, include_mount_status=False)
    if workstream_ids is None:
        target_ids = sorted(by_id.keys())
    else:
        target_ids = []
        seen = set()
        for workstream_id in workstream_ids:
            if workstream_id in by_id and workstream_id not in seen:
                target_ids.append(workstream_id)
                seen.add(workstream_id)

    statuses: dict[str, dict] = {}
    for workstream_id in target_ids:
        ws = by_id[workstream_id]
        configured_working_directory = _configured_working_directory(ws)
        if configured_working_directory is None:
            statuses[workstream_id] = {
                "configured": False,
                "exists": None,
                "resolved_path": None,
            }
            continue

        ws_state_root = _workspace_root_for(ws, _state_base_dir(base_dir))
        resolved_path = _resolve_root_path(configured_working_directory, ws_state_root)
        statuses[workstream_id] = {
            "configured": True,
            "exists": os.path.isdir(resolved_path),
            "resolved_path": resolved_path,
        }
    return statuses


def find_workstreams(query: str, base_dir: str = ".") -> list:
    query_lower = query.lower()
    result = []
    for ws in list_workstreams(base_dir):
        if query_lower in ws.name.lower():
            result.append(ws)
        elif ws.description and query_lower in ws.description.lower():
            result.append(ws)
    return result


def get_workstream_tags(workstream_id: str, base_dir: str = ".") -> list[dict]:
    ws = read_workstream(workstream_id, base_dir=base_dir)
    tag_definitions = _normalize_tag_definitions(getattr(ws, "tag_definitions", []))
    known_names = {entry["name"] for entry in tag_definitions}

    from .tasks import list_tasks

    for task in list_tasks(workstream_id, base_dir=base_dir):
        for raw_tag in getattr(task, "tags", []) or []:
            name = _normalize_tag_name(raw_tag)
            if not name or name in known_names:
                continue
            known_names.add(name)
            tag_definitions.append({"name": name, "color": _default_tag_color(name)})

    return tag_definitions


def upsert_workstream_tag(workstream_id: str, name: str, color: str = None, base_dir: str = ".") -> dict:
    ws = read_workstream(workstream_id, base_dir=base_dir)
    normalized_name = _normalize_tag_name(name)
    if not normalized_name:
        raise ValueError("Tag name is required")

    normalized_color = _normalize_tag_color(normalized_name, color)
    tag_definitions = _normalize_tag_definitions(getattr(ws, "tag_definitions", []))

    updated = None
    for entry in tag_definitions:
        if entry["name"] == normalized_name:
            entry["color"] = normalized_color
            updated = entry
            break

    if updated is None:
        updated = {"name": normalized_name, "color": normalized_color}
        tag_definitions.append(updated)

    ws.tag_definitions = tag_definitions
    save_workstream(ws, base_dir)
    return updated


def read_workstream_context(ws_id: str, base_dir: str = ".") -> dict:
    ws = read_workstream(ws_id, base_dir=base_dir)
    return {
        "workstream_id": ws.id,
        "name": ws.name,
        "context": ws.context,
    }


def set_workstream_context(
    ws_id: str,
    context: str = None,
    base_dir: str = ".",
    updated_by: str = None,
) -> Workstream:
    ws = read_workstream(ws_id, base_dir=base_dir)
    normalized = None if context is None else str(context).strip()
    if normalized == "":
        normalized = None

    previous = ws.context
    if previous == normalized:
        return ws

    ws.context = normalized
    save_workstream(ws, base_dir)

    from .workspace_audit import log_event

    actor = f" by {updated_by}" if updated_by else ""
    if normalized:
        description = f"Workstream context updated{actor}"
    else:
        description = f"Workstream context cleared{actor}"
    log_event(
        "workstream_context_updated",
        description,
        base_dir,
        workstream_id=ws.id,
    )
    return ws


def read_workstream_agent_concurrency(ws_id: str, base_dir: str = ".") -> dict:
    ws = read_workstream(ws_id, base_dir=base_dir)
    return {
        "workstream_id": ws.id,
        "name": ws.name,
        "agent_concurrency": normalize_agent_concurrency_policy(ws.agent_concurrency),
    }


def set_workstream_agent_concurrency(
    ws_id: str,
    agent_concurrency=None,
    base_dir: str = ".",
    updated_by: str = None,
) -> Workstream:
    ws = read_workstream(ws_id, base_dir=base_dir)
    normalized = normalize_agent_concurrency_policy(agent_concurrency)
    previous = normalize_agent_concurrency_policy(ws.agent_concurrency)
    if previous == normalized:
        return ws

    ws.agent_concurrency = normalized
    save_workstream(ws, base_dir)

    from .workspace_audit import log_event

    actor = f" by {updated_by}" if updated_by else ""
    if normalized:
        description = f"Workstream agent concurrency updated{actor}"
    else:
        description = f"Workstream agent concurrency cleared{actor}"
    log_event(
        "workstream_agent_concurrency_updated",
        description,
        base_dir,
        workstream_id=ws.id,
    )
    return ws


def save_workstream(ws: Workstream, base_dir: str = ".") -> None:
    """Save a workstream to its YAML file."""
    try:
        target_root = resolve_workstream_state_root(ws.id, base_dir=base_dir)
    except FileNotFoundError:
        target_root = _state_base_dir(base_dir)
    _write_workstream_to_root(ws, target_root)
