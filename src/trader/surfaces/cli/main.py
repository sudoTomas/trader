"""CLI surface (typer) — `trader ...`. Talks to the running API (base URL + key from env)
so operator commands exercise the real service, not a private in-process copy.
"""

from __future__ import annotations

import json
from typing import Annotated

import httpx
import typer

from trader.ops.config import get_settings

app = typer.Typer(help="trader — agentic-first trading agent", no_args_is_help=True)


def _client() -> httpx.Client:
    settings = get_settings()
    headers = {"x-api-key": settings.api_key} if settings.api_key else {}
    # 120s: sentiment fetch (live x_search, ~15-25s) and macro compile go through LLMs;
    # the first live fetch timed out client-side at 10s while the server succeeded,
    # burning a capped API call for nothing.
    return httpx.Client(base_url=settings.api_base_url, headers=headers, timeout=120)


def _request(
    method: str, path: str, params: dict | None = None, json_body: dict | None = None
) -> object:
    try:
        with _client() as client:
            resp = client.request(method, path, params=params, json=json_body)
            resp.raise_for_status()
            return resp.json()
    except httpx.ConnectError as exc:
        typer.echo(f"cannot reach the trader API at {get_settings().api_base_url}: {exc}", err=True)
        raise typer.Exit(1) from exc
    except httpx.HTTPStatusError as exc:
        typer.echo(f"API error {exc.response.status_code}: {exc.response.text}", err=True)
        raise typer.Exit(1) from exc


def _get(path: str, params: dict | None = None) -> object:
    return _request("GET", path, params)


def _post(path: str, params: dict | None = None, json_body: dict | None = None) -> object:
    return _request("POST", path, params, json_body)


@app.callback()
def _root() -> None:
    """trader — agentic-first trading agent."""


@app.command()
def health() -> None:
    """Print liveness, build, and store reachability."""
    typer.echo(json.dumps(_get("/health"), indent=2))


@app.command()
def audit(limit: int = typer.Option(20, help="How many events, newest first.")) -> None:
    """Print recent audit events."""
    typer.echo(json.dumps(_get("/audit", params={"limit": limit}), indent=2))


@app.command("cache-stats")
def cache_stats() -> None:
    """Print bars-cache hit/miss counters (replay path)."""
    typer.echo(json.dumps(_get("/cache-stats"), indent=2))


risk_app = typer.Typer(help="Risk status and the human-in-the-loop approval queue.")
app.add_typer(risk_app, name="risk")


@risk_app.command("status")
def risk_status() -> None:
    """Print kill switches, breaker halts, and the configured limits."""
    typer.echo(json.dumps(_get("/risk/status"), indent=2))


@risk_app.command("approvals")
def risk_approvals() -> None:
    """Print order intents parked awaiting a human."""
    typer.echo(json.dumps(_get("/risk/approvals"), indent=2))


@risk_app.command("approve")
def risk_approve(
    approval_id: int = typer.Argument(..., help="Approval id from `trader risk approvals`."),
) -> None:
    """Approve a parked intent and redeem it: fresh quote, TTL + price-tolerance check,
    RiskGuard re-run, then the executor. Places the order if every step passes."""
    typer.echo(
        json.dumps(
            _post(
                f"/risk/approvals/{approval_id}/resolve",
                params={"approved": True, "resolved_by": "cli"},
            ),
            indent=2,
        )
    )


@risk_app.command("deny")
def risk_deny(
    approval_id: int = typer.Argument(..., help="Approval id from `trader risk approvals`."),
) -> None:
    """Deny a parked intent."""
    typer.echo(
        json.dumps(
            _post(
                f"/risk/approvals/{approval_id}/resolve",
                params={"approved": False, "resolved_by": "cli"},
            ),
            indent=2,
        )
    )


@risk_app.command("pause")
def risk_pause(reason: str = typer.Option("operator", help="Why trading is being paused.")) -> None:
    """Pause trading (kill switch — blocks every order, exits included)."""
    typer.echo(json.dumps(_post("/risk/pause", params={"reason": reason}), indent=2))


