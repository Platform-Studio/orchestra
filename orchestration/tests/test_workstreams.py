"""Tests for workstream operations."""

import json
import pytest
import os
import shutil
import subprocess
import sys
from orchestration.workstreams import (
    create_workstream,
    ensure_workstream_tags,
    list_workstreams,
    read_workstream,
    read_workstream_context,
    find_workstreams,
    get_workstream_tags,
    rebuild_workstream_tag_catalog,
    save_workstream,
    resolve_workstream_workspace,
    set_workstream_env_key,
    set_workstream_context,
    upsert_workstream_tag,
    unset_workstream_env_key,
    resolve_workstream_env_key,
    resolve_env_key,
    list_workstream_hierarchy_env,
    list_effective_workstream_env,
    resolve_workstream_artifact_root,
    resolve_workstream_child_state_root,
    resolve_workstream_state_root,
)
from orchestration.tasks import create_task, update_task


class TestCreateWorkstream:
    def test_create_basic(self, workspace):
        ws = create_workstream(name="Test WS", base_dir=workspace)
        assert ws.name == "Test WS"
        assert ws.id is not None
        assert ws.description is None
        assert "pending" in ws.task_states

    def test_create_with_description(self, workspace):
        ws = create_workstream(
            name="SDR Outreach",
            description="Manage leads",
            base_dir=workspace,
        )
        assert ws.description == "Manage leads"

    def test_create_with_context(self, workspace):
        ws = create_workstream(
            name="Programmatic SEO",
            context="This week test variant B messaging.",
            base_dir=workspace,
        )
        assert ws.context == "This week test variant B messaging."

    def test_create_with_custom_states(self, workspace):
        states = {
            "To Do": ["In Progress"],
            "In Progress": ["Done", "Failed"],
            "Done": [],
            "Failed": ["To Do"],
        }
        ws = create_workstream(name="Custom", task_states=states, base_dir=workspace)
        assert ws.task_states == states
        assert ws.initial_status() == "To Do"

    def test_create_with_retry(self, workspace):
        retry = {"max_retries": 5, "backoff": "linear", "base_seconds": 30}
        ws = create_workstream(name="Retry WS", retry=retry, base_dir=workspace)
        assert ws.retry.max_retries == 5
        assert ws.retry.backoff == "linear"
        assert ws.retry.base_seconds == 30

    def test_create_with_parent(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)
        assert child.parent_id == parent.id

    def test_creates_directories(self, workspace):
        import os
        ws = create_workstream(name="Dir Test", base_dir=workspace)
        assert os.path.exists(os.path.join(workspace, "workstreams", f"{ws.id}.yaml"))
        assert os.path.isdir(os.path.join(workspace, "workstreams", ws.id, "tasks"))

    def test_create_with_explicit_routing_fields(self, workspace, tmp_path):
        working_root = tmp_path / "code_root"
        artifact_root = tmp_path / "artifact_root"
        child_root = tmp_path / "child_root"
        for path in (working_root, artifact_root, child_root):
            path.mkdir()

        ws = create_workstream(
            name="Routed",
            working_directory=str(working_root),
            artifact_root=str(artifact_root),
            child_workstream_root=str(child_root),
            base_dir=workspace,
        )

        loaded = read_workstream(ws.id, base_dir=workspace)
        assert loaded.working_directory == str(working_root)
        assert loaded.artifact_root == str(artifact_root)
        assert loaded.child_workstream_root == str(child_root)


class TestListWorkstreams:
    def test_list_empty(self, workspace):
        result = list_workstreams(base_dir=workspace)
        assert result == []

    def test_list_multiple(self, workspace):
        create_workstream(name="WS 1", base_dir=workspace)
        create_workstream(name="WS 2", base_dir=workspace)
        result = list_workstreams(base_dir=workspace)
        assert len(result) == 2

    def test_list_reflects_external_yaml_changes_after_cache_hit(self, workspace):
        create_workstream(name="WS 1", base_dir=workspace)
        assert len(list_workstreams(base_dir=workspace)) == 1

        manual_id = "manual-ws"
        with open(os.path.join(workspace, "workstreams", f"{manual_id}.yaml"), "w") as f:
            f.write("id: manual-ws\nname: Manual WS\n")

        result = list_workstreams(base_dir=workspace)
        assert len(result) == 2
        assert any(ws.id == manual_id for ws in result)


