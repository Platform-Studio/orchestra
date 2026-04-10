"""Shared fixtures for orchestration tests."""

import os
import pytest


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
