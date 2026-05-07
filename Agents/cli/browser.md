# browser.py — Persistent Browser Automation

Drive a real Google Chrome browser from the command line. Sessions persist between invocations so agents can interact with web pages step-by-step. Useful for websites that block direct HTTP requests or require JavaScript rendering.

## Setup

```bash
pip install --upgrade "playwright==1.59.0"
python -m playwright install chromium
```

If Google Chrome is installed on the machine, the tool will use it. Otherwise it falls back to Playwright's bundled Chromium.

## Architecture

The tool runs a lightweight background HTTP server that holds Playwright browser sessions in memory. The CLI is a thin client that sends commands to the server. The server auto-starts on first `open` and persists until `server-stop`.

- Server listens on `127.0.0.1` only (no external access)
- Server info stored at `/tmp/browser_server.json`
- Server logs at `/tmp/browser_server.log`
- Each session is an isolated browser context (separate cookies, storage)
- Reusable auth state lives on disk under `playwright/.auth/` and can be mapped to site keys like `linkedin`

## Commands

### Open a session

```bash
python3 Agents/cli/browser.py open https://www.example.com
# Output:
# Session: a1b2c3d4
# URL:     https://www.example.com/
# Title:   Example Domain

# Open without navigating:
python3 Agents/cli/browser.py open

# Open in headless mode (no visible window):
python3 Agents/cli/browser.py open --headless https://www.example.com

# Open with saved auth for a known site key:
python3 Agents/cli/browser.py open --site linkedin https://www.linkedin.com/feed/
```

### Reuse saved auth

Use the auth registry when multiple agents need isolated sessions against the same site. Sessions stay separate; only the stored cookies/local storage are reused.

```bash
# List saved auth states:
python3 Agents/cli/browser.py auth-list

# Check whether auth exists for a site:
python3 Agents/cli/browser.py auth-get linkedin

# After a human-assisted login, save the current session for reuse:
python3 Agents/cli/browser.py auth-save a1b2c3d4 --site linkedin --account-label primary

# Delete stale auth:
python3 Agents/cli/browser.py auth-delete linkedin
```

Typical flow:

```bash
# 1. Try to reuse auth first
python3 Agents/cli/browser.py open --site linkedin https://www.linkedin.com/feed/

# 2. If auth is missing or stale, log in manually/human-assisted in that session

# 3. Save the refreshed auth back into the registry
python3 Agents/cli/browser.py auth-save a1b2c3d4 --site linkedin --account-label primary
```

### Navigate

```bash
python3 Agents/cli/browser.py goto a1b2c3d4 https://www.google.com
```

### Get page status

```bash
python3 Agents/cli/browser.py status a1b2c3d4
# Output:
# URL:   https://www.google.com/
# Title: Google
# State: complete    (loading | interactive | complete)
```

### Read page content

```bash
# Get all visible text (preferred for reading content):
python3 Agents/cli/browser.py text a1b2c3d4

# Get text from a specific element:
python3 Agents/cli/browser.py text a1b2c3d4 --selector "main"

# Get full page HTML:
python3 Agents/cli/browser.py html a1b2c3d4

# Get HTML of a specific element:
python3 Agents/cli/browser.py html a1b2c3d4 --selector "form#login"

# Get inner HTML only:
python3 Agents/cli/browser.py html a1b2c3d4 --selector "div.results" --inner
```

### Interact with elements

```bash
# Click:
python3 Agents/cli/browser.py click a1b2c3d4 "button.submit"

# Type text into a field (clears field first):
python3 Agents/cli/browser.py type a1b2c3d4 "input#email" "user@example.com"

# Type key-by-key (for autocomplete / dynamic fields):
python3 Agents/cli/browser.py type a1b2c3d4 "input#search" "solar installer" --keys

# Press a key:
python3 Agents/cli/browser.py press a1b2c3d4 Enter
# Other keys: Tab, Escape, ArrowDown, ArrowUp, Backspace, Delete, Space

# Select dropdown option:
python3 Agents/cli/browser.py select a1b2c3d4 "select#state" "Texas"
```

