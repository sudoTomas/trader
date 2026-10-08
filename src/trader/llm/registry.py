"""Per-role model routing: ``{role -> [client, fallback, ...]}`` resolved from config.

``decide`` / ``analyst`` use ``TRADER_DECIDE_*`` (any OpenAI-compatible endpoint) or,
when that base URL is empty, the Nemotron preset. ``brief`` / ``compiler`` /
``sentiment`` / ``critic`` use ``TRADER_RESEARCH_*`` the same way; unset, xAI fronts
them when a credential exists and the decide client backstops. ``confirm`` is Jev alone
(``TRADER_CONFIRM_*``), absent unless configured, and nothing backstops it. x_search sentiment
is not routed here — it stays xAI-only. Every completion served here is metered
into ``usage_daily`` under its role — there is no unmetered path.

The active Profile (#107) overrides this per Role and per call: one registry per process
asks the Profile store for each call's Chain, so a switch reaches the loop, the desk, the
compiler and the briefs on their next call. A Role the Profile leaves out uses the
``.env`` chain built here.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol

import httpx
import structlog

from trader.llm.base import Completion, LLMClient, LLMError
from trader.llm.jev import JEV_PROVIDER, JevClient
from trader.llm.nemotron import apply_thinking_guard, nemotron_client
from trader.llm.openai_compat import OpenAIChatClient, loads_extra_body
from trader.llm.usage import UsageMeter
from trader.llm.xai import xai_client
from trader.llm.xai_auth import XaiCredentialManager
from trader.ops.config import Settings

log = structlog.get_logger(__name__)

ROLES = ("decide", "brief", "compiler", "sentiment", "analyst", "critic", "confirm")
RESEARCH_ROLES = ("brief", "compiler", "sentiment", "critic")
#: Usage-meter provider for a configured OpenAI-compatible endpoint (not the Nemotron preset).
GENERIC_PROVIDER = "openai_compat"


class NoRoute(LLMError):
    """Nothing serves this Role: unconfigured, or switched off in the active Profile."""


class ProfileRoutes(Protocol):
    """What the registry needs from the Profile store (``llm.profiles.ProfileDesk``)."""

    async def route(self, role: str) -> tuple[int, list[LLMClient]] | None: ...


class LLMRegistry:
    """Ordered fallback chain per role, with usage metering on every completion."""

    def __init__(
        self,
        routes: dict[str, list[LLMClient]],
        meter: UsageMeter | None,
        *,
        profiles: ProfileRoutes | None = None,
    ) -> None:
        self._routes = routes
        self._meter = meter
        self._profiles = profiles

    def chain_for(self, role: str) -> list[LLMClient]:
        """The ``.env`` chain for ``role``."""
        chain = self._routes.get(role, [])
        if not chain:
            raise NoRoute(f"no LLM provider configured for role '{role}'")
        return chain

    async def _chain(self, role: str) -> tuple[int | None, list[LLMClient]]:
        routed = await self._profiles.route(role) if self._profiles is not None else None
        if routed is None:
            return None, self.chain_for(role)
        version, chain = routed
        if not chain:
            raise NoRoute(f"{role} is off in the active Profile")
        return version, chain

    async def complete(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> Completion:
        errors: list[str] = []
        last: LLMError | None = None
        version, chain = await self._chain(role)
        for client in chain:
            try:
                completion = await client.complete(
                    messages, schema=schema, max_tokens=max_tokens, temperature=temperature
                )
            except LLMError as exc:
                errors.append(str(exc))
                last = exc
                log.warning(
                    "llm_provider_failed",
                    role=role,
                    provider=client.name,
                    model=client.model,
                    error=str(exc),
                )
                continue
            if version is not None:
                completion = replace(completion, profile_version=version)
            if self._meter is not None:
                await self._meter.record(role, completion)
            return completion
        # Chained to the last provider's error so a caller can report that one cause.
        raise LLMError(f"all providers failed for role '{role}': {errors}") from last


def _same_endpoint(left: str, right: str) -> bool:
    return left.strip().rstrip("/") == right.strip().rstrip("/")


def _generic_client(
    *,
    base_url: str,
    model: str,
    api_key: str,
    extra_body: dict[str, Any],
    price_in: float,
    price_out: float,
    model_setting: str,
    client: httpx.AsyncClient | None,
    grammar_safe: bool,
) -> OpenAIChatClient:
    if not model:
        raise LLMError(f"{model_setting} is required when the base URL is set")
    return OpenAIChatClient(
        name=GENERIC_PROVIDER,
        model=model,
        base_url=base_url,
        api_key=api_key.strip() or None,
        extra_body=extra_body,
        client=client,
        price_in=price_in,
        price_out=price_out,
        grammar_safe=grammar_safe,
    )


def build_confirm_client(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> JevClient | None:
    """The confirm role on TypeSafe's Jev, or None when ``TRADER_CONFIRM_PROVIDER`` is empty."""
    if settings.confirm_provider != JEV_PROVIDER:
        return None
    key = settings.confirm_api_key.strip()
    if not key:
        raise LLMError("TRADER_CONFIRM_API_KEY is required when TRADER_CONFIRM_PROVIDER=jev")
    return JevClient(
        api_key=key,
        base_url=settings.confirm_base_url.strip(),
        model=settings.confirm_model.strip(),
        client=client,
    )


