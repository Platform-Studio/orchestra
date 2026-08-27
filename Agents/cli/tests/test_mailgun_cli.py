from argparse import Namespace
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json


MODULE_PATH = Path(__file__).resolve().parents[1] / "mailgun_cli.py"


def _load_mailgun_module():
    spec = spec_from_file_location("mailgun_cli", MODULE_PATH)
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_cmd_list_filters_by_recipient_and_infers_domain(monkeypatch, capsys):
    mailgun = _load_mailgun_module()
    captured = {}

    def _fake_assert_domain_registered(domain, api_key):
        captured["domain"] = domain
        captured["api_key"] = api_key

    def _fake_request(method, url, api_key, data=None, files=None):
        return {
            "items": [
                {
                    "id": "evt-1",
                    "timestamp": 111.0,
                    "storage": {"key": "key-build"},
                    "message": {
                        "headers": {
                            "from": "Alex Example <jb@example.com>",
                            "to": "Build Pipeline <build@mail.example.com>, automatedemails@example.com",
                            "subject": "Build ready",
                        }
                    },
                },
                {
                    "id": "evt-2",
                    "timestamp": 112.0,
                    "storage": {"key": "key-other"},
                    "message": {
                        "headers": {
                            "from": "Alex Example <jb@example.com>",
                            "to": "ops@mail.example.com",
                            "subject": "Ops update",
                        }
                    },
                },
                {
                    "id": "evt-3",
                    "timestamp": 113.0,
                    "storage": {"key": "key-build"},
                    "message": {
                        "headers": {
                            "from": "Alex Example <jb@example.com>",
                            "to": "build@mail.example.com",
                            "subject": "Build ready duplicate event",
                        }
                    },
                },
            ]
        }

    monkeypatch.setattr(mailgun, "get_api_key", lambda env: "test-key")
    monkeypatch.setattr(mailgun, "assert_domain_registered", _fake_assert_domain_registered)
    monkeypatch.setattr(mailgun, "_mailgun_request", _fake_request)

    args = Namespace(domain=None, to="build@mail.example.com", limit=25, json=True)
    mailgun.cmd_list(args, env={})

    payload = json.loads(capsys.readouterr().out)
    assert captured == {"domain": "mail.example.com", "api_key": "test-key"}
    assert payload == [
        {
            "storage_key": "key-build",
            "timestamp": 111.0,
            "from": "Alex Example <jb@example.com>",
            "to": "Build Pipeline <build@mail.example.com>, automatedemails@example.com",
            "subject": "Build ready",
        }
    ]


def test_cmd_list_plain_text_heading_mentions_recipient(monkeypatch, capsys):
    mailgun = _load_mailgun_module()

    monkeypatch.setattr(mailgun, "get_api_key", lambda env: "test-key")
    monkeypatch.setattr(mailgun, "assert_domain_registered", lambda domain, api_key: None)
    monkeypatch.setattr(
        mailgun,
        "_mailgun_request",
        lambda method, url, api_key, data=None, files=None: {
            "items": [
                {
                    "id": "evt-1",
                    "timestamp": 111.0,
                    "storage": {"key": "key-build"},
                    "message": {
                        "headers": {
                            "from": "Alex Example <jb@example.com>",
                            "to": "build@mail.example.com",
                            "subject": "Build ready",
                        }
                    },
                }
            ]
        },
    )

    args = Namespace(domain="mail.example.com", to="build@mail.example.com", limit=10, json=False)
    mailgun.cmd_list(args, env={})

    output = capsys.readouterr().out
    assert "Recent inbound messages for mail.example.com to build@mail.example.com" in output
    assert "Build ready" in output


def test_cmd_create_domain_posts_domain_and_returns_dns_records(monkeypatch, capsys):
    mailgun = _load_mailgun_module()
    captured = {}
    response = {
        "domain": {"name": "vibesold.com", "state": "unverified"},
        "sending_dns_records": [
            {
                "record_type": "TXT",
                "name": "vibesold.com",
                "value": "v=spf1 include:mailgun.org ~all",
            }
        ],
        "receiving_dns_records": [],
    }

    monkeypatch.setattr(mailgun, "get_api_key", lambda env: "test-key")
    monkeypatch.setattr(mailgun, "fetch_domains", lambda api_key: [])

    def _fake_request(method, url, api_key, data=None, files=None):
        captured.update(method=method, url=url, api_key=api_key, data=data)
        return response

    monkeypatch.setattr(mailgun, "_mailgun_request", _fake_request)

    args = Namespace(domain="VibeSold.com", json=True)
    mailgun.cmd_create_domain(args, env={})

    assert json.loads(capsys.readouterr().out) == response
    assert captured == {
        "method": "POST",
        "url": "https://api.mailgun.net/v4/domains",
        "api_key": "test-key",
        "data": {"name": "vibesold.com"},
    }


def test_cmd_download_attachments_writes_files(monkeypatch, tmp_path, capsys):
    mailgun = _load_mailgun_module()

    monkeypatch.setattr(mailgun, "get_api_key", lambda env: "test-key")
    monkeypatch.setattr(mailgun, "assert_domain_registered", lambda domain, api_key: None)
    monkeypatch.setattr(
        mailgun,
        "_fetch_message",
        lambda domain, key, api_key: {
            "attachments": [
                {
                    "name": "brief.pdf",
                    "content-type": "application/pdf",
                    "size": 7,
                    "url": "https://storage.mailgun.test/brief.pdf",
                }
            ]
        },
    )
    monkeypatch.setattr(
        mailgun,
        "_mailgun_request_bytes",
        lambda method, url, api_key: (b"pdfdata", {"content-type": "application/pdf"}),
    )

    args = Namespace(domain="mail.example.com", key="msg-1", out_dir=str(tmp_path), json=True)
    mailgun.cmd_download_attachments(args, env={})

    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "name": "brief.pdf",
            "path": str(tmp_path / "brief.pdf"),
            "size": 7,
            "content_type": "application/pdf",
        }
    ]
    assert (tmp_path / "brief.pdf").read_bytes() == b"pdfdata"


def test_cmd_send_includes_optional_reply_headers(monkeypatch, capsys):
    mailgun = _load_mailgun_module()
    captured = {}

    monkeypatch.setattr(mailgun, "get_api_key", lambda env: "test-key")
    monkeypatch.setattr(mailgun, "assert_domain_registered", lambda domain, api_key: None)

    def _fake_request(method, url, api_key, data=None, files=None):
        captured["method"] = method
        captured["url"] = url
        captured["api_key"] = api_key
        captured["data"] = data
        captured["files"] = files
        return {"id": "queued-id"}

    monkeypatch.setattr(mailgun, "_mailgun_request", _fake_request)

    args = Namespace(
        domain="mail.example.com",
        from_addr="Build <build@mail.example.com>",
        to=["jb@example.com"],
        cc=None,
        bcc=None,
        in_reply_to="<parent@example.com>",
        references=["<root@example.com>", "<parent@example.com>"],
        subject="Re: Feature request",
        text="Following up",
        html=None,
        attach=None,
        json=True,
    )
    mailgun.cmd_send(args, env={})

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"id": "queued-id"}
    assert captured["method"] == "POST"
    assert captured["api_key"] == "test-key"
    assert captured["data"]["h:In-Reply-To"] == "<parent@example.com>"
    assert captured["data"]["h:References"] == "<root@example.com> <parent@example.com>"
