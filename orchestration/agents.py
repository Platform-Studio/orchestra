"""Agent operations."""

import os
import glob
import shutil
import subprocess
import sys
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Maximum bytes of agent output to store in audit trail
MAX_AUDIT_OUTPUT = 10_000


def _get_model() -> str:
    """Return the model name for Claude Code CLI from DEFAULT_LLM env var.

    Strips 'anthropic/' prefix if present (legacy CrewAI format).
    Falls back to 'sonnet' if not set.
    """
    raw = os.getenv("DEFAULT_LLM", "sonnet")
    if raw.startswith("anthropic/"):
        raw = raw[len("anthropic/"):]
    return raw


def _agents_dir(base_dir: str) -> str:
    return os.path.join(base_dir, "Agents")


def _cli_dir(base_dir: str) -> str:
    return os.path.join(_agents_dir(base_dir), "cli")


def _resolve_agent_file(agent_ref: str, base_dir: str) -> str:
    """Resolve an agent reference to an absolute file path.

    Accepts:
      - bare name:        "sorter"
      - filename:         "sorter.md"
      - relative path:    "Agents/sorter.md"
    """
    agents_dir = _agents_dir(base_dir)

    # If it looks like a path (has separator or starts with Agents/)
    if os.sep in agent_ref or agent_ref.startswith("Agents/"):
        candidate = os.path.join(base_dir, agent_ref)
        if os.path.exists(candidate):
            return candidate

    # Strip .md if present for bare-name lookup
    bare = agent_ref
    if bare.endswith(".md"):
        bare = bare[:-3]
    # Strip leading Agents/ or Agents\ prefix
    for prefix in ("Agents/", "Agents\\"):
        if bare.startswith(prefix):
            bare = bare[len(prefix):]

    candidate = os.path.join(agents_dir, f"{bare}.md")
    if os.path.exists(candidate):
        return candidate

    # Case-insensitive fallback
    if os.path.exists(agents_dir):
        for fname in os.listdir(agents_dir):
            if fname.lower() == f"{bare.lower()}.md":
                return os.path.join(agents_dir, fname)

    raise FileNotFoundError(f"Agent '{agent_ref}' not found in {agents_dir}")


def _parse_agent_md(path: str) -> dict:
    """Parse an agent .md file, extracting YAML header and body."""
    with open(path) as f:
        content = f.read()

    header = {}
    body = content
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            header = yaml.safe_load(parts[1]) or {}
            body = parts[2].strip()

    return {
        "name": header.get("name", os.path.splitext(os.path.basename(path))[0]),
        "description": header.get("description", ""),
        "agent_type": header.get("x-agent-type", "worker"),
        "tools": header.get("x-tools", []),
        "file": path,
        "body": body,
    }


def list_agents(base_dir: str = ".") -> list:
    agents_dir = _agents_dir(base_dir)
    if not os.path.exists(agents_dir):
        return []
    result = []
    for path in sorted(glob.glob(os.path.join(agents_dir, "*.md"))):
        try:
            agent = _parse_agent_md(path)
            result.append({
                "name": agent["name"],
                "description": agent["description"],
                "agent_type": agent["agent_type"],
                "file": os.path.relpath(path, base_dir),
            })
        except Exception:
            continue
    return result


# ── Dynamic CLI tool discovery ───────────────────────────────────────

def discover_cli_tools(base_dir: str = ".") -> dict:
    """Discover available CLI tools from Agents/cli/.

    Returns a dict mapping tool name to {"py": path, "md": path, "description": str}.
    Only includes tools that have both a .py and a matching .md file.
    """
    cli = _cli_dir(base_dir)
    if not os.path.isdir(cli):
        return {}

    tools = {}
    for py_path in sorted(glob.glob(os.path.join(cli, "*.py"))):
        name = os.path.splitext(os.path.basename(py_path))[0]
        md_path = os.path.join(cli, f"{name}.md")
        if os.path.exists(md_path):
            with open(md_path) as f:
                description = f.read()
            tools[name] = {
                "py": py_path,
                "md": md_path,
                "description": description,
            }
    return tools


def _build_system_prompt(agent_def: dict, base_dir: str) -> str:
    """Build the system prompt from the agent body and available tools."""
    parts = [agent_def["body"]]

    # List available CLI tools so the agent knows what it can run via bash
    requested = agent_def.get("tools", [])
    if requested:
        available = discover_cli_tools(base_dir)
        tool_docs = []
        for tool_name in requested:
            if tool_name in available:
                tool_docs.append(f"### {tool_name}\n```\npython3 {available[tool_name]['py']} <args>\n```\n{available[tool_name]['description']}")
        if tool_docs:
            parts.append("\n\n## Available CLI Tools\n" + "\n\n".join(tool_docs))

    # Always document the orchestration CLI
    parts.append(
        "\n\n## Orchestration CLI\n"
        "Use `python -m orchestration.cli <command>` to manage tasks, workstreams, etc.\n"
        "Key commands:\n"
        "- `task update <task_id> --status <new_status>` — transition a task\n"
        "- `task comment <task_id> --message '<msg>'` — add a comment\n"
        "- `task list <workstream_id>` — list tasks in a workstream\n"
        "- `task create <workstream_id> --title '<title>' --description '<desc>'` — create a task\n"
    )

    return "\n".join(parts)


