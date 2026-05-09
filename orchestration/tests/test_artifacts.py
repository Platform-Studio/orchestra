"""Tests for artifact operations."""

import os
from pathlib import Path
import pytest
from orchestration.artifacts import create_artifact, read_artifact, list_artifacts, copy_artifact_tree
from orchestration.workstreams import create_workstream, save_workstream


class TestCreateArtifact:
    def test_create_basic(self, workspace):
        result = create_artifact("report.md", "# Report\nDone.", base_dir=workspace)
        assert result["created"] is True
        assert result["path"] == "report.md"

    def test_create_nested_path(self, workspace):
        result = create_artifact("seo/backlinks.md", "# Backlinks", base_dir=workspace)
        assert result["created"] is True
        assert os.path.exists(os.path.join(workspace, "artifacts", "seo", "backlinks.md"))

    def test_create_overwrites(self, workspace):
        create_artifact("file.txt", "v1", base_dir=workspace)
        create_artifact("file.txt", "v2", base_dir=workspace)
        content = read_artifact("file.txt", base_dir=workspace)
        assert content == "v2"

    def test_path_traversal_blocked(self, workspace):
        with pytest.raises(ValueError, match="Path traversal"):
            create_artifact("../../etc/passwd", "evil", base_dir=workspace)


class TestReadArtifact:
    def test_read_existing(self, workspace):
        create_artifact("test.txt", "hello world", base_dir=workspace)
        content = read_artifact("test.txt", base_dir=workspace)
        assert content == "hello world"

    def test_read_not_found(self, workspace):
        with pytest.raises(FileNotFoundError):
            read_artifact("nonexistent.txt", base_dir=workspace)

    def test_path_traversal_blocked(self, workspace):
        with pytest.raises(ValueError, match="Path traversal"):
            read_artifact("../../etc/passwd", base_dir=workspace)


class TestListArtifacts:
    def test_list_empty(self, workspace):
        result = list_artifacts(base_dir=workspace)
        assert result == []

    def test_list_multiple(self, workspace):
        create_artifact("a.txt", "a", base_dir=workspace)
        create_artifact("b.txt", "b", base_dir=workspace)
        result = list_artifacts(base_dir=workspace)
        assert len(result) == 2

    def test_list_with_prefix(self, workspace):
        create_artifact("seo/page1.md", "p1", base_dir=workspace)
        create_artifact("seo/page2.md", "p2", base_dir=workspace)
        create_artifact("other/file.md", "o", base_dir=workspace)
        result = list_artifacts(prefix="seo/", base_dir=workspace)
        assert len(result) == 2
        assert all(r.startswith("seo/") for r in result)

    def test_list_nested(self, workspace):
        create_artifact("a/b/c.txt", "deep", base_dir=workspace)
        result = list_artifacts(base_dir=workspace)
        assert "a/b/c.txt" in result


class TestArtifactRouting:
    def test_working_directory_create_with_workstream_context_routes_to_workspace_artifacts(self, workspace, tmp_path):
        working_root = tmp_path / "career_pivot_repo"
        working_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="ideas", parent_id=parent.id, base_dir=workspace)

        create_artifact("ideas/april.md", "hello", base_dir=workspace, workstream_id=child.id)

        target = Path(workspace) / "artifacts" / "ideas" / "april.md"
        assert os.path.exists(target)
        with open(target, encoding="utf-8") as f:
            assert f.read() == "hello"

    def test_explicit_artifact_root_is_inherited_by_descendants(self, workspace, tmp_path):
        mount_root = tmp_path / "career_pivot_repo"
        mount_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.artifact_root = str(mount_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="ideas", parent_id=parent.id, base_dir=workspace)

        create_artifact("reports/a.md", "mounted", base_dir=workspace, workstream_id=parent.id)
        create_artifact("reports/b.md", "child", base_dir=workspace, workstream_id=child.id)

        mounted_paths = list_artifacts(prefix="reports/", base_dir=workspace, workstream_id=parent.id)
        child_paths = list_artifacts(prefix="reports/", base_dir=workspace, workstream_id=child.id)
        assert mounted_paths == ["reports/a.md", "reports/b.md"]
        assert child_paths == ["reports/a.md", "reports/b.md"]
        assert os.path.exists(mount_root / "artifacts" / "reports" / "a.md")
        assert os.path.exists(mount_root / "artifacts" / "reports" / "b.md")


class TestArtifactCopyTree:
    def test_copytree_can_target_local_artifact_store(self, workspace):
        ws = create_workstream(name="local_ws", base_dir=workspace)
        create_artifact("Stage 2 Research/example/readme.md", "hello", base_dir=workspace)

        result = copy_artifact_tree(
            "Stage 2 Research/example",
            base_dir=workspace,
            workstream_id=ws.id,
        )

        assert result["skipped_same_count"] == 1

    def test_copytree_uses_workspace_artifact_root_for_working_directory_context(self, workspace, tmp_path):
        working_root = tmp_path / "startup_repo"
        working_root.mkdir()
        source_root = tmp_path / "source_repo"

        parent = create_workstream(name="startup", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)
        child = create_workstream(name="product_development", parent_id=parent.id, base_dir=workspace)

        create_artifact("Stage 2 Research/example/one.md", "one", base_dir=str(source_root))
        create_artifact("Stage 2 Research/example/nested/two.md", "two", base_dir=str(source_root))

        result = copy_artifact_tree(
            "Stage 2 Research/example",
            base_dir=workspace,
            workstream_id=child.id,
            source_base_dir=str(source_root),
        )

        assert result["copy_count"] == 2
        with open(Path(workspace) / "artifacts" / "Stage 2 Research" / "example" / "one.md", encoding="utf-8") as f:
            assert f.read() == "one"
        with open(Path(workspace) / "artifacts" / "Stage 2 Research" / "example" / "nested" / "two.md", encoding="utf-8") as f:
            assert f.read() == "two"

    def test_copytree_conflict_requires_overwrite(self, workspace, tmp_path):
        mount_root = tmp_path / "startup_repo"
        mount_root.mkdir()
        source_root = tmp_path / "source_repo"

        parent = create_workstream(name="startup", base_dir=workspace)
        parent.working_directory = str(mount_root)
        save_workstream(parent, base_dir=workspace)
        child = create_workstream(name="product_development", parent_id=parent.id, base_dir=workspace)

        create_artifact("Stage 2 Research/example/file.md", "source", base_dir=str(source_root))
        create_artifact("Stage 2 Research/example/file.md", "dest", base_dir=workspace, workstream_id=child.id)

        with pytest.raises(FileExistsError, match="would overwrite"):
            copy_artifact_tree(
                "Stage 2 Research/example",
                base_dir=workspace,
                workstream_id=child.id,
                source_base_dir=str(source_root),
            )

        result = copy_artifact_tree(
            "Stage 2 Research/example",
            base_dir=workspace,
            workstream_id=child.id,
            source_base_dir=str(source_root),
            overwrite=True,
        )
        assert result["overwrite_count"] == 1
        with open(Path(workspace) / "artifacts" / "Stage 2 Research" / "example" / "file.md", encoding="utf-8") as f:
            assert f.read() == "source"


class TestArtifactRootUris:
    def test_artifact_root_file_uri_rehomes_artifacts(self, workspace, tmp_path, monkeypatch):
        artifact_root = tmp_path / "shared_artifacts"
        monkeypatch.setenv("ARTIFACT_ROOT", f"file:{artifact_root}")

        create_artifact("reports/a.md", "ok", base_dir=workspace)

        assert os.path.exists(artifact_root / "artifacts" / "reports" / "a.md")
