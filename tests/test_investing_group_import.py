"""clients/investing-group/import_emails.py: which emails become notes, and what they carry."""

import base64
import importlib.util
from pathlib import Path

_PATH = Path(__file__).parent.parent / "clients" / "investing-group" / "import_emails.py"
_spec = importlib.util.spec_from_file_location("import_emails", _PATH)
imp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(imp)

DOMAIN = "theinvestinggroupchicago.com"
PASS = f"mx.google.com; spf=pass; dmarc=pass (p=NONE) header.from={DOMAIN}"


def msg(subject: str, body: str, auth: str = PASS, mid: str = "abc123") -> dict:
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "id": mid,
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Tue, 29 Sep 2026 14:00:00 -0500"},
                {"name": "Authentication-Results", "value": auth},
            ],
            "parts": [{"mimeType": "text/plain", "body": {"data": data}}],
        },
    }


def test_meeting_summary_becomes_a_note():
    note = imp.note_from(msg("Meeting Summary: Sept 29", "Energy names in focus.\r\n"))
    assert note["title"] == "The Investing Group: Meeting Summary: Sept 29"
    assert note["body"] == "Energy names in focus."
    assert note["source"] == "The Investing Group Chicago email (gmail id abc123)"
    assert note["authored_at"] == "2026-09-29T14:00:00-05:00"
    assert note["tags"] == ["investing-group", "macro"]


def test_other_emails_are_skipped():
    assert imp.note_from(msg("Livestream link for tonight", "join here")) is None
    assert imp.note_from(msg("Reminder: meeting Tuesday", "see you")) is None


def test_member_names_stay_out():
    body = "Thesis: long ETH.\n*Attendance and Acknowledgements*\nAlice, Bob\n"
    assert imp.note_from(msg("Discussion Topics", body))["body"] == "Thesis: long ETH."


def test_only_gmail_verified_dmarc_from_the_group_is_authentic():
    assert imp.authentic(msg("Meeting Summary", "x"))
    assert not imp.authentic(
        msg("Meeting Summary", "x", auth=PASS.replace("dmarc=pass", "dmarc=fail"))
    )
    forged = f"evil.example; dmarc=pass header.from={DOMAIN}"  # not Gmail's own verdict
    assert not imp.authentic(msg("Meeting Summary", "x", auth=forged))
    other = "mx.google.com; dmarc=pass header.from=lookalike.example"
    assert not imp.authentic(msg("Meeting Summary", "x", auth=other))


def test_imported_ids_are_read_back_from_note_sources():
    notes = [
        {"source": "The Investing Group Chicago email (gmail id 19a2b)"},
        {"source": "manual"},
        {"source": None},
    ]
    assert imp.seen_ids(notes) == {"19a2b"}
