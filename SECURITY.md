# Security Policy

## Supported Versions

Until Orchestra reaches 1.0, security fixes are made on the latest released version and the `main` branch. Older pre-1.0 releases are not maintained separately.

## Reporting A Vulnerability

Do not open a public issue or discussion for a suspected vulnerability.

Use GitHub's private vulnerability reporting form:

https://github.com/Platform-Studio/orchestra/security/advisories/new

Include the affected version or commit, impact, reproduction steps, and any suggested mitigation. Remove real credentials, personal data, and unrelated private material from the report.

We aim to acknowledge a report within five business days and provide a status update within ten business days. Resolution timing depends on severity and complexity. We will coordinate disclosure and credit with the reporter unless anonymity is requested.

If private vulnerability reporting is temporarily unavailable, contact a repository maintainer privately through the contact information on the Platform-Studio GitHub organization. Do not transmit secrets in an initial message.

## Security Scope

High-value areas include:

- command and agent execution;
- path traversal or access outside configured workspace and artifact roots;
- exposure of API keys, environment variables, prompts, logs, or artifacts;
- unauthenticated network access to Workstream Manager;
- unsafe upgrade or persistence behavior;
- dependency or installation-chain compromise;
- bypasses of task locks, pause controls, or confirmation requirements.

The current trust boundaries and deployment assumptions are documented in `docs/security-model.md`.
