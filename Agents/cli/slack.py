#!/usr/bin/env python3
"""
Slack workspace management CLI for autonomous agents.

Post messages, read channel history, reply to threads, manage reactions,
look up users, upload files, pin messages, and search — all via the
Slack Web API.

Uses curl subprocess under the hood for consistency with other CLI tools.

Usage:
    # Verify token:
    python slack.py auth-test

    # List channels:
    python slack.py channels

    # Read channel history:
    python slack.py history CHANNEL_ID --limit 20

    # Post a message:
    python slack.py post CHANNEL_ID --text "Hello from the agent"

    # Reply in a thread:
    python slack.py post CHANNEL_ID --text "Threaded reply" --thread-ts 1234567890.123456

    # Send a DM:
    python slack.py dm USER_ID --text "Hey there"

Environment variables (loaded from .env):
    SLACK_BOT_TOKEN  - Bot User OAuth Token (xoxb-...)
    SLACK_USER_TOKEN - (Optional) User OAuth Token (xoxp-...) for search
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (two levels up from Agents/cli/)
load_dotenv(Path(__file__).parent.parent.parent / ".env")

BASE_URL = "https://slack.com/api"


def get_bot_token():
    """Load and validate Slack bot token from environment."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("Error: SLACK_BOT_TOKEN not set in .env", file=sys.stderr)
        sys.exit(1)
    return token


def get_user_token():
    """Load Slack user token from environment (optional, for search)."""
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        print("Error: SLACK_USER_TOKEN not set in .env (required for search)", file=sys.stderr)
        sys.exit(1)
    return token


def api_request(method_name, params=None, body=None, token=None):
    """Make an API request to Slack using curl.

    Slack Web API methods all use POST with application/x-www-form-urlencoded
    or application/json. We use JSON body for write methods and query params
    for read methods.

    Returns parsed JSON response or exits on error.
    """
    if token is None:
        token = get_bot_token()
    url = f"{BASE_URL}/{method_name}"

    cmd = [
        "curl", "-s", "--max-time", "30",
        "--request", "POST",
        "--url", url,
        "--header", f"Authorization: Bearer {token}",
        "--header", "Content-Type: application/json; charset=utf-8",
    ]

    payload = params or body or {}
    if payload:
        cmd.extend(["--data", json.dumps(payload)])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"Error: curl failed (exit {result.returncode}): {result.stderr}", file=sys.stderr)
        sys.exit(1)

    if not result.stdout.strip():
        print("Error: empty response from API", file=sys.stderr)
        sys.exit(1)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"Error: invalid JSON response:\n{result.stdout[:500]}", file=sys.stderr)
        sys.exit(1)

    if not data.get("ok"):
        err = data.get("error", "unknown error")
        needed = data.get("needed", "")
        provided = data.get("provided", "")
        msg = f"Slack API error: {err}"
        if needed:
            msg += f" (needed: {needed}, provided: {provided})"
        print(msg, file=sys.stderr)
        sys.exit(1)

    return data


