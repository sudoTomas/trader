"""CLI surface (typer) — `trader ...`. Talks to the running API (base URL + key from env)
so operator commands exercise the real service, not a private in-process copy.
"""

from __future__ import annotations

import json

import httpx
import typer

from trader.ops.config import get_settings

app = typer.Typer(help="trader — agentic-first trading agent", no_args_is_help=True)


def _client() -> httpx.Client:
    settings = get_settings()
    headers = {"x-api-key": settings.api_key} if settings.api_key else {}
    return httpx.Client(base_url=settings.api_base_url, headers=headers, timeout=10)


def _request(method: str, path: str, params: dict | None = None) -> object:
    try:
        with _client() as client:
            resp = client.request(method, path, params=params)
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


def _post(path: str, params: dict | None = None) -> object:
    return _request("POST", path, params)


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


loop_app = typer.Typer(help="Decide-loop status.")
app.add_typer(loop_app, name="loop")


@loop_app.command("status")
def loop_status() -> None:
    """Print loop mode (shadow/live) and counters."""
    typer.echo(json.dumps(_get("/loop/status"), indent=2))


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
