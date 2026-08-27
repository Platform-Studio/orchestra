# Releasing Orchestra

This is the maintainer checklist for source and Python package releases.

## Versioning

Orchestra follows Semantic Versioning. Before 1.0, a minor version may contain incompatible API or persistence changes, but those changes must be called out prominently and include an upgrade path. Patch releases must remain backward compatible.

The package version in `pyproject.toml`, the Git tag, and the GitHub release must agree. Tags use `vMAJOR.MINOR.PATCH`.

## Release Requirements

1. Update `CHANGELOG.md`, moving relevant entries from `Unreleased` into a dated version section.
2. Update the package version.
3. Document any configuration or persisted-data migration.
4. Run:

   ```bash
   python -m pytest -q
   python scripts/test_release_flow.py
   python scripts/audit_public_repo.py
   python scripts/audit_licenses.py
   gitleaks git --log-opts="--all" --redact .
   ```

5. Confirm the macOS, Windows, and Linux CI jobs pass from a clean checkout.
6. Build and inspect distributions in a clean environment:

   ```bash
   python -m pip install --upgrade build twine
   python -m build
   python -m twine check dist/*
   ```

7. Install the wheel into a new virtual environment and run `orc --help` plus the deterministic installation smoke workflow.
8. Create the signed Git tag and GitHub release from the reviewed commit.
9. Publish to PyPI using GitHub trusted publishing. Do not use a long-lived PyPI token in repository secrets.
10. Verify package metadata, provenance, installation through `pipx` and `uv tool`, and links from the published package page.

Do not publish until trusted publishing and the release environment are configured. GitHub artifact attestations or equivalent provenance should be enabled for published distributions.

## Upgrade And Data Policy

Package installation and upgrade must not delete or replace `.env`, workstreams, tasks, artifacts, logs, or audit history. Persisted-format changes require a tested migration, backup instructions, rollback considerations, and a changelog entry.

The release-flow harness builds a prior package revision, creates user data, upgrades to the current wheel, and verifies that the data remains readable and unchanged.

## Failed Releases

Do not reuse a published version number. If a release is broken, stop promotion, document the impact, and publish a corrected patch version. Yank a PyPI release only when leaving it available would predictably harm new users; record the reason in the GitHub release and changelog.
