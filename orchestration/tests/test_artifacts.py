"""Tests for artifact operations."""

import os
import pytest
from orchestration.artifacts import create_artifact, read_artifact, list_artifacts


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
