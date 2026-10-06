"""Seat runner: one xAI ``/v1/responses`` sweep → local gates → capture-key submission.

The process shape is the design's (docs/research/2026-09-16-grok-bots-macro-team.md §3):

    timer → SeatXaiClient.sweep (grok, x_search [seat's handles, if any] + web_search, 1-day window)
          → parse_notes (pydantic)          → grounding gate  (cite a URL the tool layer returned)
          → dedupe gate (GET /research/notes, last 7 days)   → size gate (distill once, then drop)
          → cap (``seat_max_notes``)         → CaptureClient.submit_note (capture key ONLY)
          → append_run (~/.local/state/trader/seat-runs.jsonl: ids, usage, estimated cost)

Honesty rules carried over from sentiment (docs/lessons.md#sentiment-no-citations-is-
unavailable): a note that cites nothing the search returned is the model's prior and is
dropped; ``authored_at`` is kept only when it parses tz-aware and is not in the future.
The seat is outside the service's usage meter, so it prices its own spend with the same
table (``llm/usage.py``) and records it locally. ``CaptureClient`` holds the capture key
and exposes exactly the capture surface — enforced in code, not prompt.

#10 lives at the bottom: ``build_report`` reads the run log back and asks the current
Macro View which submitted ids it actually cites (``[note:N]`` markers, the compiler's
own format — ``research/compiler.py``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog
from pydantic import BaseModel, Field, ValidationError

from trader.llm.base import Usage
from trader.llm.usage import (
    price_per_million,
    x_search_tool_cost_usd,
    x_search_units_from_citations,
)
from trader.llm.xai import ManagedBearerAuth
from trader.llm.xai_auth import build_manager
from trader.ops.config import Settings

log = structlog.get_logger(__name__)

PROVIDER = "xai"
#: web_search: $5 per 1k successful calls (docs.x.ai/developers/pricing, read 2026-09-16).
WEB_SEARCH_PER_1K_CALLS_USD = 5.0
#: Dedupe window: a live note with the same title or source inside it blocks a resubmit.
DEDUPE_WINDOW_DAYS = 7
#: How many live titles the prompt gets as "already filed".
ALREADY_FILED_LIMIT = 20
#: The compiler's citation marker (research/compiler.py, CITATION).
CITATION = re.compile(r"\[note:(\d+)\]")
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)

CREDENTIALS = ("api_key", "oauth", "auto")
#: The seat shares trader-api's SuperGrok login by default (#68: the token store's
#: cross-process lock makes a timer-driven refresh safe); the API key is the alternative.
DEFAULT_CREDENTIAL = "oauth"


class SeatError(RuntimeError):
    """Configuration or protocol failure; the CLI prints it and exits 1."""


#: x_search takes at most this many ``allowed_x_handles`` (docs.x.ai x-search tool;
#: docs/research/2026-09-16-grok-bots-macro-team.md §1b).
MAX_X_HANDLES = 20
#: An X handle: 1-15 letters, digits or underscores (the leading ``@`` is stripped first).
X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


def normalize_x_handles(handles: Iterable[str]) -> tuple[str, ...]:
    """Handles as x_search wants them: ``@`` and whitespace stripped, deduped
    case-insensitively (X handles are), first spelling kept. Raises SeatError on an
    empty or malformed handle, or on more than ``MAX_X_HANDLES``."""
    seen: dict[str, str] = {}
    for raw in handles:
        handle = str(raw).strip().removeprefix("@")
        if not X_HANDLE.match(handle):
            raise SeatError(f"invalid X handle {raw!r}: expected 1-15 letters, digits or _")
        seen.setdefault(handle.casefold(), handle)
    if len(seen) > MAX_X_HANDLES:
        raise SeatError(f"{len(seen)} X handles given; x_search accepts at most {MAX_X_HANDLES}")
    return tuple(seen.values())


@dataclass(frozen=True)
class Seat:
    """A desk seat: prompt + scope. Mechanics live in the runner, not here.

    ``x_handles`` restricts x_search to those accounts (``allowed_x_handles``); empty
    leaves X search open. web_search is never restricted. Handles are normalized and
    validated on construction, so a bad list fails before any request is made.
    """

    name: str
    description: str
    system_prompt: str  # format keys: max_notes, max_chars, symbols
    user_prompt: str  # format keys: from_date, to_date, already_filed, max_notes, max_chars
    symbols_allowed: tuple[str, ...]
    asset_classes: tuple[str, ...]
    tags: tuple[str, ...]
    x_handles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "x_handles", normalize_x_handles(self.x_handles))


# --- model output --------------------------------------------------------------------


class CandidateNote(BaseModel):
    """One note as the model returns it. Scope/tag defaults are the seat's, applied later."""

    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    symbols: list[str] = []
    tags: list[str] = []
    source: str | None = None
    authored_at: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    citations: list[str] = []

    def authored_at_utc(self, *, now: datetime) -> datetime | None:
        """The publication date when it is usable: tz-aware and not in the future.
        Anything else is omitted — unknown beats guessed (SKILL.md)."""
        if not self.authored_at:
            return None
        try:
            parsed = datetime.fromisoformat(self.authored_at.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed > now:
            return None
        return parsed.astimezone(UTC)

    def scoped_symbols(self, seat: Seat) -> list[str]:
        allowed = {s.upper() for s in seat.symbols_allowed}
        seen: list[str] = []
        for symbol in self.symbols:
            upper = symbol.strip().upper()
            if upper in allowed and upper not in seen:
                seen.append(upper)
        return seen


class SeatOutput(BaseModel):
    notes: list[CandidateNote] = []


NOTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "symbols": {"type": "array", "items": {"type": "string"}},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "source": {"type": "string"},
                    "authored_at": {"type": "string"},
                    "confidence": {"type": "number"},
                    "citations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title", "body", "citations"],
            },
        }
    },
    "required": ["notes"],
}


