"""Macro View compiler (EAI-178) — notes in, one versioned advisory digest out.

A Grok ``compiler`` role (Nemotron backstop via the LLM registry chain) recompiles the
Macro View whenever notes change, debounced. The anti-slop rules from the ai-quoting
audit are enforced here, not just prompted:

- **Citations or it didn't happen**: a section whose text cites no included note id is
  dropped before storage (the model invented it, or laundered a stale claim).
- **Contradictions survive**: disputed notes are labeled and included; the prompt forbids
  resolving them, and the compiler never filters a note for disagreeing.
- **Price levels freeze**: every ``$``-amount in compiled text is stamped
  ``(example-not-current)`` — the pattern is the knowledge, the number is history.
- **Staleness is upstream**: the desk's default listing already excludes stale/retired
  notes, so the compiler cannot see them.

The view is advisory by construction: RiskGuard has no parameter that could carry it
(asserted by test), and the decide prompt receives only a budgeted slice in its own lane.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from trader.llm.base import LLMError
from trader.research.notes import ResearchDesk
from trader.store.audit import audit
from trader.store.db import Database
from trader.store.repos import MacroViewsRepo

log = structlog.get_logger(__name__)

ROLE = "compiler"
DEFAULT_DEBOUNCE_SECONDS = 30.0

CITATION = re.compile(r"\[note:(\d+)\]")
# $-amounts not already stamped: "$2,400", "$2,400.50", "$ 105".
PRICE_LEVEL = re.compile(r"(\$\s?\d[\d,]*(?:\.\d+)?)(?!\s*\(example-not-current\))")

SYSTEM_PROMPT = """You compile an operator's research notes into one Macro View digest \
for an automated trading system.

Rules — violations get your output discarded:
- Every claim MUST cite the note ids it comes from, inline, as [note:<id>].
- Notes marked `disputed` contradict other notes. Present BOTH sides, each cited. \
NEVER resolve, average, or pick a winner — the dispute itself is the information.
- Weigh recency: newer notes speak louder than older ones on the same subject.
- Price levels in notes are historical examples. State the PATTERN they evidence, \
not the number as a live level.
- Write only what the notes support. No outside knowledge, no filler.

Reply with JSON matching the provided schema: a `global_thesis` string and `sections`, \
an object mapping asset-class keys (e.g. "crypto", "us_equity") to digest strings. Only \
include sections the notes support."""


class CompiledOutput(BaseModel):
    """The compiler role's contract; its JSON schema constrains the model."""

    global_thesis: str
    sections: dict[str, str] = {}


COMPILE_SCHEMA: dict[str, Any] = CompiledOutput.model_json_schema()


def freeze_price_levels(text: str) -> str:
    """Stamp every $-amount as example-not-current (idempotent)."""
    return PRICE_LEVEL.sub(r"\1 (example-not-current)", text)


class MacroViewCompiler:
    """Compile-on-change with a trailing debounce, plus an explicit ``compile_now``."""

    def __init__(
        self,
        db: Database,
        llm: Any,  # LLMRegistry-shaped: .complete(role, messages, schema=..., ...)
        *,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._db = db
        self._llm = llm
        self._views = MacroViewsRepo(db)
        self._debounce = debounce_seconds
        self._dirty = False
        self._task: asyncio.Task | None = None

    # -- change notification ------------------------------------------------

    def mark_dirty(self) -> None:
        """Called after any note change. Coalesces bursts into one recompile."""
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._debounced(), name="macro-view-compile")

    async def _debounced(self) -> None:
        # Trailing edge: recompile only after a full quiet window.
        while self._dirty:
            self._dirty = False
            await asyncio.sleep(self._debounce)
        try:
            await self.compile_now()
        except Exception:  # noqa: BLE001 - a failed compile keeps the previous view
            log.exception("macro_view_compile_failed")

    # -- compilation --------------------------------------------------------

    async def compile_now(self) -> dict[str, Any] | None:
        """One compile pass. Returns the stored view row, or None with nothing to compile."""
        notes = await ResearchDesk(self._db).list()
        if not notes:
            log.info("macro_view_compile_skipped", reason="no live notes")
            return None

        note_ids = {n["id"] for n in notes}
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._notes_prompt(notes)},
        ]
        completion = await self._llm.complete(ROLE, messages, schema=COMPILE_SCHEMA)
        try:
            output = CompiledOutput.model_validate(json.loads(completion.text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMError(f"compiler output failed validation: {exc}") from exc

        thesis = freeze_price_levels(output.global_thesis.strip())
        sections: dict[str, str] = {}
        for asset_class, text in output.sections.items():
            cited = {int(m) for m in CITATION.findall(text)}
            if not cited or not cited <= note_ids:
                # Uncited or ghost-cited: the notes don't back it. Drop whole.
                log.warning(
                    "macro_view_section_dropped",
                    asset_class=asset_class,
                    cited=sorted(cited),
                    reason="no valid note citations",
                )
                continue
            sections[asset_class] = freeze_price_levels(text.strip())

        compiled_by = f"{completion.provider}:{completion.model}"
        version = await self._views.add(
            compiled_by=compiled_by,
            global_thesis=thesis,
            sections=sections,
            note_ids=sorted(note_ids),
        )
        await audit(
            self._db,
            actor="compiler",
            category="research",
            action="compile_view",
            subject=f"macro_view:{version}",
            payload={
                "compiled_by": compiled_by,
                "note_ids": sorted(note_ids),
                "sections": sorted(sections),
                "cost_usd": completion.cost_usd,
            },
        )
        row = await self._views.get(version)
        assert row is not None
        return row

    @staticmethod
    def _notes_prompt(notes: list[dict[str, Any]]) -> str:
        blocks = [f"Operator research notes ({len(notes)}), newest first:"]
        for n in notes:
            scope = ", ".join([*n["asset_classes"], *n["symbols"]]) or "general"
            blocks.append(
                f"--- note:{n['id']} | status: {n['status']} | {n['created_at'].isoformat()}"
                f" | scope: {scope} | {n['title']}\n{n['body']}"
            )
        return "\n\n".join(blocks)
