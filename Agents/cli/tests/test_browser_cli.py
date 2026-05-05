from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[1] / "browser.py"


def _load_browser_module():
    spec = spec_from_file_location("browser_cli", MODULE_PATH)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _FakePage:
    def __init__(self):
        self.url = "about:blank"

    def goto(self, url, wait_until=None, timeout=None):
        self.url = url

    def title(self):
        return "Fake Title"


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()
        self.init_script = None

    def add_init_script(self, script):
        self.init_script = script

    def new_page(self):
        return self.page


def test_cmd_open_does_not_force_stale_user_agent():
    browser = _load_browser_module()
    manager = browser.BrowserManager()
    captured = {}
    fake_context = _FakeContext()

    def _new_context(**kwargs):
        captured.update(kwargs)
        return fake_context

    manager.browser = SimpleNamespace(new_context=_new_context)

    result = manager.cmd_open({"url": "https://example.com"})

    assert result["url"] == "https://example.com"
    assert captured["viewport"] == {"width": 1280, "height": 800}
    assert "user_agent" not in captured


def test_cmd_open_loads_auth_state_when_present(tmp_path):
    browser = _load_browser_module()
    manager = browser.BrowserManager()
    captured = {}
    fake_context = _FakeContext()
    auth_file = tmp_path / "auth.json"
    auth_file.write_text("{}", encoding="utf-8")

    def _new_context(**kwargs):
        captured.update(kwargs)
        return fake_context

    manager.browser = SimpleNamespace(new_context=_new_context)

    manager.cmd_open({"auth": str(auth_file)})

    assert captured["storage_state"] == str(auth_file)