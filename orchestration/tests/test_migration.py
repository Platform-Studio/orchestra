import os
import shutil
from pathlib import Path

import pytest

from orchestration.artifacts import create_artifact, read_artifact
from orchestration.migration import (
    migrate_artifact_root_home,
    migrate_child_workstream_layout,
)
from orchestration.tasks import create_task
from orchestration.workstreams import create_workstream, read_workstream, save_workstream


def test_migrate_artifact_root_home_dry_run_reports_target_root(workspace, tmp_path):
    mount_root = tmp_path / "mounted_repo"
    mount_root.mkdir()

    parent = create_workstream(name="Product", base_dir=workspace)
    parent.working_directory = str(mount_root)
    parent.artifact_root = str(mount_root)
    save_workstream(parent, base_dir=workspace)

    child = create_workstream(name="Go-to-Market", parent_id=parent.id, base_dir=workspace)
    create_artifact("reports/one.md", "hello", base_dir=workspace, workstream_id=parent.id)

    result = migrate_artifact_root_home(parent.id, base_dir=workspace, dry_run=True)

    assert result["planned_artifact_file_count"] == 1
    assert result["root_update"]["artifact_root"] is None


def test_migrate_artifact_root_home_apply_copies_artifacts_and_rewrites_root(workspace, tmp_path):
    mount_root = tmp_path / "mounted_repo"
    mount_root.mkdir()

    parent = create_workstream(name="Product", base_dir=workspace)
    parent.working_directory = str(mount_root)
    parent.artifact_root = str(mount_root)
    save_workstream(parent, base_dir=workspace)

    child = create_workstream(name="Go-to-Market", parent_id=parent.id, base_dir=workspace)
    create_artifact("reports/one.md", "hello", base_dir=workspace, workstream_id=parent.id)

    result = migrate_artifact_root_home(parent.id, base_dir=workspace, dry_run=False)

    assert result["applied"] is True
    assert read_artifact("reports/one.md", base_dir=workspace, workstream_id=child.id) == "hello"

    reloaded = read_workstream(parent.id, base_dir=workspace)
    assert reloaded.artifact_root is None
    assert Path(workspace, "artifacts", "reports", "one.md").exists()


def test_migrate_artifact_root_home_detects_conflicts(workspace, tmp_path):
    mount_root = tmp_path / "mounted_repo"
    mount_root.mkdir()

    parent = create_workstream(name="Product", base_dir=workspace)
    parent.working_directory = str(mount_root)
    parent.artifact_root = str(mount_root)
    save_workstream(parent, base_dir=workspace)

    child = create_workstream(name="Go-to-Market", parent_id=parent.id, base_dir=workspace)
    create_artifact("reports/one.md", "source", base_dir=workspace, workstream_id=parent.id)

    target_path = Path(workspace, "artifacts", "reports", "one.md")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("dest")

    result = migrate_artifact_root_home(parent.id, base_dir=workspace, dry_run=True)

    assert any(path.endswith(os.path.join("artifacts", "reports", "one.md")) for path in result["conflicts"])

    with pytest.raises(FileExistsError):
        migrate_artifact_root_home(parent.id, base_dir=workspace, dry_run=False)


def test_migrate_artifact_root_home_recovers_from_stale_flat_path_after_child_layout(workspace):
    root = create_workstream(name="Root", base_dir=workspace)
    child = create_workstream(name="Child", parent_id=root.id, base_dir=workspace)

    stale_flat_root = Path(workspace, "workstreams", child.id)
    current_root = Path(workspace, "workstreams", root.id, "workstreams", child.id)
    artifact_path = current_root / "artifacts" / "reports" / "one.md"
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("hello")

    reloaded = read_workstream(child.id, base_dir=workspace)
    reloaded.artifact_root = str(stale_flat_root)
    save_workstream(reloaded, base_dir=workspace)

    result = migrate_artifact_root_home(child.id, base_dir=workspace, dry_run=False)

    assert result["applied"] is True
    assert Path(workspace, "artifacts", "reports", "one.md").exists()
    updated = read_workstream(child.id, base_dir=workspace)
    assert updated.artifact_root is None


def test_migrate_artifact_root_home_can_archive_conflicts(workspace, tmp_path):
    mount_root = tmp_path / "mounted_repo"
    mount_root.mkdir()

    parent = create_workstream(name="Product", base_dir=workspace)
    parent.working_directory = str(mount_root)
    parent.artifact_root = str(mount_root)
    save_workstream(parent, base_dir=workspace)

    create_artifact("reports/one.md", "source", base_dir=workspace, workstream_id=parent.id)
    create_artifact("reports/one.md", "dest", base_dir=workspace)

    result = migrate_artifact_root_home(parent.id, base_dir=workspace, dry_run=False, archive_conflicts=True)

    assert result["applied"] is True
    assert Path(workspace, "artifacts", "_migration_conflicts", parent.id, "reports", "one.md").exists()
    updated = read_workstream(parent.id, base_dir=workspace)
    assert updated.artifact_root is None


def test_migrate_child_workstream_layout_rehomes_flat_descendants(workspace):
    root = create_workstream(name="Root", base_dir=workspace)
    child = create_workstream(name="Child", parent_id=root.id, base_dir=workspace)
    grandchild = create_workstream(name="Grandchild", parent_id=child.id, base_dir=workspace)
    task = create_task(grandchild.id, title="Ship it", base_dir=workspace)

    flat_child_yaml = Path(workspace, "workstreams", f"{child.id}.yaml")
    flat_grandchild_yaml = Path(workspace, "workstreams", f"{grandchild.id}.yaml")
    child_nested_yaml = Path(workspace, "workstreams", root.id, "workstreams", f"{child.id}.yaml")
    child_nested_dir = Path(workspace, "workstreams", root.id, "workstreams", child.id)
    grandchild_nested_yaml = child_nested_dir / "workstreams" / f"{grandchild.id}.yaml"
    grandchild_nested_dir = child_nested_dir / "workstreams" / grandchild.id

    flat_child_yaml.write_text(child_nested_yaml.read_text())
    shutil.copytree(child_nested_dir, Path(workspace, "workstreams", child.id))
    flat_grandchild_yaml.write_text(grandchild_nested_yaml.read_text())
    shutil.rmtree(child_nested_dir)
    child_nested_yaml.unlink()

    result = migrate_child_workstream_layout(base_dir=workspace, dry_run=False)

    assert result["applied"] is True
    assert child.id in result["moved_workstream_ids"]
    assert grandchild.id in result["moved_workstream_ids"]
    assert Path(workspace, "workstreams", root.id, "workstreams", f"{child.id}.yaml").exists()
    assert Path(workspace, "workstreams", root.id, "workstreams", child.id, "workstreams", f"{grandchild.id}.yaml").exists()
    assert Path(workspace, "workstreams", root.id, "workstreams", child.id, "workstreams", grandchild.id, "tasks", f"{task.id}.yaml").exists()
    assert not flat_child_yaml.exists()
    assert not flat_grandchild_yaml.exists()