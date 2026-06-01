from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "static" / "index.html"


def _index_html() -> str:
    return INDEX_HTML.read_text(encoding="utf-8")


def test_task_comments_render_via_markdown() -> None:
    html = _index_html()

    assert 'class="comment-text markdown-content"' in html
    assert 'renderMarkdown(message)' in html


def test_markdown_attachment_previews_use_markdown_renderer() -> None:
    html = _index_html()

    assert 'function isMarkdownPath(path)' in html
    assert 'setAttachmentPreviewMarkdown(text);' in html
    assert 'attachment-preview-content markdown-content markdown-preview' in html


def test_render_markdown_escapes_raw_html_before_parsing() -> None:
    html = _index_html()

    assert 'const source = esc(String(str));' in html
    assert 'return marked.parse(source, { breaks: true });' in html


def test_task_open_error_handling_distinguishes_corrupt_files() -> None:
    html = _index_html()

    assert "err && err.code === 'CORRUPT_TASK'" in html
    assert 'Cannot open this task because its task file is invalid.' in html
    assert 'Cannot open this task.\\n\\nTask ID: ' in html


def test_api_helper_preserves_error_codes_for_ui_messages() -> None:
    html = _index_html()

    assert 'throw makeApiError(data.message || \'API error\'' in html
    assert "code: data.code || 'ERROR'" in html
    assert "code: 'TIMEOUT'" in html
    assert "code: 'NETWORK'" in html


def test_api_normalizes_fetch_abort_errors() -> None:
    html = _index_html()

    assert "controller.abort(new DOMException(`Request timed out after ${timeoutMs}ms`, 'TimeoutError'))" in html
    assert "message.includes('signal is aborted')" in html
    assert "Request timed out after ${Math.round(timeoutMs / 1000)}s: /api/${path}" in html


def test_board_cards_render_agent_progress_checklist() -> None:
    html = _index_html()

    assert 'function renderCardProgress(progress)' in html
    assert 'class="card-progress-list"' in html
    assert 'progress_summary: normalizeProgressSummary(run.progress_summary)' in html
    assert 'const progressHtml = renderCardProgress(cardRun && cardRun.progress_summary);' in html
    assert '@keyframes card-progress-spin' in html


def test_navigation_routes_sync_browser_url_and_restore_state() -> None:
    html = _index_html()

    assert 'function buildWorkstreamRoute(wsId)' in html
    assert 'function buildTaskRoute(taskId)' in html
    assert 'function parseNavigatorRoute(pathname = window.location.pathname)' in html
    assert "window.history[method]({}, '', nextPath);" in html
    assert "window.addEventListener('popstate', () => {" in html
    assert "await openRouteFromNavigator({ replaceHistory: true });" in html


def test_task_modal_close_restores_workstream_route() -> None:
    html = _index_html()

    assert "if (id === 'task-modal' && opts.updateHistory !== false && selectedWsId)" in html
    assert "syncNavigatorRoute(buildWorkstreamRoute(selectedWsId), { replace: !!opts.replaceHistory });" in html
    assert "await selectWorkstream(task.workstream_id, {" in html


def test_triggers_modal_labels_state_task_selection_mode() -> None:
    html = _index_html()

    assert "typeLabel += t.task_selection === 'all_unlocked'" in html
    assert "(all unlocked tasks)" in html
    assert "(first unlocked task)" in html
    assert "Email: ${esc(eventType)} → ${esc(recipient)}" in html


def test_long_task_tags_clip_in_cards_and_modal() -> None:
    html = _index_html()

    assert '.card-tags { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 4px; min-width: 0; }' in html
    assert 'max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;' in html
    assert '.tag-editor-summary {' in html
    assert '.tag-editor-tag {' in html


def test_board_columns_include_pause_controls() -> None:
    html = _index_html()

    assert 'data-column-pause-state="${encodeURIComponent(state)}"' in html
    assert 'function toggleColumnPause(state, btn)' in html
    assert "api(`workstream/${action}/${selectedWsId}`" in html
    assert "'pause-columns'" in html
    assert "'resume-columns'" in html
    assert 'column-paused' in html


