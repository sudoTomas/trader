"""Profiles (#107): named, renamable combinations of Chains, one of them active.

A Profile maps Roles to Chains (ordered ``{provider, model}`` entries). Every edit
appends a Profile version and moves the Profile's current pointer; a revert moves it back.
The active Profile's current version serves every Role it names, from the next call and
without a restart; a Role it leaves out keeps its ``.env`` wiring, so an install with no
Profile routes exactly as before.

Nothing serves before its Probe (ADR 0001): saving a Chain, activating a Profile and
reverting the active one each run one call per entry in the Role's real contract. A
failing first entry refuses the change and leaves the active version as it was; a failing
fallback saves with a warning. Every change is audited with its caller and surface.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import psycopg
import structlog
from psycopg.types.json import Jsonb

from trader.llm.anthropic import AnthropicClient
from trader.llm.base import LLMError
from trader.llm.jev import JevClient
from trader.llm.nemotron import apply_thinking_guard
from trader.llm.openai_compat import OpenAIChatClient
from trader.llm.probe import probe
from trader.llm.providers import (
    ANTHROPIC,
    JEV,
    NAME_RE,
    OPENAI_COMPAT,
    PRESETS,
    XAI,
    ProviderDesk,
)
from trader.store.audit import CALLER, SURFACE, audit
from trader.store.db import Database

log = structlog.get_logger(__name__)

CHAT = frozenset({OPENAI_COMPAT, XAI, ANTHROPIC})
#: Which Provider kinds may serve each Role. sentiment needs x_search (xAI's
#: ``/v1/responses``); confirm needs a question-set judge (Jev).
ROLE_KINDS: dict[str, frozenset[str]] = {
    "decide": CHAT,
    "analyst": CHAT,
    "critic": CHAT,
    "brief": CHAT,
    "compiler": CHAT,
    "sentiment": frozenset({XAI}),
    "confirm": frozenset({JEV}),
}
#: The only Role whose Chain may be empty, meaning "off".
OPTIONAL_ROLES = frozenset({"confirm"})

ACTIVE_VERSION = """
    SELECT p.name, v.id, v.version, v.roles
    FROM llm_profiles p JOIN llm_profile_versions v ON v.id = p.current_version
    WHERE p.active
