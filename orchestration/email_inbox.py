"""Mailgun-backed inbound email polling helpers for orchestration triggers."""

from __future__ import annotations

import json
import os
from email.utils import getaddresses
from typing import Any


def _mailgun_request(url: str, api_key: str) -> dict[str, Any]:
    import http.client
    import urllib.parse
    from base64 import b64encode

    auth_header = "Basic " + b64encode(f"api:{api_key}".encode()).decode()

    parsed = urllib.parse.urlparse(url)
    path = parsed.path
    if parsed.query:
        path += "?" + parsed.query

    conn = http.client.HTTPSConnection(parsed.netloc)
    conn.request("GET", path, headers={"Authorization": auth_header})
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    conn.close()

    if resp.status >= 400:
        raise RuntimeError(f"Mailgun API returned {resp.status}: {raw}")

    if not raw.strip():
        return {}
    return json.loads(raw)


def _get_api_key() -> str:
    api_key = os.getenv("MAILGUN_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("MAILGUN_API_KEY is not set")
    return api_key


def _recipient_domain(recipient: str) -> str:
    if not recipient or "@" not in recipient:
        raise ValueError("recipient must be an email address")
    return recipient.rsplit("@", 1)[1].strip().lower()


def _normalized_headers(raw_headers: dict[str, Any] | None) -> dict[str, str]:
    headers = {}
    for key, value in (raw_headers or {}).items():
        normalized_key = str(key or "").strip().lower()
        if normalized_key:
            headers[normalized_key] = str(value or "")
    return headers


def _item_has_recipient(item: dict[str, Any], recipient: str) -> bool:
    wanted = recipient.strip().lower()
    headers = _normalized_headers(item.get("message", {}).get("headers", {}))

    values = []
    for key in ("to", "cc", "bcc"):
        value = headers.get(key)
        if value:
            values.append(value)

    if not values:
        return False

    addresses = [addr.lower() for _, addr in getaddresses(values) if addr]
    return wanted in addresses


def _body_from_message(message: dict[str, Any]) -> str:
    for key in ("stripped-text", "body-plain", "body-html", "body"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _attachments_from_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = []
    for raw in message.get("attachments", []) or []:
        if not isinstance(raw, dict):
            continue
        attachment = {
            "name": str(raw.get("name") or raw.get("filename") or "").strip(),
            "content_type": str(raw.get("content-type") or raw.get("content_type") or "").strip(),
            "size": raw.get("size"),
        }
        url = str(raw.get("url") or "").strip()
        if url:
            attachment["url"] = url
        attachments.append(attachment)
    return attachments


def list_new_thread_messages(recipient: str, limit: int = 100) -> list[dict[str, Any]]:
    """Return deduplicated inbound email threads with full message bodies.

    A "new_thread" is an inbound accepted message for the recipient that does
    not contain `In-Reply-To` or `References` headers.
    """

    api_key = _get_api_key()
    domain = _recipient_domain(recipient)
    events_url = f"https://api.mailgun.net/v3/{domain}/events?event=accepted&limit={int(limit)}"
    resp = _mailgun_request(events_url, api_key)

    seen_keys: set[str] = set()
    messages: list[dict[str, Any]] = []

    for item in resp.get("items", []):
        storage_key = item.get("storage", {}).get("key") or item.get("id")
        if not storage_key or storage_key in seen_keys:
            continue
        seen_keys.add(storage_key)

        if not _item_has_recipient(item, recipient):
            continue

        headers = _normalized_headers(item.get("message", {}).get("headers", {}))
        if headers.get("in-reply-to") or headers.get("references"):
            continue

        message_url = f"https://storage-us-west1.api.mailgun.net/v3/domains/{domain}/messages/{storage_key}"
        full_message = _mailgun_request(message_url, api_key)

        messages.append({
            "storage_key": storage_key,
            "timestamp": item.get("timestamp"),
            "from": full_message.get("from") or headers.get("from") or "",
            "to": recipient,
            "subject": full_message.get("subject") or headers.get("subject") or "",
            "date": full_message.get("Date") or headers.get("date") or "",
            "body": _body_from_message(full_message),
            "attachments": _attachments_from_message(full_message),
        })

    return messages