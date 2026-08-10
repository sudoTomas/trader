"""Walking-skeleton test: the health capability answers identically across all three surfaces."""

from __future__ import annotations

import os

os.environ.setdefault("TRADER_AUTH_DISABLED", "true")


async def test_service_registry_health():
    from trader.surfaces.services import build_registry

    # Without a store the registry still answers, honestly degraded.
    result = await build_registry().get("health").handler()
    assert result["status"] == "degraded"
    assert result["service"] == "trader"
    assert result["db"] == "unconfigured"


def test_api_health():
    from fastapi.testclient import TestClient

    from trader.surfaces.api.app import create_app

    with TestClient(create_app()) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


def test_registry_rejects_duplicate_capability():
    from trader.surfaces.services import ServiceRegistry

    reg = ServiceRegistry()

    @reg.register("x", kind="query", summary="")
    async def _a() -> dict:
        return {}

    import pytest

    with pytest.raises(ValueError, match="already registered"):

        @reg.register("x", kind="query", summary="")
        async def _b() -> dict:
            return {}


def test_config_requires_auth_or_explicit_optout():
    import pytest

    from trader.ops.config import Settings

    # No key, no opt-out → must refuse to boot.
    with pytest.raises(RuntimeError, match="TRADER_API_KEY is unset"):
        Settings(api_key="", auth_disabled=False).require_auth()

    # Explicit dev opt-out is allowed.
    Settings(api_key="", auth_disabled=True).require_auth()

    # A real key is allowed.
    Settings(api_key="secret", auth_disabled=False).require_auth()
