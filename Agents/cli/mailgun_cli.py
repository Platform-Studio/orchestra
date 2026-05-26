#!/usr/bin/env python3
"""Mailgun CLI for foundation agents.

Wraps the Mailgun v3 API for sending and receiving email:
- Send email (plain text, HTML, attachments, multiple recipients, CC/BCC).
- List the most recent inbound messages for a domain or a specific recipient (sender + subject summary).
- Read full details of a specific inbound message.
- List all configured Mailgun domains (validates domain is registered).

Auth comes from the project-root .env:
    MAILGUN_API_KEY

Optional defaults:
    MAILGUN_FROM_EMAIL      Default from address when --from is omitted
    MAILGUN_FROM_NAME       Default from display name
    MAILGUN_DOMAIN          Default sending domain when --domain is omitted

Usage examples:
    # List configured domains
    python Agents/cli/mailgun_cli.py domains

    # Send plain text
    python Agents/cli/mailgun_cli.py send \\
        --domain mg.hirescout.us \\
        --to alice@example.com \\
        --subject "Hello" \\
        --text "Hi there"

    # Send HTML + plain, multiple recipients, CC, attachment
    python Agents/cli/mailgun_cli.py send \\
        --domain mg.hirescout.us \\
        --from "Jeremy <jb@platformstud.io>" \\
        --to alice@example.com --to bob@example.com \\
        --cc manager@example.com \\
        --subject "Report" \\
        --text "See attached." \\
        --html "<p>See attached.</p>" \\
        --attach ./report.pdf

    # List 25 most recent inbound messages for a domain
    python Agents/cli/mailgun_cli.py list --domain mg.hirescout.us

    # List recent inbound messages for a specific email address
    python Agents/cli/mailgun_cli.py list --to build@guild.platformstud.io

    # Read full details of a specific message (storage key from list output)
    python Agents/cli/mailgun_cli.py read \\
        --domain mg.hirescout.us \\
        --key BAABAQU3_nLx9y4Rxt5HqpOTir_jXKomaQ

    # Output as JSON
    python Agents/cli/mailgun_cli.py --json list --domain mg.hirescout.us
    python Agents/cli/mailgun_cli.py --json read --domain mg.hirescout.us --key <KEY>
"""

from __future__ import annotations

import argparse
import base64
from email.utils import getaddresses
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Config / env
# ---------------------------------------------------------------------------

def find_project_root() -> Path:
    current = Path(__file__).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / "README.md").exists() and (candidate / "Agents").exists():
            return candidate
    return current


def load_env() -> dict[str, str]:
    root = find_project_root()
    env_path = root / ".env"
    result: dict[str, str] = {}
    if not env_path.exists():
        return result
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip("'\"")
    return result


def get_api_key(env: dict[str, str]) -> str:
    key = env.get("MAILGUN_API_KEY") or os.environ.get("MAILGUN_API_KEY", "")
    if not key:
        print("ERROR: MAILGUN_API_KEY not set in .env or environment.", file=sys.stderr)
        sys.exit(1)
    return key


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _mailgun_request(
    method: str,
    url: str,
    api_key: str,
    data: dict | None = None,
    files: list[tuple] | None = None,
) -> dict[str, Any]:
    """Make an authenticated Mailgun API request using only stdlib."""
    import http.client
    import urllib.parse
    from base64 import b64encode

    auth_header = "Basic " + b64encode(f"api:{api_key}".encode()).decode()

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query

    conn = http.client.HTTPSConnection(host)
    headers: dict[str, str] = {"Authorization": auth_header}

    body: bytes | None = None

    if files:
        # Multipart form data (needed for attachments)
        boundary = "----MailgunCLIBoundary7429"
        parts: list[bytes] = []

        def encode_field(name: str, value: str) -> bytes:
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode()

        def encode_file(name: str, filename: str, content: bytes, mime: str) -> bytes:
            return (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {mime}\r\n\r\n"
            ).encode() + content + b"\r\n"

        for k, v in (data or {}).items():
            if isinstance(v, list):
                for item in v:
                    parts.append(encode_field(k, item))
            else:
                parts.append(encode_field(k, v))

        for field_name, filepath, content, mime in files:
            parts.append(encode_file(field_name, filepath, content, mime))

        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        headers["Content-Length"] = str(len(body))

    elif data and method in ("POST", "PUT", "PATCH"):
        # URL-encoded form
        body = urllib.parse.urlencode(
            [(k, item) for k, v in data.items() for item in (v if isinstance(v, list) else [v])]
        ).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(body))

    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    conn.close()

    if not raw.strip():
        return {"_status": resp.status}

    try:
        parsed_body = json.loads(raw)
    except json.JSONDecodeError:
        parsed_body = {"_raw": raw}

    if resp.status >= 400:
        msg = parsed_body.get("message") or parsed_body.get("_raw") or raw
        print(f"ERROR: Mailgun API returned {resp.status}: {msg}", file=sys.stderr)
        sys.exit(1)

    return parsed_body


