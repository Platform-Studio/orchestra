from scripts.audit_licenses import incompatible_packages
from scripts.audit_public_repo import scan_text


def test_public_repo_audit_detects_internal_paths_and_private_names():
    text = "workspace=/" + "Users/example/private\nrepo=" + "found" + "ation\n"

    findings = scan_text("example.txt", text)

    assert {finding.rule for finding in findings} == {
        "macOS home path",
        "private repository name",
    }


def test_public_repo_audit_allows_public_examples():
    assert scan_text("README.md", "Use /path/to/agents and admin@example.com") == []


def test_license_audit_rejects_unknown_and_unapproved_licenses():
    rows = [
        {"Name": "allowed", "Version": "1", "License": "MIT"},
        {"Name": "unknown", "Version": "1", "License": "UNKNOWN"},
        {"Name": "copyleft", "Version": "1", "License": "GPL-3.0"},
        {"Name": "orchestra", "Version": "0.1.0", "License": "UNKNOWN"},
    ]

    assert [row["Name"] for row in incompatible_packages(rows)] == ["unknown", "copyleft"]