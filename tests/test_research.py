"""Research Desk part 1 — notes store, capture surfaces, auto-stale
(see docs/lessons.md#research-desk-capture-once).

Lifecycle (submit → list → retire), auto-stale windowing, markdown preserved verbatim,
and the three capture doors (API / MCP / CLI). Runs against the real compose Postgres.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from trader.ops.config import Settings
from trader.research.notes import ResearchDesk
from trader.store.db import Database
from trader.surfaces.api.app import create_app

DEV_SETTINGS = Settings(auth_disabled=True)

TRICKY_MARKDOWN = (
    "# Fed pivot thesis\n\n"
    "- `DXY` broke **105** — watch %s-style literals and 'quotes'\n"
    "- code block:\n\n"
    "```python\n"
    'print("hello\\n")  # 100% verbatim?\n'
    "```\n\n"
    "> em-dash — unicode ✓ 中文 \\ backslash\n"
)


async def test_submit_list_retire_lifecycle(db: Database):
    desk = ResearchDesk(db)
    note = await desk.submit(
        title="Fed pivot thesis",
        body="Rates likely peak this quarter.",
        tags=["macro", "rates"],
        asset_classes=["crypto"],
        symbols=["ETH/USD"],
        source="https://example.com/article",
    )
    assert note["id"] == 1
    assert note["status"] == "active"

    listed = await desk.list()
    assert [n["id"] for n in listed] == [1]
    assert listed[0]["title"] == "Fed pivot thesis"
    assert listed[0]["tags"] == ["macro", "rates"]

    assert await desk.retire(1) is True
    assert await desk.list() == []
    retired = await desk.list(include_retired=True)
    assert [n["status"] for n in retired] == ["retired"]

    # Retiring twice (or a missing id) is a no-op, not an error.
    assert await desk.retire(1) is False
    assert await desk.retire(999) is False

    # Every submit/retire leaves an audit row.
    events = await db.fetch_all(
        "SELECT action, subject FROM audit_events WHERE category = 'research' ORDER BY id"
    )
    assert [(e["action"], e["subject"]) for e in events] == [
        ("submit_note", "note:1"),
        ("retire_note", "note:1"),
    ]


async def test_auto_stale_windowing(db: Database):
    desk = ResearchDesk(db, stale_days=30)

    fresh = await desk.submit(title="fresh", body="within window")
    assert fresh["expires_at"] is not None  # default window applied

    stale = await desk.submit(title="stale", body="window elapsed", stale_days=0)
    evergreen = await desk.submit(title="evergreen", body="pinned", pinned=True, stale_days=0)
    assert evergreen["expires_at"] is None  # pinned notes never expire

    current = {n["title"] for n in await desk.list()}
    assert current == {"fresh", "evergreen"}  # stale dropped out, without being retired

    # Stale is invisible-to-compilation, not gone: the widened listing still shows it.
    widened = {n["title"] for n in await desk.list(include_stale=True)}
    assert widened == {"fresh", "stale", "evergreen"}
    row = await db.fetch_one("SELECT status FROM research_notes WHERE id = %s", (stale["id"],))
    assert row is not None and row["status"] == "active"


async def test_authored_at_is_the_second_date_and_anchors_freshness(db: Database):
    """Two date fields (Hermes capture requirement): created_at = when SUBMITTED (db
    default), authored_at = when the research was CREATED. Recency and auto-stale key
    off authored_at when given — an old article submitted today is old information."""
    desk = ResearchDesk(db, stale_days=30)
    authored = datetime.now(tz=UTC) - timedelta(days=10)
    note = await desk.submit(title="aged article", body="content", authored_at=authored)
    assert note["authored_at"] == authored
    assert note["created_at"] > authored  # submitted now, created 10 days ago
    # Stale window anchors on authorship: expires 30d after AUTHORED, not submitted.
    assert abs((note["expires_at"] - (authored + timedelta(days=30))).total_seconds()) < 5

    # Omitted authored_at stays NULL (unknown) and the window anchors on submission.
    plain = await desk.submit(title="fresh capture", body="content")
    assert plain["authored_at"] is None
    assert plain["expires_at"] > datetime.now(tz=UTC) + timedelta(days=29)

    # Information older than the window is stale ON ARRIVAL: compilation never sees it,
    # but the operator's widened listing does (pin or widen stale_days to keep it).
    old = await desk.submit(
        title="ancient article",
        body="content",
        authored_at=datetime.now(tz=UTC) - timedelta(days=40),
    )
    visible = [n["id"] for n in await desk.list()]
    assert note["id"] in visible and old["id"] not in visible
    widened = [n["id"] for n in await desk.list(include_stale=True)]
    assert old["id"] in widened


async def test_authored_at_rejects_future_and_naive_datetimes(db: Database):
    from datetime import UTC, datetime, timedelta

    desk = ResearchDesk(db)
    with pytest.raises(ValueError, match="future"):
        await desk.submit(title="t", body="b", authored_at=datetime.now(tz=UTC) + timedelta(days=1))
    with pytest.raises(ValueError, match="timezone"):
        await desk.submit(title="t", body="b", authored_at=datetime(2026, 9, 1))


def test_authored_at_flows_through_the_api_door(clean_db: None):
    # Relative, not a fixed date: freshness anchors on authored_at, so a hard-coded
    # date silently ages past the 30-day stale window and drops out of the listing
    # (this test went red on 2026-09-24 for exactly that reason).
    authored = (datetime.now(tz=UTC) - timedelta(days=5)).replace(microsecond=0).isoformat()
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        out = tc.post(
            "/research/notes", json={"title": "aged", "body": "b", "authored_at": authored}
        ).json()
        assert out["authored_at"] == authored
        listed = tc.get("/research/notes").json()
    assert listed[0]["authored_at"] == authored


async def test_submit_rejects_blank_notes(db: Database):
    desk = ResearchDesk(db)
    with pytest.raises(ValueError):
        await desk.submit(title="  ", body="has body")
    with pytest.raises(ValueError):
        await desk.submit(title="has title", body="\n")


def test_markdown_preserved_verbatim_through_api(clean_db: None):
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        created = tc.post(
            "/research/notes",
            json={"title": "tricky", "body": TRICKY_MARKDOWN},
        )
        assert created.status_code == 200, created.text
        note_id = created.json()["id"]
        listed = tc.get("/research/notes").json()
    assert [n["id"] for n in listed] == [note_id]
    assert listed[0]["body"] == TRICKY_MARKDOWN


def test_api_capture_lifecycle(clean_db: None):
    with TestClient(create_app(DEV_SETTINGS)) as tc:
        created = tc.post(
            "/research/notes",
            json={
                "title": "ETH supply shock",
                "body": "Staking withdrawals slowing.",
                "tags": ["onchain"],
                "symbols": ["ETH/USD"],
                "pinned": True,
            },
        ).json()
        assert created["pinned"] is True

        retired = tc.post(f"/research/notes/{created['id']}/retire")
        assert retired.status_code == 200
        assert retired.json() == {"retired": True, "id": created["id"]}
        assert tc.get("/research/notes").json() == []

        # Blank notes are refused at the door.
        assert tc.post("/research/notes", json={"title": "", "body": "x"}).status_code == 422


def test_mcp_capture_tools(clean_db: None):
    import asyncio

    from mcp.client import Client

    from trader.surfaces.mcp.server import build_mcp
    from trader.surfaces.services import build_registry

    async def run() -> None:
        db = Database(min_size=1, max_size=1)
        await db.open()
        try:
            async with Client(build_mcp(build_registry(db))) as mcp_client:
                tools = {t.name for t in (await mcp_client.list_tools()).tools}
                assert {"submit_research_note", "get_research_notes", "retire_note"} <= tools
                # Envelope contract: dict returns arrive unwrapped; list returns are
                # wrapped as {"result": [...]}.
                created = await mcp_client.call_tool(
                    "submit_research_note",
                    {"title": "via mcp", "body": "Hermes distilled this."},
                )
                note = created.structured_content
                assert note["status"] == "active"
                listed = await mcp_client.call_tool("get_research_notes", {})
                assert [n["title"] for n in listed.structured_content["result"]] == ["via mcp"]
                retired = await mcp_client.call_tool("retire_note", {"note_id": note["id"]})
                assert retired.structured_content["retired"] is True
        finally:
            await db.close()

    asyncio.run(run())


def test_cli_note_add_from_stdin(clean_db: None, monkeypatch: pytest.MonkeyPatch):
    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(DEV_SETTINGS)))
    runner = CliRunner()

    added = runner.invoke(
        cli.app,
        ["note", "add", "--title", "stdin note", "--tag", "macro", "--symbol", "ETH/USD"],
        input=TRICKY_MARKDOWN,
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.output)["title"] == "stdin note"

    listed = runner.invoke(cli.app, ["note", "list"])
    assert listed.exit_code == 0, listed.output
    notes = json.loads(listed.output)
    assert notes[0]["body"] == TRICKY_MARKDOWN

    retired = runner.invoke(cli.app, ["note", "retire", str(notes[0]["id"])])
    assert retired.exit_code == 0, retired.output
    assert json.loads(retired.output)["retired"] is True


def test_cli_note_add_from_file(clean_db: None, monkeypatch: pytest.MonkeyPatch, tmp_path):
    import trader.surfaces.cli.main as cli

    monkeypatch.setattr(cli, "_client", lambda: TestClient(create_app(DEV_SETTINGS)))
    path = tmp_path / "note.md"
    path.write_text(TRICKY_MARKDOWN, encoding="utf-8")

    added = CliRunner().invoke(
        cli.app, ["note", "add", "--title", "file note", "--file", str(path)]
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.output)["body"] == TRICKY_MARKDOWN