@risk_app.command("resume")
def risk_resume() -> None:
    """Clear the operator pause."""
    typer.echo(json.dumps(_post("/risk/resume"), indent=2))


@risk_app.command("resume-breaker")
def risk_resume_breaker(
    equity: float = typer.Option(None, help="Re-baseline the breaker windows to this equity."),
) -> None:
    """Clear a breaker halt and re-baseline its windows."""
    params = {"equity": equity} if equity is not None else None
    typer.echo(json.dumps(_post("/risk/resume-breaker", params=params), indent=2))


@app.command()
def wake() -> None:
    """Force a decide pass now, ignoring cooldowns."""
    typer.echo(json.dumps(_post("/loop/wake"), indent=2))


@app.command()
def decisions(limit: int = typer.Option(20, help="How many decisions, newest first.")) -> None:
    """Print recent journaled decisions."""
    typer.echo(json.dumps(_get("/decisions", params={"limit": limit}), indent=2))


@app.command()
def decision(decision_id: int = typer.Argument(..., help="Journaled decision id.")) -> None:
    """Print one journaled decision in full (prompt, raw output, verdicts)."""
    typer.echo(json.dumps(_get(f"/decisions/{decision_id}"), indent=2))


@app.command()
def positions() -> None:
    """Print open positions from the local ledger."""
    typer.echo(json.dumps(_get("/positions"), indent=2))


@app.command()
def orders(limit: int = typer.Option(50, help="How many orders, newest first.")) -> None:
    """Print recent orders."""
    typer.echo(json.dumps(_get("/orders", params={"limit": limit}), indent=2))


@app.command()
def usage(
    days: int = typer.Option(14, help="How many UTC days back, including today."),
    role: Annotated[
        str | None, typer.Option(help="Filter to one role (sentiment, decide, …).")
    ] = None,
    day: Annotated[str | None, typer.Option(help="Single YYYY-MM-DD; overrides --days.")] = None,
) -> None:
    """Print the usage_daily rollup (requests, tokens, USD)."""
    params: dict[str, object] = {"days": days}
    if role:
        params["role"] = role
    if day:
        params["day"] = day
    typer.echo(json.dumps(_get("/usage", params=params), indent=2))


broker_app = typer.Typer(help="Broker reconciliation and emergency flatten.")
app.add_typer(broker_app, name="broker")


@broker_app.command("reconcile")
def broker_reconcile() -> None:
    """Compare the local ledger to the broker; drift pauses routing."""
    typer.echo(json.dumps(_post("/broker/reconcile"), indent=2))


@broker_app.command("adopt")
def broker_adopt(
    confirm: str = typer.Option("", help="Must be exactly ADOPT — rewrites the ledger."),
) -> None:
    """Adopt venue positions as ledger truth and clear the drift flag (after investigating)."""
    typer.echo(json.dumps(_post("/broker/adopt", params={"confirm": confirm}), indent=2))


@broker_app.command("flatten-all")
def broker_flatten_all(
    confirm: str = typer.Option("", help="Must be exactly FLATTEN to proceed."),
) -> None:
    """Close EVERY position at the broker. Requires --confirm FLATTEN."""
    typer.echo(json.dumps(_post("/broker/flatten-all", params={"confirm": confirm}), indent=2))


loop_app = typer.Typer(help="Decide-loop status.")
app.add_typer(loop_app, name="loop")


@loop_app.command("status")
def loop_status() -> None:
    """Print loop mode (shadow/live) and counters."""
    typer.echo(json.dumps(_get("/loop/status"), indent=2))


note_app = typer.Typer(help="Research Desk capture — operator research notes.")
app.add_typer(note_app, name="note")