def _mailgun_request_bytes(method: str, url: str, api_key: str) -> tuple[bytes, dict[str, str]]:
    """Make an authenticated Mailgun API request and return raw bytes + headers."""
    import http.client
    import urllib.parse
    from base64 import b64encode

    auth_header = "Basic " + b64encode(f"api:{api_key}".encode()).decode()

    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query

    conn = http.client.HTTPSConnection(parsed.netloc)
    conn.request(method, path, headers={"Authorization": auth_header})
    resp = conn.getresponse()
    raw = resp.read()
    headers = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()

    if resp.status >= 400:
        message = raw.decode("utf-8", errors="replace")
        print(f"ERROR: Mailgun API returned {resp.status}: {message}", file=sys.stderr)
        sys.exit(1)

    return raw, headers


def _fetch_message(domain: str, key: str, api_key: str) -> dict[str, Any]:
    url = f"https://storage-us-west1.api.mailgun.net/v3/domains/{domain}/messages/{key}"
    return _mailgun_request("GET", url, api_key)


def _safe_attachment_name(raw_name: str | None, fallback: str) -> str:
    candidate = Path(str(raw_name or fallback)).name.strip()
    return candidate or fallback


# ---------------------------------------------------------------------------
# Domain validation
# ---------------------------------------------------------------------------

def fetch_domains(api_key: str) -> list[str]:
    resp = _mailgun_request("GET", "https://api.mailgun.net/v3/domains?limit=100", api_key)
    return [d["name"] for d in resp.get("items", [])]


def assert_domain_registered(domain: str, api_key: str) -> None:
    domains = fetch_domains(api_key)
    if domain not in domains:
        print(
            f"ERROR: Domain '{domain}' is not registered in your Mailgun account.\n"
            f"Registered domains: {', '.join(domains)}",
            file=sys.stderr,
        )
        sys.exit(1)


def _recipient_domain(recipient: str | None) -> str:
    if not recipient or "@" not in recipient:
        return ""
    return recipient.rsplit("@", 1)[1].strip().lower()


def _item_has_recipient(item: dict[str, Any], recipient: str | None) -> bool:
    if not recipient:
        return True

    wanted = recipient.strip().lower()
    headers = item.get("message", {}).get("headers", {})
    values: list[str] = []
    for key in ("to", "cc", "bcc"):
        value = headers.get(key)
        if value:
            values.append(value)

    if not values:
        return False

    addresses = [addr.lower() for _, addr in getaddresses(values) if addr]
    return wanted in addresses


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_domains(args: argparse.Namespace, env: dict[str, str]) -> None:
    api_key = get_api_key(env)
    resp = _mailgun_request("GET", "https://api.mailgun.net/v3/domains?limit=100", api_key)
    items = resp.get("items", [])
    if args.json:
        print(json.dumps(items, indent=2))
        return
    print(f"{'Domain':<45} {'State':<10} {'Type':<10} {'Created'}")
    print("-" * 90)
    for d in items:
        print(f"{d['name']:<45} {d.get('state','?'):<10} {d.get('type','?'):<10} {d.get('created_at','?')}")


