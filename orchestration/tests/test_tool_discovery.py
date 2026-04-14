"""Tests for dynamic CLI tool discovery and system prompt building."""

import os
import pytest

from orchestration.agents import (
    discover_cli_tools,
    _parse_agent_md,
    _build_system_prompt,
    _get_model,
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


class TestBuildSystemPrompt:
    def test_includes_agent_body(self, workspace):
        agent_def = {"body": "You are a test agent.", "tools": []}
        prompt = _build_system_prompt(agent_def, workspace)
        assert "You are a test agent." in prompt

    def test_includes_orchestration_cli_docs(self, workspace):
        agent_def = {"body": "Do stuff.", "tools": []}
        prompt = _build_system_prompt(agent_def, workspace)
        assert "orchestration.cli" in prompt
        assert "task update" in prompt

    def test_includes_requested_tool_docs(self, workspace):
        agent_def = {"body": "Do stuff.", "tools": ["alpha", "beta"]}
        prompt = _build_system_prompt(agent_def, workspace)
        assert "Alpha Tool" in prompt
        assert "Beta Tool" in prompt

    def test_ignores_unknown_tools(self, workspace):
        agent_def = {"body": "Do stuff.", "tools": ["nonexistent"]}
        prompt = _build_system_prompt(agent_def, workspace)
        assert "nonexistent" not in prompt


class TestGetModel:
    def test_default_is_sonnet(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_LLM", raising=False)
        assert _get_model() == "sonnet"

    def test_strips_anthropic_prefix(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_LLM", "anthropic/claude-opus-4-6")
        assert _get_model() == "claude-opus-4-6"

    def test_passes_through_plain_model(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_LLM", "claude-sonnet-4-20250514")
        assert _get_model() == "claude-sonnet-4-20250514"


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