def build_decide_client(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> LLMClient:
    """The money-path client: a generic endpoint, else the Nemotron preset.

    A generic URL equal to ``TRADER_NEMOTRON_BASE_URL`` still gets the thinking
    guard and the untouched wire schema. Any other host gets neither: OpenAI rejects
    ``chat_template_kwargs``, and Ollama / llama.cpp reject a schema bound of 2000
    (see ``grammar_safe_schema``).
    """
    base_url = settings.decide_base_url.strip()
    if not base_url:
        return nemotron_client(settings, client=client)
    extra = loads_extra_body(settings.decide_extra_body)
    on_preset = _same_endpoint(base_url, settings.nemotron_base_url)
    if on_preset:
        extra = apply_thinking_guard(extra)
    return _generic_client(
        base_url=base_url,
        model=settings.decide_model.strip(),
        api_key=settings.decide_api_key,
        extra_body=extra,
        price_in=settings.decide_price_in,
        price_out=settings.decide_price_out,
        model_setting="TRADER_DECIDE_MODEL",
        client=client,
        grammar_safe=not on_preset,
    )


def build_research_client(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> OpenAIChatClient | None:
    """The research chat endpoint, or None to keep the xAI-then-decide chain.

    x_search is not this client. ``build_sentiment`` stays on xAI regardless.
    """
    base_url = settings.research_base_url.strip()
    if not base_url:
        return None
    extra = loads_extra_body(settings.research_extra_body)
    on_preset = _same_endpoint(base_url, settings.nemotron_base_url)
    if on_preset:
        extra = apply_thinking_guard(extra)
    return _generic_client(
        base_url=base_url,
        model=settings.research_model.strip(),
        api_key=settings.research_api_key,
        extra_body=extra,
        price_in=settings.research_price_in,
        price_out=settings.research_price_out,
        model_setting="TRADER_RESEARCH_MODEL",
        client=client,
        grammar_safe=not on_preset,
    )


def build_llm_registry(
    settings: Settings,
    meter: UsageMeter | None = None,
    *,
    xai_manager: XaiCredentialManager | None = None,
    profiles: ProfileRoutes | None = None,
) -> LLMRegistry:
    """Wire the chains this build can serve from configured credentials.

    ``profiles`` is the Profile store (#107); given, the active Profile overrides these
    chains per Role and per call. ``xai_manager`` is the process-wide OAuth credential
    manager (``create_app`` builds one and threads it through every registry); None falls
    back to building one per call, which store-less builds and tests rely on.

    Grok fronts research roles when any xAI credential path exists and
    ``TRADER_RESEARCH_BASE_URL`` is empty — SuperGrok OAuth (device-code login)
    or the first-class ``XAI_API_KEY`` fallback; consumers can't tell which one
    is serving. An explicit research endpoint replaces that chain (xAI is
    optional). Analysts follow the decide client: the Nemotron preset unless
    ``TRADER_DECIDE_BASE_URL`` is set (they are not pinned to local silicon, and
    they are not routed at xAI on their own).
    """
    decide = build_decide_client(settings)
    backstop: list[LLMClient] = [decide]
    # Analysts run on every wakeup. On the Nemotron preset that is free; a paid
    # decide endpoint bills them too. Desk stays dark unless TRADER_DESK_ENABLED.
    routes: dict[str, list[LLMClient]] = {"decide": [decide], "analyst": backstop}
    # Jev answers question sets only, so it is the confirm role and nothing else: never
    # a decide, analyst or research backstop.
    confirm = build_confirm_client(settings)
    if confirm is not None:
        routes["confirm"] = [confirm]
    research = build_research_client(settings)
    if research is not None:
        for role in RESEARCH_ROLES:
            routes[role] = [research]
        return LLMRegistry(routes, meter, profiles=profiles)
    grok = xai_client(settings, manager=xai_manager)
    # Research prefers Grok; the decide client backstops whether xAI is absent
    # or the decide role itself moved off the self-hosted vLLM (the Desk must
    # work before any Grok credential exists — see
    # docs/lessons.md#macro-view-advisory-firewall). The critic rides the same
    # ordering: Grok reviews the decide draft when both exist; otherwise the
    # critique is same-model, not skipped.
    for role in RESEARCH_ROLES:
        routes[role] = ([grok] if grok is not None else []) + backstop
    return LLMRegistry(routes, meter, profiles=profiles)