def cmd_send(args: argparse.Namespace, env: dict[str, str]) -> None:
    api_key = get_api_key(env)

    # Build from address
    from_addr = args.from_addr
    if not from_addr:
        from_name = env.get("MAILGUN_FROM_NAME", "")
        from_email = env.get("MAILGUN_FROM_EMAIL", "")
        if not from_email:
            print("ERROR: --from is required (or set MAILGUN_FROM_EMAIL in .env)", file=sys.stderr)
            sys.exit(1)
        from_addr = f"{from_name} <{from_email}>" if from_name else from_email

    # Determine authenticated Mailgun domain.
    # Default behavior: match the domain of the From address when possible.
    from_email_token = from_addr
    if "<" in from_addr and ">" in from_addr:
        start = from_addr.rfind("<") + 1
        end = from_addr.rfind(">")
        if start > 0 and end > start:
            from_email_token = from_addr[start:end].strip()

    from_domain = ""
    if "@" in from_email_token:
        from_domain = from_email_token.rsplit("@", 1)[1].strip().lower()

    domain = args.domain or from_domain or env.get("MAILGUN_DOMAIN")
    if not domain:
        print(
            "ERROR: Could not determine sending domain. Pass --domain, or use a valid --from address, "
            "or set MAILGUN_DOMAIN in .env.",
            file=sys.stderr,
        )
        sys.exit(1)

    assert_domain_registered(domain, api_key)

    if not args.to:
        print("ERROR: At least one --to recipient is required.", file=sys.stderr)
        sys.exit(1)

    data: dict[str, Any] = {
        "from": from_addr,
        "to": args.to,
        "subject": args.subject or "(no subject)",
    }

    if args.cc:
        data["cc"] = args.cc
    if args.bcc:
        data["bcc"] = args.bcc
    if args.in_reply_to:
        data["h:In-Reply-To"] = args.in_reply_to
    if args.references:
        data["h:References"] = " ".join(args.references)
    if args.text:
        data["text"] = args.text
    if args.html:
        data["html"] = args.html
    if not args.text and not args.html:
        print("ERROR: At least one of --text or --html is required.", file=sys.stderr)
        sys.exit(1)

    # Load attachments
    file_parts: list[tuple] = []
    for path_str in (args.attach or []):
        p = Path(path_str)
        if not p.exists():
            print(f"ERROR: Attachment file not found: {p}", file=sys.stderr)
            sys.exit(1)
        mime = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        file_parts.append(("attachment", p.name, p.read_bytes(), mime))

    url = f"https://api.mailgun.net/v3/{domain}/messages"

    if file_parts:
        resp = _mailgun_request("POST", url, api_key, data=data, files=file_parts)
    else:
        resp = _mailgun_request("POST", url, api_key, data=data)

    if args.json:
        print(json.dumps(resp, indent=2))
    else:
        msg_id = resp.get("id", "?")
        print(f"✓ Message queued: {msg_id}")


def cmd_list(args: argparse.Namespace, env: dict[str, str]) -> None:
    api_key = get_api_key(env)

    domain = args.domain or _recipient_domain(args.to) or env.get("MAILGUN_DOMAIN")
    if not domain:
        print(
            "ERROR: --domain is required unless it can be inferred from --to (or set MAILGUN_DOMAIN in .env)",
            file=sys.stderr,
        )
        sys.exit(1)

    assert_domain_registered(domain, api_key)

    url = f"https://api.mailgun.net/v3/{domain}/events?event=accepted&limit={args.limit}"
    resp = _mailgun_request("GET", url, api_key)

    items = resp.get("items", [])

    # Deduplicate by storage key (accepted fires multiple times per message)
    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        key = item.get("storage", {}).get("key", item.get("id"))
        if key not in seen:
            seen.add(key)
            unique.append(item)

    filtered = [item for item in unique if _item_has_recipient(item, args.to)]

    if args.json:
        output = []
        for item in filtered:
            h = item.get("message", {}).get("headers", {})
            output.append({
                "storage_key": item.get("storage", {}).get("key"),
                "timestamp": item.get("timestamp"),
                "from": h.get("from"),
                "to": h.get("to"),
                "subject": h.get("subject"),
            })
        print(json.dumps(output, indent=2))
        return

    heading = f"\nRecent inbound messages for {domain}"
    if args.to:
        heading += f" to {args.to}"
    heading += f" (up to {args.limit}):"
    print(heading)
    print(f"{'#':<4} {'From':<40} {'Subject':<50} {'Storage Key'}")
    print("-" * 130)
    for i, item in enumerate(filtered, 1):
        h = item.get("message", {}).get("headers", {})
        from_ = h.get("from", "?")[:38]
        subject = h.get("subject", "?")[:48]
        key = item.get("storage", {}).get("key", "?")
        print(f"{i:<4} {from_:<40} {subject:<50} {key}")


def cmd_read(args: argparse.Namespace, env: dict[str, str]) -> None:
    api_key = get_api_key(env)

    domain = args.domain or env.get("MAILGUN_DOMAIN")
    if not domain:
        print("ERROR: --domain is required (or set MAILGUN_DOMAIN in .env)", file=sys.stderr)
        sys.exit(1)

    assert_domain_registered(domain, api_key)

    msg = _fetch_message(domain, args.key, api_key)

    if args.json:
        print(json.dumps(msg, indent=2))
        return

    print(f"From:     {msg.get('from', '?')}")
    print(f"To:       {msg.get('recipients', '?')}")
    print(f"Subject:  {msg.get('subject', '?')}")
    print(f"Date:     {msg.get('Date', '?')}")
    print()

    attachments = msg.get("attachments", [])
    if attachments:
        print(f"Attachments ({len(attachments)}):")
        for a in attachments:
            print(f"  - {a.get('name', '?')} ({a.get('content-type', '?')}, {a.get('size', '?')} bytes)")
        print()

    body = msg.get("stripped-text") or msg.get("body-plain") or ""
    if body:
        print("Body:")
        print("-" * 60)
        print(body.strip())
    else:
        html = msg.get("stripped-html") or msg.get("body-html") or ""
        if html:
            print("Body (HTML only):")
            print("-" * 60)
            print(html.strip())
        else:
            print("(no body content found)")