@note_app.command("add")
def note_add(
    title: str = typer.Option(..., help="Note title."),
    body: str = typer.Option(
        None, help="Markdown body. Omitted: read --file, else stdin (pipe or paste, ^D ends)."
    ),
    file: str = typer.Option(None, help="Read the body from this file."),
    tag: Annotated[list[str] | None, typer.Option(help="Tag (repeatable).")] = None,
    asset_class: Annotated[
        list[str] | None, typer.Option(help="Target asset class (repeatable).")
    ] = None,
    symbol: Annotated[list[str] | None, typer.Option(help="Target symbol (repeatable).")] = None,
    source: str = typer.Option(None, help="Where this came from (url, 'voice memo', ...)."),
    pin: bool = typer.Option(False, "--pin", help="Evergreen: exempt from auto-stale."),
    stale_days: int = typer.Option(None, help="Override the auto-stale window (days)."),
    authored: str = typer.Option(
        None, help="When the research was CREATED (ISO-8601, tz-aware); freshness anchors here."
    ),
) -> None:
    """Capture a research note. The body is stored verbatim, markdown and all."""
    import sys
    from pathlib import Path

    if body is None:
        body = Path(file).read_text(encoding="utf-8") if file else sys.stdin.read()
    payload = {
        "title": title,
        "body": body,
        "tags": tag or [],
        "asset_classes": asset_class or [],
        "symbols": symbol or [],
        "source": source,
        "pinned": pin,
        "stale_days": stale_days,
        "authored_at": authored,
    }
    typer.echo(json.dumps(_post("/research/notes", json_body=payload), indent=2))


@note_app.command("list")
def note_list(
    include_stale: bool = typer.Option(False, "--stale", help="Include auto-staled notes."),
    include_retired: bool = typer.Option(False, "--retired", help="Include retired notes."),
    limit: int = typer.Option(100, help="How many notes, newest first."),
) -> None:
    """List notes (default: the view compilation sees)."""
    params = {"include_stale": include_stale, "include_retired": include_retired, "limit": limit}
    typer.echo(json.dumps(_get("/research/notes", params=params), indent=2))


@note_app.command("retire")
def note_retire(note_id: int = typer.Argument(..., help="Note id to retire.")) -> None:
    """Explicitly retire a note (auto-stale never does this for you)."""
    typer.echo(json.dumps(_post(f"/research/notes/{note_id}/retire"), indent=2))


eval_app = typer.Typer(help="Replay eval — offline decide reruns; the decide-change gate.")
app.add_typer(eval_app, name="eval")


@eval_app.command("run")
def eval_run_cmd(
    symbol: str = typer.Option(..., help="Symbol, e.g. BTC/USD."),
    start: str = typer.Option(..., help="Window start (ISO-8601, tz-aware)."),
    end: str = typer.Option(..., help="Window end (ISO-8601, tz-aware)."),
    label: str = typer.Option("replay", help="Name this run for later comparison."),
    desk: bool = typer.Option(False, "--desk", help="Replay with the decision desk on."),
    briefs: bool = typer.Option(
        True, "--briefs/--no-briefs", help="Brief lane in the prompt (--no-briefs matches live)."
    ),
    macro_view: bool = typer.Option(
        True, "--macro-view/--no-macro-view", help="Macro View lane in the prompt."
    ),
) -> None:
    """Start a background replay over [start, end); poll with `trader eval show`."""
    params = {
        "symbol": symbol,
        "start": start,
        "end": end,
        "label": label,
        "desk": desk,
        "briefs": briefs,
        "macro_view": macro_view,
    }
    typer.echo(json.dumps(_post("/eval/replay", params=params), indent=2))


@eval_app.command("list")
def eval_list(limit: int = typer.Option(50, help="How many runs, newest first.")) -> None:
    """List replay eval runs."""
    typer.echo(json.dumps(_get("/eval/runs", params={"limit": limit}), indent=2))


@eval_app.command("show")
def eval_show(run_id: int = typer.Argument(..., help="Eval run id.")) -> None:
    """One run: status, summary, and per-step decisions (no prompt dumps)."""
    typer.echo(json.dumps(_get(f"/eval/runs/{run_id}"), indent=2))


macro_app = typer.Typer(help="The compiled Macro View (advisory digest of operator research).")
app.add_typer(macro_app, name="macro")


@macro_app.command("view")
def macro_view(
    version: int = typer.Option(None, help="A specific version (default: current)."),
) -> None:
    """Print the current (or a specific) compiled Macro View."""
    params = {"version": version} if version is not None else None
    typer.echo(json.dumps(_get("/research/view", params=params), indent=2))


