"""SuperGrok device-code OAuth: login, rotating-refresh discipline, quarantine.

Hermes-style RFC 8628 flow against ``auth.x.ai`` (operator decision from v1:EAI-142 —
no Grok CLI dependency, no ``~/.grok/auth.json`` seeding: that store's rotating tokens
are single-use, and stealing one breaks the CLI's own login). Refresher shape cribbed
from embry0's ``xai_credential.py``.

Invariants (v1:EAI-142/143):
- Exactly one process owns the refresh lineage; refreshes serialize under an
  ``asyncio.Lock`` so a rotating single-use refresh token is never double-spent.
- **The rotated refresh token is persisted BEFORE the access token is served** — a
  failed persist means no token is served and nothing is cached, so a crash can never
  strand us with a spent, unpersisted rotation.
- The access token is never persisted; expiry comes from the JWT ``exp`` claim
  (client-side, unverified decode) with ``expires_in`` fallback; monotonic clock.
- Encrypted at rest (Fernet, key from ``TRADER_XAI_TOKEN_KEY``); atomic tmp+rename
  writes; HTTP bodies are never echoed into errors (they can contain tokens).
- Terminal refresh failure (400/401) → quarantine, re-login required.
  **403 = entitlement** (SuperGrok-tier gating) → quarantine, no retry spin.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
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

#: Refresh when the cached access token has less than this many seconds left.
REFRESH_MARGIN_SECONDS = 30 * 60
_DEFAULT_EXPIRES_IN = 6 * 3600
_MIN_EXPIRES_IN = 5 * 60


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
    """The persisted, rotating credential lineage. The access token is NOT here."""

    refresh_token: str
    client_id: str
    token_endpoint: str


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
            return DurableState(
                refresh_token=data["refresh_token"],
                client_id=data["client_id"],
                token_endpoint=data["token_endpoint"],
            )
        except (OSError, KeyError, ValueError, InvalidToken) as exc:
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


class XaiCredentialManager:
    """Single owner of the rotating refresh lineage; serves in-memory access tokens."""

    def __init__(
        self,
        store: XaiCredentialStore,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        self._clock = clock
        self._lock = asyncio.Lock()
        self._access_token = ""
        self._access_expiry = 0.0  # in the clock's units
        self._quarantine: str | None = None

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
            if self._access_token and self._clock() < (
                self._access_expiry - REFRESH_MARGIN_SECONDS
            ):
                return self._access_token
            await self._refresh_locked()
            return self._access_token

    async def _refresh_locked(self) -> None:
        state = self._store.load()
        if state is None:
            raise XaiAuthError("no xAI OAuth credential; run `trader xai login`")
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

        # THE invariant: persist the rotated refresh token BEFORE caching/serving the
        # access token. If this save raises, nothing below runs and no token is served.
        new_refresh = payload.get("refresh_token") or state.refresh_token
        if new_refresh != state.refresh_token:
            self._store.save(replace(state, refresh_token=new_refresh))
            log.info("xai_refresh_token_rotated")

        self._cache_access_token(access_token, payload.get("expires_in"))
        log.info("xai_access_token_refreshed")

    def _cache_access_token(self, access_token: str, expires_in: object) -> None:
        exp = _jwt_exp(access_token)
        if exp is not None:
            ttl = exp - time.time()  # JWT exp is wall-clock; convert to a TTL
        else:
            try:
                ttl = float(expires_in) if expires_in is not None else _DEFAULT_EXPIRES_IN
            except (TypeError, ValueError):
                ttl = _DEFAULT_EXPIRES_IN
        ttl = max(ttl, _MIN_EXPIRES_IN)
        self._access_token = access_token
        self._access_expiry = self._clock() + ttl

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
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or not (parsed.hostname == "x.ai" or parsed.hostname.endswith(".x.ai"))
            ):
                raise XaiAuthError(f"refusing non-x.ai OAuth endpoint from discovery: {url}")

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
                self._store.save(
                    DurableState(
                        refresh_token=refresh_token,
                        client_id=CLIENT_ID,
                        token_endpoint=token_endpoint,
                    )
                )
                self._quarantine = None
                self._cache_access_token(payload.get("access_token", ""), payload.get("expires_in"))
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
    (the provider then resolves to the XAI_API_KEY fallback; consumers never know)."""
    if not settings.xai_token_key:
        return None
    return XaiCredentialManager(XaiCredentialStore(settings.xai_token_path, settings.xai_token_key))