### Wait for elements

```bash
# Wait for element to appear (default 30s timeout):
python3 Agents/cli/browser.py wait a1b2c3d4 "div.results"

# Custom timeout:
python3 Agents/cli/browser.py wait a1b2c3d4 "div.results" --timeout 60000
```

### Screenshot

```bash
python3 Agents/cli/browser.py screenshot a1b2c3d4
# Output: Screenshot: /tmp/browser_a1b2c3d4.png

# Custom path:
python3 Agents/cli/browser.py screenshot a1b2c3d4 --path /tmp/my_screenshot.png

# Full page:
python3 Agents/cli/browser.py screenshot a1b2c3d4 --full
```

### Run JavaScript

```bash
python3 Agents/cli/browser.py eval a1b2c3d4 "document.querySelectorAll('a').length"
# Output: 42

# Get structured data:
python3 Agents/cli/browser.py eval a1b2c3d4 "Array.from(document.querySelectorAll('h2')).map(h => h.textContent)"
```

### Session management

```bash
# List all sessions:
python3 Agents/cli/browser.py sessions

# Close a session:
python3 Agents/cli/browser.py close a1b2c3d4

# Stop the background server (closes all sessions):
python3 Agents/cli/browser.py server-stop
```

### JSON output

Add `--json` before any command for raw JSON output:

```bash
python3 Agents/cli/browser.py --json open https://example.com
# {"session": "a1b2c3d4", "url": "https://example.com/", "title": "Example Domain"}
```

## Typical Agent Workflow

### Example: Log into a website and extract data

```bash
# 1. Open session
python3 Agents/cli/browser.py open https://app.example.com/login
# Session: f3c8a1d2

# 2. Fill login form
python3 Agents/cli/browser.py type f3c8a1d2 "input[name=email]" "user@example.com"
python3 Agents/cli/browser.py type f3c8a1d2 "input[name=password]" "mypassword"
python3 Agents/cli/browser.py click f3c8a1d2 "button[type=submit]"

# 3. Wait for dashboard to load
python3 Agents/cli/browser.py wait f3c8a1d2 "div.dashboard"

# 4. Read content
python3 Agents/cli/browser.py text f3c8a1d2 --selector "div.dashboard"

# 5. Take screenshot for visual verification
python3 Agents/cli/browser.py screenshot f3c8a1d2

# 6. Clean up
python3 Agents/cli/browser.py close f3c8a1d2
```

### Example: Search and scrape results

```bash
python3 Agents/cli/browser.py open https://www.google.com
# Session: 7e2f9b01

python3 Agents/cli/browser.py type 7e2f9b01 "textarea[name=q]" "solar installer jobs Texas"
python3 Agents/cli/browser.py press 7e2f9b01 Enter
python3 Agents/cli/browser.py wait 7e2f9b01 "#search"
python3 Agents/cli/browser.py text 7e2f9b01 --selector "#search"
python3 Agents/cli/browser.py close 7e2f9b01
```

## Gotchas

- **`text` is usually better than `html`** for reading page content. HTML can be thousands of lines; text gives you just what's visible.
- **`type` clears the field first** (uses Playwright's `fill()`). Use `--keys` for fields that need keystroke events (autocomplete, search-as-you-type).
- **The server persists until stopped.** If you restart your machine, the server will be gone. Just run any command and it auto-restarts.
- **Each session is isolated.** Sessions don't share cookies or storage. If you need to log in, you log in per-session.
- **Pop-ups / new tabs** opened by clicking links are not automatically tracked as new sessions. The original session stays on its page. Use `eval` to modify link targets if needed.
- **The server is single-threaded.** It processes one command at a time. Don't send commands in parallel to the same server.
- **Screenshots** are saved as PNG. Use the `view_image` tool to inspect them.
