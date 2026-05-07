"""Tests for task operations."""

import pytest
from orchestration.workstreams import create_workstream, list_workstreams, save_workstream
from orchestration.tasks import (
    create_task,
    read_task,
    update_task,
    list_tasks,
    list_tasks_for_workstream,
    move_task_up,
    move_task_down,
    move_task_before,
    move_task_after,
    move_task_to_index,
    comment_task,
    delete_task_comment,
    edit_task_comment,
    archive_task,
    get_audit,
    move_task,
    duplicate_task,
    attach_to_task,
    detach_from_task,
)
from orchestration.artifacts import create_artifact, copy_artifact_tree
from orchestration.agents import _read_task_attachments_for_prompt


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

    def test_create_with_attachments(self, workspace, ws):
        task = create_task(
            ws.id,
            title="T",
            attachments=["Stage 2 Research/brief.md", "/Stage 2 Research/brief.md"],
            base_dir=workspace,
        )
        assert task.attachments == ["Stage 2 Research/brief.md"]

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

    def test_update_status_invalid_with_force(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, status="Done", force=True, base_dir=workspace)
        assert updated.status == "Done"
        assert any("forcibly changed" in a.description for a in updated.audit)

    def test_update_description(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, description="New desc", base_dir=workspace)
        assert updated.description == "New desc"

    def test_update_tags(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, tags=["new", "tags"], base_dir=workspace)
        assert updated.tags == ["new", "tags"]

    def test_update_attachments(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(
            task.id,
            attachments=["Theses/one.md", "Theses/two.md"],
            base_dir=workspace,
        )
        assert updated.attachments == ["Theses/one.md", "Theses/two.md"]

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

    def test_list_tasks_for_workstream_uses_cached_workspace_root_for_mounted_child(self, workspace, tmp_path, monkeypatch):
        mount_root = tmp_path / "career_pivot_repo"
        mount_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.mounted_workspace_path = str(mount_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Sales", parent_id=parent.id, base_dir=workspace)
        task = create_task(child.id, title="Mounted task", base_dir=workspace)

        loaded_child = next(ws for ws in list_workstreams(base_dir=workspace) if ws.id == child.id)

        def _boom(*args, **kwargs):
            raise AssertionError("should use cached workspace root")

        monkeypatch.setattr("orchestration.tasks.resolve_workstream_workspace", _boom)

        tasks = list_tasks_for_workstream(loaded_child, base_dir=workspace)
        assert [t.id for t in tasks] == [task.id]


class TestTaskOrdering:
    def test_create_assigns_rank(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        assert t1.rank is not None
        assert t2.rank is not None
        assert float(t2.rank) > float(t1.rank)

    def test_move_up_reorders_within_status(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        move_task_up(t3.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t1.id, t3.id, t2.id]

    def test_move_down_reorders_within_status(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        move_task_down(t1.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t2.id, t1.id, t3.id]

    def test_move_up_on_first_is_noop(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        moved = move_task_up(t1.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert moved.id == t1.id
        assert ordered == [t1.id, t2.id]

    def test_move_down_on_last_is_noop(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        moved = move_task_down(t2.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert moved.id == t2.id
        assert ordered == [t1.id, t2.id]

    def test_move_before_reorders_to_target_position(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)
        t4 = create_task(ws.id, title="T4", base_dir=workspace)

        move_task_before(t4.id, t2.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t1.id, t4.id, t2.id, t3.id]

    def test_move_after_reorders_to_target_position(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)
        t4 = create_task(ws.id, title="T4", base_dir=workspace)

        move_task_after(t1.id, t3.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t2.id, t3.id, t1.id, t4.id]

    def test_move_to_index_reorders_directly(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        move_task_to_index(t3.id, 0, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t3.id, t1.id, t2.id]

    def test_move_before_requires_same_status(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        update_task(t2.id, status="In Progress", base_dir=workspace)

        with pytest.raises(ValueError, match="same status"):
            move_task_before(t1.id, t2.id, base_dir=workspace)


class TestCommentTask:
    def test_add_comment(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = comment_task(task.id, "This is a comment", base_dir=workspace)
        assert updated.comments[0]["message"] == "This is a comment"
        assert "timestamp" in updated.comments[0]
        assert len(updated.audit) == 2  # created + comment

    def test_multiple_comments(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        comment_task(task.id, "First", base_dir=workspace)
        updated = comment_task(task.id, "Second", base_dir=workspace)
        assert len(updated.comments) == 2
        assert updated.comments[0]["message"] == "First"
        assert updated.comments[1]["message"] == "Second"

    def test_comment_with_explicit_author(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = comment_task(task.id, "Hello", author="SDR Agent", base_dir=workspace)
        assert updated.comments[0]["author"] == "SDR Agent"

    def test_comment_strips_date_prefix(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = comment_task(task.id, "[2026-04-21] Followed up", base_dir=workspace)
        assert updated.comments[0]["message"] == "Followed up"
        assert "[2026-04-21]" not in updated.audit[-1].description

    def test_comment_uses_env_agent_name_when_author_missing(self, workspace, ws, monkeypatch):
        monkeypatch.setenv("ORCHESTRATION_AGENT_NAME", "LinkedIn SDR")
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = comment_task(task.id, "Hello", base_dir=workspace)
        assert updated.comments[0]["author"] == "LinkedIn SDR"

    def test_comment_uses_shell_user_as_author_fallback(self, workspace, ws, monkeypatch):
        monkeypatch.delenv("ORCHESTRATION_AGENT_NAME", raising=False)
        monkeypatch.delenv("AGENT_NAME", raising=False)
        monkeypatch.delenv("CLAUDE_AGENT_NAME", raising=False)
        monkeypatch.setenv("USER", "jeremy")
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = comment_task(task.id, "Hello", base_dir=workspace)
        assert updated.comments[0]["author"] == "jeremy"

    def test_delete_comment_by_index(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        comment_task(task.id, "First", base_dir=workspace)
        comment_task(task.id, "Second", base_dir=workspace)

        updated = delete_task_comment(task.id, 0, base_dir=workspace)
        assert len(updated.comments) == 1
        assert updated.comments[0]["message"] == "Second"
        assert updated.audit[-1].type == "comment_deleted"

    def test_delete_comment_out_of_range(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        comment_task(task.id, "Only", base_dir=workspace)
        with pytest.raises(IndexError):
            delete_task_comment(task.id, 1, base_dir=workspace)

    def test_edit_comment_by_index(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = comment_task(task.id, "Original", author="Alice", base_dir=workspace)

        edited = edit_task_comment(task.id, 0, "Edited", base_dir=workspace)
        assert len(edited.comments) == 1
        assert edited.comments[0]["message"] == "Edited"
        assert edited.comments[0]["author"] == "Alice"
        assert "edited_at" in edited.comments[0]
        assert edited.audit[-1].type == "comment_edited"

    def test_edit_comment_with_explicit_author(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        comment_task(task.id, "Original", author="Alice", base_dir=workspace)

        edited = edit_task_comment(task.id, 0, "Edited", author="Bob", base_dir=workspace)
        assert edited.comments[0]["author"] == "Bob"

    def test_edit_comment_out_of_range(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        comment_task(task.id, "Only", base_dir=workspace)

        with pytest.raises(IndexError):
            edit_task_comment(task.id, 2, "Edited", base_dir=workspace)


class TestTaskAttachments:
    def test_attach_to_task(self, workspace, ws):
        create_artifact("Theses/proptech.md", "# Proptech", base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = attach_to_task(task.id, "Theses/proptech.md", base_dir=workspace)
        assert updated.attachments == ["Theses/proptech.md"]

    def test_attach_is_deduplicated(self, workspace, ws):
        create_artifact("Theses/proptech.md", "# Proptech", base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)
        attach_to_task(task.id, "Theses/proptech.md", base_dir=workspace)
        updated = attach_to_task(task.id, "/Theses/proptech.md", base_dir=workspace)
        assert updated.attachments == ["Theses/proptech.md"]

    def test_attach_missing_artifact_raises(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        with pytest.raises(FileNotFoundError):
            attach_to_task(task.id, "Theses/missing.md", base_dir=workspace)

    def test_attach_to_child_task_after_parent_artifact_copytree(self, workspace, tmp_path):
        mount_root = tmp_path / "stashmap_repo"
        mount_root.mkdir()

        parent = create_workstream(name="StashMap", base_dir=workspace)
        parent.mounted_workspace_path = str(mount_root)
        save_workstream(parent, base_dir=workspace)
        child = create_workstream(name="Product Development", parent_id=parent.id, base_dir=workspace)

        artifact_path = "Stage 2 Research/example/architecture.md"
        create_artifact(artifact_path, "# Architecture", base_dir=workspace)
        copy_artifact_tree(
            "Stage 2 Research/example",
            base_dir=workspace,
            workstream_id=parent.id,
            source_base_dir=workspace,
        )

        task = create_task(child.id, title="Setup skeleton", base_dir=workspace)
        updated = attach_to_task(task.id, artifact_path, base_dir=workspace)

        assert updated.attachments == [artifact_path]
        assert (mount_root / "artifacts" / artifact_path).read_text() == "# Architecture"

    def test_detach_from_task(self, workspace, ws):
        task = create_task(ws.id, title="T", attachments=["Theses/a.md"], base_dir=workspace)
        updated = detach_from_task(task.id, "Theses/a.md", base_dir=workspace)
        assert updated.attachments == []

    def test_attachment_prompt_includes_artifact_content(self, workspace, ws):
        create_artifact("Theses/future.md", "# Future\nAI-first workflows", base_dir=workspace)
        task = create_task(ws.id, title="T", attachments=["Theses/future.md"], base_dir=workspace)
        section = _read_task_attachments_for_prompt(task, workspace)
        assert "Task attachments" in section
        assert "Path: Theses/future.md" in section
        assert "AI-first workflows" in section

    def test_attachment_prompt_handles_missing_artifact(self, workspace, ws):
        task = create_task(ws.id, title="T", attachments=["missing.md"], base_dir=workspace)
        section = _read_task_attachments_for_prompt(task, workspace)
        assert "Content unavailable" in section


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


class TestMoveTask:
    @pytest.fixture
    def ws2(self, workspace):
        states = {"Backlog": ["Active"], "Active": ["Closed"], "Closed": []}
        return create_workstream(name="Target WS", task_states=states, base_dir=workspace)

    def test_move_cross_workstream(self, workspace, ws, ws2):
        task = create_task(ws.id, title="Move Me", base_dir=workspace)
        moved = move_task(task.id, target_workstream_id=ws2.id, base_dir=workspace)
        assert moved.workstream_id == ws2.id
        assert moved.status == "Backlog"  # initial state of target
        # task should now exist in target workstream
        reloaded = read_task(task.id, base_dir=workspace)
        assert reloaded.workstream_id == ws2.id
        # audit trail should contain move entry
        assert any(a.type == "moved" for a in reloaded.audit)

    def test_move_cross_workstream_to_specific_state(self, workspace, ws, ws2):
        task = create_task(ws.id, title="T", base_dir=workspace)
        moved = move_task(task.id, target_workstream_id=ws2.id, target_status="Active", base_dir=workspace)
        assert moved.status == "Active"

    def test_move_same_workstream(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        moved = move_task(task.id, target_workstream_id=ws.id, target_status="In Progress", base_dir=workspace)
        assert moved.status == "In Progress"
        assert moved.workstream_id == ws.id
        assert any(a.type == "status_change" for a in moved.audit)

    def test_move_invalid_state_raises(self, workspace, ws, ws2):
        task = create_task(ws.id, title="T", base_dir=workspace)
        with pytest.raises(ValueError, match="does not exist"):
            move_task(task.id, target_workstream_id=ws2.id, target_status="NonExistent", base_dir=workspace)

    def test_move_removes_source_file(self, workspace, ws, ws2):
        import os
        task = create_task(ws.id, title="T", base_dir=workspace)
        src_path = os.path.join(workspace, "workstreams", ws.id, "tasks", task.id + ".yaml")
        assert os.path.exists(src_path)
        move_task(task.id, target_workstream_id=ws2.id, base_dir=workspace)
        assert not os.path.exists(src_path)


class TestDuplicateTask:
    @pytest.fixture
    def ws2(self, workspace):
        states = {"Backlog": ["Active"], "Active": ["Closed"], "Closed": []}
        return create_workstream(name="Dup Target WS", task_states=states, base_dir=workspace)

    def test_duplicate_same_workstream(self, workspace, ws):
        task = create_task(ws.id, title="Original", description="Desc", tags=["a"], base_dir=workspace)
        comment_task(task.id, "Hello", base_dir=workspace)
        dup = duplicate_task(task.id, base_dir=workspace)
        assert dup.id != task.id
        assert dup.title == task.title
        assert dup.description == task.description
        assert dup.tags == task.tags
        assert len(dup.comments) == 1
        assert dup.workstream_id == task.workstream_id
        # audit should be fresh — only the "created" duplicate entry
        assert len(dup.audit) == 1
        assert dup.audit[0].type == "created"
        assert task.id in dup.audit[0].description

    def test_duplicate_cross_workstream(self, workspace, ws, ws2):
        task = create_task(ws.id, title="Cross Dup", base_dir=workspace)
        dup = duplicate_task(task.id, target_workstream_id=ws2.id, base_dir=workspace)
        assert dup.workstream_id == ws2.id
        assert dup.status == "Backlog"

    def test_duplicate_to_specific_state(self, workspace, ws, ws2):
        task = create_task(ws.id, title="T", base_dir=workspace)
        dup = duplicate_task(task.id, target_workstream_id=ws2.id, target_status="Active", base_dir=workspace)
        assert dup.status == "Active"

    def test_duplicate_invalid_state_raises(self, workspace, ws, ws2):
        task = create_task(ws.id, title="T", base_dir=workspace)
        with pytest.raises(ValueError, match="does not exist"):
            duplicate_task(task.id, target_workstream_id=ws2.id, target_status="BadState", base_dir=workspace)

    def test_duplicate_original_unmodified(self, workspace, ws):
        task = create_task(ws.id, title="Original", base_dir=workspace)
        duplicate_task(task.id, base_dir=workspace)
        original = read_task(task.id, base_dir=workspace)
        assert original.title == "Original"
        assert len(original.audit) == 1  # only the original "created" entry

    def test_duplicate_copies_attachments(self, workspace, ws):
        task = create_task(ws.id, title="T", attachments=["Theses/x.md"], base_dir=workspace)
        dup = duplicate_task(task.id, base_dir=workspace)
        assert dup.attachments == ["Theses/x.md"]
