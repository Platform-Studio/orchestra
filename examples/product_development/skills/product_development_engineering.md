# Product Development Engineering

Follow the mounted repository's own contribution, architecture, branching, testing, and release rules. Prefer the smallest change that fully satisfies the task and preserve unrelated user work.

Plans should identify affected behavior, code, tests, risks, and verification. Implementations require focused automated tests. Reviews must inspect the actual diff and report findings before summaries. Integration testing must run every repository-defined pre-deployment gate and record exact commands and results.

Never report a check as passing unless it ran successfully. Send task defects to `In Progress`, review defects to `Code Review`, and external dependencies or access problems to `Blocked`.