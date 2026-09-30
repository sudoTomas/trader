"""xAI OAuth tests — the rotating-refresh discipline, quarantine transitions, and the
API-key fallback. All HTTP is mocked; the live endpoint spike is docs/research/xai-api.md.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

from trader.llm.base import LLMError
from trader.llm.xai import xai_client
from trader.llm.xai_auth import (
    DurableState,
    XaiAuthError,
    XaiCredentialManager,
    XaiCredentialStore,
    XaiQuarantined,
)
from trader.ops.config import Settings

KEY = Fernet.generate_key().decode()
STATE = DurableState(
    refresh_token="refresh-1",
    client_id="client",
    token_endpoint="https://auth.x.ai/oauth2/token",
)


def store_at(tmp_path: Path) -> XaiCredentialStore:
    return XaiCredentialStore(tmp_path / "xai_token.enc", KEY)


def manager_with(store: XaiCredentialStore, handler) -> XaiCredentialManager:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return XaiCredentialManager(store, http_client=client)


def token_response(refresh: str = "refresh-2", access: str = "access-1") -> dict:
    return {"access_token": access, "refresh_token": refresh, "expires_in": 21600}


# --- store ------------------------------------------------------------------


def test_store_round_trip_encrypted_at_rest(tmp_path: Path):
    store = store_at(tmp_path)
    assert store.load() is None
    store.save(STATE)
    raw = store.path.read_bytes()
    assert b"refresh-1" not in raw  # Fernet at rest, token never in plaintext
    assert store.load() == STATE

    store.path.write_bytes(b"garbage")
    with pytest.raises(XaiAuthError, match="unreadable or corrupt"):
        store.load()


def test_store_requires_valid_fernet_key(tmp_path: Path):
    with pytest.raises(XaiAuthError, match="TRADER_XAI_TOKEN_KEY is required"):
        XaiCredentialStore(tmp_path / "x", "")
    with pytest.raises(XaiAuthError, match="not a valid Fernet key"):
        XaiCredentialStore(tmp_path / "x", "not-a-key")


# --- rotation discipline ----------------------------------------------------


async def test_rotated_refresh_token_persisted_on_refresh(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    manager = manager_with(store, lambda r: httpx.Response(200, json=token_response()))

    token = await manager.get_access_token()
    assert token == "access-1"
    assert store.load().refresh_token == "refresh-2"  # rotation durable


async def test_failed_persist_means_no_token_served(tmp_path: Path):
    """THE invariant: if the rotated refresh token can't be persisted, nothing is served
    or cached — a crash can never leave us holding a spent, unpersisted rotation."""
    store = store_at(tmp_path)
    store.save(STATE)

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=token_response())

    manager = manager_with(store, handler)

    def failing_save(state: DurableState) -> None:
        raise OSError("disk full")

    manager._store.save = failing_save  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await manager.get_access_token()

    # Nothing cached: the next call must attempt a fresh refresh, not serve stale state.
    assert manager._access_token == ""
    manager._store.save = lambda state: None  # type: ignore[method-assign]
    assert await manager.get_access_token() == "access-1"
    assert calls["n"] == 2


async def test_concurrent_refreshes_serialize_to_one_request(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=token_response())

    manager = manager_with(store, handler)
    tokens = await asyncio.gather(*(manager.get_access_token() for _ in range(5)))
    assert set(tokens) == {"access-1"}
    assert calls["n"] == 1  # a single-use rotating token is never double-spent


# --- quarantine -------------------------------------------------------------


async def test_invalid_grant_quarantines_without_retry(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401)

    manager = manager_with(store, handler)
    with pytest.raises(XaiQuarantined) as excinfo:
        await manager.get_access_token()
    assert excinfo.value.reason == "invalid_grant"

    with pytest.raises(XaiQuarantined):
        await manager.get_access_token()
    assert calls["n"] == 1  # quarantine means no retry spin
    assert manager.status == "quarantined:invalid_grant"


async def test_403_is_entitlement_quarantine(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    manager = manager_with(store, lambda r: httpx.Response(403))
    with pytest.raises(XaiQuarantined) as excinfo:
        await manager.get_access_token()
    assert excinfo.value.reason == "entitlement"


async def test_transient_5xx_does_not_quarantine(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    responses = iter([httpx.Response(503), httpx.Response(200, json=token_response())])
    manager = manager_with(store, lambda r: next(responses))

    with pytest.raises(XaiAuthError, match="HTTP 503"):
        await manager.get_access_token()
    assert await manager.get_access_token() == "access-1"  # recovered, no quarantine


# --- login ------------------------------------------------------------------


async def test_device_code_login_persists_lineage(tmp_path: Path):
    store = store_at(tmp_path)
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "token_endpoint": "https://auth.x.ai/oauth2/token",
                    "device_authorization_endpoint": "https://auth.x.ai/oauth2/device/code",
                },
            )
        if path.endswith("/device/code"):
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-1",
                    "user_code": "ABCD-EFGH",
                    "verification_uri_complete": "https://x.ai/activate?code=ABCD",
                    "interval": 0,
                    "expires_in": 600,
                },
            )
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(200, json=token_response(refresh="refresh-login"))

    manager = manager_with(store, handler)
    printed: list[str] = []
    await manager.login(echo=printed.append)

    assert store.load().refresh_token == "refresh-login"
    assert any("ABCD-EFGH" in line for line in printed)
    assert manager.status == "ok"


async def test_login_refuses_non_xai_endpoints(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token_endpoint": "https://evil.example.com/oauth2/token"})

    manager = manager_with(store_at(tmp_path), handler)
    with pytest.raises(XaiAuthError, match="non-x.ai OAuth endpoint"):
        await manager.login(echo=lambda _: None)


# --- fallback resolution ----------------------------------------------------


def grok_with_auth(settings: Settings, manager, seen: dict):
    from trader.llm.xai import ManagedBearerAuth

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.x.ai/v1",
        auth=ManagedBearerAuth(manager, settings.xai_api_key),
    )
    return xai_client(settings, manager=manager, client=http)


async def test_api_key_serves_when_oauth_absent():
    seen: dict = {}
    client = grok_with_auth(Settings(auth_disabled=True, xai_api_key="plain-key"), None, seen)
    await client.complete([{"role": "user", "content": "x"}])
    assert seen["auth"] == "Bearer plain-key"


async def test_api_key_serves_when_oauth_quarantined(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    manager = manager_with(store, lambda r: httpx.Response(403))
    seen: dict = {}
    client = grok_with_auth(Settings(auth_disabled=True, xai_api_key="plain-key"), manager, seen)
    await client.complete([{"role": "user", "content": "x"}])
    assert seen["auth"] == "Bearer plain-key"  # consumers never notice the quarantine


async def test_oauth_token_serves_when_healthy(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    manager = manager_with(store, lambda r: httpx.Response(200, json=token_response()))
    seen: dict = {}
    client = grok_with_auth(Settings(auth_disabled=True), manager, seen)
    await client.complete([{"role": "user", "content": "x"}])
    assert seen["auth"] == "Bearer access-1"


async def test_no_credential_at_all_fails_as_llm_error():
    # The factory refuses to build a credential-less client...
    assert xai_client(Settings(auth_disabled=True)) is None
    # ...and even a hand-built one fails at request time as an LLMError (registry-safe).
    from trader.llm.openai_compat import OpenAIChatClient
    from trader.llm.xai import ManagedBearerAuth

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        base_url="https://api.x.ai/v1",
        auth=ManagedBearerAuth(None, ""),
    )
    client = OpenAIChatClient(
        name="xai", model="grok-4", base_url="https://api.x.ai/v1", client=http
    )
    with pytest.raises(LLMError, match="no usable credential"):
        await client.complete([{"role": "user", "content": "x"}])


async def test_short_lived_tokens_use_an_adaptive_refresh_margin(tmp_path: Path):
    """Device-code grants return ~15-min access tokens (embry0 audit). A fixed
    30-min margin would then refresh — and burn a single-use rotating refresh token —
    on EVERY call. The margin must scale down with the observed TTL."""
    store = store_at(tmp_path)
    store.save(STATE)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": f"access-{calls}", "expires_in": 900})

    now = [0.0]
    manager = XaiCredentialManager(
        store,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    assert await manager.get_access_token() == "access-1"
    assert await manager.get_access_token() == "access-1"  # cache hit, not a second burn
    assert calls == 1

    now[0] = 500.0  # inside ttl - ttl//3 = 600s: still fresh
    assert await manager.get_access_token() == "access-1"
    assert calls == 1

    now[0] = 700.0  # past the adaptive margin boundary: refresh now
    assert await manager.get_access_token() == "access-2"
    assert calls == 2


async def test_refresh_refuses_tampered_token_endpoint(tmp_path: Path):
    """Endpoint pinning must hold on every refresh, not only at login: a tampered store
    must not ship the refresh token to an arbitrary host (embry0's SSRF lesson)."""
    store = store_at(tmp_path)
    store.save(
        DurableState(
            refresh_token="refresh-1",
            client_id="client",
            token_endpoint="https://notx.ai/oauth2/token",  # endswith-x.ai trap
        )
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=token_response())

    manager = manager_with(store, handler)
    with pytest.raises(XaiAuthError, match="non-x.ai"):
        await manager.get_access_token()
    assert calls == 0  # the refresh token never left the process


# --- one manager per process (#63) -----------------------------------------


class CountingStore:
    """Stands in for XaiCredentialStore: counts reads and rotations, no disk."""

    def __init__(self, state: DurableState) -> None:
        self.state = state
        self.loads = 0
        self.saves = 0

    def load(self) -> DurableState | None:
        self.loads += 1
        return self.state

    def save(self, state: DurableState) -> None:
        self.saves += 1
        self.state = state


def refresh_counter(access: str = "access-1"):
    seen: dict = {"refreshes": 0, "refresh_tokens": []}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["refreshes"] += 1
        seen["refresh_tokens"].append(
            dict(httpx.QueryParams(request.content.decode()))["refresh_token"]
        )
        return httpx.Response(200, json=token_response(access=access))

    return seen, handler


async def test_two_clients_sharing_one_manager_refresh_once():
    """Two roles (two ManagedBearerAuth clients) over ONE manager: concurrent first
    calls serialize under the manager's lock and spend a single rotating refresh token."""
    store = CountingStore(STATE)
    seen, handler = refresh_counter()
    manager = XaiCredentialManager(
        store, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    settings = Settings(auth_disabled=True)
    first: dict = {}
    second: dict = {}
    a = grok_with_auth(settings, manager, first)
    b = grok_with_auth(settings, manager, second)

    await asyncio.gather(
        a.complete([{"role": "user", "content": "x"}]),
        b.complete([{"role": "user", "content": "y"}]),
    )
    assert first["auth"] == second["auth"] == "Bearer access-1"
    assert seen["refreshes"] == 1  # one lineage, one burn
    assert store.saves == 1  # the rotation persisted exactly once
    assert store.loads == 1  # a refresh reads the store; a cache hit does not


async def test_refresh_re_reads_the_store_before_spending():
    """A refresh decides from the store as it is NOW — not from a token captured at
    construction — so a lineage rotated elsewhere (login, another writer) is honored."""
    store = CountingStore(STATE)
    seen, handler = refresh_counter()
    now = [0.0]
    manager = XaiCredentialManager(
        store,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: now[0],
    )
    await manager.get_access_token()
    assert seen["refresh_tokens"] == ["refresh-1"]

    store.state = DurableState(
        refresh_token="refresh-elsewhere", client_id="client", token_endpoint=STATE.token_endpoint
    )
    now[0] = 10 * 3600.0  # past the cached token's expiry
    await manager.get_access_token()
    assert seen["refresh_tokens"] == ["refresh-1", "refresh-elsewhere"]
    assert store.loads == 2


def test_client_factories_never_build_a_second_manager(monkeypatch: pytest.MonkeyPatch):
    """Given a manager, ``xai_http_client`` / ``xai_client`` / ``build_llm_registry``
    reuse it; the per-call ``build_manager`` fallback only runs when none is passed."""
    from trader.llm import xai_auth
    from trader.llm.registry import build_llm_registry
    from trader.llm.xai import ManagedBearerAuth, xai_http_client

    built = {"n": 0}
    real = xai_auth.XaiCredentialManager

    class Counting(real):
        def __init__(self, *args, **kwargs) -> None:
            built["n"] += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(xai_auth, "XaiCredentialManager", Counting)
    settings = Settings(auth_disabled=True, xai_token_key=KEY, xai_token_path="/nonexistent/x.enc")
    shared = XaiCredentialManager(CountingStore(STATE), http_client=httpx.AsyncClient())

    http = xai_http_client(settings, manager=shared)
    assert isinstance(http.auth, ManagedBearerAuth) and http.auth._manager is shared
    grok = xai_client(settings, manager=shared)
    assert grok._client.auth._manager is shared
    registry = build_llm_registry(settings, xai_manager=shared)
    assert registry.chain_for("brief")[0]._client.auth._manager is shared
    assert built["n"] == 0

    # No manager passed: the fallback builds one per call, exactly as before.
    fallback = build_llm_registry(settings)
    assert fallback.chain_for("brief")[0]._client.auth._manager is not shared
    assert built["n"] == 1