def parse_notes(text: str) -> list[CandidateNote]:
    """The JSON object in the model's reply → validated candidates. Raises SeatError."""
    match = JSON_OBJECT.search(text or "")
    if match is None:
        raise SeatError("seat reply carried no JSON object")
    try:
        return SeatOutput.model_validate(json.loads(match.group(0))).notes
    except (json.JSONDecodeError, ValidationError) as exc:
        raise SeatError(f"seat reply failed validation: {exc}") from exc


# --- the /v1/responses call ------------------------------------------------------------


@dataclass
class SearchResult:
    text: str
    citations: list[str]
    usage: Usage
    x_search_calls: int
    web_search_calls: int


def _is_x_url(url: str) -> bool:
    lowered = url.lower()
    return "x.com/" in lowered or "twitter.com/" in lowered


def parse_responses_payload(payload: dict[str, Any]) -> SearchResult:
    """Text, distinct citation URLs, usage and tool-call counts from a responses body.

    Tool calls are counted from output items (``*x_search*`` / ``*web_search*`` types);
    when the payload lists none, one call per tool that produced a citation is assumed —
    a lower bound, like the x_search unit count in ``llm/usage.py``.
    """
    texts: list[str] = []
    citations: list[str] = []
    x_calls = 0
    web_calls = 0
    for item in payload.get("output", []) or []:
        kind = str(item.get("type", ""))
        if "x_search" in kind:
            x_calls += 1
        elif "web_search" in kind:
            web_calls += 1
        for chunk in item.get("content", []) or []:
            if chunk.get("type") in ("output_text", "text"):
                texts.append(chunk.get("text", ""))
                for note in chunk.get("annotations", []) or []:
                    url = note.get("url")
                    if url and url not in citations:
                        citations.append(url)
    for url in payload.get("citations", []) or []:
        if url and url not in citations:
            citations.append(url)
    if x_calls == 0 and any(_is_x_url(u) for u in citations):
        x_calls = 1
    if web_calls == 0 and any(not _is_x_url(u) for u in citations):
        web_calls = 1
    usage = payload.get("usage", {}) or {}
    return SearchResult(
        text="\n".join(texts),
        citations=citations,
        usage=Usage(
            prompt_tokens=int(usage.get("input_tokens", 0)),
            completion_tokens=int(usage.get("output_tokens", 0)),
        ),
        x_search_calls=x_calls,
        web_search_calls=web_calls,
    )


