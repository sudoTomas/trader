"""`trader doctor` — first-run preflight. Six checks, one table, one exit code.

Each check answers "can the loop run here?" with ``ok`` / ``missing`` / ``degraded``,
the env var(s) that fix it, and a one-line hint. Required checks (Postgres, decide
endpoint, broker, market data) that are ``missing`` exit 1; ``degraded`` alone exits 0.
The xAI credential is optional and the mode summary is printed, not judged.

Read-only by design: it lists pending migrations without applying them, it never
constructs a live Alpaca client (``TRADER_ALPACA_LIVE_ENABLED=true`` only earns a
warning), and its one LLM completion is not metered into ``usage_daily``. Local, like
``trader xai`` — reads Settings directly and never goes through the API.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
import psycopg
from psycopg.rows import dict_row

from trader.brokers.alpaca import PAPER_REST, AlpacaBroker
from trader.brokers.base import BrokerError
from trader.core.symbols import default_registry
from trader.data.base import MarketData, MarketDataError
from trader.data.composite import build_market_data
from trader.llm.base import LLMError
from trader.llm.nemotron import PROVIDER as NEMOTRON
from trader.llm.registry import LLMRegistry, build_llm_registry
from trader.llm.xai_auth import XaiAuthError, build_manager
from trader.ops.config import Settings
from trader.store.migrate import MIGRATIONS_DIR, _load_migrations

Status = Literal["ok", "missing", "degraded"]

CHECK_ORDER = ("postgres", "decide", "broker", "market_data", "xai", "mode")

#: The smallest schema-constrained completion that proves guided decoding works.
PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}
PROBE_MESSAGES = [
    {"role": "system", "content": "You answer with JSON only."},
    {"role": "user", "content": 'Reply with exactly {"ok": true}.'},
]

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    required: bool
    detail: str  # one line: what was found
    env: tuple[str, ...] = ()  # the variable(s) that fix it
    hint: str = ""  # one line: what to do
    data: dict[str, Any] = field(default_factory=dict)  # machine detail (--json)
    warnings: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.required and self.status == "missing"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "required": self.required,
            "detail": self.detail,
            "env": list(self.env),
            "hint": self.hint,
            "data": self.data,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DoctorReport:
    checks: list[CheckResult]

    @property
    def exit_code(self) -> int:
        return 1 if any(c.blocking for c in self.checks) else 0

    @property
    def warnings(self) -> list[str]:
        return [w for c in self.checks for w in c.warnings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.exit_code == 0,
            "exit_code": self.exit_code,
            "checks": [c.to_dict() for c in self.checks],
            "warnings": self.warnings,
        }

    def render(self) -> str:
        """The default table: one row per check, then the fixes, then the verdict."""
        lines: list[str] = []
        for warning in self.warnings:
            lines.append(f"WARNING: {warning}")
        if self.warnings:
            lines.append("")
        name_w = max(len("CHECK"), *(len(c.name) for c in self.checks))
        status_w = len("degraded")
        lines.append(f"{'CHECK':<{name_w}}  {'STATUS':<{status_w}}  DETAIL")
        for c in self.checks:
            lines.append(f"{c.name:<{name_w}}  {c.status:<{status_w}}  {c.detail}")
        fixes = [c for c in self.checks if c.status != "ok" and (c.env or c.hint)]
        if fixes:
            lines.append("")
            lines.append("fix:")
            for c in fixes:
                parts = []
                if c.env:
                    parts.append("set " + ", ".join(c.env))
                if c.hint:
                    parts.append(c.hint)
                lines.append(f"  {c.name:<{name_w}}  {' — '.join(parts)}")
        blocking = [c.name for c in self.checks if c.blocking]
        lines.append("")
        if blocking:
            lines.append(f"result: required checks missing ({', '.join(blocking)}) → exit 1")
        else:
            lines.append("result: ready → exit 0")
        return "\n".join(lines)


# --- 1. Postgres -------------------------------------------------------------


async def check_postgres(
    settings: Settings, *, connect_seconds: float = 5.0, migrations_dir: Path = MIGRATIONS_DIR
) -> CheckResult:
    """Reachable on ``TRADER_DATABASE_URL``; pending migrations listed, never applied."""
    env = ("TRADER_DATABASE_URL",)
    hint_up = "docker compose -f infra/docker-compose.yml up -d db, then check the URL"
    names = [name for name, _sql in _load_migrations(migrations_dir)]
    try:
        async with await psycopg.AsyncConnection.connect(
            settings.database_url, connect_timeout=int(connect_seconds), row_factory=dict_row
        ) as conn:
            await conn.set_read_only(True)
            has_table = await (
                await conn.execute("SELECT to_regclass('schema_migrations') AS t")
            ).fetchone()
            done: set[str] = set()
            if has_table and has_table["t"]:
                rows = await (await conn.execute("SELECT name FROM schema_migrations")).fetchall()
                done = {row["name"] for row in rows}
    except (psycopg.Error, OSError) as exc:
        message = " ".join(str(exc).split()) or type(exc).__name__
        return CheckResult(
            "postgres", "missing", True, f"unreachable: {message}", env, hint_up, {"error": message}
        )
    pending = [name for name in names if name not in done]
    data = {"applied": len(done), "pending": pending, "url": _redact(settings.database_url)}
    if pending:
        return CheckResult(
            "postgres",
            "degraded",
            True,
            f"reachable; {len(pending)} migration(s) pending: {', '.join(pending)}",
            (),
            "start the API — it applies migrations on boot (nothing was applied now)",
            data,
        )
    return CheckResult(
        "postgres",
        "ok",
        True,
        f"reachable; {len(done)} migrations applied, 0 pending",
        env,
        "",
        data,
    )


def _redact(url: str) -> str:
    parsed = urlparse(url)
    if parsed.password:
        netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@")
        return parsed._replace(netloc=netloc).geturl()
    return url


# --- 2. Decide endpoint ------------------------------------------------------


def _decide_env(settings: Settings) -> tuple[str, ...]:
    if settings.decide_base_url.strip():
        return ("TRADER_DECIDE_BASE_URL", "TRADER_DECIDE_MODEL", "TRADER_DECIDE_API_KEY")
    return ("TRADER_NEMOTRON_BASE_URL",)


def _decide_hint(settings: Settings) -> str:
    if settings.decide_base_url.strip():
        return (
            "any OpenAI-compatible /v1 works; check the URL, model id, and key "
            "(or unset TRADER_DECIDE_BASE_URL to use the Nemotron preset)"
        )
    return (
        f"Nemotron preset: serve a vLLM at TRADER_NEMOTRON_BASE_URL ({settings.nemotron_base_url}) "
        "or point TRADER_DECIDE_BASE_URL/MODEL/API_KEY at any OpenAI-compatible endpoint"
    )


async def check_decide(
    settings: Settings, *, registry: LLMRegistry | None = None, wait_seconds: float = 60.0
) -> CheckResult:
    """One tiny schema-constrained completion through the loop's own decide client."""
    env, hint = _decide_env(settings), _decide_hint(settings)
    built_here = registry is None
    try:
        registry = registry or build_llm_registry(settings)
        client = registry.chain_for("decide")[0]
    except (LLMError, ValueError) as exc:
        return CheckResult("decide", "missing", True, f"not configured: {exc}", env, hint)
    provider, model = client.name, client.model
    data: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "preset": "nemotron" if provider == NEMOTRON else "openai_compat",
    }
    started = time.perf_counter()
    try:
        completion = await asyncio.wait_for(
            registry.complete("decide", PROBE_MESSAGES, schema=PROBE_SCHEMA, max_tokens=16),
            timeout=wait_seconds,
        )
    except LLMError as exc:
        return CheckResult("decide", "missing", True, f"{provider} {model}: {exc}", env, hint, data)
    except TimeoutError:
        return CheckResult(
            "decide",
            "missing",
            True,
            f"{provider} {model}: no reply in {wait_seconds:.0f}s",
            env,
            hint,
            data,
        )
    finally:
        if built_here:
            await _close(client)
    latency_ms = round((time.perf_counter() - started) * 1000)
    data["latency_ms"] = latency_ms
    label = f"{provider} {model} {latency_ms} ms"
    try:
        parsed = json.loads(completion.text)
        if not isinstance(parsed, dict) or "ok" not in parsed:
            raise ValueError("missing 'ok'")
    except (json.JSONDecodeError, ValueError):
        data["raw"] = completion.text[:120]
        return CheckResult(
            "decide",
            "degraded",
            True,
            f"{label}; reply did not honor the JSON schema: {completion.text[:60]!r}",
            env,
            "the endpoint must support response_format json_schema (vLLM, OpenAI, Groq do); "
            "decides will fail validation and retry once per wakeup",
            data,
        )
    return CheckResult("decide", "ok", True, label, env, "", data)


