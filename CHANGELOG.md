# Changelog

All notable changes to Orchestra will be recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-09-03

Orchestra has been used in production by Platform Venture Studio to coordinate the agent workflows involved in building and operating multiple companies. This beta release marks the point at which its core architecture and interfaces are considered substantially stable. Broader external use may still identify installation, compatibility, and workflow issues before 1.0.

### Added

- Packaged `orc` command with `orchestra` as a compatibility alias.
- Deterministic installation smoke workflow for credential-free release checks.
- Live Fruit and Vegetable Sorter tutorial with generator and classifier agent definitions plus a state-based trigger.
- Cross-platform clean-install, upgrade-preservation, and example release checks.
- Public release, security, contribution, governance, and support documentation.

### Changed

- Public examples and documentation use synthetic data and the canonical `orc` command.
- Agent process handling supports macOS, Windows, and Linux.

### Fixed

- `run-orchestra.sh` now detects unexpected scheduler termination, shuts down the Workstream Manager, and exits with an error instead of leaving a partially running installation.

### Security

- Added CI checks for secrets, dependency licenses, private identifiers, and internal paths.
- Documented Orchestra's local trust model and command-execution boundaries.

[0.5.0]: https://github.com/Platform-Studio/orchestra/releases/tag/v0.5.0