def test_triggers_modal_includes_pause_controls_and_column_pause_badges() -> None:
    html = _index_html()

    assert 'function triggerColumnState(trigger)' in html
    assert 'function isTriggerEffectivelyPaused(trigger, ws = selectedWs)' in html
    assert 'function toggleTriggerPrompt(btn)' in html
    assert 'Column paused' in html
    assert 'function toggleTriggerPause(triggerId, btn)' in html
    assert "api(`trigger/${action}/` + triggerId" in html
    assert 'trigger-paused-badge' in html
    assert 'class="trigger-header"' in html
    assert 'trigger-header-actions' in html
    assert 'trigger-prompt-toggle' in html
    assert 'Force Run Now' not in html


def test_workstream_manager_plays_tuning_sound_on_initial_load_and_resume() -> None:
    html = _index_html()

    assert "const WORKSTREAM_MANAGER_TUNING_SOUND = 'workstream_startup.mp3';" in html
    assert "async function maybePlayInitialTuningUpSound()" in html
    assert "void maybePlayInitialTuningUpSound();" in html
    assert "if (action === 'resume') {" in html
    assert "await playWorkstreamManagerSound(WORKSTREAM_MANAGER_TUNING_SOUND);" in html


def test_agent_runs_footer_includes_global_mute_toggle() -> None:
    html = _index_html()

    assert 'id="active-agents-label"' in html
    assert 'id="agent-runs-mute-toggle"' in html
    assert 'class="sidebar-footer-toggle"' in html
    assert "renderAgentRunsLinkLabel(data.active_agent_runs || 0);" in html


def test_global_mute_toggle_overrides_all_workstream_manager_sound_playback() -> None:
    html = _index_html()

    assert "const WORKSTREAM_MANAGER_SOUND_MUTE_STORAGE_KEY = 'workstreamManager.soundMuted';" in html
    assert "let isWorkstreamManagerMuted = loadWorkstreamManagerMutedPreference();" in html
    assert "function refreshWorkstreamManagerMutedPreference()" in html
    assert "async function syncWorkstreamManagerMutedPreferenceFromServer()" in html
    assert "const data = await api('sound/mute');" in html
    assert "if (!src || refreshWorkstreamManagerMutedPreference()) return false;" in html
    assert "function stopAllWorkstreamManagerAudio()" in html
    assert "audio.pause();" in html
    assert "audio.currentTime = 0;" in html
    assert "async function toggleWorkstreamManagerMute(nextValue = !isWorkstreamManagerMuted)" in html
    assert "await api('sound/mute', {" in html
    assert "body: { muted: isWorkstreamManagerMuted }," in html
    assert "renderWorkstreamManagerMuteToggle();" in html
    assert "window.addEventListener('storage', (event) => {" in html
    assert "await syncWorkstreamManagerMutedPreferenceFromServer();" in html


def test_agent_run_details_recognize_copilot_runtime() -> None:
    html = _index_html()

    assert "selectedCommandLine.includes(' copilot ') || selectedCommandLine.includes('/copilot ')" in html
    assert "runtimeValue === 'copilot'" in html
    assert "return 'Copilot';" in html


def test_agent_run_model_metadata_is_standardized_across_providers() -> None:
    html = _index_html()

    assert 'function formatRunModelMetadata(run, runtimeOverride = null)' in html
    assert "Provider: ${providerLabel} | Model: ${modelLabel} | Effort: ${effortLabel}" in html
    assert 'function splitProviderAndModel(model)' in html
    assert "openai: 'OpenAI'" in html
    assert "deepseek: 'DeepSeek'" in html
    assert "anthropic: 'Anthropic'" in html
    assert 'selected.model.replace(/^anthropic\\//, \'\')' not in html


def test_board_agent_run_chips_include_model_metadata() -> None:
    html = _index_html()

    assert "runtime: run.runtime || ''" in html
    assert "model: run.model || ''" in html
    assert "effort: run.effort || ''" in html
    assert 'const runMeta = formatRunModelMetadata(run);' in html
    assert 'class="card-agent-model"' in html
    assert 'runMeta.summaryText' in html


def test_agent_run_details_show_cli_flags_separately_from_prompt() -> None:
    html = _index_html()

    assert 'function parseShellWords(commandLine)' in html
    assert 'function maskPromptArgs(args)' in html
    assert "masked.push(arg, '<prompt omitted>');" in html
    assert "masked.push(arg, '<system prompt omitted>');" in html