def api_upload_file(channels, filepath, title=None, initial_comment=None, thread_ts=None):
    """Upload a file using Slack's files.getUploadURLExternal + files.completeUploadExternal flow."""
    token = get_bot_token()
    file_path = Path(filepath)
    if not file_path.exists():
        print(f"Error: file not found: {filepath}", file=sys.stderr)
        sys.exit(1)

    file_size = file_path.stat().st_size
    filename = file_path.name

    # Step 1: Get upload URL
    data = api_request("files.getUploadURLExternal", {
        "filename": filename,
        "length": file_size,
    })
    upload_url = data["upload_url"]
    file_id = data["file_id"]

    # Step 2: Upload file content to the URL
    cmd = [
        "curl", "-s", "--max-time", "60",
        "--request", "POST",
        "--url", upload_url,
        "--form", f"file=@{filepath}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error: file upload failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Step 3: Complete the upload
    files_payload = [{"id": file_id}]
    if title:
        files_payload[0]["title"] = title

    complete_body = {"files": files_payload}
    if channels:
        complete_body["channel_id"] = channels
    if initial_comment:
        complete_body["initial_comment"] = initial_comment
    if thread_ts:
        complete_body["thread_ts"] = thread_ts

    return api_request("files.completeUploadExternal", complete_body)


# ─────────────────────────────────────────────
#  Formatting helpers
# ─────────────────────────────────────────────

def format_channel(ch):
    """Format a channel for display."""
    prefix = "#" if not ch.get("is_im") and not ch.get("is_mpim") else "@"
    name = ch.get("name") or ch.get("id", "?")
    purpose = (ch.get("purpose") or {}).get("value", "")
    members = ch.get("num_members", "")
    parts = [f"  {ch['id']}  {prefix}{name}"]
    if members:
        parts[0] += f"  ({members} members)"
    if ch.get("is_private"):
        parts[0] += "  [private]"
    if ch.get("is_archived"):
        parts[0] += "  [archived]"
    if purpose:
        parts.append(f"    Purpose: {purpose[:120]}")
    return "\n".join(parts)


def format_message(msg):
    """Format a message for display."""
    user = msg.get("user") or msg.get("bot_id") or "unknown"
    ts = msg.get("ts", "")
    text = msg.get("text", "")[:300]
    thread = msg.get("thread_ts")
    reply_count = msg.get("reply_count", 0)

    parts = [f"  [{ts}] <{user}> {text}"]
    if reply_count:
        parts.append(f"    ({reply_count} replies, thread_ts: {thread})")
    if msg.get("reactions"):
        rxns = ", ".join(f":{r['name']}:({r['count']})" for r in msg["reactions"])
        parts.append(f"    Reactions: {rxns}")
    if msg.get("files"):
        for f in msg["files"]:
            parts.append(f"    File: {f.get('name', '?')} ({f.get('mimetype', '?')})")
    return "\n".join(parts)


def format_user(u):
    """Format a user for display."""
    profile = u.get("profile", {})
    name = profile.get("real_name") or u.get("real_name") or u.get("name", "?")
    display = profile.get("display_name", "")
    email = profile.get("email", "")
    title = profile.get("title", "")

    parts = [f"  {u['id']}  {name}"]
    if display:
        parts[0] += f"  (@{display})"
    if u.get("is_bot"):
        parts[0] += "  [bot]"
    if u.get("deleted"):
        parts[0] += "  [deactivated]"
    if title:
        parts.append(f"    Title: {title}")
    if email:
        parts.append(f"    Email: {email}")
    return "\n".join(parts)


# ─────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────

def cmd_auth_test(args):
    """Verify token and show bot identity."""
    data = api_request("auth.test")
    if args.json:
        print(json.dumps(data, indent=2))
        return
    print(f"Team:    {data.get('team', '?')} ({data.get('team_id', '')})")
    print(f"Bot:     {data.get('user', '?')} ({data.get('user_id', '')})")
    print(f"URL:     {data.get('url', '')}")


def cmd_channels(args):
    """List channels."""
    params = {"limit": args.limit, "exclude_archived": not args.include_archived}
    types_list = []
    if args.type == "all":
        types_list = ["public_channel", "private_channel", "mpim", "im"]
    elif args.type == "public":
        types_list = ["public_channel"]
    elif args.type == "private":
        types_list = ["private_channel"]
    elif args.type == "dm":
        types_list = ["im"]
    elif args.type == "group":
        types_list = ["mpim"]
    else:
        types_list = ["public_channel", "private_channel"]

    params["types"] = ",".join(types_list)

    all_channels = []
    while True:
        data = api_request("conversations.list", params)
        all_channels.extend(data.get("channels", []))
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor or len(all_channels) >= args.limit:
            break
        params["cursor"] = cursor

    all_channels = all_channels[:args.limit]

    if args.json:
        print(json.dumps(all_channels, indent=2))
        return

    if not all_channels:
        print("No channels found.")
        return

    print(f"Channels ({len(all_channels)}):")
    for ch in all_channels:
        print(format_channel(ch))


def cmd_channel(args):
    """Get channel info."""
    data = api_request("conversations.info", {"channel": args.channel_id})
    ch = data["channel"]
    if args.json:
        print(json.dumps(ch, indent=2))
        return
    print(format_channel(ch))
    topic = (ch.get("topic") or {}).get("value", "")
    if topic:
        print(f"    Topic: {topic}")
    print(f"    Created: {ch.get('created', '?')}")


def cmd_join(args):
    """Join a public channel."""
    data = api_request("conversations.join", {"channel": args.channel_id})
    if args.json:
        print(json.dumps(data, indent=2))
        return
    ch = data.get("channel", {})
    print(f"Joined #{ch.get('name', args.channel_id)}")


def cmd_history(args):
    """Fetch message history for a channel."""
    params = {"channel": args.channel_id, "limit": args.limit}
    if args.oldest:
        params["oldest"] = args.oldest
    if args.latest:
        params["latest"] = args.latest

    data = api_request("conversations.history", params)
    messages = data.get("messages", [])

    if args.json:
        print(json.dumps(messages, indent=2))
        return

    if not messages:
        print("No messages found.")
        return

    # Messages come newest-first, reverse for chronological
    messages.reverse()
    print(f"Messages ({len(messages)}):")
    for msg in messages:
        print(format_message(msg))


def cmd_thread(args):
    """Fetch thread replies."""
    params = {"channel": args.channel_id, "ts": args.thread_ts, "limit": args.limit}
    data = api_request("conversations.replies", params)
    messages = data.get("messages", [])

    if args.json:
        print(json.dumps(messages, indent=2))
        return

    if not messages:
        print("No replies found.")
        return

    print(f"Thread ({len(messages)} messages):")
    for msg in messages:
        print(format_message(msg))


def cmd_post(args):
    """Post a message to a channel."""
    body = {"channel": args.channel_id, "text": args.text}
    if args.thread_ts:
        body["thread_ts"] = args.thread_ts
    if args.blocks:
        body["blocks"] = json.loads(args.blocks)
    if args.unfurl_links is not None:
        body["unfurl_links"] = args.unfurl_links
    if args.unfurl_media is not None:
        body["unfurl_media"] = args.unfurl_media

    data = api_request("chat.postMessage", body)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"Message posted: ts={data.get('ts', '?')} channel={data.get('channel', '?')}")