DISTILL_PROMPT = """Distill the research note below to at most {max_chars} characters of \
tight markdown. Keep every number and claim exactly as stated; add nothing; drop padding. \
Reply with the distilled body only, no JSON, no preamble.

---
{body}"""


class SeatXaiClient:
    """Thin ``/v1/responses`` caller: one sweep (tools on) and one distill (tools off)."""

    def __init__(self, http: httpx.AsyncClient, *, model: str) -> None:
        self._http = http
        self.model = model

    async def _post(self, body: dict[str, Any]) -> SearchResult:
        try:
            resp = await self._http.post("/responses", json=body)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SeatError(
                f"xAI HTTP {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SeatError(f"xAI request failed: {exc}") from exc
        return parse_responses_payload(resp.json())

    async def sweep(
        self,
        seat: Seat,
        *,
        from_date: str,
        to_date: str,
        already_filed: list[str],
        max_notes: int,
        max_chars: int,
    ) -> SearchResult:
        filed = "\n".join(f"- {t}" for t in already_filed) or "- (nothing yet)"
        system = seat.system_prompt.format(
            max_notes=max_notes, max_chars=max_chars, symbols=list(seat.symbols_allowed)
        )
        user = seat.user_prompt.format(
            from_date=from_date,
            to_date=to_date,
            already_filed=filed,
            max_notes=max_notes,
            max_chars=max_chars,
        )
        x_search: dict[str, Any] = {"type": "x_search", "from_date": from_date, "to_date": to_date}
        if seat.x_handles:
            x_search["allowed_x_handles"] = list(seat.x_handles)
        return await self._post(
            {
                "model": self.model,
                "input": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "tools": [x_search, {"type": "web_search"}],
                "text": {
                    "format": {"type": "json_schema", "name": "seat_notes", "schema": NOTE_SCHEMA}
                },
                "store": False,
            }
        )

    async def distill(self, body: str, *, max_chars: int) -> SearchResult:
        return await self._post(
            {
                "model": self.model,
                "input": [
                    {
                        "role": "user",
                        "content": DISTILL_PROMPT.format(max_chars=max_chars, body=body),
                    }
                ],
                "store": False,
            }
        )


# --- cost --------------------------------------------------------------------------------


@dataclass
class SweepCost:
    """The seat's own estimate: token prices from the shared table plus tool fees."""

    input_tokens: int = 0
    output_tokens: int = 0
    x_search_calls: int = 0
    x_search_posts: int = 0
    x_search_profiles: int = 0
    web_search_calls: int = 0
    token_usd: float = 0.0
    x_search_usd: float = 0.0
    web_search_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return self.token_usd + self.x_search_usd + self.web_search_usd

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "total_usd": round(self.total_usd, 6)}

    def __add__(self, other: SweepCost) -> SweepCost:
        return SweepCost(
            **{k: getattr(self, k) + getattr(other, k) for k in asdict(self)},
        )


