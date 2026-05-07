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


def test_api_normalizes_fetch_abort_errors() -> None:
    html = _index_html()

    assert "controller.abort(new DOMException(`Request timed out after ${timeoutMs}ms`, 'TimeoutError'))" in html
    assert "message.includes('signal is aborted')" in html
    assert "Request timed out after ${Math.round(timeoutMs / 1000)}s: /api/${path}" in html