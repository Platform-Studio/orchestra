from pathlib import Path

from scripts.verify_external_data_roots import evaluate_repo_root


def test_evaluate_repo_root_passes_when_roots_are_external(monkeypatch, tmp_path):
    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    external_root = tmp_path / "external_data"
    external_root.mkdir()

    monkeypatch.setenv("WORKSTREAM_ROOT", str(external_root))
    monkeypatch.setenv("ARTIFACT_ROOT", str(external_root))

    report = evaluate_repo_root(repo_root)

    assert report.is_valid is True
    assert report.workstream_root == external_root
    assert report.artifact_root == external_root
    assert report.warnings == ()


def test_evaluate_repo_root_fails_when_local_persistence_remains(monkeypatch, tmp_path):
    repo_root = tmp_path / "repository"
    repo_root.mkdir()
    external_root = tmp_path / "external_data"
    external_root.mkdir()

    (repo_root / "workstreams").mkdir()
    (repo_root / "artifacts").mkdir()
    (repo_root / "workspace_audit.yaml").write_text("[]\n", encoding="utf-8")
    (repo_root / "scheduler.log").write_text("legacy\n", encoding="utf-8")

    monkeypatch.setenv("WORKSTREAM_ROOT", str(external_root))
    monkeypatch.setenv("ARTIFACT_ROOT", str(external_root))

    report = evaluate_repo_root(repo_root)

    assert report.is_valid is False
    failed_labels = {check.label for check in report.checks if not check.ok}

    assert "migrated:workstreams" in failed_labels
    assert "migrated:artifacts" in failed_labels
    assert "migrated:workspace_audit.yaml" in failed_labels
    assert report.warnings == (
        f"legacy runtime file still in repo root: {repo_root / 'scheduler.log'}",
    )