def estimate_cost(
    model: str,
    usage: Usage,
    *,
    citations: list[str],
    x_search_calls: int,
    web_search_calls: int,
    when: datetime,
) -> SweepCost:
    prompt_price, completion_price = price_per_million(PROVIDER, model)
    posts, profiles = x_search_units_from_citations(citations)
    return SweepCost(
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        x_search_calls=x_search_calls,
        x_search_posts=posts,
        x_search_profiles=profiles,
        web_search_calls=web_search_calls,
        token_usd=(usage.prompt_tokens * prompt_price + usage.completion_tokens * completion_price)
        / 1_000_000,
        x_search_usd=(
            x_search_tool_cost_usd(calls=x_search_calls, posts=posts, profiles=profiles, when=when)
            if x_search_calls
            else 0.0
        ),
        web_search_usd=web_search_calls * WEB_SEARCH_PER_1K_CALLS_USD / 1000,
    )


# --- gates ---------------------------------------------------------------------------------


#: An X post is its status id: x_search returns ``x.com/i/status/<id>`` while the model cites
#: ``x.com/<handle>/status/<id>`` for the same post, so both normalize to ``x.com/status/<id>``.
X_STATUS = re.compile(r"^/(?:[^/]+/)?status(?:es)?/(\d+)")
X_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"}


def normalize_url(url: str | None) -> str:
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    if host in X_HOSTS:
        status = X_STATUS.match(parts.path)
        if status:
            return f"x.com/status/{status.group(1)}"
    path = parts.path.rstrip("/")
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"


def normalize_title(title: str | None) -> str:
    return " ".join((title or "").casefold().split())


def ground(note: CandidateNote, returned: set[str]) -> CandidateNote | None:
    """Keep the citations the tool layer actually returned; none left → drop the note.
    ``source`` must be one of them, else it is repointed at the first grounded URL."""
    if not returned:
        return None
    by_norm = {normalize_url(u): u for u in returned}
    kept: list[str] = []
    for url in note.citations:
        hit = by_norm.get(normalize_url(url))
        if hit and hit not in kept:
            kept.append(hit)
    if not kept:
        return None
    source = note.source if normalize_url(note.source) in {normalize_url(u) for u in kept} else None
    return note.model_copy(update={"citations": kept, "source": source or kept[0]})


def _created_at(row: dict[str, Any]) -> datetime | None:
    raw = row.get("created_at")
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_duplicate(
    note: CandidateNote,
    live_notes: list[dict[str, Any]],
    *,
    now: datetime,
    window_days: int = DEDUPE_WINDOW_DAYS,
) -> bool:
    """A live note from the last ``window_days`` with the same title or source URL."""
    cutoff = now - timedelta(days=window_days)
    title = normalize_title(note.title)
    source = normalize_url(note.source)
    for row in live_notes:
        created = _created_at(row)
        if created is not None and created < cutoff:
            continue
        if title and normalize_title(row.get("title")) == title:
            return True
        if source and normalize_url(row.get("source")) == source:
            return True
    return False


# --- the capture client (the ONLY holder of the capture key) ---------------------------------