def run_agent(agent_name: str, task_id: str = None, workstream_id: str = None, base_dir: str = ".") -> dict:
    """Run an agent via Claude Code CLI, optionally against a specific task.

    If task_id is provided, the agent processes that task.
    A lock is automatically acquired for the task before execution and released
    afterwards (even on failure). If the task is already locked, raises RuntimeError.

    If only workstream_id is provided, the agent runs standalone with workstream
    context but no specific task — useful for generative agents that create tasks.
    """
    # Ensure claude CLI is available
    claude_path = shutil.which("claude")
    if not claude_path:
        raise RuntimeError("Claude Code CLI not found. Install it from https://docs.anthropic.com/en/docs/claude-code")

    agent_file = _resolve_agent_file(agent_name, base_dir)
    agent_def = _parse_agent_md(agent_file)

    from .tasks import read_task, _save_task
    from .workstreams import read_workstream

    # Resolve workstream context
    task = None
    ws = None
    if task_id:
        task = read_task(task_id, base_dir)
        ws = read_workstream(task.workstream_id, base_dir)
    elif workstream_id:
        ws = read_workstream(workstream_id, base_dir)

    # Acquire lock when running against a specific task
    lock_agent_id = agent_def["name"]
    if task_id:
        from .locks import acquire_lock, release_lock
        acquire_lock(task_id, agent_id=lock_agent_id, base_dir=base_dir)

    try:
        # Build system prompt from agent definition + tool docs
        system_prompt = _build_system_prompt(agent_def, base_dir)

        # Build task prompt based on context available
        if task and ws:
            valid_transitions = ws.task_states.get(task.status, [])
            task_prompt = (
                f"You are working on task '{task.title}' (ID: {task.id}) "
                f"in workstream '{ws.name}' (ID: {ws.id}).\n"
                f"Current status: {task.status}\n"
                f"Valid next states: {valid_transitions}\n\n"
                f"Working directory: {os.path.abspath(base_dir)}\n\n"
                f"Follow your instructions and process this task now."
            )
        elif ws:
            states = list(ws.task_states.keys())
            task_prompt = (
                f"You are running standalone in workstream '{ws.name}' (ID: {ws.id}).\n"
                f"Available states: {states}\n\n"
                f"Working directory: {os.path.abspath(base_dir)}\n\n"
                f"Follow your instructions now."
            )
        else:
            task_prompt = (
                f"You are running standalone with no specific workstream or task.\n"
                f"Working directory: {os.path.abspath(base_dir)}\n\n"
                f"Follow your instructions now."
            )

        # Add task description/context if available
        if task and task.description:
            task_prompt += f"\n\nTask description:\n{task.description}"

        # Log agent_started to audit trail
        if task:
            task.add_audit("agent_started", f"Agent '{agent_def['name']}' started processing")
            _save_task(task, base_dir)

        # Build claude CLI command
        model = _get_model()
        cmd = [
            claude_path,
            "-p", task_prompt,
            "--model", model,
            "--output-format", "text",
            "--verbose",
        ]

        # Append system prompt
        cmd.extend(["--append-system-prompt", system_prompt])

        # Use --dangerously-skip-permissions for headless/automated execution
        cmd.append("--dangerously-skip-permissions")

        # Run with environment inherited (includes ANTHROPIC_API_KEY from dotenv)
        env = os.environ.copy()
        abs_base = os.path.abspath(base_dir)

        # Use Popen so we can capture and store the subprocess PID in the lock
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=abs_base,
            env=env,
        )

        # Store subprocess PID in lock file for dead-process detection
        if task_id:
            from .locks import update_lock_pid
            update_lock_pid(task_id, proc.pid, base_dir=base_dir)

        try:
            stdout, stderr = proc.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise

        output = stdout.strip()
        stderr = stderr.strip()
        returncode = proc.returncode

        # Log agent output to audit trail
        if task:
            task = read_task(task_id, base_dir)  # Re-read in case agent modified it
            if returncode == 0:
                audit_output = output[:MAX_AUDIT_OUTPUT]
                if len(output) > MAX_AUDIT_OUTPUT:
                    audit_output += f"\n... (truncated, {len(output)} total chars)"
                task.add_audit("agent_completed", f"Agent '{agent_def['name']}' completed.\n\nOutput:\n{audit_output}")
                # Reset retry count on success
                task.retry_count = 0
                task.last_failure_at = None
            else:
                error_msg = stderr[:MAX_AUDIT_OUTPUT] if stderr else output[:MAX_AUDIT_OUTPUT]
                task.add_audit("agent_failed", f"Agent '{agent_def['name']}' failed (exit {returncode}).\n\nError:\n{error_msg}")
            _save_task(task, base_dir)

        if returncode != 0:
            raise RuntimeError(f"Agent '{agent_def['name']}' failed (exit {returncode}): {stderr or output}")

        response = {"agent": agent_def["name"], "result": output}
        if task_id:
            response["task_id"] = task_id
        if ws:
            response["workstream_id"] = ws.id
        return response

    finally:
        if task_id:
            try:
                release_lock(task_id, agent_id=lock_agent_id, base_dir=base_dir)
            except Exception:
                pass  # Lock may have been released by agent or expired
