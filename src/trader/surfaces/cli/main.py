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
    return httpx.Client(base_url=settings.api_base_url, headers=headers, timeout=10)


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


broker_app = typer.Typer(help="Broker reconciliation and emergency flatten.")
app.add_typer(broker_app, name="broker")


@broker_app.command("reconcile")
def broker_reconcile() -> None:
    """Compare the local ledger to the broker; drift pauses routing."""
    typer.echo(json.dumps(_post("/broker/reconcile"), indent=2))


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


if __name__ == "__main__":
    app()
