# Governance

Orchestra is maintained by Platform (Platform Studio Inc) with contributions from the community. Governance is intentionally lightweight so a small team can make decisions and ship fixes without unnecessary process.

## Roles

- **Contributors** report issues, propose changes, and submit pull requests.
- **Maintainers** review and merge changes, manage releases, handle security reports, and set project direction.
- **Platform** appoints maintainers and has final responsibility for the repository and its published packages.

Maintainers are the members of the Platform-Studio GitHub organization with write or maintain access to this repository. GitHub permissions are the authoritative list.

## Decisions

Routine decisions happen in issues and pull requests. Maintainers seek practical consensus, considering user impact, compatibility, security, maintenance cost, and alignment with the project direction. When consensus is not reached promptly, a maintainer makes and records the decision. Platform resolves decisions that materially affect licensing, the open-core boundary, security, or long-term direction.

## Open-Core Commitment

The following capabilities are part of Orchestra's open core:

- local orchestration and core task/workstream management;
- the Orchestra CLI and Workstream Manager;
- filesystem persistence, audit history, scheduling, locks, and public extension contracts;
- the ability to use public or private agents, skills, and CLI tools through documented extension paths.

Code released in this repository under Apache License 2.0 remains available under that license. Platform  may offer separate hosted services, enterprise integrations, managed infrastructure, or other commercial features, but using the local open core will not require those products.

Any future change to this boundary will be documented before release and discussed publicly when it affects existing users or contributors.

## Trademarks

Apache License 2.0 licenses the code, not Platform's names, logos, or branding. You may accurately state that a product uses or is derived from Orchestra and may link to this repository. Modified distributions must not imply endorsement by Platform  or present themselves as the official Orchestra distribution. Use a distinct name and branding for a materially modified distribution.

## Changes To Governance

Governance changes are made through a pull request so the rationale and review remain public.
