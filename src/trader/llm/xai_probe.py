"""xAI endpoint × model × tool probe — the diagnostic behind `trader xai probe` (#7).

Answers, from inside the running service, which of ``/v1/chat/completions`` and
``/v1/responses`` the SuperGrok OAuth bearer (and the ``XAI_API_KEY`` fallback) may
reach, per model, with and without the server-side ``x_search`` / ``web_search`` tools.
It runs on the service's own authenticated client (``llm.xai.xai_http_client``) so it
exercises the exact credential path the roles use and never opens a second OAuth
lineage.

Each probe is a minimal request (~16 output tokens; tool probes ask for the word "test"
over a one-day window) and **never raises**: every wire outcome is a ``ProbeResult``.
401/403 classify as ``entitlement`` (docs/lessons.md#oauth-refresh-margin); quarantine
decisions stay with the credential manager, this module only reports. Nothing here is
metered — it is diagnostics, not a role — but the token counts come back so the operator
can eyeball the cost of a matrix.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from trader.llm.base import LLMError
from trader.llm.xai import (
    CREDENTIAL_API_KEY,
    CREDENTIAL_AUTO,
    CREDENTIAL_EXTENSION,
    CREDENTIAL_OAUTH,
    CREDENTIAL_USED_EXTENSION,
)

ENDPOINT_CHAT = "chat/completions"
ENDPOINT_RESPONSES = "responses"
ENDPOINTS: tuple[str, ...] = (ENDPOINT_CHAT, ENDPOINT_RESPONSES)
TOOL_X_SEARCH = "x_search"
TOOL_WEB_SEARCH = "web_search"
TOOLS: tuple[str, ...] = (TOOL_X_SEARCH, TOOL_WEB_SEARCH)
CREDENTIALS: tuple[str, ...] = (CREDENTIAL_AUTO, CREDENTIAL_OAUTH, CREDENTIAL_API_KEY)

#: The issue's matrix (#7) plus the live default model; unknown ids are fine — an
#: unknown model prices at the default and this path is unmetered anyway.
DEFAULT_MODELS: tuple[str, ...] = ("grok-4", "grok-4.3", "grok-4-1-fast")

MAX_OUTPUT_TOKENS = 16
PLAIN_PROMPT = "Reply with the single word: ok"
TOOL_PROMPT = "Search for the word test and reply with the single word: ok"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_DELAY_SECONDS = 0.5
_DETAIL_CHARS = 200


@dataclass(frozen=True)
class ProbeResult:
    """One cell of the matrix. ``ok`` is "2xx with a parseable JSON body"."""

    endpoint: str
    model: str
    tool: str | None
    status_code: int | None
    ok: bool
    credential_used: str | None
    error_class: str | None
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    latency_ms: int = 0
    citations_count: int = 0
    #: A short, redacted-by-truncation excerpt of a failing body (the API's reason).
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validate(endpoint: str, tool: str | None, credential: str) -> None:
    if endpoint not in ENDPOINTS:
        raise ValueError(f"endpoint must be one of {ENDPOINTS}, got {endpoint!r}")
    if tool is not None and tool not in TOOLS:
        raise ValueError(f"tool must be one of {TOOLS} or None, got {tool!r}")
    if credential not in CREDENTIALS:
        raise ValueError(f"credential must be one of {CREDENTIALS}, got {credential!r}")


def build_request(
    endpoint: str, model: str, tool: str | None, *, today: date | None = None
) -> tuple[str, dict[str, Any]]:
    """The (path, JSON body) for one probe. Pure, so the shapes are unit-testable.

    ``x_search`` carries a one-day ``from_date``/``to_date`` window (the sentiment role's
    shape); ``web_search`` takes no window parameters on the wire, so it is sent bare.
    """
    prompt = PLAIN_PROMPT if tool is None else TOOL_PROMPT
    tools: list[dict[str, Any]] = []
    if tool == TOOL_X_SEARCH:
        end = today or datetime.now(tz=UTC).date()
        tools.append(
            {
                "type": TOOL_X_SEARCH,
                "from_date": (end - timedelta(days=1)).isoformat(),
                "to_date": end.isoformat(),
            }
        )
    elif tool == TOOL_WEB_SEARCH:
        tools.append({"type": TOOL_WEB_SEARCH})

    if endpoint == ENDPOINT_CHAT:
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
    else:
        body = {
            "model": model,
            "input": [{"role": "user", "content": prompt}],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": False,
        }
    if tools:
        body["tools"] = tools
    return f"/{endpoint}", body


def classify_status(status_code: int) -> str | None:
    """None for 2xx; otherwise the error class. 401/403 = entitlement, never retried."""
    if 200 <= status_code < 300:
        return None
    if status_code in (401, 403):
        return "entitlement"
    if status_code == 429:
        return "rate_limited"
    if status_code >= 500:
        return "server_error"
    return "bad_request"


def _usage(payload: dict[str, Any]) -> dict[str, int]:
    raw = payload.get("usage") or {}
    if not isinstance(raw, dict):
        raw = {}

    def pick(*keys: str) -> int:
        for key in keys:
            if raw.get(key) is None:
                continue
            try:
                return int(raw[key])
            except (TypeError, ValueError):
                continue
        return 0

    return {
        "input_tokens": pick("input_tokens", "prompt_tokens"),
        "output_tokens": pick("output_tokens", "completion_tokens"),
    }


def _citations(payload: dict[str, Any]) -> int:
    """Distinct citation URLs: responses annotations + either endpoint's top-level list."""
    seen: set[str] = set()
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        for chunk in item.get("content") or []:
            if not isinstance(chunk, dict):
                continue
            for note in chunk.get("annotations") or []:
                if isinstance(note, dict) and note.get("url"):
                    seen.add(str(note["url"]))
    for url in payload.get("citations") or []:
        if isinstance(url, str) and url:
            seen.add(url)
    return len(seen)


