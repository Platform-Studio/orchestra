# Xmas Movies

This example demonstrates a hierarchical workstream, a branching state machine, scheduled and state-based triggers, a reusable skill, and four agents handing work to one another.

## Install And Run

From the Orchestra repository root:

```bash
./example.sh xmas_movies
./run-orchestra.sh ./xmas-movies-workspace
```

Installation creates `Examples > Xmas Movies`, installs the agent and skill definitions, and configures this workflow:

```text
Advocate -> Skeptic -> Judgement -> Decided
                          |
                          +--------> Advocate
```

The hourly proposer trigger is installed paused to avoid unexpected ongoing provider costs. On first installation, the setup script runs the proposer once so the workflow has an initial movie to process. Use `--no-run` to install without contacting an LLM provider:

```bash
./example.sh xmas_movies --no-run
```

Rerunning the installer reuses matching workstreams and triggers. It will not create another initial movie. If an existing `Examples > Xmas Movies` workstream or event trigger conflicts with the example definition, installation stops with an explanation instead of overwriting it.