"""FastAPI surface — thin adapter over the service registry."""

from __future__ import annotations

from fastapi import FastAPI

from trader.ops.config import get_settings
from trader.surfaces.services import build_registry


def create_app() -> FastAPI:
    settings = get_settings()
    settings.require_auth()
    registry = build_registry()

    app = FastAPI(title="trader", version="0.1.0")
    app.state.registry = registry

    @app.get("/health")
    async def health() -> dict:
        return await registry.get("health").handler()

    return app


app = create_app()
