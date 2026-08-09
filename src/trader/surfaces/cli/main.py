"""CLI surface (typer) — `trader ...`. Wraps the service registry for operator use."""

from __future__ import annotations

import asyncio
import json

import typer

from trader.surfaces.services import build_registry

app = typer.Typer(help="trader — agentic-first trading agent", no_args_is_help=True)


@app.callback()
def _root() -> None:
    """trader — agentic-first trading agent."""


@app.command()
def health() -> None:
    """Print liveness and build status."""
    registry = build_registry()
    result = asyncio.run(registry.get("health").handler())
    typer.echo(json.dumps(result, indent=2))


if __name__ == "__main__":
    app()
