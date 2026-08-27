# Installation Smoke Test

This deliberately simple workflow verifies that an installed Orchestra package can create, update, and read persisted data. It uses `New`, `In Progress`, and `Done` states and requires no agent runtime, API key, or network access.

```bash
python examples/install_smoke/run.py --workspace ./smoke-workspace
orc --base-dir ./smoke-workspace workstream list
```

This is release infrastructure, not the agent tutorial. CI runs it from an installed wheel outside the source checkout and checks the result against `expected_state.yaml`.