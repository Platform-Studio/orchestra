# Public Release Audit

Audit date: August 25, 2026

The extracted repository was checked before public release automation was enabled.

## Results

- Secret scan: Gitleaks 8.30.1 scanned all 117 commits with content across reachable refs and reported no secrets.
- License scan: 25 installed dependencies across the core, development, image, and browser dependency groups used compatible Apache, MIT, BSD, PSF, or MIT-CMU licenses.
- Internal-path scan: no user home paths remain in tracked release content.
- Proprietary-name scan: internal identities, domains, repository names, artifact directories, and private workflow names were removed from tracked release content.
- Asset review: no binary assets are currently tracked, so there are no bundled image, audio, font, or other media licenses to review.

Orchestra is licensed under Apache License 2.0. Its SPDX package metadata identifies `Apache-2.0`, and the canonical license text is included in both wheel and source distributions.

Older public commits contain organization-specific example names but no detected secrets or private file paths. Those historical references are retained to preserve the repository history required by the separation plan; the release branch and CI-enforced tracked content are clean.

## Reproducing The Audit

```bash
gitleaks git --log-opts="--all" --redact .
python scripts/audit_public_repo.py
python scripts/audit_licenses.py
```

The GitHub Actions audit job runs the same checks for every push and pull request. It checks out complete Git history for Gitleaks and installs every declared optional dependency group before auditing licenses.