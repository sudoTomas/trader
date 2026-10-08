"""Providers (#103): where models are served from, hosted or local.

A Provider is data: a name, the preset it came from, its adapter kind, a base URL and,
for a hosted one, an API key. Keys are encrypted at rest with Fernet (master key
``TRADER_SECRETS_KEY``, which stays in ``.env``; see docs/adr/0002) and are write-only:
no read returns one, only ``••••`` plus its last four characters. Every change is audited
without the value.

Roles reach Providers through Profiles (``llm.profiles``, #107).
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import psycopg
from cryptography.fernet import Fernet, InvalidToken

from trader.llm.pricing import PriceBook
from trader.store.audit import audit
from trader.store.db import Database

OPENAI_COMPAT = "openai_compat"
ANTHROPIC = "anthropic"
XAI = "xai"
JEV = "jev"
#: A Provider at a URL of your choosing that speaks OpenAI chat. Local when it has no key.
CUSTOM = "custom"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
MODELS_TTL_SECONDS = 60.0
DISCOVERY_TIMEOUT_SECONDS = 1.0


class ProviderError(ValueError):
    """A Provider operation was refused; the message says why and what fixes it."""


class ProviderNotFound(LookupError):
    """No Provider has that name."""


@dataclass(frozen=True)
class Preset:
    kind: str
    base_url: str
    local: bool = False
    #: Model a new Chain entry gets when none is named (None: one must be named).
    default_model: str | None = None

    @property
    def needs_key(self) -> bool:
        return not self.local


PRESETS: dict[str, Preset] = {
    "openai": Preset(OPENAI_COMPAT, "https://api.openai.com/v1"),
    "anthropic": Preset(ANTHROPIC, "https://api.anthropic.com/v1", default_model="claude-opus-5-5"),
    "gemini": Preset(OPENAI_COMPAT, "https://generativelanguage.googleapis.com/v1beta/openai"),
    "xai": Preset(XAI, "https://api.x.ai/v1"),
    "openrouter": Preset(OPENAI_COMPAT, "https://openrouter.ai/api/v1"),
    "groq": Preset(OPENAI_COMPAT, "https://api.groq.com/openai/v1"),
    "together": Preset(OPENAI_COMPAT, "https://api.together.xyz/v1"),
    "jev": Preset(JEV, "https://api.typesafe.ai/v1/systemone"),
    "ollama": Preset(OPENAI_COMPAT, "http://localhost:11434/v1", local=True),
    "lmstudio": Preset(OPENAI_COMPAT, "http://localhost:1234/v1", local=True),
    "llamacpp": Preset(OPENAI_COMPAT, "http://localhost:8080/v1", local=True),
    "vllm": Preset(OPENAI_COMPAT, "http://localhost:8000/v1", local=True),
}


def on_private_host(url: str) -> bool:
    """True for localhost, a loopback address or a private (LAN) address."""
    host = urlparse(url).hostname or ""
    if host == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


#: Below this length no character of a key is shown: four would be most or all of it.
MIN_KEY_LEN_FOR_HINT = 8


def _hint(api_key: str) -> str:
    return api_key[-4:] if len(api_key) >= MIN_KEY_LEN_FOR_HINT else ""


def _mask(last4: str | None) -> str | None:
    return None if last4 is None else f"••••{last4}"


def _out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": row["name"],
        "preset": row["preset"],
        "kind": row["kind"],
        "base_url": row["base_url"],
        "local": row["local"],
        "key": _mask(row["key_last4"]),
    }


class ProviderDesk:
    """Every Provider operation. Built once per process; the surfaces call it."""

    def __init__(
        self,
        db: Database,
        *,
        secrets_key: str,
        actor: str = "surface",
        http: httpx.AsyncClient | None = None,
        anthropic_transport: Any | None = None,  # httpx2 transport, for tests
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._actor = actor
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self._anthropic_transport = anthropic_transport
        self.prices = PriceBook(db, self._http, clock=clock)
        # name -> (fetched at, model ids); see models().
        self._models: dict[str, tuple[float, list[str]]] = {}
        # A bad master key refuses key operations with the reason; it never stops the API
        # (and with it the decide loop) from booting.
        self._fernet: Fernet | None = None
        self._key_problem = "TRADER_SECRETS_KEY is not set"
        if secrets_key:
            try:
                self._fernet = Fernet(secrets_key.encode())
            except ValueError:
                self._key_problem = "TRADER_SECRETS_KEY is not a valid Fernet key"

    def _encrypt(self, api_key: str) -> str:
        if self._fernet is None:
            raise ProviderError(
                f"{self._key_problem}, so keys cannot be stored; fix it and restart"
            )
        return self._fernet.encrypt(api_key.encode()).decode()

    async def add(
        self, *, name: str, preset: str, api_key: str | None = None, base_url: str | None = None
    ) -> dict[str, Any]:
        """Add a Provider from a preset. ``base_url`` overrides the preset's address."""
        if not NAME_RE.match(name):
            raise ProviderError(
                f"name {name!r} must be 1-40 chars of a-z, 0-9, '-' or '_', starting alphanumeric"
            )
        api_key = (api_key or "").strip() or None
        base_url = (base_url or "").strip().rstrip("/") or None
        if preset == CUSTOM:
            if base_url is None:
                raise ProviderError("custom needs a base_url")
            spec = Preset(OPENAI_COMPAT, base_url, local=api_key is None)
        elif preset in PRESETS:
            spec = PRESETS[preset]
        else:
            raise ProviderError(f"unknown preset {preset!r}; one of {sorted([*PRESETS, CUSTOM])}")
        if spec.needs_key and api_key is None:
            raise ProviderError(f"{preset} needs an API key")
        url = base_url or spec.base_url
        # Local means the model runs on hardware you own: a local preset, or any Provider
        # on a loopback or private address (Kev on 127.0.0.1 needs a key and is still local).
        local = spec.local or on_private_host(url)
        enc = self._encrypt(api_key) if api_key else None
        try:
            row = await self._db.fetch_one(
                """
                INSERT INTO llm_providers (name, preset, kind, base_url, local, api_key_enc,
                                           key_last4)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    name,
                    preset,
                    spec.kind,
                    url,
                    local,
                    enc,
                    _hint(api_key) if api_key else None,
                ),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ProviderError(f"a Provider named {name!r} already exists") from exc
        assert row is not None
        await self._audit("add_provider", name, {"preset": preset, "base_url": url, "local": local})
        return _out(row)

    async def set_key(self, name: str, api_key: str) -> dict[str, Any]:
        """Replace (or first set) a Provider's key. The old key is gone for good."""
        api_key = api_key.strip()
        if not api_key:
            raise ProviderError("the key is empty")
        row = await self._db.fetch_one(
            """
            UPDATE llm_providers SET api_key_enc = %s, key_last4 = %s, updated_at = now()
            WHERE name = %s RETURNING *
            """,
            (self._encrypt(api_key), _hint(api_key), name),
        )
        if row is None:
            raise ProviderNotFound(name)
        await self._audit("set_provider_key", name, {})
        return _out(row)

    async def rename(self, name: str, new_name: str) -> dict[str, Any]:
        if not NAME_RE.match(new_name):
            raise ProviderError(f"name {new_name!r} must be 1-40 chars of a-z, 0-9, '-' or '_'")
        try:
            row = await self._db.fetch_one(
                "UPDATE llm_providers SET name = %s, updated_at = now() WHERE name = %s"
                " RETURNING *",
                (new_name, name),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ProviderError(f"a Provider named {new_name!r} already exists") from exc
        if row is None:
            raise ProviderNotFound(name)
        self._models.pop(name, None)
        await self._audit("rename_provider", new_name, {"from": name})
        return _out(row)

    async def remove(self, name: str) -> dict[str, Any]:
        """Remove a Provider. Refused while a Profile's current Chain uses it; older
        versions may still name it (they read as ``(removed)``)."""
        users = await self._db.fetch_all(
            """
            SELECT p.name AS profile, r.role
            FROM llm_profiles p
            JOIN llm_profile_versions v ON v.id = p.current_version
            CROSS JOIN LATERAL jsonb_each(v.roles) AS r(role, chain)
            JOIN llm_providers lp ON lp.name = %s
            WHERE r.chain @> jsonb_build_array(jsonb_build_object('provider_id', lp.id))
            ORDER BY p.name, r.role
            """,
            (name,),
        )
        if users:
            uses = ", ".join(f"{u['profile']} uses it ({u['role']})" for u in users)
            raise ProviderError(f"{uses}; change that Chain first")
        row = await self._db.fetch_one(
            "DELETE FROM llm_providers WHERE name = %s RETURNING name", (name,)
        )
        if row is None:
            raise ProviderNotFound(name)
        await self._audit("remove_provider", name, {})
        return {"removed": True, "name": name}

    @staticmethod
    def presets() -> dict[str, dict[str, Any]]:
        """Every preset a Provider can be added from, plus ``custom`` (any OpenAI-chat URL)."""
        out = {
            name: {
                "kind": p.kind,
                "base_url": p.base_url,
                "local": p.local,
                "needs_key": p.needs_key,
                "default_model": p.default_model,
            }
            for name, p in PRESETS.items()
        }
        out[CUSTOM] = {
            "kind": OPENAI_COMPAT,
            "base_url": None,
            "local": None,
            "needs_key": False,
            "default_model": None,
        }
        return out

    async def models(self, name: str) -> list[dict[str, Any]]:
        """The models the Provider serves now, each with its price and the price's source
        (fetched live, cached briefly). Any id can still be typed where a model is
        picked; this list is a convenience."""
        row = await self._row(name)
        ids = await self._model_ids(name, row)
        priced = await self.prices.prices(row, ids)
        return [{"id": model, **price} for model, price in zip(ids, priced, strict=True)]

    async def price(self, name: str, model: str) -> dict[str, Any]:
        """What one model costs on this Provider, USD per 1M tokens, and why."""
        return await self.prices.price(await self._row(name), model)

    async def set_price(
        self, name: str, model: str, *, price_in: float, price_out: float
    ) -> dict[str, Any]:
        """Price a model by hand (USD per 1M tokens); wins over every live source."""
        if not all(math.isfinite(p) and p >= 0 for p in (price_in, price_out)):
            raise ProviderError("prices must be non-negative numbers (USD per 1M tokens)")
        row = await self._row(name)
        if row["local"]:
            raise ProviderError(f"{name} is local and local models are free; nothing to price")
        model = model.strip()
        if not model:
            raise ProviderError("name the model to price")
        await self._db.execute(
            """
            INSERT INTO llm_price_overrides (provider_id, model, price_in, price_out)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (provider_id, model) DO UPDATE
            SET price_in = EXCLUDED.price_in, price_out = EXCLUDED.price_out, updated_at = now()
            """,
            (row["id"], model, price_in, price_out),
        )
        await self._audit(
            "set_model_price", name, {"model": model, "price_in": price_in, "price_out": price_out}
        )
        return {"model": model, **await self.prices.price(row, model)}

    async def clear_price(self, name: str, model: str) -> dict[str, Any]:
        """Drop a model's manual price; the live sources price it again."""
        row = await self._row(name)
        cleared = await self._db.execute(
            "DELETE FROM llm_price_overrides WHERE provider_id = %s AND model = %s",
            (row["id"], model),
        )
        if cleared:
            await self._audit("clear_model_price", name, {"model": model})
        return {"provider": name, "model": model, "cleared": bool(cleared)}

    async def _model_ids(self, name: str, row: dict[str, Any]) -> list[str]:
        cached = self._models.get(name)
        if cached is not None and time.monotonic() - cached[0] < MODELS_TTL_SECONDS:
            return cached[1]
        if row["kind"] == JEV:
            return []  # Jev serves no model list; type the model id
        try:
            if row["kind"] == ANTHROPIC:
                ids = await self._anthropic_ids(row)
            else:
                headers = {}
                if row["api_key_enc"]:
                    headers["Authorization"] = f"Bearer {self._decrypt(row['api_key_enc'])}"
                ids = await self._fetch_ids(row["preset"], row["base_url"], headers)
        except ProviderError as exc:
            raise ProviderError(f"{name}: {exc}") from exc
        result = sorted(ids)
        self._models[name] = (time.monotonic(), result)
        return result

    async def discover(self) -> list[dict[str, Any]]:
        """Local model servers answering on this host's usual ports. Adds nothing."""

        async def probe(preset: str) -> dict[str, Any] | None:
            base = PRESETS[preset].base_url
            try:
                ids = await self._fetch_ids(
                    preset, base, {}, request_timeout=DISCOVERY_TIMEOUT_SECONDS
                )
            except ProviderError:
                return None  # nothing there, or something that is not a model server
            return {"preset": preset, "base_url": base, "models": len(ids)}

        local = sorted(p for p, spec in PRESETS.items() if spec.local)
        found = await asyncio.gather(*(probe(p) for p in local))
        return [f for f in found if f is not None]

    async def _fetch_ids(
        self,
        preset: str,
        base_url: str,
        headers: dict[str, str],
        *,
        request_timeout: float | None = None,
    ) -> list[str]:
        ollama = preset == "ollama"
        url = base_url.removesuffix("/v1") + "/api/tags" if ollama else base_url + "/models"
        kwargs: dict[str, Any] = {"headers": headers}
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        try:
            resp = await self._http.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise ProviderError(f"unreachable ({type(exc).__name__})") from exc
        if resp.status_code != 200:
            # Status only: a response body can echo the request, key included.
            raise ProviderError(f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
            if ollama:
                return [m["name"] for m in payload["models"]]
            return [m["id"] for m in payload["data"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(f"malformed model list ({type(exc).__name__})") from exc

    async def _anthropic_ids(self, row: dict[str, Any]) -> list[str]:
        """Claude model ids from the Models API, through the SDK."""
        from trader.llm.anthropic import list_models
        from trader.llm.base import LLMError

        try:
            return await list_models(
                self.api_key(row) or "", row["base_url"], self._anthropic_transport
            )
        except LLMError as exc:
            raise ProviderError(str(exc).removeprefix("anthropic: ")) from exc

    async def _row(self, name: str) -> dict[str, Any]:
        row = await self._db.fetch_one("SELECT * FROM llm_providers WHERE name = %s", (name,))
        if row is None:
            raise ProviderNotFound(name)
        return row

    def api_key(self, row: dict[str, Any]) -> str | None:
        """A Provider row's key in plaintext, for building its client. Never returned by a
        surface; only the Profile routes call this."""
        return self._decrypt(row["api_key_enc"]) if row["api_key_enc"] else None

    def _decrypt(self, token: str) -> str:
        if self._fernet is None:
            raise ProviderError(f"{self._key_problem}, so stored keys cannot be read")
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken as exc:
            raise ProviderError(
                "a stored key does not decrypt with TRADER_SECRETS_KEY; was the key changed?"
            ) from exc

    async def _audit(self, action: str, name: str, payload: dict[str, Any]) -> None:
        self._models.pop(name, None)
        # Never a key, old or new: the trail records that it changed, not what to.
        await audit(
            self._db,
            actor=self._actor,
            category="providers",
            action=action,
            subject=name,
            payload=payload,
        )

    async def list(self) -> list[dict[str, Any]]:
        rows = await self._db.fetch_all("SELECT * FROM llm_providers ORDER BY name")
        return [_out(r) for r in rows]
