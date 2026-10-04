# Investing Group email import

Turns new emails from The Investing Group (Chicago) into Research Desk notes, which the
compiler folds into the Macro View the agent decides with. It is a client: it writes through
`POST /research/notes`, the same route the CLI and MCP use, and changes nothing else.

## What it takes

- **Keeps:** meeting summaries and discussion-topic emails from the last 30 days (trader
  stales a note 30 days after it was written, so older mail would expire on arrival).
- **Skips:** livestream links, reminders, announcements.
- **Checks:** Gmail's own `Authentication-Results` must show `dmarc=pass` for the group's
  domain, so a forged email can't reach the decide prompt.
- **Once only:** each email's Gmail id goes into the note's `source`; later runs skip it,
  including after the note goes stale or is retired.
- **Leaves out:** the attendance section (member names) and the recipient list.
- **Quiet** while the API is down.

## Setup

1. An inbox subscribed to the group's emails, and a Google OAuth client allowed the
   `gmail.readonly` scope. Save an `authorized_user` JSON (`client_id`, `client_secret`,
   `refresh_token`, `token_uri`) for that inbox.
2. A settings file, for example `~/.config/trader/investing-group.env`:

   ```bash
   TIG_GMAIL_TOKEN_FILE=/path/to/gmail-token.json
   TRADER_API_BASE_URL=http://127.0.0.1:8080
   TRADER_API_KEY=<the capture-scoped key, TRADER_CAPTURE_API_KEY>   # only when auth is on
   ```

3. Run it once, then install the hourly timer:

   ```bash
   set -a; . ~/.config/trader/investing-group.env; set +a
   ./import_emails.py                  # one pass by hand
   ./install-timer.sh                  # hourly systemd user timer
   journalctl --user -u tig-import     # what it did
   ```

`TIG_SENDER_DOMAIN` overrides the group's domain (default `theinvestinggroupchicago.com`).

Tests: `tests/test_investing_group_import.py` (subject filter, member-name stripping, the
DMARC check, and the once-only id read-back).