def cmd_update(args):
    """Update an existing message."""
    body = {"channel": args.channel_id, "ts": args.ts, "text": args.text}
    if args.blocks:
        body["blocks"] = json.loads(args.blocks)

    data = api_request("chat.update", body)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"Message updated: ts={data.get('ts', '?')}")


def cmd_delete(args):
    """Delete a message."""
    data = api_request("chat.delete", {"channel": args.channel_id, "ts": args.ts})

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"Message deleted: ts={data.get('ts', '?')}")


def cmd_react(args):
    """Add a reaction to a message."""
    data = api_request("reactions.add", {
        "channel": args.channel_id,
        "timestamp": args.ts,
        "name": args.emoji,
    })

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"Reaction :{args.emoji}: added")


def cmd_unreact(args):
    """Remove a reaction from a message."""
    data = api_request("reactions.remove", {
        "channel": args.channel_id,
        "timestamp": args.ts,
        "name": args.emoji,
    })

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"Reaction :{args.emoji}: removed")


def cmd_users(args):
    """List workspace users."""
    params = {"limit": args.limit}
    all_users = []
    while True:
        data = api_request("users.list", params)
        all_users.extend(data.get("members", []))
        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor or len(all_users) >= args.limit:
            break
        params["cursor"] = cursor

    all_users = all_users[:args.limit]

    if args.json:
        print(json.dumps(all_users, indent=2))
        return

    if not all_users:
        print("No users found.")
        return

    print(f"Users ({len(all_users)}):")
    for u in all_users:
        if not args.include_bots and u.get("is_bot"):
            continue
        if u.get("id") == "USLACKBOT":
            continue
        print(format_user(u))


def cmd_user(args):
    """Get user profile."""
    data = api_request("users.info", {"user": args.user_id})
    u = data["user"]

    if args.json:
        print(json.dumps(u, indent=2))
        return

    print(format_user(u))


def cmd_user_by_email(args):
    """Look up a user by email."""
    data = api_request("users.lookupByEmail", {"email": args.email})
    u = data["user"]

    if args.json:
        print(json.dumps(u, indent=2))
        return

    print(format_user(u))


