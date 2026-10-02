#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Import new The Investing Group (Chicago) emails into the Research Desk as notes.

Reads one Gmail inbox through a Google OAuth refresh token, keeps the meeting summaries and
discussion-topic emails that Gmail verified as really from the group's domain, and posts each
one to the trader API's `POST /research/notes` exactly once. It is a client: it writes notes
through the same route the CLI and MCP use, and does nothing while the API is down.
"""

import base64
import json
import os
import re
import sys
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx

SENDER_DOMAIN = os.environ.get("TIG_SENDER_DOMAIN", "theinvestinggroupchicago.com")
# A Google "authorized_user" JSON (client_id, client_secret, refresh_token, token_uri) with
# the gmail.readonly scope, for the inbox that receives the group's emails.
TOKEN_FILE = Path(os.environ.get("TIG_GMAIL_TOKEN_FILE", "gmail-token.json"))
API = os.environ.get("TRADER_API_BASE_URL", "http://127.0.0.1:8080")
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
# Trader stales a note 30 days after it was written; older mail would expire on arrival.
QUERY = f"from:info@{SENDER_DOMAIN} newer_than:30d"
WANTED = re.compile(r"meeting summary|discussion topics", re.I)


def trader_headers() -> dict[str, str]:
    """Send the API key when trader's auth is on."""
    key = os.environ.get("TRADER_API_KEY", "")
    return {"X-API-Key": key} if key else {}


def gmail_token() -> str:
    creds = json.loads(TOKEN_FILE.read_text())
    r = httpx.post(
        creds["token_uri"],
        data={
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "refresh_token": creds["refresh_token"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"Gmail token refresh failed ({r.status_code}): re-authorize {TOKEN_FILE}")
    return r.json()["access_token"]


def header(msg: dict, name: str) -> list[str]:
    return [h["value"] for h in msg["payload"]["headers"] if h["name"].lower() == name.lower()]


def authentic(msg: dict, domain: str = SENDER_DOMAIN) -> bool:
    """Gmail's own verdict on this delivery, not what the sender claims about itself."""
    return any(
        v.startswith("mx.google.com") and "dmarc=pass" in v and f"header.from={domain}" in v
        for v in header(msg, "Authentication-Results")
    )


def plain_text(part: dict) -> str | None:
    if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", "replace")
    for child in part.get("parts") or []:
        if (text := plain_text(child)) is not None:
            return text
    return None


def note_from(msg: dict) -> dict | None:
    subject = re.sub(r"^(Subject:\s*)+", "", header(msg, "Subject")[0]).strip()
    if not WANTED.search(subject):
        return None  # livestream links, reminders, announcements
    body = plain_text(msg["payload"])
    if not body:
        return None
    body = body.replace("\r\n", "\n")
    # Member names stay out of the research store.
    body = re.split(r"\n\*Attendance and Acknowledgements\*", body)[0].strip().rstrip("-").strip()
    title = subject if "investing group" in subject.lower() else f"The Investing Group: {subject}"
    return {
        "title": title,
        "body": body,
        "tags": ["investing-group", "macro"],
        "source": f"The Investing Group Chicago email (gmail id {msg['id']})",
        "authored_at": parsedate_to_datetime(header(msg, "Date")[0]).isoformat(),
    }


def seen_ids(notes: list[dict]) -> set[str]:
    """Gmail ids already imported, including notes that went stale or were retired."""
    return {m.group(1) for n in notes if (m := re.search(r"gmail id (\w+)", n.get("source") or ""))}


def main() -> None:
    trader = httpx.Client(base_url=API, headers=trader_headers(), timeout=30)
    try:
        notes = trader.get(
            "/research/notes",
            params={"include_stale": True, "include_retired": True, "limit": 500},
        )
    except httpx.ConnectError:
        print(f"trader API not running on {API}; nothing to do")
        return
    notes.raise_for_status()
    seen = seen_ids(notes.json())

    gmail = httpx.Client(
        base_url=GMAIL, headers={"Authorization": f"Bearer {gmail_token()}"}, timeout=30
    )
    listing = gmail.get("/messages", params={"q": QUERY, "maxResults": 50})
    listing.raise_for_status()
    imported = 0
    for ref in listing.json().get("messages", []):
        if ref["id"] in seen:
            continue
        msg = gmail.get(f"/messages/{ref['id']}", params={"format": "full"})
        msg.raise_for_status()
        msg = msg.json()
        note = note_from(msg)
        if note is None:
            continue
        if not authentic(msg):
            print(f"skipped {ref['id']}: failed Gmail's DMARC check for {SENDER_DOMAIN}")
            continue
        r = trader.post("/research/notes", json=note)
        r.raise_for_status()
        imported += 1
        print(f"imported note {r.json()['id']}: {note['title']}")
    print(f"done: {imported} new")


if __name__ == "__main__":
    main()