class CaptureClient:
    """Exactly the capture surface: list notes, submit a note, read the Macro View.

    No other method exists on purpose (design §3.3: the runner's capture client calls
    ``submit_research_note`` / ``get_research_notes`` and nothing else, enforced in code).
    """

    __slots__ = ("_http",)

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SeatError(
                f"trader API {method} {path}: HTTP {exc.response.status_code} "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SeatError(f"trader API {method} {path} failed: {exc}") from exc
        return resp.json()

    async def list_notes(self, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = await self._request("GET", "/research/notes", params={"limit": limit})
        return list(rows or [])

    async def submit_note(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/research/notes", json=payload)

    async def macro_view(self) -> dict[str, Any] | None:
        return await self._request("GET", "/research/view")


def build_capture_http(settings: Settings) -> httpx.AsyncClient:
    """The trader API client for the write path: capture key, never the operator key."""
    if not settings.capture_api_key:
        raise SeatError(
            "TRADER_CAPTURE_API_KEY is unset: the seat submits with the capture-scoped key "
            "only (#8); it never uses TRADER_API_KEY for the write path."
        )
    return httpx.AsyncClient(
        base_url=settings.api_base_url,
        headers={"x-api-key": settings.capture_api_key},
        timeout=60.0,
    )


def build_operator_http(settings: Settings) -> httpx.AsyncClient | None:
    """Operator-key client for the one operator-only read (`GET /usage`) in `seat report`."""
    if not settings.api_key:
        return None
    return httpx.AsyncClient(
        base_url=settings.api_base_url, headers={"x-api-key": settings.api_key}, timeout=60.0
    )


def build_xai_http(
    settings: Settings, *, credential: str = DEFAULT_CREDENTIAL
) -> httpx.AsyncClient:
    """The xAI client for a standalone seat process.

    Default ``oauth``: the same SuperGrok login as ``trader-api``, through the shared
    encrypted token store. Safe from a timer because the manager holds a cross-process
    lock across every refresh and adopts a rotation the service already made (#68,
    docs/lessons.md#oauth-refresh-margin). With the store configured, ``oauth`` never
    falls back to the key silently (a quarantine surfaces as a failed run); with no store
    configured (``TRADER_XAI_TOKEN_KEY`` unset) it runs on ``XAI_API_KEY``. ``api_key``
    pins the key alone; ``auto`` is OAuth with the key as a runtime fallback. The seat
    refuses only when neither credential path exists.
    """
    if credential not in CREDENTIALS:
        raise SeatError(f"credential must be one of {', '.join(CREDENTIALS)}")
    manager = None
    if credential == "api_key":
        if not settings.xai_api_key:
            raise SeatError(
                "XAI_API_KEY is unset; --credential api_key needs it. Omit the flag to share "
                "the service's SuperGrok OAuth login (TRADER_XAI_TOKEN_KEY + `trader xai login`)."
            )
    else:
        manager = build_manager(settings)
        if manager is None and not settings.xai_api_key:
            raise SeatError(
                "no xAI credential: set TRADER_XAI_TOKEN_KEY and run `trader xai login` "
                "(the shared OAuth login), or set XAI_API_KEY"
            )
    # ``oauth`` with a store: the login only. Without one it can only mean the key.
    fallback = settings.xai_api_key if (credential != "oauth" or manager is None) else ""
    return httpx.AsyncClient(
        base_url=settings.xai_base_url,
        auth=ManagedBearerAuth(manager, fallback),
        timeout=180.0,
    )


# --- run log ---------------------------------------------------------------------------------


def append_run(path: Path | str, record: dict[str, Any]) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return target


def read_runs(
    path: Path | str, *, seat: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """Runs newest first. Corrupt lines are skipped, a missing file is no runs."""
    target = Path(path).expanduser()
    if not target.exists():
        return []
    runs: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and (seat is None or record.get("seat") == seat):
            runs.append(record)
    runs.reverse()
    return runs[:limit] if limit else runs


# --- the runner --------------------------------------------------------------------------------


@dataclass
class _Survivor:
    note: CandidateNote
    body: str
    authored_at: datetime | None
    symbols: list[str]


@dataclass
class SeatRunner:
    seat: Seat
    xai: SeatXaiClient
    capture: CaptureClient
    max_notes: int = 3
    max_chars: int = 1400
    state_path: Path | str = "~/.local/state/trader/seat-runs.jsonl"
    now: datetime | None = None
    window_hours: int = 24
    dropped: list[dict[str, str]] = field(default_factory=list)

    def _drop(self, title: str, reason: str) -> None:
        self.dropped.append({"title": title, "reason": reason})
        log.info("seat_note_dropped", seat=self.seat.name, title=title, reason=reason)

    async def run(self, *, dry_run: bool = False) -> dict[str, Any]:
        now = self.now or datetime.now(tz=UTC)
        self.dropped = []
        from_date = (now - timedelta(hours=self.window_hours)).date().isoformat()
        to_date = now.date().isoformat()

        live = await self.capture.list_notes()
        already_filed = [
            str(n.get("title", ""))
            for n in live
            if self.seat.tags[0] in (n.get("tags") or []) and n.get("title")
        ][:ALREADY_FILED_LIMIT]

        result = await self.xai.sweep(
            self.seat,
            from_date=from_date,
            to_date=to_date,
            already_filed=already_filed,
            max_notes=self.max_notes,
            max_chars=self.max_chars,
        )
        cost = estimate_cost(
            self.xai.model,
            result.usage,
            citations=result.citations,
            x_search_calls=result.x_search_calls,
            web_search_calls=result.web_search_calls,
            when=now,
        )
        candidates = parse_notes(result.text)
        returned = set(result.citations)

        survivors: list[_Survivor] = []
        seen_titles: set[str] = set()
        seen_sources: set[str] = set()
        for raw in candidates:
            grounded = ground(raw, returned)
            if grounded is None:
                self._drop(raw.title, "ungrounded")
                continue
            title_key = normalize_title(grounded.title)
            source_key = normalize_url(grounded.source)
            if (
                title_key in seen_titles
                or (source_key and source_key in seen_sources)
                or is_duplicate(grounded, live, now=now)
            ):
                self._drop(grounded.title, "duplicate")
                continue
            if len(survivors) >= self.max_notes:
                self._drop(grounded.title, "max_notes")
                continue
            body = grounded.body.strip()
            if len(body) > self.max_chars:
                distilled = await self.xai.distill(body, max_chars=self.max_chars)
                cost = cost + estimate_cost(
                    self.xai.model,
                    distilled.usage,
                    citations=[],
                    x_search_calls=0,
                    web_search_calls=0,
                    when=now,
                )
                body = distilled.text.strip()
                if not body or len(body) > self.max_chars:
                    self._drop(grounded.title, "oversize")
                    continue
            seen_titles.add(title_key)
            if source_key:
                seen_sources.add(source_key)
            survivors.append(
                _Survivor(
                    note=grounded,
                    body=body,
                    authored_at=grounded.authored_at_utc(now=now),
                    symbols=grounded.scoped_symbols(self.seat),
                )
            )

        submitted: list[dict[str, Any]] = []
        would_submit: list[dict[str, Any]] = []
        for s in survivors:
            payload = self._payload(s)
            if dry_run:
                would_submit.append(payload)
                continue
            row = await self.capture.submit_note(payload)
            submitted.append(
                {
                    "id": row.get("id"),
                    "title": payload["title"],
                    "source": payload.get("source"),
                    "expires_at": row.get("expires_at"),
                }
            )
            log.info("seat_note_submitted", seat=self.seat.name, id=row.get("id"))

        record = {
            "ts": now.isoformat(),
            "seat": self.seat.name,
            "model": self.xai.model,
            "window": {"from": from_date, "to": to_date},
            "x_handles": list(self.seat.x_handles),
            "candidates": len(candidates),
            "citations_returned": len(result.citations),
            "submitted": submitted,
            "would_submit": would_submit,
            "dropped": list(self.dropped),
            "usage": {"input_tokens": cost.input_tokens, "output_tokens": cost.output_tokens},
            "cost_usd": cost.to_dict(),
            "dry_run": dry_run,
        }
        append_run(self.state_path, record)
        log.info(
            "seat_run_complete",
            seat=self.seat.name,
            x_handles=list(self.seat.x_handles),
            submitted=len(submitted),
            dropped=len(self.dropped),
            cost_usd=round(cost.total_usd, 6),
            dry_run=dry_run,
        )
        return record

    def _payload(self, s: _Survivor) -> dict[str, Any]:
        extra = [t.strip().lower() for t in s.note.tags if t.strip()]
        tags = list(self.seat.tags) + [t for t in dict.fromkeys(extra) if t not in self.seat.tags]
        payload: dict[str, Any] = {
            "title": s.note.title.strip(),
            "body": s.body,
            "tags": tags,
            "asset_classes": list(self.seat.asset_classes),
            "symbols": s.symbols,
            "source": s.note.source,
        }
        if s.authored_at is not None:
            payload["authored_at"] = s.authored_at.isoformat()
        return payload


# --- #10: the citation loop ----------------------------------------------------------------------


def cited_note_ids(view: dict[str, Any] | None) -> set[int]:
    """Note ids the compiled Macro View cites: ``[note:N]`` in the thesis and sections."""
    if not view:
        return set()
    texts = [str(view.get("global_thesis") or "")]
    sections = view.get("sections") or {}
    if isinstance(sections, dict):
        texts.extend(str(v) for v in sections.values())
    return {int(m) for text in texts for m in CITATION.findall(text)}


async def _usage_total_for_day(usage_http: httpx.AsyncClient, day: str) -> float | None:
    try:
        resp = await usage_http.get("/usage", params={"day": day})
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("seat_report_usage_unavailable", day=day, error=str(exc))
        return None
    totals = (resp.json() or {}).get("totals") or {}
    value = totals.get("cost_usd")
    return float(value) if value is not None else None


async def build_report(
    runs: list[dict[str, Any]],
    capture: CaptureClient,
    *,
    usage_http: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """Per run: which submitted ids the CURRENT Macro View cites, plus cost.

    ``seat_usd`` is the seat's own estimate from the run log. ``metered_usd`` is the
    day's ``usage_daily`` total (service-side spend: the compile the notes triggered
    etc.), read with the operator key when one is given, else None. Dry runs are skipped.
    """
    view = await capture.macro_view()
    cited = cited_note_ids(view)
    inputs = {int(i) for i in (view or {}).get("note_ids") or []}
    version = (view or {}).get("version")
    usage_cache: dict[str, float | None] = {}
    rows: list[dict[str, Any]] = []
    for run in runs:
        if run.get("dry_run"):
            continue
        ids = [int(s["id"]) for s in run.get("submitted") or [] if s.get("id") is not None]
        day = str(run.get("ts", ""))[:10]
        metered: float | None = None
        if usage_http is not None and day:
            if day not in usage_cache:
                usage_cache[day] = await _usage_total_for_day(usage_http, day)
            metered = usage_cache[day]
        rows.append(
            {
                "ts": run.get("ts"),
                "seat": run.get("seat"),
                "model": run.get("model"),
                "submitted_ids": ids,
                "cited_ids": sorted(i for i in ids if i in cited),
                "in_view_ids": sorted(i for i in ids if i in inputs),
                "view_version": version,
                "seat_usd": float((run.get("cost_usd") or {}).get("total_usd") or 0.0),
                "metered_usd": metered,
            }
        )
    return rows


def render_report(rows: list[dict[str, Any]]) -> str:
    header = (
        f"{'run (UTC)':<17}{'seat':<14}{'cited/sub':<11}{'cited ids':<18}"
        f"{'seat $':>8}{'day metered $':>15}"
    )
    lines = [header]
    total_sub = total_cited = 0
    for r in rows:
        n_sub, n_cited = len(r["submitted_ids"]), len(r["cited_ids"])
        total_sub += n_sub
        total_cited += n_cited
        metered = "-" if r["metered_usd"] is None else f"{r['metered_usd']:.4f}"
        lines.append(
            f"{str(r['ts'])[:16]:<17}{str(r['seat']):<14}{f'{n_cited}/{n_sub}':<11}"
            f"{(','.join(str(i) for i in r['cited_ids']) or '-'):<18}"
            f"{r['seat_usd']:>8.4f}{metered:>15}"
        )
    version = rows[0]["view_version"] if rows else None
    lines.append(
        f"cited {total_cited}/{total_sub} submitted notes in Macro View "
        f"{'v' + str(version) if version else '(none compiled)'}"
    )
    return "\n".join(lines)