async def _close(client: Any) -> None:
    close = getattr(client, "close", None)
    if close is not None:
        try:
            await close()
        except Exception:  # best-effort cleanup of a probe client; the verdict stands
            pass


# --- 3. Broker ---------------------------------------------------------------

_ALPACA_ENV = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET")
_ALPACA_HINT = "paper keys from https://app.alpaca.markets (Paper Trading → API keys)"


async def check_broker(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> CheckResult:
    """``GET /v2/account`` on the PAPER host. Never constructs a live client."""
    warnings: list[str] = []
    if settings.alpaca_live_enabled:
        warnings.append(
            "TRADER_ALPACA_LIVE_ENABLED=true is set. The doctor probes the PAPER host only and "
            "never constructs a live client; unset this unless you deliberately mean live money."
        )
    if not (settings.alpaca_api_key_id and settings.alpaca_api_secret):
        return CheckResult(
            "broker",
            "missing",
            True,
            "ALPACA_API_KEY_ID / ALPACA_API_SECRET unset",
            _ALPACA_ENV,
            _ALPACA_HINT,
            {"host": PAPER_REST},
            warnings,
        )
    data: dict[str, Any] = {"host": PAPER_REST}
    try:
        broker = AlpacaBroker(
            settings.alpaca_api_key_id,
            settings.alpaca_api_secret,
            live=False,
            live_enabled=False,
            client=client,
        )
    except BrokerError as exc:
        return CheckResult(
            "broker", "missing", True, str(exc), _ALPACA_ENV, _ALPACA_HINT, data, warnings
        )
    try:
        account = await broker.get_account()
    except BrokerError as exc:
        return CheckResult(
            "broker",
            "missing",
            True,
            f"paper account rejected: {exc}",
            _ALPACA_ENV,
            "check the keys are PAPER keys (paper-api.alpaca.markets), not live ones",
            data,
            warnings,
        )
    finally:
        if client is None:
            await _close(broker)
    data.update(
        equity=account.equity,
        cash=account.cash,
        buying_power=account.buying_power,
        account_status=account.status,
        tradable=account.tradable,
    )
    label = f"paper account {account.status}, equity ${account.equity:,.2f}"
    if not account.tradable:
        return CheckResult(
            "broker",
            "degraded",
            True,
            f"{label}; trading blocked at the venue",
            _ALPACA_ENV,
            "the account is not tradable (status/blocks) — check the Alpaca dashboard",
            data,
            warnings,
        )
    return CheckResult("broker", "ok", True, label, _ALPACA_ENV, "", data, warnings)


# --- 4. Market data ----------------------------------------------------------


def _loop_symbols(settings: Settings) -> list[str]:
    return [t.strip() for t in settings.loop_symbols.split(",") if t.strip()]


async def check_market_data(
    settings: Settings, *, market_data: MarketData | None = None
) -> CheckResult:
    """One latest quote for the first symbol in ``TRADER_LOOP_SYMBOLS``."""
    tickers = _loop_symbols(settings)
    if not tickers:
        return CheckResult(
            "market_data",
            "missing",
            True,
            "TRADER_LOOP_SYMBOLS is empty",
            ("TRADER_LOOP_SYMBOLS",),
            "comma-separated tickers, e.g. BTC/USD,ETH/USD",
        )
    ticker = tickers[0]
    try:
        symbol = default_registry().get(ticker)
    except KeyError as exc:
        return CheckResult(
            "market_data",
            "missing",
            True,
            str(exc),
            ("TRADER_LOOP_SYMBOLS",),
            "only symbols in the seed universe are tradable",
        )
    data: dict[str, Any] = {"symbol": ticker, "asset_class": symbol.asset_class.value}
    if not (settings.alpaca_api_key_id and settings.alpaca_api_secret):
        return CheckResult(
            "market_data",
            "missing",
            True,
            "no Alpaca keys (same keys serve broker and market data)",
            _ALPACA_ENV,
            _ALPACA_HINT,
            data,
        )
    provider = market_data or build_market_data(settings)
    try:
        quote = await provider.get_latest_quote(symbol)
    except (MarketDataError, ValueError) as exc:
        return CheckResult(
            "market_data",
            "missing",
            True,
            f"no quote for {ticker}: {exc}",
            _ALPACA_ENV,
            "Alpaca Data uses the broker keys; crypto is free, equities are the IEX feed",
            data,
        )
    data.update(bid=quote.bid, ask=quote.ask, mid=quote.mid, ts=quote.ts.isoformat())
    return CheckResult(
        "market_data",
        "ok",
        True,
        f"{ticker} bid {quote.bid:g} / ask {quote.ask:g} @ {quote.ts.isoformat()}",
        _ALPACA_ENV,
        "",
        data,
    )


# --- 5. xAI (optional) -------------------------------------------------------

_XAI_ENV = ("XAI_API_KEY", "TRADER_XAI_TOKEN_KEY")
_XAI_ABSENT = (
    "no xAI credential — research roles (brief/compiler/critic) fall back to the decide model; "
    "X sentiment is off"
)


def check_xai(settings: Settings) -> CheckResult:
    """Optional: OAuth store or ``XAI_API_KEY``. Absent is degraded, never blocking."""
    oauth = "disabled"
    try:
        manager = build_manager(settings)
        if manager is not None:
            oauth = manager.status
    except XaiAuthError as exc:
        oauth = f"error: {exc}"
    data = {"oauth": oauth, "api_key": bool(settings.xai_api_key)}
    if oauth == "ok":
        data["source"] = "oauth"
        return CheckResult(
            "xai", "ok", False, "SuperGrok OAuth credential present", _XAI_ENV, "", data
        )
    if settings.xai_api_key:
        data["source"] = "api_key"
        detail = "XAI_API_KEY present" + (f" (oauth {oauth})" if oauth != "disabled" else "")
        return CheckResult("xai", "ok", False, detail, _XAI_ENV, "", data)
    hint = "optional — an API key, or a Fernet TRADER_XAI_TOKEN_KEY then `trader xai login`"
    detail = _XAI_ABSENT if oauth == "disabled" else f"{_XAI_ABSENT} (oauth {oauth})"
    return CheckResult("xai", "degraded", False, detail, _XAI_ENV, hint, data)


# --- 6. Mode summary ---------------------------------------------------------


def _on(flag: bool) -> str:
    return "on" if flag else "off"


def check_mode(settings: Settings) -> CheckResult:
    """Printed, not judged — except auth off on a non-loopback bind."""
    host = urlparse(settings.api_base_url).hostname or ""
    if settings.auth_disabled:
        auth = "disabled"
    elif settings.api_key:
        auth = "api-key"
    else:
        auth = "unset"
    data = {
        "shadow_mode": settings.shadow_mode,
        "loop_enabled": settings.loop_enabled,
        "symbols": _loop_symbols(settings),
        "shadow_symbols": [t.strip() for t in settings.shadow_symbols.split(",") if t.strip()],
        "desk_enabled": settings.desk_enabled,
        "briefs_enabled": settings.briefs_enabled,
        "sentiment_trigger_enabled": settings.sentiment_trigger_enabled,
        "auth": auth,
        "api_base_url": settings.api_base_url,
    }
    detail = (
        f"shadow={_on(settings.shadow_mode)} loop={_on(settings.loop_enabled)} "
        f"symbols={','.join(data['symbols'])} desk={_on(settings.desk_enabled)} "
        f"briefs={_on(settings.briefs_enabled)} "
        f"sentiment_trigger={_on(settings.sentiment_trigger_enabled)} auth={auth}"
    )
    if auth == "unset":
        # Not judged, but the API refuses to boot like this (Settings.require_auth) —
        # the first-run user would only learn that from a traceback one step later.
        return CheckResult(
            "mode",
            "degraded",
            False,
            f"{detail} (the API refuses to boot without a key or the dev opt-out)",
            ("TRADER_API_KEY", "TRADER_AUTH_DISABLED"),
            "set TRADER_API_KEY, or TRADER_AUTH_DISABLED=true for local dev",
            data,
        )
    warnings: list[str] = []
    if settings.auth_disabled and host not in _LOOPBACK_HOSTS:
        warnings.append(
            f"TRADER_AUTH_DISABLED=true with a non-loopback API address ({settings.api_base_url}): "
            "anyone who can reach the port can wake, pause, and flatten. Set TRADER_API_KEY."
        )
        return CheckResult(
            "mode",
            "degraded",
            False,
            detail,
            ("TRADER_API_KEY",),
            "set TRADER_API_KEY and drop TRADER_AUTH_DISABLED, or bind to localhost",
            data,
            warnings,
        )
    return CheckResult("mode", "ok", False, detail, (), "", data)


# --- Runner ------------------------------------------------------------------


async def run_doctor(
    settings: Settings,
    *,
    registry: LLMRegistry | None = None,
    broker_client: httpx.AsyncClient | None = None,
    market_data: MarketData | None = None,
) -> DoctorReport:
    """All six checks in ``CHECK_ORDER``. Injectables exist for tests; the CLI passes none."""
    checks = [
        await check_postgres(settings),
        await check_decide(settings, registry=registry),
        await check_broker(settings, client=broker_client),
        await check_market_data(settings, market_data=market_data),
        check_xai(settings),
        check_mode(settings),
    ]
    return DoctorReport(checks=checks)