async def probe(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    model: str,
    tool: str | None = None,
    credential: str = CREDENTIAL_AUTO,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> ProbeResult:
    """One minimal request; every wire outcome comes back as a result, never an exception.

    Only a caller error (unknown endpoint/tool/credential) raises, before any I/O.
    """
    _validate(endpoint, tool, credential)
    path, body = build_request(endpoint, model, tool)
    extensions = {CREDENTIAL_EXTENSION: credential} if credential != CREDENTIAL_AUTO else {}

    def result(**kw: Any) -> ProbeResult:
        return ProbeResult(endpoint=endpoint, model=model, tool=tool, **kw)

    started = time.perf_counter()
    try:
        resp = await client.post(path, json=body, timeout=timeout_seconds, extensions=extensions)
    except httpx.TimeoutException:
        return result(
            status_code=None,
            ok=False,
            credential_used=None,
            error_class="timeout",
            latency_ms=_elapsed_ms(started),
        )
    except httpx.HTTPError as exc:
        return result(
            status_code=None,
            ok=False,
            credential_used=None,
            error_class="transport",
            latency_ms=_elapsed_ms(started),
            detail=_short(f"{type(exc).__name__}: {exc}"),
        )
    except LLMError as exc:  # ManagedBearerAuth: no usable (or no forced) credential
        return result(
            status_code=None,
            ok=False,
            credential_used=None,
            error_class="no_credential",
            latency_ms=_elapsed_ms(started),
            detail=_short(str(exc)),
        )

    latency_ms = _elapsed_ms(started)
    used = resp.request.extensions.get(CREDENTIAL_USED_EXTENSION)
    error_class = classify_status(resp.status_code)
    if error_class is not None:
        return result(
            status_code=resp.status_code,
            ok=False,
            credential_used=used,
            error_class=error_class,
            latency_ms=latency_ms,
            detail=_short(resp.text),
        )
    try:
        payload = resp.json()
        if not isinstance(payload, dict):
            raise ValueError("not a JSON object")
    except ValueError as exc:
        return result(
            status_code=resp.status_code,
            ok=False,
            credential_used=used,
            error_class="malformed",
            latency_ms=latency_ms,
            detail=_short(f"{exc}: {resp.text}"),
        )
    return result(
        status_code=resp.status_code,
        ok=True,
        credential_used=used,
        error_class=None,
        usage=_usage(payload),
        latency_ms=latency_ms,
        citations_count=_citations(payload),
    )


async def probe_matrix(
    client: httpx.AsyncClient,
    *,
    models: list[str],
    endpoints: list[str],
    tools: list[str | None],
    credential: str = CREDENTIAL_AUTO,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[ProbeResult]:
    """Every endpoint × model × tool cell, sequentially (no bursts against the vendor,
    and one OAuth refresh at most), with a short pause between calls."""
    cells = [(e, m, t) for e in endpoints for m in models for t in tools]
    results: list[ProbeResult] = []
    for index, (endpoint, model, tool) in enumerate(cells):
        if index:
            await asyncio.sleep(delay_seconds)
        results.append(
            await probe(
                client,
                endpoint=endpoint,
                model=model,
                tool=tool,
                credential=credential,
                timeout_seconds=timeout_seconds,
            )
        )
    return results


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _DETAIL_CHARS else text[:_DETAIL_CHARS] + "…"
