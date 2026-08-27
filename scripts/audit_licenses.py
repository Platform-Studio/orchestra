#!/usr/bin/env python3
"""Check installed Orchestra dependencies against approved license metadata."""

from __future__ import annotations

import json
import subprocess
import sys


ALLOWED_LICENSES = {
    "Apache Software License; MIT License",
    "Apache-2.0",
    "Apache-2.0 OR BSD-2-Clause",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "MIT",
    "MIT AND PSF-2.0",
    "MIT License",
    "MIT-CMU",
    "PSF-2.0",
}
IGNORED_PACKAGES = {"orchestra"}


def incompatible_packages(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if row.get("Name", "").lower() not in IGNORED_PACKAGES
        and row.get("License") not in ALLOWED_LICENSES
    ]


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "piplicenses", "--format=json"],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = json.loads(result.stdout)
    incompatible = incompatible_packages(rows)
    for row in incompatible:
        print(f"{row.get('Name')} {row.get('Version')}: {row.get('License') or 'UNKNOWN'}")
    if incompatible:
        print(f"License audit failed with {len(incompatible)} package(s) requiring review.")
        return 1
    audited_count = sum(1 for row in rows if row.get("Name", "").lower() not in IGNORED_PACKAGES)
    print(f"License audit passed for {audited_count} installed dependencies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())