class TestReadWorkstream:
    def test_read_existing(self, workspace):
        ws = create_workstream(name="Read Test", base_dir=workspace)
        loaded = read_workstream(ws.id, base_dir=workspace)
        assert loaded.name == "Read Test"
        assert loaded.id == ws.id

    def test_read_not_found(self, workspace):
        with pytest.raises(FileNotFoundError):
            read_workstream("nonexistent-id", base_dir=workspace)

    def test_read_context_view(self, workspace):
        ws = create_workstream(name="Read Context", context="brief", base_dir=workspace)
        data = read_workstream_context(ws.id, base_dir=workspace)
        assert data["context"] == "brief"

    def test_save_does_not_persist_transient_resolution_fields(self, workspace, tmp_path):
        mount_root = tmp_path / "mounted_repo"
        mount_root.mkdir()

        ws = create_workstream(name="Mounted", base_dir=workspace)
        ws.working_directory = str(mount_root)
        save_workstream(ws, base_dir=workspace)

        loaded = read_workstream(ws.id, base_dir=workspace)
        save_workstream(loaded, base_dir=workspace)

        persisted = os.path.join(workspace, "workstreams", f"{ws.id}.yaml")
        text = open(persisted, encoding="utf-8").read()
        assert "resolved_workspace_path:" not in text
        assert "resolved_artifact_root:" not in text
        assert "resolved_child_workstream_root:" not in text
        assert "mount_available:" not in text


class TestWorkstreamContext:
    def test_set_context_persists_and_audits(self, workspace):
        from orchestration.workspace_audit import get_audit_log

        ws = create_workstream(name="Sales Funnel", base_dir=workspace)
        updated = set_workstream_context(ws.id, "Try CFO messaging this week", base_dir=workspace, updated_by="tester")

        assert updated.context == "Try CFO messaging this week"
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.context == "Try CFO messaging this week"

        audit = get_audit_log(base_dir=workspace, workstream_id=ws.id, event_type="workstream_context_updated")
        assert audit
        assert audit[0]["description"] == "Workstream context updated by tester"

    def test_empty_context_clears_field(self, workspace):
        ws = create_workstream(name="Sales Funnel", context="x", base_dir=workspace)
        updated = set_workstream_context(ws.id, "   ", base_dir=workspace)
        assert updated.context is None


class TestFindWorkstreams:
    def test_find_by_name(self, workspace):
        create_workstream(name="SDR Outreach", base_dir=workspace)
        create_workstream(name="Product Dev", base_dir=workspace)
        results = find_workstreams("sdr", base_dir=workspace)
        assert len(results) == 1
        assert results[0].name == "SDR Outreach"

    def test_find_by_description(self, workspace):
        create_workstream(name="WS1", description="sales pipeline", base_dir=workspace)
        results = find_workstreams("pipeline", base_dir=workspace)
        assert len(results) == 1

    def test_find_no_match(self, workspace):
        create_workstream(name="WS1", base_dir=workspace)
        results = find_workstreams("nonexistent", base_dir=workspace)
        assert len(results) == 0


