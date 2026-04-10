"""Tests for dynamic CLI tool discovery and provisioning."""

import os
import pytest

from orchestration.agents import (
    discover_cli_tools,
    _make_cli_tool,
    _run_cli_tool,
    _parse_agent_md,
)


class TestDiscoverCliTools:
    def test_discovers_paired_tools(self, workspace):
        tools = discover_cli_tools(workspace)
        assert "alpha" in tools
        assert "beta" in tools

    def test_ignores_orphan_py(self, workspace):
        tools = discover_cli_tools(workspace)
        assert "orphan" not in tools

    def test_tool_has_py_and_md_paths(self, workspace):
        tools = discover_cli_tools(workspace)
        assert tools["alpha"]["py"].endswith("alpha.py")
        assert tools["alpha"]["md"].endswith("alpha.md")

    def test_tool_has_description(self, workspace):
        tools = discover_cli_tools(workspace)
        assert "Alpha Tool" in tools["alpha"]["description"]

    def test_empty_cli_dir(self, tmp_path):
        (tmp_path / "Agents" / "cli").mkdir(parents=True)
        assert discover_cli_tools(str(tmp_path)) == {}

    def test_no_cli_dir(self, tmp_path):
        (tmp_path / "Agents").mkdir()
        assert discover_cli_tools(str(tmp_path)) == {}


class TestRunCliTool:
    def test_runs_tool_and_returns_stdout(self, workspace):
        tools = discover_cli_tools(workspace)
        py = tools["alpha"]["py"]
        result = _run_cli_tool(py, "hello world", workspace)
        assert result == "hello world"

    def test_returns_error_on_failure(self, workspace):
        # Point at a non-existent script
        result = _run_cli_tool("/nonexistent.py", "", workspace)
        assert "ERROR" in result


class TestMakeCliTool:
    def test_creates_tool_with_correct_name(self, workspace):
        tools = discover_cli_tools(workspace)
        t = tools["alpha"]
        tool = _make_cli_tool("alpha", t["py"], t["description"], workspace)
        assert tool.name == "alpha"

    def test_tool_description_includes_usage(self, workspace):
        tools = discover_cli_tools(workspace)
        t = tools["beta"]
        tool = _make_cli_tool("beta", t["py"], t["description"], workspace)
        assert "Beta Tool" in tool.description

    def test_tool_can_execute(self, workspace):
        tools = discover_cli_tools(workspace)
        t = tools["beta"]
        tool = _make_cli_tool("beta", t["py"], t["description"], workspace)
        result = tool._run("foo bar")
        assert result == "beta: foo bar"


class TestAgentToolDeclaration:
    def test_x_tools_parsed_from_frontmatter(self, workspace):
        agent_path = os.path.join(workspace, "Agents", "tooled_agent.md")
        with open(agent_path, "w") as f:
            f.write(
                "---\n"
                "name: Tooled Agent\n"
                "description: Agent with tools\n"
                "x-tools: [alpha, beta]\n"
                "---\n"
                "Do stuff.\n"
            )
        agent_def = _parse_agent_md(agent_path)
        assert agent_def["tools"] == ["alpha", "beta"]

    def test_no_x_tools_defaults_empty(self, workspace):
        agent_path = os.path.join(workspace, "Agents", "plain_agent.md")
        with open(agent_path, "w") as f:
            f.write(
                "---\nname: Plain\ndescription: No tools\n---\nJust text.\n"
            )
        agent_def = _parse_agent_md(agent_path)
        assert agent_def["tools"] == []
