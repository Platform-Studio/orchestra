# Fruit and Vegetable Sorter

This five-minute tutorial demonstrates real Orchestra agent definitions, a state-based trigger, custom workstream columns, comments, tags, state transitions, agent runs, and audit history.

It uses your configured Claude Code, Cline, or GitHub Copilot CLI runtime. That runtime must already be authenticated and may use its provider's network API and incur normal provider costs.

## Set Up The Tutorial

From the Orchestra repository root:

```bash
./example.sh fruit_and_veg
```

The command prints the new workstream ID. It also:

- creates `Unsorted`, `Fruit`, and `Vegetable` columns;
- copies the generator and classifier definitions into the workspace's `Agents/` directory;
- creates a schedule trigger that runs the generator every minute;
- creates a trigger that runs the classifier whenever a task is in `Unsorted`.

## Run It

Start Orchestra from the repository root:

```bash
./run-orchestra.sh
```

Open `http://localhost:8080`. Every minute, the generator adds one `Unsorted` task. The classifier trigger reads it, explains its culinary classification in a comment, adds descriptive tags, and moves it to `Fruit` or `Vegetable`.

## Agent Definitions

- `agents/fruit_vegetable_generator.md` creates an item but deliberately does not classify it.
- `agents/fruit_vegetable_classifier.md` is selected by the `Unsorted` trigger and owns classification, comments, tags, and state movement.

The setup test verifies this wiring without pretending that deterministic Python code is an LLM agent. The separate `examples/install_smoke/` workflow handles credential-free release testing.