def test_agent_run_details_group_content_into_tabs_with_outputs_default() -> None:
    html = _index_html()

    assert "let selectedAgentDetailTab = 'outputs';" in html
    assert "function setSelectedAgentDetailTab(tab)" in html
    assert 'data-agent-detail-tab="inputs"' in html
    assert 'data-agent-detail-tab="outputs"' in html
    assert 'data-agent-detail-tab="retries"' in html
    assert 'data-agent-detail-panel="inputs"' in html
    assert 'data-agent-detail-panel="outputs"' in html
    assert 'data-agent-detail-panel="retries"' in html
    assert "selectedAgentDetailTab = 'outputs';" in html
    assert 'appearance: none;' in html
    assert 'border-radius: 6px 6px 0 0;' in html
    assert 'box-shadow: inset 0 2px 0 #58a6ff;' in html
    assert 'const selectedCommandArgs = parsedCommandLine && parsedCommandLine.length > 1' in html
    assert "CLI Flags" in html


def test_agent_run_details_include_provider_context_section() -> None:
    html = _index_html()

    assert "Provider Context" in html
    assert "providerContext.messages" in html
    assert "Raw Captured Files" in html


def test_workstream_manager_schedules_interaction_retry_when_initial_autoplay_is_blocked() -> None:
    html = _index_html()

    assert "function scheduleInitialTuningUpRetry()" in html
    assert "document.addEventListener('pointerdown', retry, true);" in html
    assert "document.addEventListener('keydown', retry, true);" in html


def test_workstream_info_includes_agent_concurrency_editor() -> None:
    html = _index_html()

    assert 'id="workstream-concurrency-textarea"' in html
    assert 'Concurrency Policy' in html
    assert 'Simple example' in html
    assert 'saveWorkstreamConcurrency()' in html
    assert "api('workstream/concurrency/' + selectedWsId" in html
    assert 'state_overrides' in html


def test_sidebar_uses_separate_data_and_code_mount_badges() -> None:
    html = _index_html()

    assert 'Artifact root:' in html
    assert 'Child workstream root:' in html
    assert 'class="mount-badge"' in html
    assert "'code-badge code-ready'" in html
    assert "'code-badge code-missing'" in html
    assert "api('workstream/code-status'" in html
    assert 'Working directory missing:' in html


def test_workstream_info_shows_explicit_routing_fields() -> None:
    html = _index_html()

    assert 'Working directory:' in html
    assert 'Artifact root:' in html
    assert 'Child workstream root:' in html
    assert 'Resolved artifact directory:' in html
    assert 'Resolved child workstream root:' in html


def test_create_task_modal_can_be_opened_from_column_header() -> None:
    html = _index_html()

    assert 'id="create-task-modal"' in html
    assert 'data-create-state="${encodeURIComponent(state)}"' in html
    assert "showCreateTaskModal(decodeURIComponent(el.dataset.createState || ''));" in html
    assert 'function showCreateTaskModal(state) {' in html


def test_create_task_modal_posts_title_description_and_state() -> None:
    html = _index_html()

    assert "await api('task/create/' + selectedWsId, {" in html
    assert 'status: targetState,' in html
    assert 'Title is required.' in html
    assert "closeModal('create-task-modal');" in html


def test_reusable_tag_editor_is_shared_between_task_modals() -> None:
    html = _index_html()

    assert 'function renderTagEditor({' in html
    assert 'function initTagEditor(editorEl, opts = {}) {' in html
    assert 'data-tag-editor-id="create-task-tags"' in html
    assert "editorId: 'task-tags-editor'" in html
    assert 'data-tag-picker-toggle="true"' in html


def test_create_and_task_modals_both_wire_tag_editor_to_api_calls() -> None:
    html = _index_html()

    assert 'const createTaskTagEditor = initTagEditor(' in html
    assert 'tags: createTaskTagEditor.getTags(),' in html
    assert 'const taskTagEditor = initTagEditor(' in html
    assert 'async function persistTaskTags(nextTags) {' in html
    assert "await api('task/update/' + task.id, {" in html
    assert 'persistTaskTags(nextTags);' in html