class TestWorkstreamTags:
    def test_gettags_returns_catalog_and_task_tags_from_cached_definitions(self, workspace):
        ws = create_workstream(name="Tag WS", base_dir=workspace)
        upsert_workstream_tag(ws.id, "Urgent", "#eb5a46", base_dir=workspace)
        create_task(ws.id, title="T1", tags=["Urgent", "Needs Review"], base_dir=workspace)

        tags = get_workstream_tags(ws.id, base_dir=workspace)

        assert {tag["name"] for tag in tags} == {"Urgent", "Needs Review"}
        assert next(tag for tag in tags if tag["name"] == "Urgent")["color"] == "#eb5a46"
        assert all(tag["color"].startswith("#") for tag in tags)

    def test_gettags_does_not_scan_tasks(self, workspace, monkeypatch):
        ws = create_workstream(name="Tag WS", base_dir=workspace)
        upsert_workstream_tag(ws.id, "Urgent", "#eb5a46", base_dir=workspace)

        def fail_scan(*args, **kwargs):
            raise AssertionError("get_workstream_tags should not scan task YAML")

        monkeypatch.setattr("orchestration.tasks.list_tasks", fail_scan)

        assert get_workstream_tags(ws.id, base_dir=workspace) == [{"name": "Urgent", "color": "#eb5a46"}]

    def test_task_create_adds_tags_to_workstream_catalog(self, workspace):
        ws = create_workstream(name="Tag WS", base_dir=workspace)

        create_task(ws.id, title="T1", tags=["Needs Review", "P1"], base_dir=workspace)

        assert {tag["name"] for tag in get_workstream_tags(ws.id, base_dir=workspace)} == {"Needs Review", "P1"}

    def test_task_update_adds_tags_to_workstream_catalog(self, workspace):
        ws = create_workstream(name="Tag WS", base_dir=workspace)
        task = create_task(ws.id, title="T1", base_dir=workspace)

        update_task(task.id, tags=["Human Added"], base_dir=workspace)

        assert get_workstream_tags(ws.id, base_dir=workspace)[0]["name"] == "Human Added"

    def test_ensure_workstream_tags_dedupes_and_preserves_existing_color(self, workspace):
        ws = create_workstream(name="Tag WS", base_dir=workspace)
        upsert_workstream_tag(ws.id, "P1", "#eb5a46", base_dir=workspace)

        ensure_workstream_tags(ws.id, ["P1", "P2", "P2", "  "], base_dir=workspace)

        tags = get_workstream_tags(ws.id, base_dir=workspace)
        assert [tag["name"] for tag in tags] == ["P1", "P2"]
        assert tags[0]["color"] == "#eb5a46"

    def test_rebuild_workstream_tag_catalog_backfills_existing_task_tags(self, workspace):
        ws = create_workstream(name="Tag WS", base_dir=workspace)
        create_task(ws.id, title="T1", tags=["Legacy", "P1"], base_dir=workspace)
        loaded = read_workstream(ws.id, base_dir=workspace)
        loaded.tag_definitions = []
        save_workstream(loaded, base_dir=workspace)

        rebuilt = rebuild_workstream_tag_catalog(ws.id, base_dir=workspace)

        assert {tag["name"] for tag in rebuilt} == {"Legacy", "P1"}
        assert {tag["name"] for tag in get_workstream_tags(ws.id, base_dir=workspace)} == {"Legacy", "P1"}

    def test_upsert_tag_updates_existing_definition(self, workspace):
        ws = create_workstream(name="Tag WS", base_dir=workspace)

        created = upsert_workstream_tag(ws.id, "Customer", "#0079bf", base_dir=workspace)
        updated = upsert_workstream_tag(ws.id, "Customer", "#61bd4f", base_dir=workspace)

        assert created["name"] == "Customer"
        assert updated["color"] == "#61bd4f"
        reloaded = read_workstream(ws.id, base_dir=workspace)
        assert reloaded.tag_definitions == [{"name": "Customer", "color": "#61bd4f"}]


class TestValidateTransition:
    def test_valid_transition(self, workspace):
        ws = create_workstream(name="Trans", base_dir=workspace)
        assert ws.validate_transition("pending", "in_progress") is True

    def test_invalid_transition(self, workspace):
        ws = create_workstream(name="Trans", base_dir=workspace)
        assert ws.validate_transition("pending", "completed") is False

    def test_custom_states_transition(self, workspace):
        states = {"To Do": ["In Progress"], "In Progress": ["Done"], "Done": []}
        ws = create_workstream(name="Custom", task_states=states, base_dir=workspace)
        assert ws.validate_transition("To Do", "In Progress") is True
        assert ws.validate_transition("To Do", "Done") is False
        assert ws.validate_transition("Done", "To Do") is False


