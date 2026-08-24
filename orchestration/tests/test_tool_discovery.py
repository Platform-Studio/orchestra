"""Tests for dynamic CLI tool discovery and system prompt building."""

import os
import pytest

from orchestration.agents import (
    discover_cli_tools,
    _parse_agent_md,
    _resolve_agent_file,
    _build_system_prompt,
    _get_model,
    _cline_thinking_level,
    _resolve_agent_model,
    _resolve_agent_effort,
    _resolve_agent_runtime,
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

    def test_includes_configured_external_resource_paths(self, workspace, tmp_path, monkeypatch):
        skill_dir = tmp_path / "skills"
        cli_dir = tmp_path / "cli"
        monkeypatch.setenv("ORCHESTRA_SKILL_PATHS", str(skill_dir))
        monkeypatch.setenv("ORCHESTRA_CLI_PATHS", str(cli_dir))

        prompt = _build_system_prompt({"body": "Do stuff.", "tools": []}, workspace)

        assert f"Additional skill definitions can be found in: {skill_dir}" in prompt
        assert f"Additional CLI tools can be found in: {cli_dir}" in prompt

    def test_omits_external_resource_guidance_when_unset(self, workspace, monkeypatch):
        monkeypatch.delenv("ORCHESTRA_SKILL_PATHS", raising=False)
        monkeypatch.delenv("ORCHESTRA_CLI_PATHS", raising=False)
        monkeypatch.delenv("ORKESTRA_SKILL_PATHS", raising=False)
        monkeypatch.delenv("ORKESTRA_CLI_PATHS", raising=False)

        prompt = _build_system_prompt({"body": "Do stuff.", "tools": []}, workspace)

        assert "Additional Resources" not in prompt


class TestExternalAgentPaths:
    def test_resolves_from_multiple_paths_with_later_path_winning(self, workspace, tmp_path, monkeypatch):
        first_dir = tmp_path / "first-agents"
        second_dir = tmp_path / "second-agents"
        first_dir.mkdir()
        second_dir.mkdir()
        (first_dir / "external.md").write_text("First definition", encoding="utf-8")
        selected = second_dir / "external.md"
        selected.write_text("Second definition", encoding="utf-8")
        monkeypatch.setenv(
            "ORCHESTRA_AGENT_PATHS",
            os.pathsep.join([str(first_dir), str(second_dir)]),
        )

        assert _resolve_agent_file("external", workspace) == str(selected)

    def test_external_definition_overrides_project_definition(self, workspace, tmp_path, monkeypatch):
        external_dir = tmp_path / "external-agents"
        external_dir.mkdir()
        selected = external_dir / "test_agent.md"
        selected.write_text("External definition", encoding="utf-8")
        monkeypatch.setenv("ORCHESTRA_AGENT_PATHS", str(external_dir))

        assert _resolve_agent_file("test_agent", workspace) == str(selected)

    def test_legacy_external_agent_paths_remain_supported(self, workspace, tmp_path, monkeypatch):
        external_dir = tmp_path / "legacy-agents"
        external_dir.mkdir()
        selected = external_dir / "legacy.md"
        selected.write_text("Legacy definition", encoding="utf-8")
        monkeypatch.delenv("ORCHESTRA_AGENT_PATHS", raising=False)
        monkeypatch.setenv("ORKESTRA_AGENT_PATHS", str(external_dir))

        assert _resolve_agent_file("legacy", workspace) == str(selected)


class TestGetModel:
    def test_default_is_sonnet(self, monkeypatch):
        monkeypatch.delenv("DEFAULT_LLM", raising=False)
        assert _get_model() == "sonnet"

    def test_cline_default_uses_runtime_specific_fallback(self, monkeypatch):
        monkeypatch.delenv("CLINE_DEFAULT_LLM", raising=False)
        assert _get_model("cline") == "deepseek/deepseek-v4-flash"

    def test_strips_anthropic_prefix(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_LLM", "anthropic/claude-opus-4-6")
        assert _get_model() == "claude-opus-4-6"

    def test_passes_through_plain_model(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_LLM", "claude-sonnet-4-20250514")
        assert _get_model() == "claude-sonnet-4-20250514"

    def test_cline_default_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("CLINE_DEFAULT_LLM", "google/gemini-2.5-flash")
        assert _get_model("cline") == "google/gemini-2.5-flash"

    def test_copilot_default_uses_runtime_specific_fallback(self, monkeypatch):
        monkeypatch.delenv("COPILOT_MODEL", raising=False)
        assert _get_model("copilot") == "auto"

    def test_copilot_default_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("COPILOT_MODEL", "gpt-5")
        assert _get_model("copilot") == "gpt-5"


class TestModelAndEffortResolution:
    def test_resolve_agent_model_prefers_x_model(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_LLM", "claude-sonnet-4-6")
        monkeypatch.setenv("HIGH_LLM", "claude-opus-4-6")
        agent_def = {"model": "anthropic/claude-haiku-4-5", "model_level": "high"}
        assert _resolve_agent_model(agent_def) == "claude-haiku-4-5"

    def test_resolve_agent_model_from_level(self, monkeypatch):
        monkeypatch.setenv("MEDIUM_LLM", "anthropic/claude-sonnet-4-6")
        agent_def = {"model_level": "medium"}
        assert _resolve_agent_model(agent_def) == "claude-sonnet-4-6"

    def test_resolve_agent_model_from_cline_high_level_default(self, monkeypatch):
        monkeypatch.delenv("CLINE_HIGH_LLM", raising=False)
        agent_def = {"model_level": "high"}
        assert _resolve_agent_model(agent_def, runtime="cline") == "deepseek/deepseek-v4-pro"

    def test_resolve_agent_model_from_cline_level_default(self, monkeypatch):
        monkeypatch.delenv("CLINE_MEDIUM_LLM", raising=False)
        agent_def = {"model_level": "medium"}
        assert _resolve_agent_model(agent_def, runtime="cline") == "deepseek/deepseek-v4-flash"

    def test_resolve_agent_model_from_cline_level_override(self, monkeypatch):
        monkeypatch.setenv("CLINE_HIGH_LLM", "qwen/qwen3-coder-next")
        agent_def = {"model_level": "high"}
        assert _resolve_agent_model(agent_def, runtime="cline") == "qwen/qwen3-coder-next"

    def test_resolve_agent_model_from_cline_coding_level_falls_back_to_default_model(self, monkeypatch):
        monkeypatch.delenv("CLINE_CODING_LLM", raising=False)
        monkeypatch.setenv("CLINE_DEFAULT_LLM", "deepseek/deepseek-v4-pro")
        agent_def = {"model_level": "coding"}
        assert _resolve_agent_model(agent_def, runtime="cline") == "deepseek/deepseek-v4-pro"

    def test_resolve_agent_model_from_copilot_level_default(self, monkeypatch):
        monkeypatch.delenv("COPILOT_HIGH_LLM", raising=False)
        agent_def = {"model_level": "high"}
        assert _resolve_agent_model(agent_def, runtime="copilot") == "auto"

    def test_resolve_agent_model_from_copilot_level_override(self, monkeypatch):
        monkeypatch.setenv("COPILOT_HIGH_LLM", "gpt-5")
        agent_def = {"model_level": "high"}
        assert _resolve_agent_model(agent_def, runtime="copilot") == "gpt-5"

    def test_resolve_agent_model_from_copilot_coding_level_override(self, monkeypatch):
        monkeypatch.setenv("COPILOT_CODING_LLM", "gpt-5.3-codex")
        agent_def = {"model_level": "coding"}
        assert _resolve_agent_model(agent_def, runtime="copilot") == "gpt-5.3-codex"

    def test_resolve_agent_model_invalid_level_raises(self):
        with pytest.raises(ValueError, match="Invalid x-model-level"):
            _resolve_agent_model({"model_level": "urgent"})

    def test_resolve_agent_effort_defaults_none(self):
        assert _resolve_agent_effort({}) is None

    def test_resolve_agent_effort_valid_values(self):
        assert _resolve_agent_effort({"effort": "high"}) == "high"
        assert _resolve_agent_effort({"effort": "XHIGH"}) == "xhigh"

    def test_resolve_agent_effort_uses_default_env(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_EFFORT", "medium")
        assert _resolve_agent_effort({}) == "medium"

    def test_resolve_agent_effort_uses_runtime_default_env(self, monkeypatch):
        monkeypatch.setenv("CLINE_DEFAULT_EFFORT", "high")
        assert _resolve_agent_effort({}, runtime="cline") == "high"

    def test_resolve_agent_effort_uses_level_specific_env(self, monkeypatch):
        monkeypatch.setenv("HIGH_EFFORT", "xhigh")
        assert _resolve_agent_effort({"model_level": "high"}) == "xhigh"

    def test_resolve_agent_effort_uses_runtime_level_specific_env(self, monkeypatch):
        monkeypatch.setenv("COPILOT_CODING_EFFORT", "medium")
        assert _resolve_agent_effort({"model_level": "coding"}, runtime="copilot") == "medium"

    def test_cline_thinking_level_maps_effort_values(self):
        assert _cline_thinking_level(None) is None
        assert _cline_thinking_level("low") is None
        assert _cline_thinking_level("medium") is None
        assert _cline_thinking_level("high") == "high"
        assert _cline_thinking_level("xhigh") == "xhigh"
        assert _cline_thinking_level("max") == "xhigh"

    def test_resolve_agent_effort_prefers_x_effort_over_env(self, monkeypatch):
        monkeypatch.setenv("HIGH_EFFORT", "xhigh")
        assert _resolve_agent_effort({"model_level": "high", "effort": "low"}) == "low"

    def test_resolve_agent_effort_invalid_env_raises(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_EFFORT", "turbo")
        with pytest.raises(ValueError, match="Invalid env var DEFAULT_EFFORT"):
            _resolve_agent_effort({})

    def test_resolve_agent_effort_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid x-effort"):
            _resolve_agent_effort({"effort": "turbo"})

    def test_resolve_agent_runtime_defaults_claude_code(self, monkeypatch):
        monkeypatch.delenv("ORCHESTRATION_AGENT_RUNTIME", raising=False)
        assert _resolve_agent_runtime({}) == "claude-code"

    def test_resolve_agent_runtime_uses_header(self):
        assert _resolve_agent_runtime({"runtime": "cline"}) == "cline"

    def test_resolve_agent_runtime_uses_copilot_header(self):
        assert _resolve_agent_runtime({"runtime": "copilot"}) == "copilot"

    def test_resolve_agent_runtime_uses_env_default(self, monkeypatch):
        monkeypatch.setenv("ORCHESTRATION_AGENT_RUNTIME", "copilot")
        assert _resolve_agent_runtime({}) == "copilot"

    def test_resolve_agent_runtime_invalid_raises(self):
        with pytest.raises(ValueError, match="Invalid x-runtime"):
            _resolve_agent_runtime({"runtime": "cursor"})


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

    def test_x_timeout_parsed_from_frontmatter(self, workspace):
        agent_path = os.path.join(workspace, "Agents", "slow_agent.md")
        with open(agent_path, "w") as f:
            f.write(
                "---\n"
                "name: Slow Agent\n"
                "description: Takes a while\n"
                "x-timeout: 3600\n"
                "---\n"
                "Do slow stuff.\n"
            )
        agent_def = _parse_agent_md(agent_path)
        assert agent_def["timeout"] == 3600

    def test_no_x_timeout_defaults_none(self, workspace):
        agent_path = os.path.join(workspace, "Agents", "fast_agent.md")
        with open(agent_path, "w") as f:
            f.write(
                "---\nname: Fast\ndescription: Quick\n---\nDo stuff.\n"
            )
        agent_def = _parse_agent_md(agent_path)
        assert agent_def["timeout"] is None

    def test_model_headers_parsed_from_frontmatter(self, workspace):
        agent_path = os.path.join(workspace, "Agents", "model_agent.md")
        with open(agent_path, "w") as f:
            f.write(
                "---\n"
                "name: Model Agent\n"
                "description: Model headers\n"
                "x-model: anthropic/claude-opus-4-6\n"
                "x-model-level: high\n"
                "x-effort: medium\n"
                "x-runtime: cline\n"
                "---\n"
                "Do model-aware stuff.\n"
            )
        agent_def = _parse_agent_md(agent_path)
        assert agent_def["model"] == "anthropic/claude-opus-4-6"
        assert agent_def["model_level"] == "high"
        assert agent_def["effort"] == "medium"
        assert agent_def["runtime"] == "cline"

    def test_x_role_parsed_from_frontmatter(self, workspace):
        agent_path = os.path.join(workspace, "Agents", "role_agent.md")
        with open(agent_path, "w") as f:
            f.write(
                "---\n"
                "name: Role Agent\n"
                "description: Role header\n"
                "x-role: manager\n"
                "---\n"
                "Do role-aware work.\n"
            )
        agent_def = _parse_agent_md(agent_path)
        assert agent_def["agent_type"] == "manager"

    def test_x_learning_defaults_true(self, workspace):
        agent_path = os.path.join(workspace, "Agents", "learning_default_agent.md")
        with open(agent_path, "w", encoding="utf-8") as f:
            f.write(
                "---\n"
                "name: Learning Default Agent\n"
                "description: No explicit x-learning\n"
                "---\n"
                "Do work.\n"
            )
        agent_def = _parse_agent_md(agent_path)
        assert agent_def["learning_enabled"] is True

    def test_x_learning_false_disables_feature(self, workspace):
        agent_path = os.path.join(workspace, "Agents", "learning_off_agent.md")
        with open(agent_path, "w", encoding="utf-8") as f:
            f.write(
                "---\n"
                "name: Learning Off Agent\n"
                "description: Turns learning off\n"
                "x-learning: false\n"
                "---\n"
                "Do work.\n"
            )
        agent_def = _parse_agent_md(agent_path)
        assert agent_def["learning_enabled"] is False
