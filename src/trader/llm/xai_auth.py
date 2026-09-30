"""SuperGrok device-code OAuth: login, rotating-refresh discipline, quarantine.

Hermes-style RFC 8628 flow against ``auth.x.ai`` (an operator decision carried over from
v1 — no Grok CLI dependency, no ``~/.grok/auth.json`` seeding: that store's rotating
tokens are single-use, and stealing one breaks the CLI's own login). Refresher shape
cribbed from embry0's ``xai_credential.py``.

Invariants (see docs/lessons.md#oauth-refresh-margin):
- Refreshes serialize under an ``asyncio.Lock`` (one manager per process, #63) AND an
  advisory ``flock`` on ``<store>.lock`` (#68), held across read → decide → POST → write,
  so a rotating single-use refresh token is never double-spent — not by two clients in
  one process, not by trader-api and a timer-driven seat sharing one login. After taking
  the file lock the manager re-reads the store: a rotation another process already made
  is adopted (its access token served while still valid) instead of spending our own.
- **The rotated refresh token is persisted BEFORE the access token is served** — a
  failed persist means no token is served and nothing is cached, so a crash can never
  strand us with a spent, unpersisted rotation.
- The access token rides along in the store (encrypted, beside the lineage) so a second
  process can adopt it; in memory, expiry comes from the JWT ``exp`` claim (client-side,
  unverified decode) with ``expires_in`` fallback; monotonic clock.
- Encrypted at rest (Fernet, key from ``TRADER_XAI_TOKEN_KEY``); atomic tmp+rename
  writes; HTTP bodies are never echoed into errors (they can contain tokens).
- The file lock is taken in a worker thread with a bounded wait; a timeout is a transient
  ``XaiAuthError`` (no quarantine). A lock file that cannot be created (read-only dir,
  container) degrades to the in-process lock with one warning.
- Terminal refresh failure (400/401) → quarantine, re-login required.
  **403 = entitlement** (SuperGrok-tier gating) → quarantine, no retry spin.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import fcntl
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog
from cryptography.fernet import Fernet, InvalidToken

from trader.llm.base import LLMError
from trader.ops.config import Settings

log = structlog.get_logger(__name__)

AUTH_BASE = "https://auth.x.ai"
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
SCOPE = "openid profile email offline_access grok-cli:access api:access"

#: Refresh when the cached access token has less than this many seconds left. The
#: effective margin adapts to the observed TTL (``min(this, ttl // 3)``): xAI's
#: device-code grants return ~15-minute access tokens (embry0 audit), and a
#: fixed 30-minute margin would then refresh — burning a single-use rotating refresh
#: token — on every call.
REFRESH_MARGIN_SECONDS = 30 * 60
_DEFAULT_EXPIRES_IN = 6 * 3600
_MIN_EXPIRES_IN = 5 * 60
#: A stored access token another process left behind is adopted only with at least this
#: much life left (the margin a ~15-minute grant refreshes at anyway).
ADOPT_MIN_REMAINING_SECONDS = _MIN_EXPIRES_IN
#: Bounded wait for the cross-process refresh lock; past it the call fails transient.
LOCK_TIMEOUT_SECONDS = 30.0
_LOCK_POLL_SECONDS = 0.05


def lock_path_for(store_path: str | Path) -> Path:
    """``<store>.lock`` in the store's own directory (the advisory flock target, #68)."""
    path = Path(store_path).expanduser()
    return path.with_name(path.name + ".lock")


def _acquire_file_lock(path: Path, timeout: float) -> int:
    """Blocking (call in a worker thread): open/create ``path`` 0600 and ``flock`` it.

    Polls ``LOCK_NB`` so the wait is bounded (real monotonic time — never an injected
    clock, which a test may freeze). Returns the fd holding the lock; the caller releases
    it with :func:`_release_file_lock`. ``OSError`` from the open means the lock location
    is unusable (caller falls back); ``TimeoutError`` means another process held it for
    the whole wait.
    """
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(path) from None
                time.sleep(_LOCK_POLL_SECONDS)
    except BaseException:
        os.close(fd)
        raise


