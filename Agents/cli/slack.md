## Slack Workspace Management

Post messages, read channel history, reply to threads, manage reactions, look up users, upload files, pin messages, search, and more — all from the command line using `Agents/cli/slack.py`.

**Important:** Uses `curl` under the hood (consistent with other CLI tools in this repo).

### Prerequisites

- `SLACK_BOT_TOKEN` must be set in the root `.env` file (Bot User OAuth Token, starts with `xoxb-`)
- `SLACK_USER_TOKEN` (optional) for search commands — User OAuth Token, starts with `xoxp-`
- `pip install python-dotenv` (already in project)
- `curl` must be available on the system

#### Setting up the Slack App

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name it (e.g. "Platform Agent") and pick your workspace
3. Go to **App Home** → ensure the bot display name is set
4. Go to **OAuth & Permissions** → add these **Bot Token Scopes**:
   - `channels:read` — List public channels
   - `channels:history` — Read messages in public channels
   - `channels:join` — Join public channels
   - `groups:read` — List private channels the bot is in
   - `groups:history` — Read messages in private channels
   - `chat:write` — Post messages
   - `chat:write.public` — Post to channels the bot isn't a member of
   - `im:read` — List DMs
   - `im:write` — Open/send DMs
   - `im:history` — Read DM history
   - `mpim:read` — List group DMs
   - `mpim:history` — Read group DM history
   - `reactions:read` — Read reactions
   - `reactions:write` — Add/remove reactions
   - `users:read` — List users
   - `users:read.email` — Look up users by email
   - `files:write` — Upload files
   - `pins:read` — List pins
   - `pins:write` — Pin/unpin messages
5. (Optional) Add **User Token Scopes** for search: `search:read`
6. Click **Install to Workspace** and authorize
7. Copy the **Bot User OAuth Token** (`xoxb-...`) and (optionally) the **User OAuth Token** (`xoxp-...`)
8. Add to your `.env`:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_USER_TOKEN=xoxp-...
   ```
9. Invite the bot to channels: open a channel → channel name → **Integrations** → **Add an App**

### Commands

All commands support `--json` for raw JSON output suitable for piping or programmatic use.

---

#### auth-test — Verify token and show bot identity

```bash
python Agents/cli/slack.py auth-test
```

---

### Channel Management

#### channels — List channels

```bash
# Public + private channels (default):
python Agents/cli/slack.py channels

# All channel types:
python Agents/cli/slack.py channels --type all

# Only public:
python Agents/cli/slack.py channels --type public

# Only DMs:
python Agents/cli/slack.py channels --type dm

# Include archived, limit results:
python Agents/cli/slack.py channels --include-archived --limit 50

# JSON output:
python Agents/cli/slack.py channels --json
```

#### channel — Get channel info

```bash
python Agents/cli/slack.py channel CHANNEL_ID
python Agents/cli/slack.py channel CHANNEL_ID --json
```

#### join — Join a public channel

```bash
python Agents/cli/slack.py join CHANNEL_ID
```

---

### Messages

#### history — Fetch message history

```bash
# Latest 25 messages (default):
python Agents/cli/slack.py history CHANNEL_ID

# More messages:
python Agents/cli/slack.py history CHANNEL_ID --limit 100

# Time range (Unix timestamps):
python Agents/cli/slack.py history CHANNEL_ID --oldest 1713100000 --latest 1713200000

# JSON output:
python Agents/cli/slack.py history CHANNEL_ID --json
```

#### thread — Fetch thread replies

```bash
python Agents/cli/slack.py thread CHANNEL_ID THREAD_TS
python Agents/cli/slack.py thread CHANNEL_ID 1713123456.789012 --limit 100
```

#### post — Post a message

```bash
# Simple message:
python Agents/cli/slack.py post CHANNEL_ID --text "Hello from the agent"

# Reply in a thread:
python Agents/cli/slack.py post CHANNEL_ID --text "Threaded reply" --thread-ts 1713123456.789012

