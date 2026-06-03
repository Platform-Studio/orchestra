from orchestration.progress import add_progress_items, advance_progress, current_progress, init_progress, progress_path, read_progress_summary, update_progress_item


def test_progress_init_update_and_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_AGENT_RUN_ID", "run-env")
    monkeypatch.setenv("ORCHESTRATION_AGENT_WORKSTREAM_ID", "ws-1")
    monkeypatch.setenv("ORCHESTRATION_AGENT_TASK_IDS", "[task-1, task-2]")
    monkeypatch.setenv("ORCHESTRATION_AGENT_NAME", "coder")

    progress = init_progress(["Read context", "Implement change", "Validate"], base_dir=str(tmp_path))

    assert progress["run_id"] == "run-env"
    assert progress["workstream_id"] == "ws-1"
    assert progress["task_ids"] == ["task-1", "task-2"]
    assert progress["agent"] == "coder"
    assert progress["items"][0]["id"] == "read-context"
    assert progress_path("run-env", base_dir=str(tmp_path)).endswith(".orchestration/agent_runs/run-env/progress.yaml")

    update_progress_item("read-context", "active", run_id="run-env", base_dir=str(tmp_path))
    update_progress_item("read-context", "done", run_id="run-env", base_dir=str(tmp_path))
    summary = read_progress_summary("run-env", base_dir=str(tmp_path))

    assert summary["done"] == 1
    assert summary["total"] == 3
    assert summary["current_item_id"] == "implement-change"
    assert summary["inferred_current"] is True


def test_progress_keeps_one_active_item(tmp_path):
    init_progress(
        [
            {"id": "one", "text": "One", "status": "active"},
            {"id": "two", "text": "Two"},
        ],
        run_id="run-1",
        base_dir=str(tmp_path),
    )

    progress = update_progress_item("two", "active", run_id="run-1", base_dir=str(tmp_path))

    statuses = {item["id"]: item["status"] for item in progress["items"]}
    assert statuses == {"one": "pending", "two": "active"}
    assert progress["current_item_id"] == "two"


def test_progress_can_add_items_to_fluid_checklist(tmp_path):
    init_progress(["Read context"], run_id="run-1", base_dir=str(tmp_path))

    progress = add_progress_items(["Update prompt", "Validate behavior"], run_id="run-1", base_dir=str(tmp_path))

    assert [item["id"] for item in progress["items"]] == ["read-context", "update-prompt", "validate-behavior"]
    assert progress["events"][-2]["type"] == "item_added"
    summary = read_progress_summary("run-1", base_dir=str(tmp_path))
    assert summary["total"] == 3
    assert summary["current_item_id"] == "read-context"


def test_current_progress_returns_active_item_and_age(tmp_path):
    init_progress(
        [
            {"id": "one", "text": "One", "status": "active"},
            {"id": "two", "text": "Two"},
        ],
        run_id="run-cur",
        base_dir=str(tmp_path),
    )

    current = current_progress("run-cur", base_dir=str(tmp_path))

    assert current["current_item_id"] == "one"
    assert current["inferred_current"] is False
    assert current["total"] == 2
    assert current["counts"]["active"] == 1
    assert current["seconds_since_update"] is not None
    assert current["seconds_since_update"] >= 0


def test_current_progress_missing_returns_missing_flag(tmp_path):
    current = current_progress("does-not-exist", base_dir=str(tmp_path))
    assert current["missing"] is True
    assert current["current_item_id"] is None


def test_advance_progress_completes_active_and_activates_next(tmp_path):
    init_progress(
        [
            {"id": "one", "text": "One", "status": "active"},
            {"id": "two", "text": "Two"},
            {"id": "three", "text": "Three"},
        ],
        run_id="run-adv",
        base_dir=str(tmp_path),
    )

    progress = advance_progress("two", run_id="run-adv", base_dir=str(tmp_path))

    statuses = {item["id"]: item["status"] for item in progress["items"]}
    assert statuses == {"one": "done", "two": "active", "three": "pending"}
    assert progress["current_item_id"] == "two"


def test_advance_progress_with_no_active_just_activates(tmp_path):
    init_progress(
        [{"id": "one", "text": "One"}, {"id": "two", "text": "Two"}],
        run_id="run-adv2",
        base_dir=str(tmp_path),
    )

    progress = advance_progress("two", run_id="run-adv2", base_dir=str(tmp_path))

    statuses = {item["id"]: item["status"] for item in progress["items"]}
    assert statuses == {"one": "pending", "two": "active"}
    assert progress["current_item_id"] == "two"