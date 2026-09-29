"""Per-candidate research briefs — symbol-scoped inputs in, one versioned
per-symbol digest out.

The Macro View (compiler.py) digests notes per asset class; the brief digests per
symbol. Inputs are store-resident only — notes scoped to the symbol plus the latest ok
sentiment row — synthesized by the ``brief`` LLM role (Grok, Nemotron backstop). No live
search, no outside knowledge: a brief the store can't back is the model-prior dishonesty
the sentiment poller guards against, so no inputs means no brief and no LLM call
(see docs/lessons.md#no-inputs-no-brief).

The Macro View anti-slop rules (see docs/lessons.md#macro-view-advisory-firewall) apply per
part: an uncited/ghost-cited thesis drops the
whole brief (a brief with no backed thesis isn't a brief); uncited points drop
individually; every ``$``-amount is stamped example-not-current.

Advisory by construction: the decide prompt receives only a budgeted lane (behind
``TRADER_BRIEFS_ENABLED``), and RiskGuard has no parameter that could carry a brief.
Compiles run out-of-band (debounced on note change, or explicit) — never in the decide
hot path.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from trader.llm.base import LLMError
from trader.research.compiler import freeze_price_levels
from trader.research.notes import ResearchDesk
from trader.store.audit import audit
from trader.store.db import Database
from trader.store.repos import BriefsRepo, SentimentRepo

log = structlog.get_logger(__name__)

ROLE = "brief"
DEFAULT_DEBOUNCE_SECONDS = 30.0

CITATION = re.compile(r"\[(note|sent):(\d+)\]")

SYSTEM_PROMPT = """You compile the research on ONE trading candidate into a short brief \
for an automated trading system.

Rules — violations get your output discarded:
- Every claim MUST cite its inputs inline: [note:<id>] for research notes, \
[sent:<id>] for the sentiment reading.
- Write only what the inputs support. No outside knowledge, no filler, no invented \
context about the asset.
- Weigh recency: newer inputs speak louder on the same subject.
- Price levels in inputs are historical examples. State the PATTERN they evidence, \
not the number as a live level.

Reply with JSON matching the provided schema: a one-or-two-sentence `thesis` and \
`points`, a list of short supporting observations. Only include points the inputs \
support."""


class BriefOutput(BaseModel):
    """The brief role's contract; its JSON schema constrains the model."""

    thesis: str
    points: list[str] = []


BRIEF_SCHEMA: dict[str, Any] = BriefOutput.model_json_schema()


class BriefCompiler:
    """Compile-on-change with a trailing debounce, plus an explicit ``compile_now``."""

    def __init__(
        self,
        db: Database,
        llm: Any,  # LLMRegistry-shaped: .complete(role, messages, schema=..., ...)
        *,
        symbols: list[str],
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._db = db
        self._llm = llm
        self._briefs = BriefsRepo(db)
        self._sentiment = SentimentRepo(db)
        self._symbols = list(symbols)
        self._debounce = debounce_seconds
        self._dirty = False
        self._task: asyncio.Task | None = None

    # -- change notification ------------------------------------------------

    def mark_dirty(self) -> None:
        """Called after any note change. Coalesces bursts into one recompile sweep."""
        self._dirty = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._debounced(), name="brief-compile")

    async def _debounced(self) -> None:
        # Trailing edge: recompile only after a full quiet window.
        while self._dirty:
            self._dirty = False
            await asyncio.sleep(self._debounce)
        for symbol in self._symbols:
            try:
                await self.compile_now(symbol)
            except Exception:  # noqa: BLE001 - a failed compile keeps the previous brief
                log.exception("brief_compile_failed", symbol=symbol)

    # -- compilation --------------------------------------------------------

    async def compile_now(self, symbol: str) -> dict[str, Any] | None:
        """One compile pass for one symbol. Returns the stored row, or None when the
        store holds nothing for it (or the output failed the citation rules)."""
        notes = [n for n in await ResearchDesk(self._db).list() if symbol in n["symbols"]]
        sentiment = await self._sentiment.latest(symbol)
        if not notes and sentiment is None:
            log.info("brief_compile_skipped", symbol=symbol, reason="no symbol-scoped inputs")
            return None

        valid_ids = {("note", n["id"]) for n in notes}
        if sentiment is not None:
            valid_ids.add(("sent", sentiment["id"]))
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._inputs_prompt(symbol, notes, sentiment)},
        ]
        completion = await self._llm.complete(ROLE, messages, schema=BRIEF_SCHEMA)
        try:
            output = BriefOutput.model_validate(json.loads(completion.text))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMError(f"brief output failed validation: {exc}") from exc

        def cited(text: str) -> set[tuple[str, int]]:
            return {(kind, int(num)) for kind, num in CITATION.findall(text)}

        thesis_cites = cited(output.thesis)
        if not thesis_cites or not thesis_cites <= valid_ids:
            # Uncited or ghost-cited thesis: the store doesn't back it. Drop whole.
            log.warning(
                "brief_dropped",
                symbol=symbol,
                cited=sorted(thesis_cites),
                reason="thesis has no valid citations",
            )
            return None
        points: list[str] = []
        for point in output.points:
            point_cites = cited(point)
            if not point_cites or not point_cites <= valid_ids:
                log.warning(
                    "brief_point_dropped",
                    symbol=symbol,
                    cited=sorted(point_cites),
                    reason="no valid citations",
                )
                continue
            points.append(freeze_price_levels(point.strip()))

        compiled_by = f"{completion.provider}:{completion.model}"
        version = await self._briefs.add(
            symbol=symbol,
            compiled_by=compiled_by,
            thesis=freeze_price_levels(output.thesis.strip()),
            points=points,
            note_ids=sorted(n["id"] for n in notes),
            sentiment_id=sentiment["id"] if sentiment is not None else None,
        )
        await audit(
            self._db,
            actor="compiler",
            category="research",
            action="compile_brief",
            subject=f"brief:{version}",
            payload={
                "symbol": symbol,
                "compiled_by": compiled_by,
                "note_ids": sorted(n["id"] for n in notes),
                "sentiment_id": sentiment["id"] if sentiment is not None else None,
                "cost_usd": completion.cost_usd,
            },
        )
        return await self._briefs.get(version)

    @staticmethod
    def _inputs_prompt(
        symbol: str, notes: list[dict[str, Any]], sentiment: dict[str, Any] | None
    ) -> str:
        blocks = [f"Candidate: {symbol}. Store-resident research inputs, newest first:"]
        for n in notes:
            authored = (n["authored_at"] or n["created_at"]).date().isoformat()
            blocks.append(
                f"--- note:{n['id']} | status: {n['status']} | authored: {authored}"
                f" | submitted: {n['created_at'].date().isoformat()}"
                f" | {n['title']}\n{n['body']}"
            )
        if sentiment is not None:
            drivers = ", ".join(sentiment["drivers"]) or "none reported"
            blocks.append(
                f"--- sent:{sentiment['id']} | live X sentiment"
                f" | {sentiment['ts'].isoformat()}\nscore: {sentiment['score']}"
                f" (-1 bearish .. +1 bullish), volume: {sentiment['volume']},"
                f" drivers: {drivers}"
            )
        return "\n\n".join(blocks)
