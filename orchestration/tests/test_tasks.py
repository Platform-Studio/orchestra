"""Tests for task operations."""

import os
from pathlib import Path

import pytest
from orchestration.workstreams import create_workstream, get_workstream_tags, list_workstreams, save_workstream
from orchestration.tasks import (
    add_task_error,
    clear_task_errors,
    create_task,
    read_task,
    read_task_from_workstream,
    update_task,
    list_tasks,
    list_board_tasks,
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
    reorder_tasks_in_workstream,
    pause_task,
    resume_task,
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

    def test_create_with_explicit_initial_status(self, workspace, ws):
        task = create_task(ws.id, title="T", initial_status="In Progress", base_dir=workspace)
        assert task.status == "In Progress"

    def test_create_with_invalid_initial_status_raises(self, workspace, ws):
        with pytest.raises(ValueError, match="Unknown task state"):
            create_task(ws.id, title="T", initial_status="Not A State", base_dir=workspace)


class TestReadTask:
    def test_read_existing(self, workspace, ws):
        task = create_task(ws.id, title="Read Me", base_dir=workspace)
        loaded = read_task(task.id, base_dir=workspace)
        assert loaded.title == "Read Me"
        assert loaded.id == task.id

    def test_read_preserves_token_usage(self, workspace, ws):
        task = create_task(ws.id, title="Tokened", base_dir=workspace)
        task.token_usage = {
            "pseudo_key": f"task-{task.id}",
            "input_tokens": 120,
            "output_tokens": 45,
            "request_count": 2,
            "updated_at": "2026-06-10T00:00:00+00:00",
        }
        from orchestration.tasks import _save_task
        _save_task(task, base_dir=workspace)

        loaded = read_task(task.id, base_dir=workspace)
        assert loaded.token_usage["input_tokens"] == 120
        assert loaded.token_usage["output_tokens"] == 45

    def test_read_preserves_task_errors(self, workspace, ws):
        task = create_task(ws.id, title="Errored", base_dir=workspace)

        add_task_error(
            task.id,
            message="Invalid image attachment",
            error_type="preflight",
            source="Coder",
            base_dir=workspace,
        )

        loaded = read_task(task.id, base_dir=workspace)
        assert loaded.task_errors[0]["type"] == "preflight"
        assert loaded.task_errors[0]["message"] == "Invalid image attachment"
        assert loaded.task_errors[0]["source"] == "Coder"

        clear_task_errors(task.id, base_dir=workspace)
        cleared = read_task(task.id, base_dir=workspace)
        assert cleared.task_errors[0]["cleared_at"]

    def test_read_task_uses_workspace_index_not_per_task_root_resolution(self, workspace, tmp_path, monkeypatch):
        working_root = tmp_path / "career_pivot_repo"
        working_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Sales", parent_id=parent.id, base_dir=workspace)
        task = create_task(child.id, title="Mounted task", base_dir=workspace)

        def _boom(*args, **kwargs):
            raise AssertionError("should use workspace index")

        monkeypatch.setattr("orchestration.tasks.resolve_workstream_state_root", _boom)

        loaded = read_task(task.id, base_dir=workspace)
        assert loaded.id == task.id

    def test_read_not_found(self, workspace):
        with pytest.raises(FileNotFoundError):
            read_task("nonexistent-id", base_dir=workspace)

    def test_read_from_workstream(self, workspace, ws):
        task = create_task(ws.id, title="Direct Read", base_dir=workspace)
        loaded = read_task_from_workstream(ws.id, task.id, base_dir=workspace)
        assert loaded.id == task.id
        assert loaded.title == "Direct Read"


class TestUpdateTask:
    def test_update_title(self, workspace, ws):
        task = create_task(ws.id, title="Original", base_dir=workspace)
        updated = update_task(task.id, title="  Renamed task  ", base_dir=workspace)
        assert updated.title == "Renamed task"
        assert any(a.description == "Title updated" for a in updated.audit)

    def test_update_title_rejects_empty_value(self, workspace, ws):
        task = create_task(ws.id, title="Original", base_dir=workspace)
        with pytest.raises(ValueError, match="Task title cannot be empty"):
            update_task(task.id, title="   ", base_dir=workspace)
        assert read_task(task.id, base_dir=workspace).title == "Original"

    def test_update_status_valid(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        updated = update_task(task.id, status="In Progress", base_dir=workspace)
        assert updated.status == "In Progress"

    def test_update_status_uses_summary_rank_scan(self, workspace, ws, monkeypatch):
        existing = create_task(ws.id, title="Existing", base_dir=workspace)
        update_task(existing.id, status="In Progress", base_dir=workspace)

        pending = create_task(ws.id, title="Pending", base_dir=workspace)

        from orchestration import tasks as tasks_mod

        def _boom(*args, **kwargs):
            raise AssertionError("full task scan should not be used for rank lookup")

        monkeypatch.setattr(tasks_mod, "list_tasks", _boom)

        updated = update_task(pending.id, status="In Progress", base_dir=workspace)
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

    def test_list_tasks_for_workstream_uses_cached_workspace_root_for_working_directory_child(self, workspace, tmp_path, monkeypatch):
        working_root = tmp_path / "career_pivot_repo"
        working_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Sales", parent_id=parent.id, base_dir=workspace)
        task = create_task(child.id, title="Mounted task", base_dir=workspace)

        loaded_child = next(ws for ws in list_workstreams(base_dir=workspace) if ws.id == child.id)

        def _boom(*args, **kwargs):
            raise AssertionError("should use cached workspace root")

        monkeypatch.setattr("orchestration.tasks.resolve_workstream_state_root", _boom)

        tasks = list_tasks_for_workstream(loaded_child, base_dir=workspace)
        assert [t.id for t in tasks] == [task.id]

    def test_list_board_tasks_skips_heavy_fields_but_preserves_board_metadata(self, workspace, ws):
        task = create_task(ws.id, title="Board task", base_dir=workspace)
        task.description = "Long description"
        task.comments = [{"message": "hello\nworld\nwith wrapped content"}]
        task.attachments = ["foo/bar.md"]
        task.token_usage = {"input_tokens": 123, "output_tokens": 45}
        task.retry_count = 2
        task.last_failure_at = "2026-06-10T00:00:00+00:00"
        task.paused = True
        task.add_audit("updated", "Added details")

        from orchestration.tasks import _save_task

        _save_task(task, base_dir=workspace)

        loaded = list_board_tasks(ws.id, base_dir=workspace)

        assert len(loaded) == 1
        board_task = loaded[0]
        assert board_task.title == "Board task"
        assert board_task.description is None
        assert board_task.comments == []
        assert board_task.audit == []
        assert board_task.attachments == []
        assert board_task.token_usage == {"input_tokens": 123, "output_tokens": 45}
        assert board_task.retry_count == 2
        assert board_task.last_failure_at == "2026-06-10T00:00:00+00:00"
        assert board_task.paused is True

    def test_list_board_tasks_handles_wrapped_quoted_title(self, workspace, ws):
        task = create_task(ws.id, title="placeholder", base_dir=workspace)

        from orchestration.tasks import _task_path

        title = 'Setup stepper shows "5First Meeting Type" — number badge and label run together with no space'
        wrapped_title = (
            'title: "Setup stepper shows \\\"5First Meeting Type\\\" \\u2014 number badge and label' + '\\'
        )
        task_path = Path(_task_path(workspace, ws.id, task.id))
        task_path.write_text(
            "\n".join(
                [
                    f"id: {task.id}",
                    f"workstream_id: {ws.id}",
                    wrapped_title,
                    '  \\ run together with no space"',
                    "status: To Do",
                    "tags: []",
                    "comments: []",
                    "audit: []",
                    "attachments: []",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        loaded = list_board_tasks(ws.id, base_dir=workspace)
        board_task = next(item for item in loaded if item.id == task.id)

        assert board_task.status == "To Do"
        assert board_task.title == title
        assert getattr(board_task, "_parse_error", None) is None


class TestTaskOrdering:
    def test_create_assigns_rank(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        assert t1.rank is not None
        assert t2.rank is not None
        assert float(t2.rank) < float(t1.rank)

    def test_new_tasks_are_added_to_top_of_state_list(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t3.id, t2.id, t1.id]

    def test_move_up_reorders_within_status(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        move_task_up(t1.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t3.id, t1.id, t2.id]

    def test_move_down_reorders_within_status(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        move_task_down(t3.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t2.id, t3.id, t1.id]

    def test_move_up_on_first_is_noop(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        moved = move_task_up(t2.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert moved.id == t2.id
        assert ordered == [t2.id, t1.id]

    def test_move_down_on_last_is_noop(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        moved = move_task_down(t1.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert moved.id == t1.id
        assert ordered == [t2.id, t1.id]

    def test_move_before_reorders_to_target_position(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)
        t4 = create_task(ws.id, title="T4", base_dir=workspace)

        move_task_before(t4.id, t2.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t3.id, t4.id, t2.id, t1.id]

    def test_move_after_reorders_to_target_position(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)
        t4 = create_task(ws.id, title="T4", base_dir=workspace)

        move_task_after(t1.id, t3.id, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t4.id, t3.id, t1.id, t2.id]

    def test_move_to_index_reorders_directly(self, workspace, ws):
        t1 = create_task(ws.id, title="T1", base_dir=workspace)
        t2 = create_task(ws.id, title="T2", base_dir=workspace)
        t3 = create_task(ws.id, title="T3", base_dir=workspace)

        move_task_to_index(t1.id, 0, base_dir=workspace)
        ordered = [t.id for t in list_tasks(ws.id, status="To Do", base_dir=workspace)]
        assert ordered == [t1.id, t3.id, t2.id]

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

    def test_attach_invalid_image_rejects_with_clear_error(self, workspace, ws):
        create_artifact("assets/logo.png", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)

        with pytest.raises(ValueError, match="Invalid image attachment 'assets/logo.png':"):
            attach_to_task(task.id, "assets/logo.png", base_dir=workspace)

    def test_create_task_rejects_invalid_image_attachment(self, workspace, ws):
        create_artifact("assets/logo.png", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", base_dir=workspace)

        with pytest.raises(ValueError, match="Invalid image attachment 'assets/logo.png':"):
            create_task(ws.id, title="T", attachments=["assets/logo.png"], base_dir=workspace)

    def test_update_task_rejects_invalid_image_attachment(self, workspace, ws):
        create_artifact("assets/logo.png", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB", base_dir=workspace)
        task = create_task(ws.id, title="T", base_dir=workspace)

        with pytest.raises(ValueError, match="Invalid image attachment 'assets/logo.png':"):
            update_task(task.id, attachments=["assets/logo.png"], base_dir=workspace)

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
        working_root = tmp_path / "stashmap_repo"
        working_root.mkdir()

        parent = create_workstream(name="StashMap", base_dir=workspace)
        parent.working_directory = str(working_root)
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
        assert os.path.exists(os.path.join(workspace, "artifacts", artifact_path))

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


class TestPauseTask:
    def test_default_task_is_not_paused(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        assert task.paused is False
        # `paused` should not be persisted in the YAML when falsy.
        assert "paused" not in task.to_dict()

    def test_pause_and_resume_round_trip(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        paused = pause_task(task.id, base_dir=workspace)
        assert paused.paused is True

        reread = read_task(task.id, base_dir=workspace)
        assert reread.paused is True
        assert reread.to_dict()["paused"] is True
        assert any(a.type == "task_paused" for a in reread.audit)

        resumed = resume_task(task.id, base_dir=workspace)
        assert resumed.paused is False
        reread2 = read_task(task.id, base_dir=workspace)
        assert reread2.paused is False
        assert any(a.type == "task_resumed" for a in reread2.audit)

    def test_pause_when_already_paused_is_idempotent(self, workspace, ws):
        task = create_task(ws.id, title="T", base_dir=workspace)
        pause_task(task.id, base_dir=workspace)
        before = read_task(task.id, base_dir=workspace)
        before_audit_len = len(before.audit)

        pause_task(task.id, base_dir=workspace)
        after = read_task(task.id, base_dir=workspace)
        assert after.paused is True
        assert len(after.audit) == before_audit_len


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

    def test_move_cross_workstream_adds_tags_to_target_catalog(self, workspace, ws, ws2):
        task = create_task(ws.id, title="T", tags=["P1", "Ready"], base_dir=workspace)

        move_task(task.id, target_workstream_id=ws2.id, base_dir=workspace)

        assert {tag["name"] for tag in get_workstream_tags(ws2.id, base_dir=workspace)} == {"P1", "Ready"}


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

    def test_duplicate_cross_workstream_adds_tags_to_target_catalog(self, workspace, ws, ws2):
        task = create_task(ws.id, title="Cross Dup", tags=["P1", "Ready"], base_dir=workspace)

        duplicate_task(task.id, target_workstream_id=ws2.id, base_dir=workspace)

        assert {tag["name"] for tag in get_workstream_tags(ws2.id, base_dir=workspace)} == {"P1", "Ready"}

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


class TestReorderTasksInWorkstream:
    @pytest.fixture
    def prod_ws(self, workspace):
        states = {
            "Backlog": ["On Deck"],
            "On Deck": ["In Progress"],
            "In Progress": ["Done"],
            "Done": [],
        }
        return create_workstream(name="Product Dev", task_states=states, base_dir=workspace)

    def test_return_dict_structure(self, workspace, prod_ws):
        result = reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        assert result["workstream_id"] == prod_ws.id
        assert result["workstream_name"] == "Product Dev"
        assert "total_tasks" in result
        assert "columns_checked" in result
        assert "columns_reordered" in result
        assert "reordered_columns" in result
        assert "total_reordered" in result

    def test_empty_workstream(self, workspace, prod_ws):
        result = reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        assert result["total_tasks"] == 0
        assert result["columns_reordered"] == 0
        assert result["total_reordered"] == 0

    def test_already_correct_order_no_reorder(self, workspace, prod_ws):
        # create_task prepends (lower rank), so create P1 first then P0
        # — P0 ends up with a lower rank and thus appears first in list_tasks
        create_task(prod_ws.id, title="User Story #2", tags=["P1"], base_dir=workspace)
        create_task(prod_ws.id, title="User Story #1", tags=["P0"], base_dir=workspace)
        result = reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        assert result["columns_reordered"] == 0
        assert result["total_reordered"] == 0

    def test_priority_order_p0_before_p1_before_p2(self, workspace, prod_ws):
        # create_task prepends; create P0 first so it ends up with the highest rank
        # and thus appears last — the column is in wrong (P2-first) order initially
        t_p0 = create_task(prod_ws.id, title="Card #3", tags=["P0"], base_dir=workspace)
        t_p1 = create_task(prod_ws.id, title="Card #2", tags=["P1"], base_dir=workspace)
        t_p2 = create_task(prod_ws.id, title="Card #1", tags=["P2"], base_dir=workspace)
        result = reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        tasks = list_tasks(prod_ws.id, status="Backlog", base_dir=workspace)
        assert tasks[0].id == t_p0.id
        assert tasks[1].id == t_p1.id
        assert tasks[2].id == t_p2.id
        assert result["columns_reordered"] == 1

    def test_lower_card_number_first_within_same_priority(self, workspace, prod_ws):
        # Card #3 inserted before card #1 — after reorder card #1 must come first
        t3 = create_task(prod_ws.id, title="User Story #3", tags=["P1"], base_dir=workspace)
        t1 = create_task(prod_ws.id, title="User Story #1", tags=["P1"], base_dir=workspace)
        reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        tasks = list_tasks(prod_ws.id, status="Backlog", base_dir=workspace)
        assert tasks[0].id == t1.id
        assert tasks[1].id == t3.id

    def test_card_number_extraction_various_patterns(self, workspace, prod_ws):
        # Titles using "User Story #N", "Card #N", and "#N prefix" formats
        t5 = create_task(prod_ws.id, title="User Story #5 Login flow", tags=["P1"], base_dir=workspace)
        t2 = create_task(prod_ws.id, title="Card #2 Dashboard", tags=["P1"], base_dir=workspace)
        t1 = create_task(prod_ws.id, title="#1 Homepage", tags=["P1"], base_dir=workspace)
        reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        tasks = list_tasks(prod_ws.id, status="Backlog", base_dir=workspace)
        assert tasks[0].id == t1.id  # card 1
        assert tasks[1].id == t2.id  # card 2
        assert tasks[2].id == t5.id  # card 5

    def test_us_dash_pattern_extracted(self, workspace, prod_ws):
        # "US-N" format should resolve card number N
        t5 = create_task(prod_ws.id, title="US-5 Profile page", tags=["P0"], base_dir=workspace)
        t1 = create_task(prod_ws.id, title="US-1 Auth flow", tags=["P0"], base_dir=workspace)
        reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        tasks = list_tasks(prod_ws.id, status="Backlog", base_dir=workspace)
        assert tasks[0].id == t1.id
        assert tasks[1].id == t5.id

    def test_no_priority_tag_ranks_below_p2(self, workspace, prod_ws):
        # An untagged task should end up after a P2 task regardless of card number
        t_none = create_task(prod_ws.id, title="Card #1 No priority", base_dir=workspace)
        t_p2 = create_task(prod_ws.id, title="Card #2 P2 task", tags=["P2"], base_dir=workspace)
        reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        tasks = list_tasks(prod_ws.id, status="Backlog", base_dir=workspace)
        assert tasks[0].id == t_p2.id
        assert tasks[1].id == t_none.id

    def test_multiple_columns_reordered_independently(self, workspace, prod_ws):
        # Backlog: create P0 first (higher rank = appears last) then P1 (lower rank = first)
        # — so the column initially shows [P1, P0] which is wrong order
        b_p0 = create_task(prod_ws.id, title="Card #1", tags=["P0"], base_dir=workspace)
        b_p1 = create_task(prod_ws.id, title="Card #2", tags=["P1"], base_dir=workspace)
        # On Deck: card #3 before card #1 (wrong card-number order)
        d_c3 = create_task(prod_ws.id, title="Card #3", tags=["P0"], base_dir=workspace)
        d_c1 = create_task(prod_ws.id, title="Card #1 on deck", tags=["P0"], base_dir=workspace)
        update_task(d_c3.id, status="On Deck", base_dir=workspace)
        update_task(d_c1.id, status="On Deck", base_dir=workspace)

        result = reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)

        assert result["columns_reordered"] == 2
        backlog = list_tasks(prod_ws.id, status="Backlog", base_dir=workspace)
        assert backlog[0].id == b_p0.id
        assert backlog[1].id == b_p1.id
        on_deck = list_tasks(prod_ws.id, status="On Deck", base_dir=workspace)
        assert on_deck[0].id == d_c1.id
        assert on_deck[1].id == d_c3.id

    def test_reorder_persists_to_disk(self, workspace, prod_ws):
        # Ranks written by reorder should survive a fresh list_tasks call
        t2 = create_task(prod_ws.id, title="Card #2", tags=["P0"], base_dir=workspace)
        t1 = create_task(prod_ws.id, title="Card #1", tags=["P0"], base_dir=workspace)
        reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        reloaded = list_tasks(prod_ws.id, status="Backlog", base_dir=workspace)
        assert reloaded[0].id == t1.id
        assert reloaded[1].id == t2.id

    def test_total_tasks_count(self, workspace, prod_ws):
        create_task(prod_ws.id, title="T1", base_dir=workspace)
        create_task(prod_ws.id, title="T2", base_dir=workspace)
        create_task(prod_ws.id, title="T3", base_dir=workspace)
        result = reorder_tasks_in_workstream(prod_ws.id, base_dir=workspace)
        assert result["total_tasks"] == 3


class TestAtomicSave:
    """Regression tests for crash-safe task file writes.

    These guard against the corruption pattern seen on 2026-06-10 where a laptop
    restart mid-write left a task YAML truncated (an unterminated quoted
    scalar in the last ``comments`` entry, which made the file unloadable
    until it was patched by hand). The fix is to write task YAMLs to a
    tempfile, ``fsync``, then ``os.replace`` into place — so a SIGKILL or
    power loss mid-write leaves the previous good copy intact.
    """

    def _task_path(self, workspace, ws_id, task_id):
        from orchestration.tasks import _task_path
        return _task_path(workspace, ws_id, task_id)

    def test_normal_save_writes_valid_yaml(self, workspace, ws):
        # Baseline: a normal save produces a parseable file on disk.
        from orchestration import tasks as tasks_mod
        from orchestration.workstreams import resolve_workstream_state_root

        task = create_task(ws.id, title="Baseline", base_dir=workspace)
        path = self._task_path(workspace, ws.id, task.id)
        assert os.path.exists(path)
        # File parses and round-trips
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert data["id"] == task.id
        assert data["title"] == "Baseline"

    def test_interrupted_write_leaves_prior_file_intact(self, workspace, ws, monkeypatch):
        # If yaml.dump raises mid-save, the *real* task file must remain the
        # old valid version. Under the old (non-atomic) implementation this
        # left a zero-byte or truncated file.
        task = create_task(ws.id, title="Original title", base_dir=workspace)
        path = self._task_path(workspace, ws.id, task.id)
        original_bytes = open(path, "rb").read()
        assert original_bytes  # non-empty

        # Make yaml.dump explode the moment _save_task tries to serialize
        # the updated task. This simulates a SIGKILL / OOM mid-write.
        from orchestration import tasks as tasks_mod
        import yaml as _yaml

        real_dump = _yaml.dump
        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash mid-dump")
        monkeypatch.setattr(tasks_mod.yaml, "dump", boom)

        with pytest.raises(RuntimeError, match="simulated crash mid-dump"):
            update_task(task.id, description="This update should not land", base_dir=workspace)

        # The on-disk file must still be the previous good version.
        after_bytes = open(path, "rb").read()
        assert after_bytes == original_bytes, (
            "Atomic write failed: file content changed despite the dump raising. "
            "A non-atomic implementation would have truncated or zeroed the file."
        )
        # And the task is still readable as the original.
        reloaded = read_task(task.id, base_dir=workspace)
        assert reloaded.title == "Original title"
        assert reloaded.description != "This update should not land"

    def test_interrupted_write_leaves_no_leftover_tempfile(self, workspace, ws, monkeypatch):
        # If the save fails, the .tmp file in the tasks directory must be
        # cleaned up so it doesn't accumulate cruft.
        task = create_task(ws.id, title="T", base_dir=workspace)
        tasks_dir = os.path.dirname(self._task_path(workspace, ws.id, task.id))

        from orchestration import tasks as tasks_mod
        import yaml as _yaml

        def boom(*args, **kwargs):
            raise RuntimeError("simulated crash")
        monkeypatch.setattr(tasks_mod.yaml, "dump", boom)

        with pytest.raises(RuntimeError):
            update_task(task.id, description="x", base_dir=workspace)

        leftovers = [
            name for name in os.listdir(tasks_dir)
            if name.startswith(".atomic.") and name.endswith(".tmp")
        ]
        assert leftovers == [], f"Tempfile leak after failed write: {leftovers}"

    def test_atomic_write_uses_os_replace(self, workspace, ws, monkeypatch):
        # Sanity-check the mechanism: after a successful save, no .tmp file
        # is left in the tasks directory (os.replace moved it into place).
        task = create_task(ws.id, title="T", base_dir=workspace)
        tasks_dir = os.path.dirname(self._task_path(workspace, ws.id, task.id))
        leftovers_before = [
            n for n in os.listdir(tasks_dir)
            if n.startswith(".atomic.") and n.endswith(".tmp")
        ]
        assert leftovers_before == []

        update_task(task.id, description="updated", base_dir=workspace)

        leftovers_after = [
            n for n in os.listdir(tasks_dir)
            if n.startswith(".atomic.") and n.endswith(".tmp")
        ]
        assert leftovers_after == [], (
            f"atomic_write_yaml did not move the tempfile into place: {leftovers_after}"
        )
