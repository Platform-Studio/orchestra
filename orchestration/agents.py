"""Agent operations."""

import json
import os
import glob
import shutil
import subprocess
import sys
import tempfile
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

    Strips 'anthropic/' prefix if present (legacy format).
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

    # Normalize spaces to underscores (agent names use spaces, filenames use underscores)
    bare_underscore = bare.replace(" ", "_")
    candidate = os.path.join(agents_dir, f"{bare_underscore}.md")
    if os.path.exists(candidate):
        return candidate

    # Case-insensitive fallback (try both space and underscore variants)
    if os.path.exists(agents_dir):
        for fname in os.listdir(agents_dir):
            if fname.lower() == f"{bare.lower()}.md" or fname.lower() == f"{bare_underscore.lower()}.md":
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
        "timeout": header.get("x-timeout"),
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
        "Use `python -m orchestration.cli <command>` to manage tasks, workstreams, artifacts, etc.\n"
        "Key commands:\n"
        "- `workstream find --query '<name>'` — find a workstream by name\n"
        "- `workstream read <workstream_id>` — read workstream details\n"
        "- `workstream descendants <workstream_id>` — list all descendant workstreams as JSON\n"
        "- `task create <workstream_id> --title '<title>' --description '<desc>'` — create a task\n"
        "- `task update <task_id> --status <new_status>` — transition a task\n"
        "- `task comment <task_id> --message '<msg>' --author '<agent_name>'` — add a comment with author\n"
        "- `task list <workstream_id>` — list tasks in a workstream\n"
        "- `artifact create --path '<path>' --content '<content>'` — save an artifact\n"
        "- `artifact read '<path>'` — read an artifact\n"
        "- `artifact list` — list all artifacts\n"
        "- `artifact list --prefix '<prefix>'` — list artifacts under a path\n"
        "\nFull CLI reference: Agents/cli/orchestration_cli.md\n"
    )

    return "\n".join(parts)


# Default agent execution timeout in seconds (30 minutes)
DEFAULT_AGENT_TIMEOUT = 1800


def run_agent(agent_name: str, task_ids: list = None, workstream_id: str = None, prompt: str = None, timeout: int = None, base_dir: str = ".") -> dict:
    """Run an agent via Claude Code CLI against 0-N tasks.

    Callers are responsible for locking/unlocking tasks. This function
    does not acquire or release locks.

    Task IDs are written to a temporary JSON file and passed to the agent
    via the prompt so the agent knows which tasks to work on.

    Args:
        agent_name: Agent reference (bare name, filename, or path).
        task_ids: List of task IDs to process. May be None or empty.
        workstream_id: Workstream context. Inferred from first task if not provided.
        prompt: Optional custom prompt from trigger, appended to the task prompt.
        timeout: Execution timeout in seconds. Overrides agent x-timeout. Defaults to DEFAULT_AGENT_TIMEOUT.
        base_dir: Workspace root.
    """
    if task_ids is None:
        task_ids = []

    # Ensure claude CLI is available
    claude_path = shutil.which("claude")
    if not claude_path:
        raise RuntimeError("Claude Code CLI not found. Install it from https://docs.anthropic.com/en/docs/claude-code")

    agent_file = _resolve_agent_file(agent_name, base_dir)
    agent_def = _parse_agent_md(agent_file)

    from .tasks import read_task, _save_task
    from .workstreams import read_workstream

    # Load tasks and resolve workstream context
    tasks = []
    ws = None
    for tid in task_ids:
        tasks.append(read_task(tid, base_dir))

    if tasks and not workstream_id:
        workstream_id = tasks[0].workstream_id
    if workstream_id:
        ws = read_workstream(workstream_id, base_dir)

    # Write task IDs to a temp file for the agent to reference
    task_file_path = None
    if task_ids:
        fd, task_file_path = tempfile.mkstemp(suffix=".json", prefix="agent_tasks_")
        with os.fdopen(fd, "w") as f:
            json.dump({"task_ids": task_ids, "workstream_id": workstream_id}, f)

    try:
        # Build system prompt from agent definition + tool docs
        system_prompt = _build_system_prompt(agent_def, base_dir)

        # Build task prompt based on context available
        if tasks and ws:
            if len(tasks) == 1:
                task = tasks[0]
                valid_transitions = ws.task_states.get(task.status, [])
                task_prompt = (
                    f"You are working on task '{task.title}' (ID: {task.id}) "
                    f"in workstream '{ws.name}' (ID: {ws.id}).\n"
                    f"Current status: {task.status}\n"
                    f"Valid next states: {valid_transitions}\n\n"
                    f"Working directory: {os.path.abspath(base_dir)}\n\n"
                    f"Follow your instructions and process this task now."
                )
                if task.description:
                    task_prompt += f"\n\nTask description:\n{task.description}"
            else:
                task_lines = []
                for t in tasks:
                    transitions = ws.task_states.get(t.status, [])
                    task_lines.append(f"- '{t.title}' (ID: {t.id}, status: {t.status}, valid next: {transitions})")
                task_prompt = (
                    f"You are working on {len(tasks)} tasks "
                    f"in workstream '{ws.name}' (ID: {ws.id}).\n\n"
                    f"Tasks:\n" + "\n".join(task_lines) + "\n\n"
                    f"Task IDs file: {task_file_path}\n\n"
                    f"Working directory: {os.path.abspath(base_dir)}\n\n"
                    f"Follow your instructions and process these tasks now."
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

        # Append custom trigger prompt if provided
        if prompt:
            task_prompt += f"\n\nAdditional instructions:\n{prompt}"

        # Log agent_started to audit trail for each task
        for t in tasks:
            t.add_audit("agent_started", f"Agent '{agent_def['name']}' started processing")
            _save_task(t, base_dir)

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
        env["ORCHESTRATION_AGENT_NAME"] = agent_def["name"]
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

        # Store subprocess PID in lock files for dead-process detection
        from .locks import update_lock_pid
        for tid in task_ids:
            update_lock_pid(tid, proc.pid, base_dir=base_dir)

        try:
            # Resolve timeout: caller override > agent x-timeout > default
            effective_timeout = timeout or agent_def.get("timeout") or DEFAULT_AGENT_TIMEOUT
            stdout, stderr = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise

        output = stdout.strip()
        stderr = stderr.strip()
        returncode = proc.returncode

        # Log agent output to audit trail for each task
        for tid in task_ids:
            t = read_task(tid, base_dir)  # Re-read in case agent modified it
            if returncode == 0:
                audit_output = output[:MAX_AUDIT_OUTPUT]
                if len(output) > MAX_AUDIT_OUTPUT:
                    audit_output += f"\n... (truncated, {len(output)} total chars)"
                t.add_audit("agent_completed", f"Agent '{agent_def['name']}' completed.\n\nOutput:\n{audit_output}")
                # Reset retry count on success
                t.retry_count = 0
                t.last_failure_at = None
            else:
                error_msg = stderr[:MAX_AUDIT_OUTPUT] if stderr else output[:MAX_AUDIT_OUTPUT]
                t.add_audit("agent_failed", f"Agent '{agent_def['name']}' failed (exit {returncode}).\n\nError:\n{error_msg}")
            _save_task(t, base_dir)

        if returncode != 0:
            raise RuntimeError(f"Agent '{agent_def['name']}' failed (exit {returncode}): {stderr or output}")

        response = {"agent": agent_def["name"], "result": output}
        if task_ids:
            response["task_ids"] = task_ids
        if ws:
            response["workstream_id"] = ws.id
        return response

    finally:
        # Clean up temp file
        if task_file_path and os.path.exists(task_file_path):
            os.remove(task_file_path)
