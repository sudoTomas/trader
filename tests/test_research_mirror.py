"""Research Desk part 3 — read-only markdown mirror export
(see docs/lessons.md#research-desk-capture-once).

One-way: DB → files. The mirror must be regenerable, overwrite local edits, and never
be a write path back into the store.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from trader.ops.config import Settings
from trader.research.mirror import MarkdownMirror
from trader.research.notes import ResearchDesk
from trader.store.db import Database
from trader.store.repos import MacroViewsRepo
from trader.surfaces.api.app import create_app

BODY = "# Thesis\n\nFlows drive chop — watch `ETH/USD`.\n"


async def test_export_writes_notes_with_frontmatter_and_verbatim_body(db: Database, tmp_path):
    desk = ResearchDesk(db)
    note = await desk.submit(
        title="ETH Flows: a thesis!", body=BODY, tags=["flows"], symbols=["ETH/USD"]
    )
    result = await MarkdownMirror(db, tmp_path).export()
    assert result["notes"] == 1

    files = list((tmp_path / "notes").glob("*.md"))
    assert len(files) == 1
    assert files[0].name == f"{note['id']}-eth-flows-a-thesis.md"
    text = files[0].read_text(encoding="utf-8")
    assert text.endswith(BODY)  # body verbatim, byte-for-byte, at the end
    assert "status: active" in text
    assert "DO NOT EDIT" in text  # the one-way warning is in every file


async def test_export_is_one_way_and_overwrites_local_edits(db: Database, tmp_path):
    desk = ResearchDesk(db)
    note = await desk.submit(title="note", body="original body\n")
    mirror = MarkdownMirror(db, tmp_path)
    await mirror.export()

    (path,) = (tmp_path / "notes").glob("*.md")
    path.write_text("hand-edited — must not survive nor import", encoding="utf-8")
    await mirror.export()
    assert "hand-edited" not in path.read_text(encoding="utf-8")
    # And the DB never saw the edit.
    assert (await desk.list())[0]["body"] == "original body\n"
    assert note["id"] == 1


async def test_export_covers_retired_notes_and_cleans_orphans(db: Database, tmp_path):
    desk = ResearchDesk(db)
    keep = await desk.submit(title="keep", body="b")
    gone = await desk.submit(title="gone", body="b")
    await desk.retire(gone["id"])

    mirror = MarkdownMirror(db, tmp_path)
    (tmp_path / "notes").mkdir(parents=True)
    orphan = tmp_path / "notes" / "999-stray.md"
    orphan.write_text("stray file from an older export", encoding="utf-8")

    await mirror.export()
    names = sorted(p.name for p in (tmp_path / "notes").glob("*.md"))
    assert names == [f"{keep['id']}-keep.md", f"{gone['id']}-gone.md"] or names == sorted(
        [f"{keep['id']}-keep.md", f"{gone['id']}-gone.md"]
    )
    assert not orphan.exists()
    retired_text = (tmp_path / "notes" / f"{gone['id']}-gone.md").read_text(encoding="utf-8")
    assert "status: retired" in retired_text


async def test_macro_view_exported_when_present(db: Database, tmp_path):
    mirror = MarkdownMirror(db, tmp_path)
    await mirror.export()
    assert not (tmp_path / "macro-view.md").exists()  # nothing compiled yet

    await MacroViewsRepo(db).add(
        compiled_by="stub:stub-model",
        global_thesis="Chop over trend [note:1]",
        sections={"crypto": "Flows negative [note:1]"},
        note_ids=[1],
    )
    await mirror.export()
    text = (tmp_path / "macro-view.md").read_text(encoding="utf-8")
    assert "version: 1" in text
    assert "Chop over trend" in text
    assert "Flows negative" in text
    assert "DO NOT EDIT" in text


def test_capture_through_the_api_refreshes_the_mirror(clean_db: None, tmp_path, monkeypatch):
    settings = Settings(auth_disabled=True, research_mirror_dir=str(tmp_path))
    with TestClient(create_app(settings)) as tc:
        created = tc.post("/research/notes", json={"title": "auto mirror", "body": "b"})
        assert created.status_code == 200
        note_id = created.json()["id"]
        assert (tmp_path / "notes" / f"{note_id}-auto-mirror.md").exists()

        tc.post(f"/research/notes/{note_id}/retire")
        text = (tmp_path / "notes" / f"{note_id}-auto-mirror.md").read_text(encoding="utf-8")
        assert "status: retired" in text