def cmd_dm(args):
    """Open a DM and send a message."""
    # Open/find the DM channel
    open_data = api_request("conversations.open", {"users": args.user_id})
    dm_channel = open_data["channel"]["id"]

    # Post the message
    body = {"channel": dm_channel, "text": args.text}
    data = api_request("chat.postMessage", body)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"DM sent: ts={data.get('ts', '?')} channel={dm_channel}")


def cmd_upload(args):
    """Upload a file to a channel."""
    data = api_upload_file(
        channels=args.channel_id,
        filepath=args.file,
        title=args.title,
        initial_comment=args.comment,
        thread_ts=args.thread_ts,
    )

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print("File uploaded successfully.")


def cmd_pins(args):
    """List pinned items in a channel."""
    data = api_request("pins.list", {"channel": args.channel_id})
    items = data.get("items", [])

    if args.json:
        print(json.dumps(items, indent=2))
        return

    if not items:
        print("No pinned items.")
        return

    print(f"Pinned items ({len(items)}):")
    for item in items:
        msg = item.get("message", {})
        print(format_message(msg))


def cmd_pin(args):
    """Pin a message."""
    data = api_request("pins.add", {"channel": args.channel_id, "timestamp": args.ts})

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print("Message pinned.")


def cmd_unpin(args):
    """Unpin a message."""
    data = api_request("pins.remove", {"channel": args.channel_id, "timestamp": args.ts})

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print("Message unpinned.")


def cmd_permalink(args):
    """Get a permalink URL for a message."""
    data = api_request("chat.getPermalink", {
        "channel": args.channel_id,
        "message_ts": args.ts,
    })

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(data.get("permalink", "?"))


def cmd_schedule(args):
    """Schedule a message."""
    body = {
        "channel": args.channel_id,
        "text": args.text,
        "post_at": int(args.time),
    }
    if args.thread_ts:
        body["thread_ts"] = args.thread_ts

    data = api_request("chat.scheduleMessage", body)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    print(f"Message scheduled: id={data.get('scheduled_message_id', '?')} post_at={data.get('post_at', '?')}")


def cmd_search(args):
    """Search messages (requires SLACK_USER_TOKEN)."""
    token = get_user_token()
    params = {"query": args.query, "count": args.limit}
    if args.sort:
        params["sort"] = args.sort
    if args.sort_dir:
        params["sort_dir"] = args.sort_dir

    data = api_request("search.messages", params, token=token)
    matches = data.get("messages", {}).get("matches", [])

    if args.json:
        print(json.dumps(matches, indent=2))
        return

    if not matches:
        print("No messages found.")
        return

    total = data.get("messages", {}).get("total", len(matches))
    print(f"Search results ({len(matches)} of {total}):")
    for msg in matches:
        ch_name = msg.get("channel", {}).get("name", "?")
        user = msg.get("user") or msg.get("username", "?")
        ts = msg.get("ts", "")
        text = msg.get("text", "")[:200]
        print(f"  [{ts}] #{ch_name} <{user}> {text}")
        permalink = msg.get("permalink", "")
        if permalink:
            print(f"    {permalink}")


