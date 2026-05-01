"""Tests for artifact operations."""

import os
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


class TestMountedArtifactRouting:
    def test_create_with_workstream_context_routes_to_mounted_workspace(self, workspace, tmp_path):
        mount_root = tmp_path / "career_pivot_repo"
        mount_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.mounted_workspace_path = str(mount_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="ideas", parent_id=parent.id, base_dir=workspace)

        create_artifact("ideas/april.md", "hello", base_dir=workspace, workstream_id=child.id)

        target = mount_root / "artifacts" / "ideas" / "april.md"
        assert target.exists()
        assert target.read_text() == "hello"

    def test_create_with_logical_path_uses_mounted_node_name(self, workspace, tmp_path):
        mount_root = tmp_path / "career_pivot_repo"
        mount_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.mounted_workspace_path = str(mount_root)
        save_workstream(parent, base_dir=workspace)

        logical = "/Platform/Stage 3/career_pivot/ideas/april.md"
        create_artifact(logical, "lobster", base_dir=workspace)

        target = mount_root / "artifacts" / "ideas" / "april.md"
        assert target.exists()
        assert target.read_text() == "lobster"

    def test_list_with_workstream_context_uses_mounted_artifact_root(self, workspace, tmp_path):
        mount_root = tmp_path / "career_pivot_repo"
        mount_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.mounted_workspace_path = str(mount_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="ideas", parent_id=parent.id, base_dir=workspace)

        create_artifact("reports/a.md", "mounted", base_dir=workspace, workstream_id=child.id)
        create_artifact("reports/b.md", "local", base_dir=workspace)

        mounted_paths = list_artifacts(prefix="reports/", base_dir=workspace, workstream_id=child.id)
        assert mounted_paths == ["reports/a.md"]


class TestArtifactCopyTree:
    def test_copytree_requires_mounted_destination(self, workspace):
        ws = create_workstream(name="local_ws", base_dir=workspace)
        create_artifact("Stage 2 Research/example/readme.md", "hello", base_dir=workspace)

        with pytest.raises(ValueError, match="not mounted"):
            copy_artifact_tree(
                "Stage 2 Research/example",
                base_dir=workspace,
                workstream_id=ws.id,
            )

    def test_copytree_copies_into_mounted_workspace(self, workspace, tmp_path):
        mount_root = tmp_path / "startup_repo"
        mount_root.mkdir()

        parent = create_workstream(name="startup", base_dir=workspace)
        parent.mounted_workspace_path = str(mount_root)
        save_workstream(parent, base_dir=workspace)
        child = create_workstream(name="product_development", parent_id=parent.id, base_dir=workspace)

        create_artifact("Stage 2 Research/example/one.md", "one", base_dir=workspace)
        create_artifact("Stage 2 Research/example/nested/two.md", "two", base_dir=workspace)

        result = copy_artifact_tree(
            "Stage 2 Research/example",
            base_dir=workspace,
            workstream_id=child.id,
            source_base_dir=workspace,
        )

        assert result["copy_count"] == 2
        assert (mount_root / "artifacts" / "Stage 2 Research" / "example" / "one.md").read_text() == "one"
        assert (mount_root / "artifacts" / "Stage 2 Research" / "example" / "nested" / "two.md").read_text() == "two"

    def test_copytree_conflict_requires_overwrite(self, workspace, tmp_path):
        mount_root = tmp_path / "startup_repo"
        mount_root.mkdir()

        parent = create_workstream(name="startup", base_dir=workspace)
        parent.mounted_workspace_path = str(mount_root)
        save_workstream(parent, base_dir=workspace)
        child = create_workstream(name="product_development", parent_id=parent.id, base_dir=workspace)

        create_artifact("Stage 2 Research/example/file.md", "source", base_dir=workspace)
        create_artifact("Stage 2 Research/example/file.md", "dest", base_dir=str(mount_root))

        with pytest.raises(FileExistsError, match="would overwrite"):
            copy_artifact_tree(
                "Stage 2 Research/example",
                base_dir=workspace,
                workstream_id=child.id,
                source_base_dir=workspace,
            )

        result = copy_artifact_tree(
            "Stage 2 Research/example",
            base_dir=workspace,
            workstream_id=child.id,
            source_base_dir=workspace,
            overwrite=True,
        )
        assert result["overwrite_count"] == 1
        assert (mount_root / "artifacts" / "Stage 2 Research" / "example" / "file.md").read_text() == "source"