def cmd_download_attachments(args: argparse.Namespace, env: dict[str, str]) -> None:
    api_key = get_api_key(env)

    domain = args.domain or env.get("MAILGUN_DOMAIN")
    if not domain:
        print("ERROR: --domain is required (or set MAILGUN_DOMAIN in .env)", file=sys.stderr)
        sys.exit(1)

    assert_domain_registered(domain, api_key)

    msg = _fetch_message(domain, args.key, api_key)
    attachments = list(msg.get("attachments", []) or [])
    output_dir = Path(args.out_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for idx, attachment in enumerate(attachments, 1):
        url = str(attachment.get("url") or "").strip()
        if not url:
            continue
        filename = _safe_attachment_name(attachment.get("name"), f"attachment-{idx}")
        destination = output_dir / filename
        content, _headers = _mailgun_request_bytes("GET", url, api_key)
        destination.write_bytes(content)
        saved.append({
            "name": filename,
            "path": str(destination),
            "size": len(content),
            "content_type": attachment.get("content-type") or attachment.get("content_type"),
        })

    if args.json:
        print(json.dumps(saved, indent=2))
        return

    if not saved:
        print(f"No downloadable attachments found for message {args.key}.")
        return

    print(f"Downloaded {len(saved)} attachment(s) to {output_dir}:")
    for item in saved:
        print(f"- {item['name']} -> {item['path']}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mailgun_cli",
        description="Mailgun CLI — send and receive email via Mailgun API.",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    # domains
    p_domains = sub.add_parser("domains", help="List all Mailgun domains on this account")

    # send
    p_send = sub.add_parser("send", help="Send an email")
    p_send.add_argument("--domain", help="Sending domain (or MAILGUN_DOMAIN in .env)")
    p_send.add_argument("--from", dest="from_addr", metavar="FROM",
                        help='From address, e.g. "Name <email@domain>"')
    p_send.add_argument("--to", action="append", required=True, metavar="TO",
                        help="Recipient(s), repeatable")
    p_send.add_argument("--cc", action="append", metavar="CC", help="CC recipient(s), repeatable")
    p_send.add_argument("--bcc", action="append", metavar="BCC", help="BCC recipient(s), repeatable")
    p_send.add_argument("--in-reply-to", metavar="MESSAGE_ID",
                        help="Optional Message-ID to send this email as a reply to")
    p_send.add_argument("--references", action="append", metavar="MESSAGE_ID",
                        help="Optional References header value(s); repeat to build a thread chain")
    p_send.add_argument("--subject", help="Email subject")
    p_send.add_argument("--text", help="Plain text body")
    p_send.add_argument("--html", help="HTML body")
    p_send.add_argument("--attach", action="append", metavar="FILE",
                        help="Path to attachment file, repeatable")

    # list
    p_list = sub.add_parser("list", help="List most recent inbound messages for a domain or recipient")
    p_list.add_argument("--domain", help="Inbound domain (or MAILGUN_DOMAIN in .env)")
    p_list.add_argument("--to", metavar="EMAIL",
                        help="Filter to a specific recipient email; if --domain is omitted, infer it from this address")
    p_list.add_argument("--limit", type=int, default=25,
                        help="Max messages to show (default: 25)")

    # read
    p_read = sub.add_parser("read", help="Read full content of a specific message")
    p_read.add_argument("--domain", required=True, help="Domain the message was received on")
    p_read.add_argument("--key", required=True, help="Storage key from 'list' output")

    # download-attachments
    p_download = sub.add_parser("download-attachments", help="Download attachments for a specific inbound message")
    p_download.add_argument("--domain", required=True, help="Domain the message was received on")
    p_download.add_argument("--key", required=True, help="Storage key from 'list' output")
    p_download.add_argument("--out-dir", required=True, help="Directory to write downloaded attachments into")

    return parser


def main() -> None:
    env = load_env()
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "domains":
        cmd_domains(args, env)
    elif args.command == "send":
        cmd_send(args, env)
    elif args.command == "list":
        cmd_list(args, env)
    elif args.command == "read":
        cmd_read(args, env)
    elif args.command == "download-attachments":
        cmd_download_attachments(args, env)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
