"""Cross-process refresh lock on the xAI OAuth token store (#68).

The crypto-flows seat runs from a timer on the SAME SuperGrok login as trader-api, so a
single-use rotating refresh token must survive two processes deciding to refresh in the
same instant. The lock is an advisory ``flock`` beside the store, taken in a worker
thread; after taking it a manager re-reads the store and adopts a rotation another
process already made instead of spending one of its own. All HTTP is mocked.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import threading
import time
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

from trader.llm.xai_auth import (
    DurableState,
    XaiAuthError,
    XaiCredentialManager,
    XaiCredentialStore,
    XaiQuarantined,
    lock_path_for,
)

from .test_xai_auth import KEY, STATE, store_at, token_response


class CountingRefresh:
    """A fake token endpoint: counts POSTs, hands out access-N / refresh-(N+1)."""

    def __init__(self, *, delay: float = 0.0, expires_in: int = 21600) -> None:
        self.posts = 0
        self.refresh_tokens: list[str] = []
        self._delay = delay
        self._expires_in = expires_in
        self._guard = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._guard:
            self.posts += 1
            n = self.posts
        self.refresh_tokens.append(
            dict(httpx.QueryParams(request.content.decode()))["refresh_token"]
        )
        if self._delay:
            time.sleep(self._delay)  # widen the window so the other process contends
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{n}",
                "refresh_token": f"refresh-{n + 1}",
                "expires_in": self._expires_in,
            },
        )


def manager_over(store: XaiCredentialStore, handler, **kwargs) -> XaiCredentialManager:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return XaiCredentialManager(store, http_client=client, **kwargs)


# --- (1) two processes, one lineage, one refresh -----------------------------------------


def test_two_managers_in_two_event_loops_refresh_exactly_once(tmp_path: Path):
    """Two managers over ONE store, each in its own thread and event loop (the shape of
    trader-api and a timer-driven seat): exactly one POST, both serve the same token,
    and the store ends on that single rotation."""
    store_at(tmp_path).save(STATE)
    endpoint = CountingRefresh(delay=0.3)
    start = threading.Barrier(2)
    results: dict[str, str] = {}
    errors: dict[str, BaseException] = {}

    def process(name: str) -> None:
        manager = manager_over(store_at(tmp_path), endpoint)  # a fresh store object each
        start.wait()
        try:
            results[name] = asyncio.run(manager.get_access_token())
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors[name] = exc

    threads = [threading.Thread(target=process, args=(n,)) for n in ("api", "seat")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert endpoint.posts == 1  # the single-use rotation was spent exactly once
    assert results == {"api": "access-1", "seat": "access-1"}
    assert endpoint.refresh_tokens == ["refresh-1"]
    assert store_at(tmp_path).load().refresh_token == "refresh-2"


# --- (2) adopt a rotation made elsewhere --------------------------------------------------


async def test_manager_adopts_a_rotation_made_by_another_process(tmp_path: Path):
    """After taking the lock the manager re-reads the store. Another process already
    rotated the lineage and left a still-valid access token: adopt it, POST nothing."""
    store = store_at(tmp_path)
    store.save(STATE)
    endpoint = CountingRefresh(expires_in=900)
    now = [0.0]
    manager = manager_over(store, endpoint, clock=lambda: now[0])

    assert await manager.get_access_token() == "access-1"
    assert endpoint.posts == 1
    assert store.load().refresh_token == "refresh-2"

    # "Another process" refreshes: new lineage + a fresh access token, persisted.
    store.save(
        DurableState(
            refresh_token="refresh-elsewhere",
            client_id="client",
            token_endpoint=STATE.token_endpoint,
            access_token="access-elsewhere",
            access_expires_at=time.time() + 900,
        )
    )
    now[0] = 3600.0  # our cached token is past its expiry

    assert await manager.get_access_token() == "access-elsewhere"
    assert endpoint.posts == 1  # adopted, not spent
    assert store.load().refresh_token == "refresh-elsewhere"


async def test_stale_stored_access_token_is_not_adopted(tmp_path: Path):
    """A rotation seen elsewhere whose access token is (nearly) expired is honored as
    the lineage — the refresh spends ITS token, never the one we last saw."""
    store = store_at(tmp_path)
    store.save(
        DurableState(
            refresh_token="refresh-elsewhere",
            client_id="client",
            token_endpoint=STATE.token_endpoint,
            access_token="access-old",
            access_expires_at=time.time() + 30,  # inside the margin: not adoptable
        )
    )
    endpoint = CountingRefresh()
    manager = manager_over(store, endpoint)
    assert await manager.get_access_token() == "access-1"
    assert endpoint.refresh_tokens == ["refresh-elsewhere"]


# --- (3) lock timeout is transient ---------------------------------------------------------


async def test_lock_timeout_is_transient_not_quarantine(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    endpoint = CountingRefresh()
    manager = manager_over(store, endpoint, lock_timeout=0.2)

    lock = lock_path_for(store.path)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # "another process" holds the lock and never lets go
    try:
        with pytest.raises(XaiAuthError, match="lock") as excinfo:
            await manager.get_access_token()
        assert not isinstance(excinfo.value, XaiQuarantined)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert endpoint.posts == 0  # the refresh token never left the process
    assert manager.status == "ok"  # no quarantine flag
    assert await manager.get_access_token() == "access-1"  # released → works, no re-login


def test_lock_file_lives_beside_the_store_mode_0600(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    manager = manager_over(store, CountingRefresh())
    asyncio.run(manager.get_access_token())
    lock = lock_path_for(store.path)
    assert lock.parent == store.path.parent
    assert lock.name == store.path.name + ".lock"
    assert oct(lock.stat().st_mode & 0o777) == oct(0o600)


# --- (4) unwritable lock location → in-process lock only, one warning ----------------------


async def test_unwritable_lock_falls_back_to_in_process_lock_with_one_warning(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    lock_path_for(store.path).mkdir()  # the lock path cannot be opened for writing
    endpoint = CountingRefresh(expires_in=900)
    now = [0.0]
    manager = manager_over(store, endpoint, clock=lambda: now[0])

    with capture_logs() as logs:
        assert await manager.get_access_token() == "access-1"
        now[0] = 3600.0
        assert await manager.get_access_token() == "access-2"
    warnings = [e for e in logs if e["event"] == "xai_refresh_lock_unavailable"]
    assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"
    assert endpoint.posts == 2
    assert store.load().refresh_token == "refresh-3"


async def test_store_stand_in_without_a_path_uses_the_in_process_lock():
    """Stores that are not files (test doubles) get the asyncio lock only — no crash."""
    from .test_xai_auth import CountingStore

    store = CountingStore(STATE)
    endpoint = CountingRefresh()
    manager = XaiCredentialManager(
        store, http_client=httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    )
    assert await manager.get_access_token() == "access-1"
    assert store.saves == 1


# --- the store carries the access token now -----------------------------------------------


def test_store_persists_the_access_token_and_reads_legacy_files(tmp_path: Path):
    """The access token rides along (encrypted) so a second process can adopt it. A store
    written before #68 (no access fields) still loads."""
    store = store_at(tmp_path)
    state = DurableState(
        refresh_token="r",
        client_id="c",
        token_endpoint=STATE.token_endpoint,
        access_token="a",
        access_expires_at=123.0,
    )
    store.save(state)
    assert store.load() == state
    assert b"access-token" not in store.path.read_bytes()

    import json

    from cryptography.fernet import Fernet

    legacy = {"refresh_token": "r0", "client_id": "c", "token_endpoint": STATE.token_endpoint}
    store.path.write_bytes(Fernet(KEY.encode()).encrypt(json.dumps(legacy).encode()))
    loaded = store.load()
    assert loaded.refresh_token == "r0"
    assert loaded.access_token == "" and loaded.access_expires_at is None


async def test_refresh_persists_access_token_before_serving(tmp_path: Path):
    store = store_at(tmp_path)
    store.save(STATE)
    manager = manager_over(store, lambda r: httpx.Response(200, json=token_response()))
    assert await manager.get_access_token() == "access-1"
    stored = store.load()
    assert stored.access_token == "access-1"
    assert stored.access_expires_at is not None and stored.access_expires_at > time.time()
