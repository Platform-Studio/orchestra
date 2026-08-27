## Mailgun CLI

Send and receive email via the Mailgun v3 API.

Script: `Agents/cli/mailgun_cli.py`

Uses only Python stdlib — no extra packages required beyond `python-dotenv` (already in the project venv).

---

### Auth (.env)

```bash
MAILGUN_API_KEY=key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional defaults (used when --domain / --from are not passed)
MAILGUN_DOMAIN=mg.example.com
MAILGUN_FROM_EMAIL=no-reply@mg.example.com
MAILGUN_FROM_NAME=Agent
```

---

### List configured domains

Shows all domains registered in your Mailgun account. Any `send` or `list` call on an unregistered domain will error immediately.

```bash
python Agents/cli/mailgun_cli.py domains
python Agents/cli/mailgun_cli.py --json domains
```

---

### Create or inspect a domain

Creates the domain when it is absent. If it is already registered, returns its
current configuration. JSON output includes the exact sending and receiving DNS
records required by Mailgun.

```bash
python Agents/cli/mailgun_cli.py create-domain vibesold.com
python Agents/cli/mailgun_cli.py --json create-domain vibesold.com
```

---

### Sending email

```bash
# Plain text (domain + from defaults from .env)
python Agents/cli/mailgun_cli.py send \
  --to alice@example.com \
  --subject "Hello" \
  --text "Hi there"

# Explicit domain + from
python Agents/cli/mailgun_cli.py send \
  --domain mg.example.com \
  --from "Alex Example <jb@example.com>" \
  --to alice@example.com \
  --subject "Hello" \
  --text "Hi there"

# HTML + plain text
python Agents/cli/mailgun_cli.py send \
  --domain mg.example.com \
  --to alice@example.com \
  --subject "Update" \
  --text "See the report." \
  --html "<p>See the <strong>report</strong>.</p>"

# Multiple recipients, CC, BCC
python Agents/cli/mailgun_cli.py send \
  --domain mg.example.com \
  --to alice@example.com --to bob@example.com \
  --cc manager@example.com \
  --bcc audit@example.com \
  --subject "Team update" \
  --text "Hi team"

# With attachments (multiple --attach supported)
python Agents/cli/mailgun_cli.py send \
  --domain mg.example.com \
  --to alice@example.com \
  --subject "Report" \
  --text "See attached." \
  --attach ./report.pdf \
  --attach ./data.csv

# Send as a threaded reply using message IDs from the earlier email
python Agents/cli/mailgun_cli.py send \
  --domain mg.example.com \
  --to alice@example.com \
  --subject "Re: Hello" \
  --text "Following up here." \
  --in-reply-to "<parent@example.com>" \
  --references "<root@example.com>" \
  --references "<parent@example.com>"

# JSON output (shows Mailgun message ID)
python Agents/cli/mailgun_cli.py --json send \
  --domain mg.example.com \
  --to alice@example.com \
  --subject "Hello" --text "Hi" \
```

Reply threading notes:
- `--in-reply-to` sets the outbound `In-Reply-To` header.
- `--references` sets the outbound `References` header; repeat the flag to build a full thread chain.
- These are optional. Normal sends do not need them.

---

### Receiving / reading inbound email

**List recent messages** — shows the 25 most recent inbound accepted events with sender, subject, and the storage key needed to fetch the full message.

```bash
python Agents/cli/mailgun_cli.py list --domain mg.example.com
python Agents/cli/mailgun_cli.py list --domain mg.example.com --limit 10
python Agents/cli/mailgun_cli.py --json list --domain mg.example.com

# Filter to a specific recipient address (domain inferred if omitted)
python Agents/cli/mailgun_cli.py list --to build@mail.example.com
python Agents/cli/mailgun_cli.py --json list --to build@mail.example.com
```

Example output:
```
Recent inbound messages for mg.example.com (up to 25):
#    From                                     Subject                                            Storage Key
----------------------------------------------------------------------------------------------------------------------------------
1    Alex Example <jb@example.com>       Test                                               <STORAGE_KEY>
```

**Read full message** — fetches headers, body, and attachment metadata for a specific message using the storage key from `list`.

```bash
python Agents/cli/mailgun_cli.py read \
  --domain mg.example.com \
  --key <STORAGE_KEY>

python Agents/cli/mailgun_cli.py read \
  --domain mg.example.com \
  --key <STORAGE_KEY> \
  
python Agents/cli/mailgun_cli.py --json read \
  --domain mg.example.com \
  --key <STORAGE_KEY>
```

Example output:
```
From:     Alex Example <jb@example.com>
To:       substack@mg.example.com
Subject:  Test
Date:     Thu, 30 Apr 2026 ...

Body:
------------------------------------------------------------
Alex Example
```

**Download attachments** — saves any downloadable attachment payloads for a stored inbound message.

```bash
python Agents/cli/mailgun_cli.py download-attachments \
  --domain mg.example.com \
  --key <STORAGE_KEY> \
  --out-dir ./tmp/mailgun-downloads

python Agents/cli/mailgun_cli.py --json download-attachments \
  --domain mg.example.com \
  --key <STORAGE_KEY> \
  --out-dir ./tmp/mailgun-downloads
```

---

### Notes

- **Domain validation**: All commands that take `--domain` validate it against your registered Mailgun domains before proceeding. An unregistered domain returns a clear error listing valid options.
- **Recipient filtering**: `list --to someone@example.com` filters results to messages addressed to that specific recipient. If `--domain` is omitted, the CLI infers the domain from the recipient address.
- **Inbound storage**: Mailgun stores inbound messages for `message_ttl` seconds (default 3 days / 259200s). Messages older than this will return a 404 on `read`.
- **Attachments**: Sending attachments uses multipart form upload — no size limit beyond Mailgun's 25 MB message cap.
- **Storage region**: Currently hardcoded to `us-west1` storage endpoint (matching our account). If you move to an EU Mailgun account, update the storage URL in `cmd_read`.
