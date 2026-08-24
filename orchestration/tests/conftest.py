"""Shared fixtures for orchestration tests."""

import os
import pytest


@pytest.fixture(autouse=True)
def _isolate_test_workspace_env(monkeypatch):
    # Prevent local developer .env persistence roots and audio defaults from
    # leaking into tests that should operate entirely inside tmp_path workspaces.
    monkeypatch.setenv("WORKSTREAM_ROOT", "")
    monkeypatch.setenv("ARTIFACT_ROOT", "")
    monkeypatch.setenv("ARTICACT_ROOT", "")
    monkeypatch.setenv("ORCHESTRATION_BASE_ENV_PATH", "")
    monkeypatch.setenv("AUDIO_FILE_PATH", "")
    monkeypatch.setenv("DEFAULT_AGENT_START_SOUND", "")
    monkeypatch.setenv("DEFAULT_AGENT_FINISHED_SOUND", "")
    monkeypatch.setenv("DEFAULT_AGENT_ERROR_SOUND", "")

    from orchestration.workstreams import clear_workstream_cache

    clear_workstream_cache()
    yield
    clear_workstream_cache()


@pytest.fixture
def workspace(tmp_path):
    """Create a temporary workspace directory with Agents/ and workstreams/ dirs."""
    agents_dir = tmp_path / "Agents"
    agents_dir.mkdir()

    # Create a sample agent .md file
    (agents_dir / "test_agent.md").write_text(
        "---\n"
        "name: Test Agent\n"
        "description: A test agent for unit testing\n"
        "x-agent-type: worker\n"
        "---\n"
        "You are a test agent. Do nothing.\n"
    )

    # Create Agents/cli/ with sample CLI tools for discovery tests
    cli_dir = agents_dir / "cli"
    cli_dir.mkdir()

    (cli_dir / "alpha.py").write_text(
        "import sys\nprint(' '.join(sys.argv[1:]))\n"
    )
    (cli_dir / "alpha.md").write_text(
        "## Alpha Tool\nUsage: python3 alpha.py <args>\n"
    )

    (cli_dir / "beta.py").write_text(
        "import sys\nprint('beta:', ' '.join(sys.argv[1:]))\n"
    )
    (cli_dir / "beta.md").write_text(
        "## Beta Tool\nUsage: python3 beta.py <args>\n"
    )

    # A .py with no matching .md — should NOT be discovered
    (cli_dir / "orphan.py").write_text("print('orphan')\n")

    return str(tmp_path)