class TestWorkstreamEnv:
    def test_env_inherits_from_parent(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        set_workstream_env_key(parent.id, "OPENAI_API_KEY", "parent-key", base_dir=workspace)

        value = resolve_workstream_env_key(child.id, "OPENAI_API_KEY", base_dir=workspace)
        assert value == "parent-key"

    def test_env_child_override(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        set_workstream_env_key(parent.id, "MODEL", "gpt-parent", base_dir=workspace)
        set_workstream_env_key(child.id, "MODEL", "gpt-child", base_dir=workspace)

        value = resolve_workstream_env_key(child.id, "MODEL", base_dir=workspace)
        assert value == "gpt-child"

    def test_env_unset_masks_parent_and_system(self, workspace, monkeypatch):
        monkeypatch.setenv("SECRET_TOKEN", "system-secret")
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        set_workstream_env_key(parent.id, "SECRET_TOKEN", "parent-secret", base_dir=workspace)
        unset_workstream_env_key(child.id, "SECRET_TOKEN", base_dir=workspace)

        value = resolve_workstream_env_key(child.id, "SECRET_TOKEN", base_dir=workspace)
        assert value is None

    def test_env_falls_back_to_system_env(self, workspace, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "system-anthropic")
        ws = create_workstream(name="Standalone", base_dir=workspace)

        value = resolve_workstream_env_key(ws.id, "ANTHROPIC_API_KEY", base_dir=workspace)
        assert value == "system-anthropic"

    def test_resolve_env_key_with_task_context(self, workspace):
        ws = create_workstream(name="WS", base_dir=workspace)
        task = create_task(ws.id, title="Task", base_dir=workspace)
        set_workstream_env_key(ws.id, "MODEL", "gpt-5", base_dir=workspace)

        value = resolve_env_key("MODEL", task_id=task.id, base_dir=workspace)
        assert value == "gpt-5"

    def test_list_effective_workstream_env_excludes_masked(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        set_workstream_env_key(parent.id, "MODEL", "gpt-parent", base_dir=workspace)
        set_workstream_env_key(parent.id, "TEMPERATURE", "0.1", base_dir=workspace)
        unset_workstream_env_key(child.id, "MODEL", base_dir=workspace)

        env_map = list_effective_workstream_env(child.id, base_dir=workspace)
        assert "MODEL" not in env_map
        assert env_map["TEMPERATURE"] == "0.1"

    def test_base_env_is_lowest_precedence_layer(self, workspace, tmp_path, monkeypatch):
        base_env = tmp_path / "base.env"
        base_env.write_text("BASE_ONLY=from-base\nSHARED=from-base\nMASK_ME=from-base\n")
        monkeypatch.setenv("ORCHESTRATION_BASE_ENV_PATH", str(base_env))

        ws = create_workstream(name="WS", base_dir=workspace)
        set_workstream_env_key(ws.id, "SHARED", "from-workstream", base_dir=workspace)
        unset_workstream_env_key(ws.id, "MASK_ME", base_dir=workspace)

        hierarchy = list_workstream_hierarchy_env(ws.id, base_dir=workspace)
        assert hierarchy[0]["kind"] == "orchestration-root"
        assert hierarchy[0]["path"] == str(base_env)

        effective = list_effective_workstream_env(ws.id, base_dir=workspace)
        assert effective["BASE_ONLY"] == "from-base"
        assert effective["SHARED"] == "from-workstream"
        assert "MASK_ME" not in effective

        assert resolve_workstream_env_key(ws.id, "BASE_ONLY", base_dir=workspace) == "from-base"
        assert resolve_workstream_env_key(ws.id, "SHARED", base_dir=workspace) == "from-workstream"
        assert resolve_workstream_env_key(ws.id, "MASK_ME", base_dir=workspace) is None

    def test_working_directory_env_is_included_for_descendants(self, workspace, tmp_path):
        working_root = tmp_path / "career_pivot_repo"
        working_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        set_workstream_env_key(parent.id, "SHARED", "parent", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)

        (working_root / ".env").write_text("WORKING_ONLY=from-working-dir\nSHARED=from-working-dir\n")

        child = create_workstream(name="Programmatic SEO", parent_id=parent.id, base_dir=workspace)

        assert resolve_workstream_env_key(child.id, "WORKING_ONLY", base_dir=workspace) == "from-working-dir"
        assert resolve_workstream_env_key(child.id, "SHARED", base_dir=workspace) == "from-working-dir"

        set_workstream_env_key(child.id, "SHARED", "child", base_dir=workspace)
        assert resolve_workstream_env_key(child.id, "SHARED", base_dir=workspace) == "child"

        unset_workstream_env_key(child.id, "WORKING_ONLY", base_dir=workspace)
        assert resolve_workstream_env_key(child.id, "WORKING_ONLY", base_dir=workspace) is None

    def test_working_directory_env_is_included_for_selected_workstream(self, workspace, tmp_path):
        working_root = tmp_path / "career_pivot_repo"
        working_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)

        (working_root / ".env").write_text("WORKING_ONLY=from-working-dir\nSHARED=from-working-dir\n")

        hierarchy = list_workstream_hierarchy_env(parent.id, base_dir=workspace)
        assert any(layer["kind"] == "working-directory-root" for layer in hierarchy)

        effective = list_effective_workstream_env(parent.id, base_dir=workspace)
        assert effective["WORKING_ONLY"] == "from-working-dir"
        assert resolve_workstream_env_key(parent.id, "SHARED", base_dir=workspace) == "from-working-dir"


class TestWorkingDirectoryDescendants:
    def test_lists_and_reads_children_when_parent_has_working_directory(self, workspace, tmp_path):
        working_root = tmp_path / "career_pivot_repo"
        working_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(
            name="Go-to-Market",
            parent_id=parent.id,
            base_dir=workspace,
        )

        all_ws = list_workstreams(base_dir=workspace)
        ids = {w.id for w in all_ws}
        assert parent.id in ids
        assert child.id in ids

        loaded = read_workstream(child.id, base_dir=workspace)
        assert loaded.name == "Go-to-Market"
        assert loaded.parent_id == parent.id

    def test_working_directory_parent_keeps_child_state_local(self, workspace, tmp_path):
        mount_root = tmp_path / "career_pivot_repo"
        mount_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(mount_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Sales", parent_id=parent.id, base_dir=workspace)

        assert os.path.exists(os.path.join(workspace, "workstreams", parent.id, "workstreams", f"{child.id}.yaml"))
        assert not os.path.exists(mount_root / "workstreams" / f"{child.id}.yaml")

    def test_create_task_on_working_directory_child_keeps_state_local(self, workspace, tmp_path):
        mount_root = tmp_path / "career_pivot_repo"
        mount_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(mount_root)
        save_workstream(parent, base_dir=workspace)

        mounted_child = create_workstream(
            name="linkedin",
            parent_id=parent.id,
            base_dir=workspace,
        )

        task = create_task(mounted_child.id, title="Reach out", base_dir=workspace)
        task_path = os.path.join(workspace, "workstreams", parent.id, "workstreams", mounted_child.id, "tasks", f"{task.id}.yaml")
        assert os.path.exists(task_path)
        assert not os.path.exists(mount_root / "workstreams" / mounted_child.id / "tasks" / f"{task.id}.yaml")

    def test_resolve_workspace_prefers_working_directory_ancestor_even_with_stale_code_workspace_yaml(self, workspace, tmp_path, monkeypatch):
        working_root = tmp_path / "planetdb_repo"
        working_root.mkdir()
        state_root = tmp_path / "shared_state"
        monkeypatch.setenv("WORKSTREAM_ROOT", f"file:{state_root}")

        parent = create_workstream(name="planetdb", base_dir=workspace)
        parent.working_directory = str(working_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Go-to-Market", parent_id=parent.id, base_dir=workspace)
        grandchild = create_workstream(name="Programmatic SEO", parent_id=child.id, base_dir=workspace)

        # Simulate stale YAML in the code workspace; state now lives under WORKSTREAM_ROOT.
        stale_local_path = os.path.join(workspace, "workstreams", f"{grandchild.id}.yaml")
        os.makedirs(os.path.dirname(stale_local_path), exist_ok=True)
        with open(stale_local_path, "w") as f:
            f.write("id: stale\nname: stale\n")
        assert os.path.exists(stale_local_path)

        resolved = resolve_workstream_workspace(grandchild.id, base_dir=workspace)
        assert resolved == str(working_root.resolve())

    def test_missing_working_directory_keeps_descendants_visible(self, workspace, tmp_path):
        missing_root = tmp_path / "missing_repo"
        missing_root.mkdir()

        parent = create_workstream(name="career_pivot", base_dir=workspace)
        parent.working_directory = str(missing_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Sales", parent_id=parent.id, base_dir=workspace)

        shutil.rmtree(missing_root)

        visible = {ws.id: ws for ws in list_workstreams(base_dir=workspace)}
        assert parent.id in visible
        assert child.id in visible
        assert visible[parent.id].to_dict()["mount_available"] is False

        loaded_child = read_workstream(child.id, base_dir=workspace)
        assert loaded_child.id == child.id


class TestPersistenceRoots:
    def test_workstream_root_file_uri_rehomes_state(self, workspace, tmp_path, monkeypatch):
        state_root = tmp_path / "shared_state"
        monkeypatch.setenv("WORKSTREAM_ROOT", f"file:{state_root}")

        ws = create_workstream(name="Shared", base_dir=workspace)

        assert os.path.exists(state_root / "workstreams" / f"{ws.id}.yaml")
        assert not os.path.exists(os.path.join(workspace, "workstreams", f"{ws.id}.yaml"))

    def test_artifact_root_defaults_to_workspace_when_unset(self, workspace, tmp_path, monkeypatch):
        from orchestration.artifacts import create_artifact

        state_root = tmp_path / "shared_state"
        monkeypatch.setenv("WORKSTREAM_ROOT", f"file:{state_root}")

        create_artifact("reports/test.md", "ok", base_dir=workspace)

        assert os.path.exists(os.path.join(workspace, "artifacts", "reports", "test.md"))
        assert not os.path.exists(state_root / "artifacts" / "reports" / "test.md")

    def test_child_workstream_root_routes_descendants_without_changing_code_workspace(self, workspace, tmp_path):
        child_root = tmp_path / "shared_state"
        child_root.mkdir()

        parent = create_workstream(name="Parent", base_dir=workspace)
        parent.child_workstream_root = str(child_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        assert os.path.exists(child_root / "workstreams" / f"{child.id}.yaml")
        assert resolve_workstream_workspace(child.id, base_dir=workspace) == os.path.abspath(workspace)

    def test_default_child_workstream_root_uses_parent_node_directory(self, workspace):
        parent = create_workstream(name="Parent", base_dir=workspace)
        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        expected_child_root = os.path.join(workspace, "workstreams", parent.id)

        assert resolve_workstream_child_state_root(parent.id, base_dir=workspace) == expected_child_root
        assert resolve_workstream_state_root(child.id, base_dir=workspace) == expected_child_root
        assert os.path.exists(os.path.join(expected_child_root, "workstreams", f"{child.id}.yaml"))

    def test_list_workstreams_cache_detects_external_nested_child_addition(self, workspace, tmp_path, monkeypatch):
        state_root = tmp_path / "shared_state"
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
        monkeypatch.setenv("WORKSTREAM_ROOT", f"file:{state_root}")

        parent = create_workstream(name="Parent", base_dir=workspace)

        warmed = {ws.id for ws in list_workstreams(base_dir=workspace)}
        assert parent.id in warmed

        child_json = subprocess.check_output(
            [
                sys.executable,
                "-m",
                "orchestration.cli",
                "--base-dir",
                workspace,
                "workstream",
                "create",
                "--name",
                "Child",
                "--parent",
                parent.id,
            ],
            cwd=workspace,
            text=True,
            env={
                **os.environ,
                "WORKSTREAM_ROOT": f"file:{state_root}",
                "PYTHONPATH": repo_root if not os.environ.get("PYTHONPATH") else repo_root + os.pathsep + os.environ["PYTHONPATH"],
            },
        )
        child_id = json.loads(child_json)["data"]["id"]

        visible = {ws.id for ws in list_workstreams(base_dir=workspace)}
        assert child_id in visible

    def test_artifact_root_override_is_inherited_by_descendants(self, workspace, tmp_path):
        artifact_root = tmp_path / "shared_artifacts"
        artifact_root.mkdir()

        parent = create_workstream(name="Parent", base_dir=workspace)
        parent.artifact_root = str(artifact_root)
        save_workstream(parent, base_dir=workspace)

        child = create_workstream(name="Child", parent_id=parent.id, base_dir=workspace)

        assert resolve_workstream_artifact_root(parent.id, base_dir=workspace) == str(artifact_root.resolve())
        assert resolve_workstream_artifact_root(child.id, base_dir=workspace) == str(artifact_root.resolve())
