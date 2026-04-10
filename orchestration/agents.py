"""Agent operations."""

import os
import glob
import subprocess
import yaml

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

LLM = os.getenv("CREWAI_LLM", "anthropic/claude-opus-4-6")


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


def _make_cli_tool(tool_name: str, py_path: str, description: str, base_dir: str):
    """Create a CrewAI BaseTool wrapper for a CLI tool.

    The tool runs `python3 <py_path> <args>` and returns stdout/stderr.
    The .md content is used as the tool description so the LLM knows usage.
    """
    from crewai.tools import BaseTool

    # Truncate description to first 4000 chars to stay within LLM tool limits
    desc_truncated = description[:4000]
    abs_base = os.path.abspath(base_dir)
    abs_py = os.path.abspath(py_path)
    py_basename = os.path.basename(py_path)
    tool_desc = (
        f"Run the {tool_name} CLI tool. Pass the command-line arguments as a "
        f"single string (everything after 'python3 {py_basename}').\n\n"
        f"Usage reference:\n{desc_truncated}"
    )

    # Closure-based approach avoids Pydantic private-attr issues
    class CLITool(BaseTool):
        name: str = tool_name
        description: str = tool_desc

        def _run(self, command: str) -> str:
            return _run_cli_tool(abs_py, command, abs_base)

    # Give the class a unique name so CrewAI doesn't deduplicate
    CLITool.__name__ = f"CLITool_{tool_name}"
    CLITool.__qualname__ = f"CLITool_{tool_name}"
    return CLITool()


def _run_cli_tool(abs_py: str, command: str, cwd: str) -> str:
    """Execute a CLI tool and return output."""
    full = f"python3 {abs_py} {command}"
    r = subprocess.run(
        full, shell=True, capture_output=True, text=True,
        timeout=120, cwd=cwd,
    )
    out = r.stdout.strip()
    err = r.stderr.strip()
    if r.returncode == 0:
        return out if out else "(no output)"
    return f"ERROR (exit {r.returncode}):\n{err}\n{out}".strip()


def _make_orchestration_tool(base_dir: str):
    """Create the built-in orchestration CLI tool."""
    from crewai.tools import BaseTool
    abs_base = os.path.abspath(base_dir)

    # Read the orchestration CLI docs if available
    orch_md = os.path.join(_cli_dir(base_dir), "orchestration_cli.md")
    if os.path.exists(orch_md):
        with open(orch_md) as f:
            orch_desc = f.read()[:4000]
    else:
        orch_desc = "Manage workstreams, tasks, locks, triggers, artifacts, and agents."

    class OrchestrationTool(BaseTool):
        name: str = "orchestration"
        description: str = (
            "Run an orchestration CLI command. Pass the arguments after "
            "'python -m orchestration.cli', e.g. "
            "'task update <task_id> --status veg'.\n\n"
            f"Usage reference:\n{orch_desc}"
        )

        def _run(self, command: str) -> str:
            return _run_orchestration(command, abs_base)

    return OrchestrationTool()


def _run_orchestration(command: str, cwd: str) -> str:
    full = f"python -m orchestration.cli {command}"
    r = subprocess.run(
        full, shell=True, capture_output=True, text=True,
        timeout=30, cwd=cwd,
    )
    out = r.stdout.strip()
    err = r.stderr.strip()
    if r.returncode == 0:
        return out if out else "(no output)"
    return f"ERROR (exit {r.returncode}):\n{err}\n{out}".strip()


def build_tools_for_agent(agent_def: dict, base_dir: str = ".") -> list:
    """Build the CrewAI tool list for an agent based on its x-tools declaration.

    - "orchestration" is always included.
    - Each name in x-tools is matched against Agents/cli/<name>.py + .md.
    - FileReadTool is always included.
    """
    from crewai_tools import FileReadTool
    tools = [FileReadTool()]

    # Always include orchestration
    tools.append(_make_orchestration_tool(base_dir))

    # Discover available CLI tools
    requested = agent_def.get("tools", [])
    if requested:
        available = discover_cli_tools(base_dir)
        for tool_name in requested:
            if tool_name == "orchestration":
                continue  # already added
            if tool_name in available:
                t = available[tool_name]
                tools.append(_make_cli_tool(tool_name, t["py"], t["description"], base_dir))

    return tools


def run_agent(agent_name: str, task_id: str, base_dir: str = ".") -> dict:
    """Run an agent against a specific task using CrewAI."""
    agent_file = _resolve_agent_file(agent_name, base_dir)
    agent_def = _parse_agent_md(agent_file)

    # Load the task and its workstream for context
    from .tasks import read_task, _save_task
    from .workstreams import read_workstream
    task = read_task(task_id, base_dir)
    ws = read_workstream(task.workstream_id, base_dir)

    try:
        from crewai import Agent as CrewAgent, Task as CrewTask, Crew

        tools = build_tools_for_agent(agent_def, base_dir)

        agent = CrewAgent(
            role=agent_def["name"],
            goal=agent_def["description"],
            backstory=agent_def["body"],
            tools=tools,
            llm=LLM,
            verbose=False,
            allow_delegation=False,
        )

        # Build a rich task description with context
        valid_transitions = ws.task_states.get(task.status, [])
        task_desc = (
            f"You are working on task '{task.title}' (ID: {task.id}) "
            f"in workstream '{ws.name}' (ID: {ws.id}).\n"
            f"Current status: {task.status}\n"
            f"Valid next states: {valid_transitions}\n\n"
            f"To update the task status, use the orchestration tool with:\n"
            f"  task update {task.id} --status <new_status>\n\n"
            f"To add a comment, use the orchestration tool with:\n"
            f"  task comment {task.id} --message '<your message>'\n\n"
            f"Working directory: {os.path.abspath(base_dir)}\n\n"
            f"Follow your instructions and process this task now."
        )

        crew_task = CrewTask(
            description=task_desc,
            agent=agent,
            expected_output="Confirm the task was processed and what action was taken.",
        )

        crew = Crew(agents=[agent], tasks=[crew_task], verbose=False)
        result = crew.kickoff()

        # Re-read task to get the final state (agent may have updated it via CLI)
        task = read_task(task_id, base_dir)

        return {"agent": agent_def["name"], "task_id": task_id, "result": str(result)}

    except ImportError:
        raise RuntimeError(
            "CrewAI is not installed. Install it with: pip install crewai"
        )
