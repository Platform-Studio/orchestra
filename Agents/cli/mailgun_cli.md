## Mailgun CLI

Send and receive email via the Mailgun v3 API.

Script: `Agents/cli/mailgun_cli.py`

Uses only Python stdlib — no extra packages required beyond `python-dotenv` (already in the project venv).

---

### Auth (.env)

```bash
MAILGUN_API_KEY=key-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Optional defaults (used when --domain / --from are not passed)
MAILGUN_DOMAIN=mg.hirescout.us
MAILGUN_FROM_EMAIL=no-reply@mg.hirescout.us
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

### Sending email

```bash
# Plain text (domain + from defaults from .env)
python Agents/cli/mailgun_cli.py send \
  --to alice@example.com \
  --subject "Hello" \
  --text "Hi there"

# Explicit domain + from
python Agents/cli/mailgun_cli.py send \
  --domain mg.hirescout.us \
  --from "Jeremy Burton <jb@platformstud.io>" \
  --to alice@example.com \
  --subject "Hello" \
  --text "Hi there"

# HTML + plain text
python Agents/cli/mailgun_cli.py send \
  --domain mg.hirescout.us \
  --to alice@example.com \
  --subject "Update" \
  --text "See the report." \
  --html "<p>See the <strong>report</strong>.</p>"

# Multiple recipients, CC, BCC
python Agents/cli/mailgun_cli.py send \
  --domain mg.hirescout.us \
  --to alice@example.com --to bob@example.com \
  --cc manager@example.com \
  --bcc audit@example.com \
  --subject "Team update" \
  --text "Hi team"

# With attachments (multiple --attach supported)
python Agents/cli/mailgun_cli.py send \
  --domain mg.hirescout.us \
  --to alice@example.com \
  --subject "Report" \
  --text "See attached." \
  --attach ./report.pdf \
  --attach ./data.csv

# Send as a threaded reply using message IDs from the earlier email
python Agents/cli/mailgun_cli.py send \
  --domain mg.hirescout.us \
  --to alice@example.com \
  --subject "Re: Hello" \
  --text "Following up here." \
  --in-reply-to "<parent@example.com>" \
  --references "<root@example.com>" \
  --references "<parent@example.com>"

# JSON output (shows Mailgun message ID)
python Agents/cli/mailgun_cli.py --json send \
  --domain mg.hirescout.us \
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
python Agents/cli/mailgun_cli.py list --domain mg.hirescout.us
python Agents/cli/mailgun_cli.py list --domain mg.hirescout.us --limit 10
python Agents/cli/mailgun_cli.py --json list --domain mg.hirescout.us

# Filter to a specific recipient address (domain inferred if omitted)
python Agents/cli/mailgun_cli.py list --to build@guild.platformstud.io
python Agents/cli/mailgun_cli.py --json list --to build@guild.platformstud.io
```

Example output:
```
Recent inbound messages for mg.hirescout.us (up to 25):
#    From                                     Subject                                            Storage Key
----------------------------------------------------------------------------------------------------------------------------------
1    Jeremy Burton <jb@platformstud.io>       Test                                               BAABAQU3_nLx9y4Rxt5HqpOT...
```

**Read full message** — fetches headers, body, and attachment metadata for a specific message using the storage key from `list`.

```bash
python Agents/cli/mailgun_cli.py read \
  --domain mg.hirescout.us \
  --key BAABAQU3_nLx9y4Rxt5HqpOTir_jXKomaQ

python Agents/cli/mailgun_cli.py read \
  --domain mg.hirescout.us \
  --key BAABAQU3_nLx9y4Rxt5HqpOTir_jXKomaQ \
  
python Agents/cli/mailgun_cli.py --json read \
  --domain mg.hirescout.us \
  --key BAABAQU3_nLx9y4Rxt5HqpOTir_jXKomaQ
```

Example output:
```
From:     Jeremy Burton <jb@platformstud.io>
To:       substack@mg.hirescout.us
Subject:  Test
Date:     Thu, 30 Apr 2026 ...

Body:
------------------------------------------------------------
Jeremy Burton
```

**Download attachments** — saves any downloadable attachment payloads for a stored inbound message.

```bash
python Agents/cli/mailgun_cli.py download-attachments \
  --domain mg.hirescout.us \
  --key BAABAQU3_nLx9y4Rxt5HqpOTir_jXKomaQ \
  --out-dir ./tmp/mailgun-downloads

python Agents/cli/mailgun_cli.py --json download-attachments \
  --domain mg.hirescout.us \
  --key BAABAQU3_nLx9y4Rxt5HqpOTir_jXKomaQ \
  --out-dir ./tmp/mailgun-downloads
```

---

### Notes

- **Domain validation**: All commands that take `--domain` validate it against your registered Mailgun domains before proceeding. An unregistered domain returns a clear error listing valid options.
- **Recipient filtering**: `list --to someone@example.com` filters results to messages addressed to that specific recipient. If `--domain` is omitted, the CLI infers the domain from the recipient address.
- **Inbound storage**: Mailgun stores inbound messages for `message_ttl` seconds (default 3 days / 259200s). Messages older than this will return a 404 on `read`.
- **Attachments**: Sending attachments uses multipart form upload — no size limit beyond Mailgun's 25 MB message cap.
- **Storage region**: Currently hardcoded to `us-west1` storage endpoint (matching our account). If you move to an EU Mailgun account, update the storage URL in `cmd_read`.