def test_tag_editor_loads_workstream_catalog_and_can_create_colored_tags() -> None:
    html = _index_html()

    assert "api('workstream/gettags/' + wsId)" in html
    assert "api('workstream/upsert-tag/' + wsId, {" in html
    assert 'const syncCatalog = (rawTags, { updateGlobalCache = false } = {}) => {' in html
    assert ".sort((left, right) => left.name.localeCompare(right.name, undefined, { sensitivity: 'base' }))" in html
    assert 'if (updateGlobalCache) {' in html
    assert 'setWorkstreamTagCatalog(wsId, rawTags || []);' in html
    assert 'style="background:${option.value};color:${optionTextColor}"' in html
    assert 'style="background:${state.draftColor};color:${draftTextColor}"' in html
    assert 'colorSelect.style.background = state.draftColor;' in html
    assert 'colorSelect.style.color = getTagColorText(state.draftColor);' in html
    assert '>${esc(option.label)}</option>' in html
    assert 'data-tag-option-checkbox="${encodeURIComponent(tag.name)}"' in html
    assert 'data-tag-create-input="true"' in html
    assert 'data-tag-create-color="true"' in html


def test_tag_editor_clicks_do_not_immediately_close_picker() -> None:
    html = _index_html()

    assert "editorEl.addEventListener('click', async (event) => {" in html
    assert 'event.stopPropagation();' in html
    assert "document.addEventListener('click', onDocumentClick);" in html


def test_task_tag_editor_autosaves_without_explicit_save_button() -> None:
    html = _index_html()

    assert 'id="task-tags-status"' in html
    assert 'Save Tags' not in html
    assert 'persistTaskTags(nextTags);' in html


def test_load_board_retries_transient_not_found_when_workstream_still_exists() -> None:
    html = _index_html()

    assert "board = await api('board/' + selectedWsId, { timeoutMs: 30000 });" in html
    assert "if (e && e.code === 'NOT_FOUND') {" in html
    assert "await api('workstream/read/' + selectedWsId, { timeoutMs: 10000 });" in html
    assert "board = await api('board/' + selectedWsId, { timeoutMs: 30000 });" in html


def test_tag_editor_does_not_treat_local_task_tags_as_full_board_catalog() -> None:
    html = _index_html()

    assert 'syncCatalog(getWorkstreamTagCatalog(wsId));' in html
    assert 'syncCatalog(tags, { updateGlobalCache: true });' in html
    assert "syncCatalog([...(getWorkstreamTagCatalog(wsId) || []), createdTag], { updateGlobalCache: true });" in html


def test_board_polling_is_change_driven_and_agent_links_open_runs() -> None:
    html = _index_html()

    assert 'function boardRenderKey(ws, tasks, lockMap)' in html
    assert 'if (!options.force && nextKey === currentBoardRenderKey)' in html
    assert "let currentBoardRevision = '';" in html
    assert "const data = await api('poll/status');" in html
    assert "api('board/' + selectedWsId + '?meta=1&locks=1'" in html
    assert 'const SIDEBAR_REFRESH_MS = 30000;' in html
    assert "if (nextRevision && nextRevision !== currentBoardRevision)" in html
    assert 'data-agent-run-id="${esc(run.run_id)}"' in html
    assert 'await openAgentRun(el.dataset.agentRunId);' in html


def test_task_detail_uses_workstream_scoped_reads() -> None:
    html = _index_html()

    assert 'const detailQuery = detailWorkstreamId ? `?workstream_id=${encodeURIComponent(detailWorkstreamId)}` : \'\';' in html
    assert "api('task/read/' + taskId + detailQuery)" in html
    assert "api('lock/status/' + taskId + detailQuery)" in html


def test_board_agent_run_timers_update_without_board_rerender() -> None:
    html = _index_html()

    assert 'function updateBoardAgentDurations()' in html
    assert "boardAgentTimer = setInterval(updateBoardAgentDurations, 1000);" in html
    assert "boardActiveAgentRuns = data.active_agent_run_summaries || [];" in html


def test_agent_runs_modal_defaults_to_running_filter() -> None:
    html = _index_html()

    assert "const DEFAULT_AGENT_RUN_STATUS_FILTER = 'running';" in html
    assert "let agentRunStatusFilter = DEFAULT_AGENT_RUN_STATUS_FILTER;" in html
    assert "agentRunStatusFilter = DEFAULT_AGENT_RUN_STATUS_FILTER;" in html
    assert 'function filteredAgentRunsForModal()' in html
    assert "api('agent/runs?limit=25'" in html


def test_sidebar_treats_missing_parent_as_root() -> None:
    html = _index_html()

    assert 'const workstreamIds = new Set(sorted.map(ws => ws.id));' in html
    assert 'if (ws.parent_id && workstreamIds.has(ws.parent_id))' in html