@macro_app.command("compile")
def macro_compile() -> None:
    """Recompile the Macro View from live notes now, skipping the debounce."""
    typer.echo(json.dumps(_post("/research/view/compile"), indent=2))


@note_app.command("mirror")
def note_mirror() -> None:
    """Rewrite the one-way markdown mirror (research/) from the store now."""
    typer.echo(json.dumps(_post("/research/mirror/export"), indent=2))


brief_app = typer.Typer(help="Per-candidate research briefs (advisory; prompt lane is gated).")
app.add_typer(brief_app, name="brief")


@brief_app.command("show")
def brief_show(
    symbol: str = typer.Argument(..., help="Symbol, e.g. ETH/USD."),
    version: int = typer.Option(None, help="A specific version (default: current)."),
) -> None:
    """Print the current (or a specific) compiled brief for one symbol."""
    params: dict = {"symbol": symbol}
    if version is not None:
        params["version"] = version
    typer.echo(json.dumps(_get("/research/briefs", params=params), indent=2))


@brief_app.command("compile")
def brief_compile(symbol: str = typer.Argument(..., help="Symbol, e.g. ETH/USD.")) -> None:
    """Compile one symbol's brief from store inputs now, skipping the debounce."""
    typer.echo(json.dumps(_post("/research/briefs/compile", params={"symbol": symbol}), indent=2))


sentiment_app = typer.Typer(help="X sentiment (server-side x_search; shadow-first).")
app.add_typer(sentiment_app, name="sentiment")


@sentiment_app.command("history")
def sentiment_history(
    symbol: str = typer.Option(None, help="Filter to one symbol."),
    limit: int = typer.Option(50, help="How many polls, newest first."),
) -> None:
    """Print journaled sentiment polls (scored and unavailable)."""
    params: dict = {"limit": limit}
    if symbol is not None:
        params["symbol"] = symbol
    typer.echo(json.dumps(_get("/research/sentiment", params=params), indent=2))


@sentiment_app.command("fetch")
def sentiment_fetch(symbol: str = typer.Argument(..., help="Symbol, e.g. ETH/USD.")) -> None:
    """Poll X sentiment for one symbol now (counts against the daily cap)."""
    typer.echo(json.dumps(_post("/research/sentiment/fetch", params={"symbol": symbol}), indent=2))


fred_app = typer.Typer(help="FRED macro series (VIX etc.) — the prints the decide context sees.")
app.add_typer(fred_app, name="fred")


@fred_app.command("latest")
def fred_latest(
    series: str = typer.Option("VIXCLS", help="FRED series id, e.g. VIXCLS, DGS10."),
    limit: int = typer.Option(10, help="How many observations, newest first."),
) -> None:
    """Print the latest observations of one FRED series (unavailable without FRED_API_KEY)."""
    typer.echo(json.dumps(_get("/fred", params={"series": series, "limit": limit}), indent=2))


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Machine-readable report."),
) -> None:
    """Preflight this box: Postgres + migrations, decide endpoint, Alpaca paper account,
    one market-data quote, xAI credential (optional), and the mode flags. Local like
    `trader xai` — reads Settings (.env) directly, never the API. Exit 1 when a required
    check is missing; degraded alone exits 0."""
    import asyncio
    import sys

    import structlog

    from trader.ops import doctor as doctor_mod

    if not structlog.is_configured():
        # Provider clients log on failure; keep that off stdout so --json stays parseable.
        structlog.configure(logger_factory=structlog.PrintLoggerFactory(sys.stderr))
    report = asyncio.run(doctor_mod.run_doctor(get_settings()))
    typer.echo(json.dumps(report.to_dict(), indent=2) if json_output else report.render())
    raise typer.Exit(report.exit_code)


xai_app = typer.Typer(help="SuperGrok OAuth credential management (local, not via the API).")
app.add_typer(xai_app, name="xai")


