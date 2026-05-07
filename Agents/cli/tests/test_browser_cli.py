from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
import sys


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


def test_auth_registry_round_trip(tmp_path, monkeypatch):
    browser = _load_browser_module()
    monkeypatch.setattr(browser, "AUTH_DIR", tmp_path / ".auth")
    monkeypatch.setattr(browser, "AUTH_INDEX", browser.AUTH_DIR / "index.json")

    auth_file = browser.AUTH_DIR / "linkedin.json"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text("{}", encoding="utf-8")

    saved = browser._auth_save("https://www.linkedin.com", str(auth_file), account_label="Primary")
    loaded = browser._auth_get("linkedin")

    assert saved["site"] == "linkedin"
    assert loaded == {
        "site": "linkedin",
        "found": True,
        "path": str(auth_file),
        "updated_at": saved["updated_at"],
        "account_label": "Primary",
    }


def test_main_open_with_site_loads_saved_auth(tmp_path, monkeypatch, capsys):
    browser = _load_browser_module()
    monkeypatch.setattr(browser, "AUTH_DIR", tmp_path / ".auth")
    monkeypatch.setattr(browser, "AUTH_INDEX", browser.AUTH_DIR / "index.json")

    auth_file = browser.AUTH_DIR / "linkedin.json"
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth_file.write_text("{}", encoding="utf-8")
    browser._auth_save("linkedin", str(auth_file))

    captured = {}

    def _send(port, command, params):
        captured["port"] = port
        captured["command"] = command
        captured["params"] = params
        return {"session": "sess-1", "url": "https://www.linkedin.com/feed/", "title": "LinkedIn"}

    monkeypatch.setattr(browser, "ensure_server", lambda headless=False: 7777)
    monkeypatch.setattr(browser, "send", _send)
    monkeypatch.setattr(sys, "argv", ["browser.py", "open", "https://www.linkedin.com/feed/", "--site", "linkedin"])

    browser.main()

    output = capsys.readouterr().out
    assert captured["command"] == "open"
    assert captured["params"]["auth"] == str(auth_file)
    assert captured["params"]["site"] == "linkedin"
    assert f"Auth:    {auth_file} (linkedin)" in output


def test_main_auth_save_updates_registry(tmp_path, monkeypatch, capsys):
    browser = _load_browser_module()
    monkeypatch.setattr(browser, "AUTH_DIR", tmp_path / ".auth")
    monkeypatch.setattr(browser, "AUTH_INDEX", browser.AUTH_DIR / "index.json")
    monkeypatch.setattr(browser, "ensure_server", lambda headless=False: 8888)

    def _send(port, command, params):
        Path(params["path"]).parent.mkdir(parents=True, exist_ok=True)
        Path(params["path"]).write_text("{}", encoding="utf-8")
        return {"path": params["path"]}

    monkeypatch.setattr(browser, "send", _send)
    monkeypatch.setattr(
        sys,
        "argv",
        ["browser.py", "auth-save", "sess-2", "--site", "linkedin", "--account-label", "Primary"],
    )

    browser.main()

    output = capsys.readouterr().out
    auth_info = browser._auth_get("linkedin")
    assert auth_info["found"] is True
    assert auth_info["path"] == str(browser.AUTH_DIR / "linkedin.json")
    assert auth_info["account_label"] == "Primary"
    assert f"Auth saved for linkedin [Primary]: {browser.AUTH_DIR / 'linkedin.json'}" in output