# ─────────────────────────────────────────────
#  CLI argument parser
# ─────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        description="Slack workspace management CLI for autonomous agents.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # auth-test
    p = sub.add_parser("auth-test", help="Verify token and show bot identity")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # channels
    p = sub.add_parser("channels", help="List channels")
    p.add_argument("--type", default="default", choices=["all", "public", "private", "dm", "group", "default"],
                    help="Channel type filter (default: public + private)")
    p.add_argument("--limit", type=int, default=100, help="Max channels to return")
    p.add_argument("--include-archived", action="store_true", help="Include archived channels")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # channel
    p = sub.add_parser("channel", help="Get channel info")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # join
    p = sub.add_parser("join", help="Join a public channel")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # history
    p = sub.add_parser("history", help="Fetch message history")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("--limit", type=int, default=25, help="Max messages (default: 25)")
    p.add_argument("--oldest", help="Start of time range (Unix ts)")
    p.add_argument("--latest", help="End of time range (Unix ts)")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # thread
    p = sub.add_parser("thread", help="Fetch thread replies")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("thread_ts", help="Thread timestamp (ts of parent message)")
    p.add_argument("--limit", type=int, default=50, help="Max replies")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # post
    p = sub.add_parser("post", help="Post a message")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("--text", required=True, help="Message text")
    p.add_argument("--thread-ts", help="Reply in thread (parent message ts)")
    p.add_argument("--blocks", help="Block Kit JSON string")
    p.add_argument("--unfurl-links", type=bool, default=None, help="Unfurl links")
    p.add_argument("--unfurl-media", type=bool, default=None, help="Unfurl media")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # update
    p = sub.add_parser("update", help="Update a message")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("ts", help="Message timestamp to update")
    p.add_argument("--text", required=True, help="New message text")
    p.add_argument("--blocks", help="Block Kit JSON string")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # delete
    p = sub.add_parser("delete", help="Delete a message")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("ts", help="Message timestamp to delete")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # react
    p = sub.add_parser("react", help="Add a reaction")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("ts", help="Message timestamp")
    p.add_argument("--emoji", required=True, help="Emoji name (without colons)")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # unreact
    p = sub.add_parser("unreact", help="Remove a reaction")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("ts", help="Message timestamp")
    p.add_argument("--emoji", required=True, help="Emoji name (without colons)")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # users
    p = sub.add_parser("users", help="List workspace users")
    p.add_argument("--limit", type=int, default=200, help="Max users")
    p.add_argument("--include-bots", action="store_true", help="Include bot users")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # user
    p = sub.add_parser("user", help="Get user profile")
    p.add_argument("user_id", help="User ID")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # user-by-email
    p = sub.add_parser("user-by-email", help="Look up user by email")
    p.add_argument("email", help="Email address")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # dm
    p = sub.add_parser("dm", help="Send a direct message")
    p.add_argument("user_id", help="User ID to DM")
    p.add_argument("--text", required=True, help="Message text")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # upload
    p = sub.add_parser("upload", help="Upload a file")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("--file", required=True, help="Path to file")
    p.add_argument("--title", help="File title")
    p.add_argument("--comment", help="Initial comment")
    p.add_argument("--thread-ts", help="Upload in thread")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # pins
    p = sub.add_parser("pins", help="List pinned items")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # pin
    p = sub.add_parser("pin", help="Pin a message")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("ts", help="Message timestamp to pin")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # unpin
    p = sub.add_parser("unpin", help="Unpin a message")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("ts", help="Message timestamp to unpin")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # permalink
    p = sub.add_parser("permalink", help="Get permalink for a message")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("ts", help="Message timestamp")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # schedule
    p = sub.add_parser("schedule", help="Schedule a message")
    p.add_argument("channel_id", help="Channel ID")
    p.add_argument("--text", required=True, help="Message text")
    p.add_argument("--time", required=True, help="Unix timestamp to post at")
    p.add_argument("--thread-ts", help="Schedule in thread")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    # search
    p = sub.add_parser("search", help="Search messages (requires SLACK_USER_TOKEN)")
    p.add_argument("query", help="Search query")
    p.add_argument("--limit", type=int, default=20, help="Max results")
    p.add_argument("--sort", choices=["score", "timestamp"], help="Sort order")
    p.add_argument("--sort-dir", choices=["asc", "desc"], help="Sort direction")
    p.add_argument("--json", action="store_true", help="Raw JSON output")

    return parser


COMMAND_MAP = {
    "auth-test": cmd_auth_test,
    "channels": cmd_channels,
    "channel": cmd_channel,
    "join": cmd_join,
    "history": cmd_history,
    "thread": cmd_thread,
    "post": cmd_post,
    "update": cmd_update,
    "delete": cmd_delete,
    "react": cmd_react,
    "unreact": cmd_unreact,
    "users": cmd_users,
    "user": cmd_user,
    "user-by-email": cmd_user_by_email,
    "dm": cmd_dm,
    "upload": cmd_upload,
    "pins": cmd_pins,
    "pin": cmd_pin,
    "unpin": cmd_unpin,
    "permalink": cmd_permalink,
    "schedule": cmd_schedule,
    "search": cmd_search,
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    handler = COMMAND_MAP.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)
    handler(args)


if __name__ == "__main__":
    main()