def _release_file_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _validate_token_endpoint(url: str) -> None:
    """Pin OAuth endpoints to https://*.x.ai — checked at login AND before every refresh,
    so a tampered store can never ship the refresh token to an arbitrary host."""
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not (parsed.hostname == "x.ai" or parsed.hostname.endswith(".x.ai"))
    ):
        raise XaiAuthError(f"refusing non-x.ai OAuth endpoint: {url}")


class XaiAuthError(LLMError):
    """OAuth credential unavailable/failed. Subclasses LLMError so the registry and the
    managed-bearer fallback treat auth failures like any provider failure."""


class XaiQuarantined(XaiAuthError):
    """Terminal until re-login (invalid lineage) or entitlement change. No retries."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DurableState:
    """The persisted, rotating credential lineage, plus the last access token it minted
    (wall-clock expiry) so another process can adopt it instead of rotating again."""

    refresh_token: str
    client_id: str
    token_endpoint: str
    access_token: str = ""
    access_expires_at: float | None = None  # epoch seconds (shared across processes)

    def access_remaining(self, *, now: float | None = None) -> float:
        """Seconds of life left in the stored access token; 0 when there is none."""
        if not self.access_token or self.access_expires_at is None:
            return 0.0
        return max(0.0, self.access_expires_at - (time.time() if now is None else now))


class XaiCredentialStore:
    """Fernet-encrypted single-file store; atomic writes so a crash can't tear the file."""

    def __init__(self, path: str | Path, fernet_key: str) -> None:
        if not fernet_key:
            raise XaiAuthError(
                "TRADER_XAI_TOKEN_KEY is required to encrypt the xAI credential store "
                '(generate one: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())")'
            )
        self._path = Path(path).expanduser()
        try:
            self._fernet = Fernet(fernet_key.encode())
        except (ValueError, binascii.Error) as exc:
            raise XaiAuthError("TRADER_XAI_TOKEN_KEY is not a valid Fernet key") from exc

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> DurableState | None:
        if not self._path.exists():
            return None
        try:
            plaintext = self._fernet.decrypt(self._path.read_bytes())
            data = json.loads(plaintext)
            expires_at = data.get("access_expires_at")
            return DurableState(
                refresh_token=data["refresh_token"],
                client_id=data["client_id"],
                token_endpoint=data["token_endpoint"],
                access_token=str(data.get("access_token") or ""),
                access_expires_at=float(expires_at) if expires_at is not None else None,
            )
        except (OSError, KeyError, TypeError, ValueError, InvalidToken) as exc:
            raise XaiAuthError(
                f"xAI credential store at {self._path} is unreadable or corrupt: "
                f"{type(exc).__name__}"
            ) from exc

    def save(self, state: DurableState) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        ciphertext = self._fernet.encrypt(
            json.dumps(
                {
                    "refresh_token": state.refresh_token,
                    "client_id": state.client_id,
                    "token_endpoint": state.token_endpoint,
                    "access_token": state.access_token,
                    "access_expires_at": state.access_expires_at,
                }
            ).encode()
        )
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_bytes(ciphertext)
        os.replace(tmp, self._path)


def _jwt_exp(token: str) -> float | None:
    """The ``exp`` claim of an unverified JWT, or None. We only need the lifetime hint."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return float(payload["exp"])
    except (IndexError, KeyError, ValueError, binascii.Error):
        return None


def _ttl_for(access_token: str, expires_in: object) -> float:
    """Seconds of life in a freshly minted access token: JWT ``exp`` first, then the
    response's ``expires_in``, then a conservative default."""
    exp = _jwt_exp(access_token)
    if exp is not None:
        return exp - time.time()  # JWT exp is wall-clock; convert to a TTL
    try:
        return float(expires_in) if expires_in is not None else float(_DEFAULT_EXPIRES_IN)
    except (TypeError, ValueError):
        return float(_DEFAULT_EXPIRES_IN)