@xai_app.command("login")
def xai_login() -> None:
    """Device-code login against auth.x.ai; stores the encrypted refresh lineage."""
    import asyncio

    from trader.llm.xai_auth import XaiAuthError, build_manager

    manager = build_manager(get_settings())
    if manager is None:
        typer.echo(
            "TRADER_XAI_TOKEN_KEY is unset. Generate one:\n"
            '  python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"',
            err=True,
        )
        raise typer.Exit(1)
    try:
        asyncio.run(manager.login(echo=typer.echo))
    except XaiAuthError as exc:
        typer.echo(f"login failed: {exc}", err=True)
        raise typer.Exit(1) from exc


@xai_app.command("status")
def xai_status() -> None:
    """Print the OAuth credential state (ok | absent | quarantined | corrupt)."""
    from trader.llm.xai_auth import build_manager

    settings = get_settings()
    manager = build_manager(settings)
    status = "disabled (TRADER_XAI_TOKEN_KEY unset)" if manager is None else manager.status
    typer.echo(
        json.dumps({"oauth": status, "api_key_fallback": bool(settings.xai_api_key)}, indent=2)
    )


@xai_app.command("probe")
def xai_probe_cmd(
    model: Annotated[
        list[str] | None,
        typer.Option(help="Model id (repeatable). Default: grok-4, grok-4.3, grok-4-1-fast."),
    ] = None,
    endpoint: Annotated[
        list[str] | None,
        typer.Option(help="chat/completions | responses (repeatable). Default: both."),
    ] = None,
    tool: Annotated[
        list[str] | None,
        typer.Option(help="none | x_search | web_search (repeatable). Default: all three."),
    ] = None,
    credential: str = typer.Option(
        "auto", help="auto (OAuth then XAI_API_KEY) | oauth | api_key — pin one path."
    ),
    json_output: bool = typer.Option(False, "--json", help="Full results as JSON."),
) -> None:
    """Probe xAI endpoints × models × tools from INSIDE the running service, on its own
    credential (the OAuth lineage the service owns, or XAI_API_KEY). Unlike `xai login`
    this goes through the API: never run a second OAuth client beside trader-api.
    Diagnostics only — unmetered, ~16 output tokens per cell; one audit row."""
    payload: dict[str, object] = {"credential": credential}
    if model:
        payload["models"] = model
    if endpoint:
        payload["endpoints"] = endpoint
    if tool:
        payload["tools"] = tool
    out = _post("/xai/probe", json_body=payload)
    if json_output or not isinstance(out, dict) or out.get("status") != "ok":
        typer.echo(json.dumps(out, indent=2))
        return
    header = f"{'endpoint':<17}{'model':<14}{'tool':<11}{'cred':<8}{'http':<6}{'result':<13}"
    header += f"{'in/out':<10}{'cites':<6}{'ms':>7}"
    typer.echo(header)
    for r in out["results"]:
        usage = r.get("usage") or {}
        verdict = "ok" if r["ok"] else (r.get("error_class") or "error")
        in_out = f"{usage.get('input_tokens', 0)}/{usage.get('output_tokens', 0)}"
        line = (
            f"{r['endpoint']:<17}{r['model']:<14}{(r.get('tool') or 'none'):<11}"
            f"{(r.get('credential_used') or '-'):<8}{str(r.get('status_code') or '-'):<6}"
            f"{verdict:<13}{in_out:<10}{r.get('citations_count', 0):<6}{r.get('latency_ms', 0):>7}"
        )
        typer.echo(line)
        if r.get("detail"):
            typer.echo(f"{'':<17}{r['detail']}")
    typer.echo(f"summary: {json.dumps(out['summary'])}")


screener_app = typer.Typer(
    help="Screener — rank a static universe into a bounded shortlist (query only)."
)
app.add_typer(screener_app, name="screener")


@screener_app.command("run")
def screener_run(
    universe: str = typer.Option("sp100", help="Universe name (sp100)."),
    limit: int = typer.Option(10, min=1, max=50, help="Shortlist size."),
) -> None:
    """Rank the universe by risk-adjusted momentum (20d/60d return over 20d vol), after
    a dollar-volume floor and with the loop's own symbols excluded. Reads Alpaca daily
    bars on the spot; stores nothing; does not touch the decide loop."""
    params = {"universe": universe, "limit": limit}
    typer.echo(json.dumps(_get("/screener", params=params), indent=2))


if __name__ == "__main__":
    app()
