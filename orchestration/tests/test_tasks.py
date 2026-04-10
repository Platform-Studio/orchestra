"""Tests for task operations."""

import pytest
from orchestration.workstreams import create_workstream
from orchestration.tasks import (
    create_task,
    read_task,
    update_task,
    list_tasks,
    comment_task,
    archive_task,
    get_audit,
)


@pytest.fixture
def ws(workspace):
    """Create a workstream with custom states for testing."""
    states = {
        "To Do": ["In Progress", "Invalid"],
        "In Progress": ["Done", "Failed"],
        "Done": [],
        "Failed": ["To Do"],
        "Invalid": [],
    }
    return create_workstream(name="Test WS", task_states=states, base_dir=workspace)


class TestCreateTask:
    def test_create_basic(self, workspace, ws):
        task = create_task(ws.id, title="Test Task", base_dir=workspace)
        assert task.title == "Test Task"
        assert task.workstream_id == ws.id
        assert task.status == "To Do"  # first state
        assert task.id is not None

    def test_create_with_description(self, workspace, ws):
        task = create_task(ws.id, title="T", description="Do the thing", base_dir=workspace)
        assert task.description == "Do the thing"

    def test_create_with_tags(self, workspace, ws):
        task = create_task(ws.id, title="T", tags=["urgent", "sales"], base_dir=workspace)
        assert task.tags == ["urgent", "sales"]

    def test_create_adds_audit(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert len(task.audit) == 1
        assert task.audit[0].type == "created"

    def test_create_with_default_states(self, workspace):
        ws = create_workstream(name="Default", base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert task.status == "pending"


class TestReadTask:
    def test_read_existing(self, workspace, ws):
        task = create_task(ws.id, title="Read Me", base_dir=workspace)
        loaded = read_task(task.id, base_dir=workspace)
        assert loaded.title == "Read Me"
        assert loaded.id == task.id

    def test_read_not_found(self, workspace):
        with pytest.raises(FileNotFoundError):
            read_task("nonexistent-id", base_dir=workspace)


class TestUpdateTask:
    def test_update_status_valid(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, status="In Progress", base_dir=workspace)
        assert updated.status == "In Progress"

    def test_update_status_invalid(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        with pytest.raises(ValueError, match="Invalid state transition"):
            update_task(task.id, status="Done", base_dir=workspace)

    def test_update_description(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, description="New desc", base_dir=workspace)
        assert updated.description == "New desc"

    def test_update_tags(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, tags=["new", "tags"], base_dir=workspace)
        assert updated.tags == ["new", "tags"]

    def test_update_adds_audit_entry(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, status="In Progress", base_dir=workspace)
        assert len(updated.audit) == 2  # created + status_change
        assert updated.audit[1].type == "status_change"

    def test_update_same_status_no_change(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, status="To Do", base_dir=workspace)
        assert len(updated.audit) == 1  # only original created

    def test_multi_step_transitions(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        task = update_task(task.id, status="In Progress", base_dir=workspace)
        task = update_task(task.id, status="Done", base_dir=workspace)
        assert task.status == "Done"
        assert len(task.audit) == 3


class TestListTasks:
    def test_list_empty(self, workspace, ws):
        tasks = list_tasks(ws.id, base_dir=workspace)
        assert tasks == []

    def test_list_all(self, workspace, ws):
        create_task(ws.id, title="T1", base_dir=workspace)
        create_task(ws.id, title="T2", base_dir=workspace)
        tasks = list_tasks(ws.id, base_dir=workspace)
        assert len(tasks) == 2

    def test_list_filter_by_status(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        create_task(ws.id, title="T2", base_dir=workspace)
        update_task(t1.id, status="In Progress", base_dir=workspace)
        tasks = list_tasks(ws.id, status="In Progress", base_dir=workspace)
        assert len(tasks) == 1
        assert tasks[0].title == "T1"

    def test_list_filter_by_tags(self, workspace, ws):
        create_task(ws.id, title="T1", tags=["urgent"], base_dir=workspace)
        create_task(ws.id, title="T2", tags=["low"], base_dir=workspace)
        tasks = list_tasks(ws.id, tags=["urgent"], base_dir=workspace)
        assert len(tasks) == 1
        assert tasks[0].title == "T1"


class TestCommentTask:
    def test_add_comment(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = comment_task(task.id, "This is a comment", base_dir=workspace)
        assert "This is a comment" in updated.comments
        assert len(updated.audit) == 2  # created + comment

    def test_multiple_comments(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        comment_task(task.id, "First", base_dir=workspace)
        updated = comment_task(task.id, "Second", base_dir=workspace)
        assert len(updated.comments) == 2


class TestArchiveTask:
    def test_archive(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        result = archive_task(task.id, base_dir=workspace)
        assert result["archived"] is True
        with pytest.raises(FileNotFoundError):
            read_task(task.id, base_dir=workspace)

    def test_archive_not_found(self, workspace):
        with pytest.raises(FileNotFoundError):
            archive_task("nonexistent-id", base_dir=workspace)


class TestGetAudit:
    def test_get_audit_trail(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        update_task(task.id, status="In Progress", base_dir=workspace)
        comment_task(task.id, "A comment", base_dir=workspace)
        audit = get_audit(task.id, base_dir=workspace)
        assert len(audit) == 3
        assert audit[0]["type"] == "created"
        assert audit[1]["type"] == "status_change"
        assert audit[2]["type"] == "comment"
