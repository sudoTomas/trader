"""Read-only markdown mirror (EAI-179) — browse the Research Desk Obsidian-style.

Strictly one-way: the DB is the single source of truth, every export rewrites the folder
from scratch (local edits are overwritten, orphaned files removed), and nothing in this
codebase reads these files back. The CLI/API/MCP are the only write paths — this avoids
ai-quoting's out-of-band sync fragility. The folder is gitignored: generated content,
regenerable from the store at any time.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import structlog

from trader.research.notes import ResearchDesk
from trader.store.db import Database
from trader.store.repos import MacroViewsRepo

log = structlog.get_logger(__name__)

WARNING = "<!-- DO NOT EDIT. One-way export from the trader store; edits are overwritten. -->"


def slugify(title: str, max_length: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "note"


class MarkdownMirror:
    """Rewrites the mirror folder from the store. Idempotent, cheap, never a write path."""

    def __init__(self, db: Database, root: Path | str) -> None:
        self._db = db
        self._root = Path(root)

    async def export(self) -> dict[str, Any]:
        notes_dir = self._root / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)

        # Everything the desk holds, lifecycle state included — a browse view, not a filter.
        notes = await ResearchDesk(self._db).list(
            include_stale=True, include_retired=True, limit=500
        )
        expected: set[str] = set()
        for note in notes:
            name = f"{note['id']}-{slugify(note['title'])}.md"
            expected.add(name)
            (notes_dir / name).write_text(self._render_note(note), encoding="utf-8")
        for stray in notes_dir.glob("*.md"):
            if stray.name not in expected:
                stray.unlink()  # orphan from an older export; the DB no longer backs it

        view = await MacroViewsRepo(self._db).current()
        view_path = self._root / "macro-view.md"
        if view is not None:
            view_path.write_text(self._render_view(view), encoding="utf-8")
        elif view_path.exists():
            view_path.unlink()

        result = {"root": str(self._root), "notes": len(notes), "view": view is not None}
        log.info("research_mirror_exported", **result)
        return result

    async def export_quietly(self) -> None:
        """Export, but never let a mirror problem break the write that triggered it."""
        try:
            await self.export()
        except Exception as exc:  # noqa: BLE001 - the mirror is a convenience, not a store
            log.warning("research_mirror_export_failed", error=str(exc))

    @staticmethod
    def _render_note(note: dict[str, Any]) -> str:
        front = [
            "---",
            f"id: {note['id']}",
            f"title: {json_str(note['title'])}",
            f"status: {note['status']}",
            f"created_at: {note['created_at'].isoformat()}",
            f"expires_at: {note['expires_at'].isoformat() if note['expires_at'] else 'null'}",
            f"pinned: {str(note['pinned']).lower()}",
            f"tags: {list(note['tags'])}",
            f"asset_classes: {list(note['asset_classes'])}",
            f"symbols: {list(note['symbols'])}",
            f"source: {json_str(note['source']) if note['source'] else 'null'}",
            "---",
            WARNING,
            "",
        ]
        return "\n".join(front) + note["body"]

    @staticmethod
    def _render_view(view: dict[str, Any]) -> str:
        lines = [
            "---",
            f"version: {view['version']}",
            f"compiled_by: {view['compiled_by']}",
            f"created_at: {view['created_at'].isoformat()}",
            f"note_ids: {list(view['note_ids'])}",
            "---",
            WARNING,
            "",
            "# Macro View",
            "",
            view["global_thesis"],
        ]
        for asset_class, text in sorted((view["sections"] or {}).items()):
            lines += ["", f"## {asset_class}", "", text]
        return "\n".join(lines) + "\n"


def json_str(value: str) -> str:
    """A YAML-safe quoted scalar (JSON string quoting is valid YAML)."""
    import json

    return json.dumps(value)