# With Block Kit:
python Agents/cli/slack.py post CHANNEL_ID --text "fallback" --blocks '[{"type":"section","text":{"type":"mrkdwn","text":"*Bold* message"}}]'
```

#### update — Update an existing message

```bash
python Agents/cli/slack.py update CHANNEL_ID MESSAGE_TS --text "Updated text"
```

#### delete — Delete a message

```bash
python Agents/cli/slack.py delete CHANNEL_ID MESSAGE_TS
```

#### schedule — Schedule a message

```bash
# post_at is a Unix timestamp:
python Agents/cli/slack.py schedule CHANNEL_ID --text "Reminder!" --time 1713200000

# Schedule in a thread:
python Agents/cli/slack.py schedule CHANNEL_ID --text "Follow up" --time 1713200000 --thread-ts 1713123456.789012
```

#### permalink — Get permalink URL for a message

```bash
python Agents/cli/slack.py permalink CHANNEL_ID MESSAGE_TS
```

---

### Reactions

#### react — Add a reaction

```bash
python Agents/cli/slack.py react CHANNEL_ID MESSAGE_TS --emoji thumbsup
```

#### unreact — Remove a reaction

```bash
python Agents/cli/slack.py unreact CHANNEL_ID MESSAGE_TS --emoji thumbsup
```

---

### Users

#### users — List workspace users

```bash
python Agents/cli/slack.py users
python Agents/cli/slack.py users --include-bots --limit 50
python Agents/cli/slack.py users --json
```

#### user — Get user profile

```bash
python Agents/cli/slack.py user USER_ID
python Agents/cli/slack.py user USER_ID --json
```

#### user-by-email — Look up user by email

```bash
python Agents/cli/slack.py user-by-email someone@example.com
```

---

### Direct Messages

#### dm — Send a direct message

```bash
python Agents/cli/slack.py dm USER_ID --text "Hey, checking in on the outreach task"
```

---

### Files

#### upload — Upload a file to a channel

```bash
python Agents/cli/slack.py upload CHANNEL_ID --file ./report.pdf
python Agents/cli/slack.py upload CHANNEL_ID --file ./data.csv --title "Q2 Data" --comment "Here's the latest export"
python Agents/cli/slack.py upload CHANNEL_ID --file ./notes.txt --thread-ts 1713123456.789012
```

---

### Pins

#### pins — List pinned items

```bash
python Agents/cli/slack.py pins CHANNEL_ID
```

#### pin — Pin a message

```bash
python Agents/cli/slack.py pin CHANNEL_ID MESSAGE_TS
```

#### unpin — Unpin a message

```bash
python Agents/cli/slack.py unpin CHANNEL_ID MESSAGE_TS
```

---

### Search (requires SLACK_USER_TOKEN)

#### search — Search messages

```bash
python Agents/cli/slack.py search "onboarding checklist"
python Agents/cli/slack.py search "from:@paul deployment" --limit 10
python Agents/cli/slack.py search "in:#general budget" --sort timestamp --sort-dir desc
python Agents/cli/slack.py search "has:link api" --json
```

**Note:** The `search` command requires `SLACK_USER_TOKEN` (user token with `search:read` scope). Bot tokens cannot use the search API.

---

### Agent Usage Examples

Read a workstream summary and post it to Slack:
```bash
# Get workstream tasks
TASKS=$(orc workstream get WS_ID)

# Post summary to a channel
python Agents/cli/slack.py post C0123CHANNEL --text "Workstream update: $TASKS"
```

Monitor a channel and reply to a specific thread:
```bash
# Read recent messages
python Agents/cli/slack.py history C0123CHANNEL --limit 5 --json

# Reply in a thread
python Agents/cli/slack.py post C0123CHANNEL --text "Done — PR is up" --thread-ts 1713123456.789012
```

DM a user based on email lookup:
```bash
# Find user
USER=$(python Agents/cli/slack.py user-by-email alex@example.com --json | python -c "import sys,json; print(json.load(sys.stdin)['id'])")

# Send DM
python Agents/cli/slack.py dm "$USER" --text "Hey Alex, the report is ready"
```