class XaiCredentialManager:
    """Single owner of the rotating refresh lineage; serves in-memory access tokens.

    Build ONE per process and share it (``surfaces/api/app.py::create_app`` does): every
    ``get_access_token`` call — from any client — takes the same lock, re-checks the cache
    inside it, and a refresh re-reads the store before spending the refresh token (#63).
    Across processes (trader-api and the seat timer share one login) the same discipline
    holds under the store's file lock: take it, re-read, adopt a rotation made elsewhere
    or spend ours, persist, release (#68).
    """

    def __init__(
        self,
        store: XaiCredentialStore,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._lock_timeout = float(lock_timeout)
        self._lock_unavailable = False  # warned once; in-process lock only from then on
        self._access_token = ""
        self._access_expiry = 0.0  # in the clock's units
        self._refresh_margin = float(REFRESH_MARGIN_SECONDS)
        self._quarantine: str | None = None

    @property
    def lock_path(self) -> Path | None:
        """Where the cross-process lock lives, or None for stores that are not files."""
        path = getattr(self._store, "path", None)
        return lock_path_for(path) if path is not None else None

    @property
    def status(self) -> str:
        """``ok`` | ``absent`` | ``quarantined:<reason>`` — surfaceable in /health."""
        if self._quarantine:
            return f"quarantined:{self._quarantine}"
        try:
            return "ok" if self._store.load() else "absent"
        except XaiAuthError:
            return "corrupt"

    async def get_access_token(self) -> str:
        if self._quarantine:
            raise XaiQuarantined(
                self._quarantine, f"xai oauth quarantined ({self._quarantine}); re-login"
            )
        async with self._lock:
            if self._access_token and self._clock() < (self._access_expiry - self._refresh_margin):
                return self._access_token
            await self._refresh_locked()
            return self._access_token

    async def _refresh_locked(self) -> None:
        """Under the asyncio lock: take the file lock (worker thread, bounded), then read →
        adopt-or-refresh → persist → release."""
        fd = await self._acquire_cross_process_lock()
        try:
            await self._refresh_cross_process_locked()
        finally:
            if fd is not None:
                _release_file_lock(fd)

    async def _acquire_cross_process_lock(self) -> int | None:
        path = self.lock_path
        if path is None or self._lock_unavailable:
            return None
        try:
            return await asyncio.to_thread(_acquire_file_lock, path, self._lock_timeout)
        except TimeoutError:
            raise XaiAuthError(
                f"timed out after {self._lock_timeout:g}s waiting for the xAI refresh lock "
                f"({path}); another process is refreshing — transient, retry"
            ) from None
        except OSError as exc:
            self._lock_unavailable = True
            log.warning(
                "xai_refresh_lock_unavailable",
                path=str(path),
                error=f"{type(exc).__name__}: {exc}",
                fallback="in-process lock only; do not share this store across processes",
            )
            return None

    async def _refresh_cross_process_locked(self) -> None:
        state = self._store.load()
        if state is None:
            raise XaiAuthError("no xAI OAuth credential; run `trader xai login`")
        _validate_token_endpoint(state.token_endpoint)

        # Another process may have rotated the lineage while we waited for the lock: its
        # access token is as good as one we would mint, and costs no rotation. Our OWN
        # stored token is never re-adopted — the in-memory TTL-adaptive margin already
        # judged it due, and re-reading it would shrink that margin to the adoption floor.
        remaining = state.access_remaining()
        if remaining > ADOPT_MIN_REMAINING_SECONDS and state.access_token != self._access_token:
            self._cache_access_token(state.access_token, remaining)
            log.info("xai_access_token_adopted", remaining_s=int(remaining))
            return

        try:
            resp = await self._http.post(
                state.token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": state.refresh_token,
                    "client_id": state.client_id,
                },
            )
        except httpx.HTTPError as exc:
            raise XaiAuthError(f"xAI token refresh request failed: {exc}") from exc

        if resp.status_code == 403:
            self._quarantine = "entitlement"
            raise XaiQuarantined(
                "entitlement",
                "xAI returned 403 on refresh: the account lacks SuperGrok API access. "
                "Quarantined — fix the subscription, then `trader xai login`.",
            )
        if resp.status_code in (400, 401):
            self._quarantine = "invalid_grant"
            raise XaiQuarantined(
                "invalid_grant",
                f"xAI refused the refresh token (HTTP {resp.status_code}); the lineage is "
                "dead. Quarantined — run `trader xai login`.",
            )
        if resp.status_code != 200:
            # Transient (5xx etc.): error out but do NOT quarantine. Body redacted.
            raise XaiAuthError(f"xAI token refresh returned HTTP {resp.status_code}")

        payload = resp.json()
        access_token = payload.get("access_token", "")
        if not access_token:
            raise XaiAuthError("xAI token refresh response had no access_token")

        # THE invariant: persist the rotated refresh token (and the access token, for the
        # other process) BEFORE caching/serving. If this save raises, nothing below runs
        # and no token is served.
        new_refresh = payload.get("refresh_token") or state.refresh_token
        ttl = _ttl_for(access_token, payload.get("expires_in"))
        self._persist(state, new_refresh, access_token, ttl)
        if new_refresh != state.refresh_token:
            log.info("xai_refresh_token_rotated")

        self._cache_access_token(access_token, ttl)
        log.info("xai_access_token_refreshed")

    def _persist(self, state: DurableState, refresh: str, access_token: str, ttl: float) -> None:
        self._store.save(
            replace(
                state,
                refresh_token=refresh,
                access_token=access_token,
                access_expires_at=time.time() + ttl,
            )
        )

    def _cache_access_token(self, access_token: str, ttl: float) -> None:
        ttl = max(ttl, _MIN_EXPIRES_IN)
        self._access_token = access_token
        self._access_expiry = self._clock() + ttl
        self._refresh_margin = min(float(REFRESH_MARGIN_SECONDS), ttl // 3)

    # ------------------------------------------------------------------
    # Device-code login (rare operator action — CLI-local, never a server path)
    # ------------------------------------------------------------------

    async def login(self, *, echo: Callable[[str], None] = print) -> None:
        """RFC 8628 device-code login. Blocks until approved, then persists the lineage."""
        discovery = (await self._http.get(f"{AUTH_BASE}/.well-known/openid-configuration")).json()
        token_endpoint = discovery["token_endpoint"]
        device_endpoint = discovery.get(
            "device_authorization_endpoint", f"{AUTH_BASE}/oauth2/device/code"
        )
        for url in (token_endpoint, device_endpoint):
            _validate_token_endpoint(url)

        resp = await self._http.post(device_endpoint, data={"client_id": CLIENT_ID, "scope": SCOPE})
        resp.raise_for_status()
        grant = resp.json()
        verification = grant.get("verification_uri_complete") or grant.get("verification_uri")
        echo(f"Open {verification} and enter code: {grant.get('user_code', '')}")
        interval = float(grant.get("interval", 5))
        deadline = self._clock() + float(grant.get("expires_in", 600))

        while True:
            if self._clock() > deadline:
                raise XaiAuthError("device-code login expired before approval")
            poll = await self._http.post(
                token_endpoint,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": grant["device_code"],
                    "client_id": CLIENT_ID,
                },
            )
            if poll.status_code == 200:
                payload = poll.json()
                refresh_token = payload.get("refresh_token", "")
                if not refresh_token:
                    raise XaiAuthError("login response had no refresh_token")
                # Persist the lineage before caching the access token (same discipline).
                access_token = payload.get("access_token", "")
                ttl = _ttl_for(access_token, payload.get("expires_in"))
                self._store.save(
                    DurableState(
                        refresh_token=refresh_token,
                        client_id=CLIENT_ID,
                        token_endpoint=token_endpoint,
                        access_token=access_token,
                        access_expires_at=time.time() + ttl,
                    )
                )
                self._quarantine = None
                self._cache_access_token(access_token, ttl)
                echo("Login complete; credential stored.")
                return
            error = poll.json().get("error", "") if poll.status_code == 400 else ""
            if error == "authorization_pending":
                pass
            elif error == "slow_down":
                interval += 5
            else:
                raise XaiAuthError(
                    f"device-code login failed (HTTP {poll.status_code}, {error or 'error'})"
                )
            await asyncio.sleep(interval)


def build_manager(settings: Settings) -> XaiCredentialManager | None:
    """A manager when the encrypted store is usable in this environment, else None
    (the provider then resolves to the XAI_API_KEY fallback; consumers never know).

    Call it once per process and pass the result down (``create_app`` does); the
    client factories only fall back to it when no manager is handed in."""
    if not settings.xai_token_key:
        return None
    return XaiCredentialManager(XaiCredentialStore(settings.xai_token_path, settings.xai_token_key))
