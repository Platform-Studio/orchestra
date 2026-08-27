# Security Model

Orchestra coordinates tools that can execute commands, read and write files, access credentials, and incur provider costs. It is a local orchestration tool for a trusted operator, not a sandbox for hostile users or hostile code.

## Protected Assets

- API keys and other values in `.env` or the process environment;
- source repositories and files reachable by agent runtimes;
- workstream, task, artifact, comment, and audit data;
- provider accounts and the spending they authorize;
- the integrity of triggers, agent definitions, and persisted state.

## Trust Boundaries

### Workstream Manager

Workstream Manager binds to `127.0.0.1` by default and does not implement user authentication. Any local process able to connect can use its API with the current user's authority. Do not expose it through port forwarding, a reverse proxy, a container port, or a non-loopback bind without adding authentication, authorization, transport encryption, and request limits in front of it.

### CLI And Scheduler

The CLI and scheduler act with the permissions of the operating-system user that starts them. Task and trigger operations can mutate persisted state. Scheduled command triggers intentionally execute configured commands through a system shell. Treat permission to create or edit triggers as permission to execute commands.

### Agent Runtimes

Claude Code, Cline, GitHub Copilot CLI, and future runtimes are separate processes with access determined by their own configuration and the operating-system user. Orchestra does not sandbox them. Prompts, task text, attachments, repositories, external agent definitions, and tool documentation may contain malicious or misleading instructions. Review untrusted inputs before starting an agent with filesystem, shell, browser, or network access.

### External Services

Provider CLIs and optional Mailgun, Slack, image, voice, browser, or proxy integrations send data to their respective services under those services' policies. Use least-privilege credentials and avoid sending secrets or regulated data unless the provider and deployment are approved for it.

## Existing Controls

- Workstream Manager listens only on the IPv4 loopback interface.
- Artifact and attachment paths are resolved against configured roots and validated before access.
- Runtime data and `.env` are ignored by Git.
- Agent runs, task changes, comments, and transitions retain audit information.
- Locks and pause controls reduce accidental concurrent or unwanted execution.
- Agent runtime timeouts limit a single orchestration invocation.
- CI scans Git history for secrets and scans tracked content for internal paths and private identifiers.
- Dependency licenses are checked before release.
- Orchestra currently collects no product telemetry.

These controls reduce risk but do not make agent output or commands trustworthy.

## Operator Responsibilities

- Run Orchestra under a dedicated, non-administrator account when practical.
- Keep Workstream Manager on localhost.
- Scope API keys to the minimum permissions and spending limits required.
- Review agent definitions, external discovery paths, triggers, and commands before enabling them.
- Keep secrets out of tasks, comments, artifacts, logs, screenshots, and bug reports.
- Back up workspace and artifact roots before upgrades or bulk operations.
- Keep Python, Orchestra, agent runtimes, browsers, and provider CLIs updated.
- Stop the scheduler or pause workstreams when investigating unexpected execution.

## Out Of Scope

The current release does not claim to protect against:

- a malicious or compromised local operating-system user;
- a compromised agent runtime or provider account;
- arbitrary hostile code executed with the same user permissions;
- denial of service by trusted local users or configured agents;
- secure multi-user or Internet-facing deployment of Workstream Manager.

Report vulnerabilities using `SECURITY.md`.