"""


class ProfileError(ValueError):
    """A Profile operation was refused; the message says why."""


class ProfileNotFound(LookupError):
    """No Profile (or Profile version) by that name."""


def _same_endpoint(left: str, right: str) -> bool:
    return bool(right.strip()) and left.strip().rstrip("/") == right.strip().rstrip("/")


class ProfileDesk:
    """Every Profile operation, and the Role routes the shared registry reads per call."""

    def __init__(
        self,
        db: Database,
        providers: ProviderDesk,
        *,
        nemotron_base_url: str = "",
        sentiment_handles: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        anthropic_transport: Any | None = None,  # httpx2 transport, for tests
        caps: Any | None = None,  # llm.caps.SpendCaps
    ) -> None:
        self._db = db
        self._providers = providers
        self._nemotron_base_url = nemotron_base_url
        self._handles = sentiment_handles or []
        self._transport = transport
        self._anthropic_transport = anthropic_transport
        self._caps = caps
        # (provider id, provider updated_at, role kind, model) -> client. A key change or
        # rename bumps updated_at, so the next call builds a fresh client.
        self._clients: dict[tuple[Any, ...], Any] = {}

    # --- reads ------------------------------------------------------------------------

    async def list(self) -> list[dict[str, Any]]:
        names = await self._provider_names()
        rows = await self._db.fetch_all(
            """
            SELECT p.name, p.active, v.version, v.roles
            FROM llm_profiles p JOIN llm_profile_versions v ON v.id = p.current_version
            ORDER BY p.name
            """
        )
        return [self._out(r, names) for r in rows]

    async def get(self, name: str) -> dict[str, Any]:
        """One Profile with every version, newest first."""
        names = await self._provider_names()
        profile = await self._profile(name)
        versions = await self._db.fetch_all(
            "SELECT version, roles, created_at FROM llm_profile_versions"
            " WHERE profile_id = %s ORDER BY version DESC",
            (profile["id"],),
        )
        current = next(v for v in versions if v["version"] == profile["version"])
        return {
            **self._out({**profile, "roles": current["roles"]}, names),
            "versions": [
                {
                    "version": v["version"],
                    "roles": self._roles_out(v["roles"], names),
                    "created_at": v["created_at"].isoformat(),
                }
                for v in versions
            ],
        }

    async def routes(self) -> dict[str, dict[str, Any]]:
        """Where each Role is served from now: the active Profile, or ``.env``."""
        rows = {r["id"]: r for r in await self._db.fetch_all("SELECT * FROM llm_providers")}
        active = await self._db.fetch_one(ACTIVE_VERSION)
        out: dict[str, dict[str, Any]] = {}
        for role in ROLE_KINDS:
            if active is not None and role in active["roles"]:
                chain = []
                for entry in active["roles"][role]:
                    row = rows.get(entry["provider_id"])
                    chain.append(
                        {
                            "provider": row["name"] if row else "(removed)",
                            "model": entry["model"],
                            "price": await self._providers.prices.price(row, entry["model"])
                            if row
                            else None,
                        }
                    )
                out[role] = {
                    "source": "profile",
                    "profile": active["name"],
                    "version": active["version"],
                    "chain": chain,
                }
            else:
                out[role] = {"source": ".env"}
        return out

    # --- writes -----------------------------------------------------------------------

    async def create(self, name: str) -> dict[str, Any]:
        """An empty Profile (every Role on ``.env``). The first one becomes active."""
        self._check_name(name)
        profile = await self._insert_profile(name, {})
        await self._audit("create_profile", name, {"active": profile["active"]})
        return profile

    async def duplicate(self, name: str, new_name: str) -> dict[str, Any]:
        """A copy of a Profile's current Chains under a new name, inactive."""
        self._check_name(new_name)
        source = await self._profile(name)
        profile = await self._insert_profile(new_name, source["roles"])
        await self._audit(
            "duplicate_profile", new_name, {"from": name, "version": source["version"]}
        )
        return profile

    async def rename(self, name: str, new_name: str) -> dict[str, Any]:
        self._check_name(new_name)
        try:
            row = await self._db.fetch_one(
                "UPDATE llm_profiles SET name = %s, updated_at = now() WHERE name = %s"
                " RETURNING id",
                (new_name, name),
            )
        except psycopg.errors.UniqueViolation as exc:
            raise ProfileError(f"a Profile named {new_name!r} already exists") from exc
        if row is None:
            raise ProfileNotFound(name)
        await self._audit("rename_profile", new_name, {"from": name})
        return await self._summary(new_name)

    async def set_chain(self, name: str, role: str, chain: list[dict[str, str]]) -> dict[str, Any]:
        """Save a Role's Chain as a new version, after the Probe."""
        profile = await self._profile(name)
        entries = await self._resolve_chain(role, chain)
        prices = [await self._providers.prices.price(e, e["model"]) for e in entries]
        if self._caps is not None and await self._caps.cap(role) is not None:
            # A capped Role must be able to count what it spends (#108).
            for entry, price in zip(entries, prices, strict=True):
                if price["source"] == "unknown":
                    raise ProfileError(
                        f"{entry['name']}:{entry['model']} has no price and {role} has a"
                        " spend cap; set one first (trader provider price)"
                    )
        results, warnings = await self._probe(role, entries)
        roles = {
            **profile["roles"],
            role: [{"provider_id": e["id"], "model": e["model"]} for e in entries],
        }
        version = await self._append_version(profile, roles)
        await self._audit(
            "set_role_chain",
            name,
            {"role": role, "version": version, "chain": [self._entry(e) for e in entries]},
        )
        estimate = None
        if self._caps is not None and entries:
            first = prices[0]
            usd = await self._caps.estimate(role, (first["price_in"], first["price_out"]))
            estimate = {
                "usd_per_day": usd,
                "basis": f"7-day average volume × {entries[0]['name']}:{entries[0]['model']}",
            }
        return {
            "profile": await self._summary(name),
            "probe": results,
            "warnings": warnings,
            "estimate": estimate,
        }

    async def reset_role(self, name: str, role: str) -> dict[str, Any]:
        """Drop a Role from the Profile, so it serves from ``.env`` again."""
        self._check_role(role)
        profile = await self._profile(name)
        if role not in profile["roles"]:
            raise ProfileError(f"{name!r} does not set {role}; it already uses .env")
        roles = {r: c for r, c in profile["roles"].items() if r != role}
        version = await self._append_version(profile, roles)
        await self._audit("reset_role", name, {"role": role, "version": version})
        return await self._summary(name)

    async def activate(self, name: str) -> dict[str, Any]:
        """Make this Profile the one that serves, after probing its every Chain."""
        profile = await self._profile(name)
        results, warnings = await self._probe_roles(profile["roles"])
        async with self._db.connection() as conn:
            await conn.execute("UPDATE llm_profiles SET active = false WHERE active")
            await conn.execute(
                "UPDATE llm_profiles SET active = true, updated_at = now() WHERE id = %s",
                (profile["id"],),
            )
        await self._audit("activate_profile", name, {"version": profile["version"]})
        return {"profile": await self._summary(name), "probe": results, "warnings": warnings}

    async def revert(self, name: str, version: int) -> dict[str, Any]:
        """Make an earlier version current again. On the active Profile, Probe first."""
        profile = await self._profile(name)
        row = await self._db.fetch_one(
            "SELECT id, roles FROM llm_profile_versions WHERE profile_id = %s AND version = %s",
            (profile["id"], version),
        )
        if row is None:
            raise ProfileNotFound(f"{name} v{version}")
        results: dict[str, list[dict[str, Any]]] = {}
        warnings: list[str] = []
        if profile["active"]:
            results, warnings = await self._probe_roles(row["roles"])
        await self._db.execute(
            "UPDATE llm_profiles SET current_version = %s, updated_at = now() WHERE id = %s",
            (row["id"], profile["id"]),
        )
        await self._audit("revert_profile", name, {"from": profile["version"], "to": version})
        return {"profile": await self._summary(name), "probe": results, "warnings": warnings}

    # --- what the registry reads per call ----------------------------------------------

    async def route(self, role: str) -> tuple[int, list[Any]] | None:
        """The active version's id and clients for ``role``; None when ``.env`` serves it."""
        active = await self._db.fetch_one(ACTIVE_VERSION)
        if active is None or role not in active["roles"]:
            return None
        entries = active["roles"][role]
        rows = {
            r["id"]: r
            for r in await self._db.fetch_all(
                "SELECT * FROM llm_providers WHERE id = ANY(%s)",
                ([e["provider_id"] for e in entries],),
            )
        }
        clients = []
        for entry in entries:
            row = rows.get(entry["provider_id"])
            if row is None:
                log.warning("profile_provider_missing", role=role, provider_id=entry["provider_id"])
                continue
            # Priced at build, so usage_daily meters what the picker shows (#105).
            price = await self._providers.prices.price(row, entry["model"])
            clients.append(
                self._client(
                    role, {**row, "model": entry["model"]}, (price["price_in"], price["price_out"])
                )
            )
        if entries and not clients:
            raise LLMError(f"every Provider in the active Profile's {role} Chain was removed")
        return active["id"], clients

    # --- internals --------------------------------------------------------------------

    def _client(
        self, role: str, entry: dict[str, Any], price: tuple[float, float] = (0.0, 0.0)
    ) -> Any:
        """One client per Provider, model, price and kind of call; rebuilt after a key
        change (updated_at) or a price change. Probe clients are unpriced: unmetered."""
        sentiment = role == "sentiment"
        cache_key = (entry["id"], entry["updated_at"], entry["model"], sentiment, price)
        price_in, price_out = price
        client = self._clients.get(cache_key)
        if client is not None:
            return client
        api_key = self._providers.api_key(entry)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        if sentiment:
            from trader.research.sentiment import XSentimentClient

            http = httpx.AsyncClient(
                base_url=entry["base_url"], headers=headers, timeout=60.0, transport=self._transport
            )
            client = XSentimentClient(http, model=entry["model"], allowed_x_handles=self._handles)
            client.name = entry["name"]
        elif entry["kind"] == ANTHROPIC:
            client = AnthropicClient(
                name=entry["name"],
                model=entry["model"],
                api_key=api_key or "",
                base_url=entry["base_url"],
                transport=self._anthropic_transport,
                price_in=price_in,
                price_out=price_out,
            )
        elif entry["kind"] == JEV:
            client = JevClient(
                api_key=api_key or "",
                base_url=entry["base_url"],
                model=entry["model"],
                client=httpx.AsyncClient(timeout=30.0, transport=self._transport),
                price_in=price_in,
                price_out=price_out,
            )
            client.name = entry["name"]
        else:
            # The Nemotron thinking guard follows the URL, never the preset name: the
            # self-hosted vLLM gets it whatever it is called, and no vendor ever does.
            on_preset = _same_endpoint(entry["base_url"], self._nemotron_base_url)
            client = OpenAIChatClient(
                name=entry["name"],
                model=entry["model"],
                base_url=entry["base_url"],
                extra_body=apply_thinking_guard({}) if on_preset else {},
                grammar_safe=not on_preset,
                price_in=price_in,
                price_out=price_out,
                client=httpx.AsyncClient(
                    base_url=entry["base_url"],
                    headers=headers,
                    timeout=120.0,
                    transport=self._transport,
                ),
            )
        self._clients[cache_key] = client
        return client

    async def _resolve_chain(self, role: str, chain: list[dict[str, str]]) -> list[dict[str, Any]]:
        self._check_role(role)
        if not chain and role not in OPTIONAL_ROLES:
            raise ProfileError(f"{role} needs at least one model; reset it to use .env instead")
        entries = []
        for item in chain:
            provider = str(item.get("provider", "")).strip()
            model = str(item.get("model") or "").strip()
            if not provider:
                raise ProfileError("each Chain entry needs a provider and a model")
            row = await self._db.fetch_one(
                "SELECT * FROM llm_providers WHERE name = %s", (provider,)
            )
            if row is None:
                raise ProfileError(f"no Provider named {provider!r}")
            preset = PRESETS.get(row["preset"])
            model = model or (preset.default_model if preset else None) or ""
            if not model:
                raise ProfileError("each Chain entry needs a provider and a model")
            if row["kind"] not in ROLE_KINDS[role]:
                raise ProfileError(self._refusal(role, provider, row["kind"]))
            entries.append({**row, "model": model})
        return entries

    @staticmethod
    def _refusal(role: str, provider: str, kind: str) -> str:
        if role == "sentiment":
            return f"sentiment needs x_search, which only xAI serves; {provider} is {kind}"
        if role == "confirm":
            return (
                f"confirm takes a Jev Provider only (or an empty Chain: off); {provider} is {kind}"
            )
        return f"{role} needs a chat model; {provider} is {kind}, which answers question sets only"

    async def _probe(
        self, role: str, entries: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        results = list(await asyncio.gather(*(probe(role, self._client(role, e)) for e in entries)))
        for entry, result in zip(entries, results, strict=True):
            result.update(provider=entry["name"], model=entry["model"])
        if results and not results[0]["ok"]:
            first = results[0]
            raise ProfileError(
                f"{first['provider']}:{first['model']} failed its {role} Probe: {first['error']};"
                " nothing was saved"
            )
        warnings = [
            f"{role} fallback {r['provider']}:{r['model']} failed its Probe: {r['error']}"
            for r in results[1:]
            if not r["ok"]
        ]
        return results, warnings

    async def _probe_roles(
        self, roles: dict[str, list[dict[str, Any]]]
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        rows = {r["id"]: r for r in await self._db.fetch_all("SELECT * FROM llm_providers")}
        results: dict[str, list[dict[str, Any]]] = {}
        warnings: list[str] = []
        for role, chain in roles.items():
            missing = [e["provider_id"] for e in chain if e["provider_id"] not in rows]
            if missing:
                raise ProfileError(f"{role} names a Provider that was removed")
            entries = [{**rows[e["provider_id"]], "model": e["model"]} for e in chain]
            results[role], role_warnings = await self._probe(role, entries)
            warnings.extend(role_warnings)
        return results, warnings

    async def _insert_profile(self, name: str, roles: dict[str, Any]) -> dict[str, Any]:
        async with self._db.connection() as conn:
            try:
                cur = await conn.execute(
                    """
                    INSERT INTO llm_profiles (name, active)
                    VALUES (%s, NOT EXISTS (SELECT 1 FROM llm_profiles WHERE active))
                    RETURNING id
                    """,
                    (name,),
                )
            except psycopg.errors.UniqueViolation as exc:
                raise ProfileError(f"a Profile named {name!r} already exists") from exc
            row = await cur.fetchone()
            assert row is not None
            cur = await conn.execute(
                "INSERT INTO llm_profile_versions (profile_id, version, roles)"
                " VALUES (%s, 1, %s) RETURNING id",
                (row["id"], Jsonb(roles)),
            )
            version = await cur.fetchone()
            assert version is not None
            await conn.execute(
                "UPDATE llm_profiles SET current_version = %s WHERE id = %s",
                (version["id"], row["id"]),
            )
        return await self._summary(name)

    async def _append_version(self, profile: dict[str, Any], roles: dict[str, Any]) -> int:
        async with self._db.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO llm_profile_versions (profile_id, version, roles)
                SELECT %s, max(version) + 1, %s FROM llm_profile_versions WHERE profile_id = %s
                RETURNING id, version
                """,
                (profile["id"], Jsonb(roles), profile["id"]),
            )
            row = await cur.fetchone()
            assert row is not None
            await conn.execute(
                "UPDATE llm_profiles SET current_version = %s, updated_at = now() WHERE id = %s",
                (row["id"], profile["id"]),
            )
        return row["version"]

    async def _profile(self, name: str) -> dict[str, Any]:
        row = await self._db.fetch_one(
            """
            SELECT p.id, p.name, p.active, v.version, v.roles
            FROM llm_profiles p JOIN llm_profile_versions v ON v.id = p.current_version
            WHERE p.name = %s
            """,
            (name,),
        )
        if row is None:
            raise ProfileNotFound(name)
        return row

    async def _summary(self, name: str) -> dict[str, Any]:
        return self._out(await self._profile(name), await self._provider_names())

    async def _provider_names(self) -> dict[int, str]:
        return {
            r["id"]: r["name"]
            for r in await self._db.fetch_all("SELECT id, name FROM llm_providers")
        }

    def _out(self, row: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
        return {
            "name": row["name"],
            "active": row["active"],
            "version": row["version"],
            "roles": self._roles_out(row["roles"], names),
        }

    @staticmethod
    def _roles_out(roles: dict[str, Any], names: dict[int, str]) -> dict[str, list[dict[str, str]]]:
        return {
            role: [
                {"provider": names.get(e["provider_id"], "(removed)"), "model": e["model"]}
                for e in chain
            ]
            for role, chain in sorted(roles.items())
        }

    @staticmethod
    def _entry(entry: dict[str, Any]) -> dict[str, str]:
        return {"provider": entry["name"], "model": entry["model"]}

    @staticmethod
    def _check_name(name: str) -> None:
        if not NAME_RE.match(name):
            raise ProfileError(
                f"name {name!r} must be 1-40 chars of a-z, 0-9, '-' or '_', starting alphanumeric"
            )

    @staticmethod
    def _check_role(role: str) -> None:
        if role not in ROLE_KINDS:
            raise ProfileError(f"unknown Role {role!r}; one of {sorted(ROLE_KINDS)}")

    async def _audit(self, action: str, name: str, payload: dict[str, Any]) -> None:
        await audit(
            self._db,
            actor=CALLER.get(),
            category="profiles",
            action=action,
            subject=name,
            payload={**payload, "surface": SURFACE.get()},
        )
