"""Runtime configuration (pydantic-settings). Env-driven, validated at startup.

Every field carries its documentation here, as `Field(description=...)` plus a
category and section in `json_schema_extra`; `scripts/gen_config_reference.py`
renders that into `docs/configuration.md` and `.env.example` (#27). CI fails when
those files drift, so a new setting is documented by describing it in this file.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Categories: what flipping a field can do.
GATE = "gate"  # fail-closed safety switch; the default is the closed position
TUNING = "tuning"  # numeric or behavioral parameter; never opens a door by itself
ENDPOINT = "endpoint"  # where a dependency lives: URL, served model id, path
CREDENTIAL = "credential"  # secret; blank means that integration stays off
CATEGORIES = (GATE, TUNING, ENDPOINT, CREDENTIAL)

# Sections, in the order the reference and .env.example present them.
SECTIONS = (
    "Surfaces / auth",
    "Store",
    "Safety gates",
    "Decide loop",
    "Risk limits (RiskGuard)",
    "Research Desk",
    "LLM endpoints",
    "Broker / market data credentials",
)


def setting(default: Any, *, category: str, section: str, description: str, **kwargs: Any) -> Any:
    """A documented field: description + category + section travel with the default."""
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category!r}")
    if section not in SECTIONS:
        raise ValueError(f"unknown section {section!r}")
    return Field(
        default,
        description=description,
        json_schema_extra={"category": category, "section": section},
        **kwargs,
    )


class Settings(BaseSettings):
    """Process configuration. See docs/architecture.md for the safety invariants."""

    model_config = SettingsConfigDict(env_prefix="TRADER_", env_file=".env", extra="ignore")

    # --- Surfaces / auth ---
    api_key: str = setting(
        "",
        category=CREDENTIAL,
        section="Surfaces / auth",
        description=(
            "Bearer key the API and MCP surfaces require. Mandatory outside dev: an unset "
            "key with `TRADER_AUTH_DISABLED` unset refuses to boot."
        ),
    )
    capture_api_key: str = setting(
        "",
        category=CREDENTIAL,
        section="Surfaces / auth",
        description=(
            "Capture-scoped second key for external research agents (Hermes). It may only "
            "submit, list and retire research notes and read the Macro View: on REST those "
            "four routes (plus `/health`), on MCP the `/mcp-capture/` mount alone. Every other "
            "route and `/mcp/` answer 403. Blank means no capture key exists; it never "
            "substitutes for `TRADER_API_KEY`."
        ),
    )
    auth_disabled: bool = setting(
        False,
        category=GATE,
        section="Surfaces / auth",
        description=(
            "Explicit dev opt-out from API auth. Without it, an unset `TRADER_API_KEY` fails "
            "boot (fail closed). Never set on a reachable box."
        ),
    )
    api_base_url: str = setting(
        "http://localhost:8080",
        category=ENDPOINT,
        section="Surfaces / auth",
        description="Where the CLI finds the running API.",
    )

    # --- Store ---
    database_url: str = setting(
        "postgresql://trader:trader@localhost:5544/trader",
        category=ENDPOINT,
        section="Store",
        description=(
            "Postgres DSN for the journal, ledger, research store and audit log. The test "
            "suite appends `_test` to it and truncates that sibling database."
        ),
    )

    # --- Safety gates (default: paper only, live impossible) ---
    alpaca_live_enabled: bool = setting(
        False,
        category=GATE,
        section="Safety gates",
        description=(
            "Live Alpaca client construction refuses unless this is `true`. Paper only by "
            "default; there is no other path to a live venue."
        ),
    )

    # --- Decide loop ---
    shadow_mode: bool = setting(
        True,
        category=GATE,
        section="Decide loop",
        description=(
            "Shadow mode defaults ON: the pipeline runs and journals every decision, and "
            "submits nothing. Set `false` to let RiskGuard-approved intents reach the paper "
            "broker."
        ),
    )
    loop_enabled: bool = setting(
        False,
        category=GATE,
        section="Decide loop",
        description=(
            "Runs the background decide loop inside the API process. An explicit operator "
            "opt-in; off, the surfaces still serve but nothing ticks."
        ),
    )
    loop_tick_seconds: float = setting(
        60.0,
        category=TUNING,
        section="Decide loop",
        description="Seconds between scheduler ticks (quote fetch, mark, trigger evaluation).",
    )
    loop_max_heartbeats_per_tick: int = setting(
        0,
        category=TUNING,
        section="Decide loop",
        description=(
            "Cap on heartbeat wakeups per tick (0 = no cap). Set it when the watchlist is "
            "large: heartbeats beyond the cap wait for the next tick, most overdue first, so "
            "startup, each cadence and each market open do not burst the data API's rate "
            "limit. Price-move, vol-spike and sentiment triggers are never capped."
        ),
    )
    loop_symbols: str = setting(
        "BTC/USD,ETH/USD",
        category=TUNING,
        section="Decide loop",
        description="Comma-separated symbols the loop watches and may trade.",
    )
    shadow_symbols: str = setting(
        "",
        category=GATE,
        section="Decide loop",
        description=(
            "Per-symbol shadow: comma-separated tickers that decide and "
            "risk-check but never submit, while the rest of the build trades live-paper. A "
            "new symbol earns its flip here first."
        ),
    )
    exit_review_enabled: bool = setting(
        False,
        category=GATE,
        section="Decide loop",
        description=(
            "Exit-review lane: with a position held, the decide prompt gains a held-position "
            "block (entry, age, unrealized P&L, latest sentiment) and an explicit ask to "
            "justify holding versus `close`/reduce. Advisory only, RiskGuard never sees it. "
            "A decide-behavior change: flip only through `docs/runbooks/decide-change-gate.md` "
            "(replay it with `trader eval run --exit-review --seed-qty --seed-entry`)."
        ),
    )
    shadow_equity: float = setting(
        100_000.0,
        category=TUNING,
        section="Decide loop",
        description=(
            "Equity the risk chain assumes in shadow builds (no broker). Live-paper builds "
            "read equity from the broker and keep the last good value when it is down."
        ),
    )

    # --- Risk limits (RiskGuard) — code/config only, never prompt-driven ---
    risk_max_position_frac: float = setting(
        0.10,
        category=TUNING,
        section="Risk limits (RiskGuard)",
        description="Max notional of one position as a fraction of equity.",
    )
    risk_min_notional_usd: float = setting(
        5.0,
        category=TUNING,
        section="Risk limits (RiskGuard)",
        description="Opens below this notional are denied (no dust orders), clamps included.",
    )
    risk_max_concurrent_positions: int = setting(
        5,
        category=TUNING,
        section="Risk limits (RiskGuard)",
        description="Max open positions; a new open beyond it is denied, adds and exits pass.",
    )
    risk_max_trades_per_day: int = setting(
        20,
        category=TUNING,
        section="Risk limits (RiskGuard)",
        description="Max submitted trades per UTC day, counted across symbols.",
    )
    risk_daily_loss_limit_frac: float = setting(
        0.03,
        category=TUNING,
        section="Risk limits (RiskGuard)",
        description=(
            "Daily loss breaker: equity down this fraction from the session start halts "
            "opens until an operator resumes it."
        ),
    )
    risk_weekly_drawdown_limit_frac: float = setting(
        0.10,
        category=TUNING,
        section="Risk limits (RiskGuard)",
        description="Weekly drawdown breaker, same mechanics as the daily limit.",
    )
    risk_approval_notional_usd: float = setting(
        1000.0,
        category=TUNING,
        section="Risk limits (RiskGuard)",
        description=(
            "Unattended opens above this notional clamp down to it and execute; the clamp "
            "is journaled. Exits are never clamped."
        ),
    )
    risk_park_above_usd: float = setting(
        0.0,
        category=TUNING,
        section="Risk limits (RiskGuard)",
        description=(
            "`0` disables parking. A value above the approval cap parks larger opens for a "
            "human (`trader risk approve`); everything between the cap and this band clamps "
            "to the cap and executes unattended."
        ),
    )

    # --- Research Desk ---
    research_stale_days: int = setting(
        30,
        category=TUNING,
        section="Research Desk",
        description=(
            "Days before an unpinned note auto-stales out of compilation (retire stays explicit)."
        ),
    )
    research_compile_debounce_seconds: float = setting(
        30.0,
        category=TUNING,
        section="Research Desk",
        description="Quiet window after the last note change before the Macro View recompiles.",
    )
    research_mirror_dir: str = setting(
        "research",
        category=TUNING,
        section="Research Desk",
        description="One-way markdown mirror folder (gitignored, regenerable). Empty disables.",
    )
    briefs_enabled: bool = setting(
        False,
        category=GATE,
        section="Research Desk",
        description=(
            "Per-candidate briefs: briefs compile and store on note changes "
            "regardless; this flag alone lets them into the decide prompt. A decide-behavior "
            "change: flip only through `docs/runbooks/decide-change-gate.md`."
        ),
    )
    desk_enabled: bool = setting(
        False,
        category=GATE,
        section="Research Desk",
        description=(
            "Decision desk: analyst fan-out + cross-model critic around the decide. "
            "Dark by default; earns its flip via replay desk-on vs desk-off, then a shadow "
            "window."
        ),
    )
    desk_critic_daily_usd: float = setting(
        1.0,
        category=TUNING,
        section="Research Desk",
        description=(
            "Critic spend cap per UTC day (the critic prefers Grok, cross-model). At the cap, "
            "reviews degrade to journaled pass-throughs. Analysts run on the decide client."
        ),
    )
    sentiment_poll_seconds: float = setting(
        10_800.0,
        category=TUNING,
        section="Research Desk",
        description=(
            "X sentiment poll interval for held symbols; needs an xAI credential "
            "(x_search runs server-side on xAI). The daily call cap still bounds a shorter "
            "interval."
        ),
    )
    sentiment_daily_call_cap: int = setting(
        24,
        category=TUNING,
        section="Research Desk",
        description="Max sentiment polls per UTC day across symbols.",
    )
    sentiment_trigger_enabled: bool = setting(
        False,
        category=GATE,
        section="Research Desk",
        description=(
            "Lets a sentiment swing wake the decide loop. Shadow-first: leave OFF until at "
            "least a week of journaled scores looks sane. Code default off; a live book arms "
            "it in `.env`, never here."
        ),
    )
    sentiment_swing_threshold: float = setting(
        0.5,
        category=TUNING,
        section="Research Desk",
        description="Score move between polls that wakes the trader; scores are on [-1, 1].",
    )
    sentiment_x_handles: str = setting(
        "",
        category=TUNING,
        section="Research Desk",
        description="Comma-separated allowlist of X handles for x_search; empty = open search.",
    )
    screener_min_dollar_volume_usd: float = setting(
        50_000_000.0,
        category=TUNING,
        section="Research Desk",
        description=(
            "Screener liquidity floor: symbols whose 20-day average dollar volume "
            "(close x volume) is below this are skipped before ranking. The screener is a "
            "query (`GET /screener`, `get_screener`, `trader screener run`); it is not wired "
            "into the decide loop."
        ),
    )
    screener_lookback_days: int = setting(
        90,
        category=TUNING,
        section="Research Desk",
        description=(
            "Daily bars the screener fetches per symbol (one or two multi-symbol Alpaca "
            "requests per query, nothing stored). Must cover the 60-day momentum window: "
            "at least 61 trading days; 90 leaves slack for holidays."
        ),
        ge=61,
    )
    seat_model: str = setting(
        "grok-4.3",
        category=ENDPOINT,
        section="Research Desk",
        description=(
            "Grok model for the desk seats (`trader seat run <seat>`, a standalone process "
            "outside `trader-api`). Must be entitled on `/v1/responses` with `x_search` and "
            "`web_search` (probe matrix: docs/research/xai-api.md). The seat authenticates "
            "with the shared SuperGrok OAuth login by default (cross-process refresh lock, #68); "
            "`--credential api_key` pins `XAI_API_KEY` instead."
        ),
    )
    seat_max_notes: int = setting(
        3,
        category=TUNING,
        section="Research Desk",
        description=(
            "Hard cap on notes one seat sweep may submit after the grounding and dedupe "
            "gates; the rest are dropped and logged."
        ),
        ge=1,
    )
    seat_note_max_chars: int = setting(
        1400,
        category=TUNING,
        section="Research Desk",
        description=(
            "Seat note body cap. A longer body is sent back to the model to distill once; "
            "still over → dropped. Keeps notes inside the decide prompt's research lane."
        ),
        ge=200,
    )
    seat_state_path: str = setting(
        "~/.local/state/trader/seat-runs.jsonl",
        category=TUNING,
        section="Research Desk",
        description=(
            "Append-only JSONL of seat runs (submitted note ids, usage, estimated cost). The "
            "seat runs outside the service meter, so this is its spend record; `trader seat "
            "report` reads it back for the citation loop."
        ),
    )

    # --- LLM endpoints ---
    nemotron_base_url: str = setting(
        "http://localhost:8009/v1",
        category=ENDPOINT,
        section="LLM endpoints",
        description=(
            "Nemotron preset: a self-hosted vLLM `/v1` (default :8009, no auth). "
            "Used for decide and analysts when `TRADER_DECIDE_BASE_URL` is empty; the preset "
            "client sends `chat_template_kwargs: {enable_thinking: false}` on every request "
            "(without it the endpoint collapses to ~0.35 tok/s). A generic URL equal to this "
            "one gets the same flag forced on. Inside compose, point it at "
            "`http://host.docker.internal:8009/v1`."
        ),
    )
    nemotron_model: str = setting(
        "nemotron-3-super",
        category=ENDPOINT,
        section="LLM endpoints",
        description=(
            "Served model id on the Nemotron vLLM. Swapping the decide model on an existing "
            "box is a decide-behavior change (`docs/runbooks/decide-change-gate.md`); for a "
            "new user it is simply the first run."
        ),
    )
    decide_base_url: str = setting(
        "",
        category=ENDPOINT,
        section="LLM endpoints",
        description=(
            "Any OpenAI-compatible `/v1` for the decide (and analyst) role: OpenAI, Groq, "
            "Together, Ollama, vLLM, xAI. Empty selects the Nemotron preset above, which is "
            "the only client that sends `chat_template_kwargs`; a vendor API such as OpenAI "
            "rejects that argument, so it is never sent to a generic URL."
        ),
    )
    decide_model: str = setting(
        "",
        category=ENDPOINT,
        section="LLM endpoints",
        description="Model id at `TRADER_DECIDE_BASE_URL` (ignored under the Nemotron preset).",
    )
    decide_api_key: str = setting(
        "",
        category=CREDENTIAL,
        section="LLM endpoints",
        description="Bearer key for `TRADER_DECIDE_BASE_URL`; blank for unauthenticated hosts.",
    )
    decide_extra_body: str = setting(
        "",
        category=TUNING,
        section="LLM endpoints",
        description=(
            "JSON object merged into every chat-completions body for the decide client "
            '(e.g. `{"reasoning_effort": "low"}`). Must parse as an object. Ollama / '
            "llama.cpp ignore `chat_template_kwargs`; their no-thinking switch is "
            '`{"reasoning_effort": "none"}`. Without it a reasoning model burns the '
            "decide token budget thinking and returns empty content."
        ),
    )
    decide_price_in: float = setting(
        0.0,
        category=TUNING,
        section="LLM endpoints",
        description=(
            "USD per 1M input tokens for the decide model. `0` reads the built-in PRICES "
            "table, else $0 (logged once); non-zero overrides the table."
        ),
    )
    decide_price_out: float = setting(
        0.0,
        category=TUNING,
        section="LLM endpoints",
        description="USD per 1M output tokens for the decide model; same rules as the input price.",
    )
    research_base_url: str = setting(
        "",
        category=ENDPOINT,
        section="LLM endpoints",
        description=(
            "Same shape as the decide endpoint, for the brief, compiler, critic and registry "
            "sentiment roles. Empty keeps xAI if configured, else the decide client. x_search "
            "stays xAI-only."
        ),
    )
    research_model: str = setting(
        "",
        category=ENDPOINT,
        section="LLM endpoints",
        description="Model id at `TRADER_RESEARCH_BASE_URL`.",
    )
    research_api_key: str = setting(
        "",
        category=CREDENTIAL,
        section="LLM endpoints",
        description="Bearer key for `TRADER_RESEARCH_BASE_URL`.",
    )
    research_extra_body: str = setting(
        "",
        category=TUNING,
        section="LLM endpoints",
        description=(
            "JSON object merged into the research roles' chat-completions bodies. On "
            'Ollama / llama.cpp use `{"reasoning_effort": "none"}` to turn thinking off '
            "(`chat_template_kwargs` is ignored there); otherwise a reasoning model spends "
            "the token budget thinking and returns empty content."
        ),
    )
    research_price_in: float = setting(
        0.0,
        category=TUNING,
        section="LLM endpoints",
        description="USD per 1M input tokens for the research model (`0` = PRICES table, else $0).",
    )
    research_price_out: float = setting(
        0.0,
        category=TUNING,
        section="LLM endpoints",
        description="USD per 1M output tokens for the research model.",
    )
    xai_base_url: str = setting(
        "https://api.x.ai/v1",
        category=ENDPOINT,
        section="LLM endpoints",
        description="xAI API base for Grok research roles and x_search sentiment.",
    )
    xai_model: str = setting(
        "grok-4",
        category=ENDPOINT,
        section="LLM endpoints",
        description="Grok model id for the xAI research roles.",
    )
    xai_api_key: str = setting(
        "",
        category=CREDENTIAL,
        section="LLM endpoints",
        description=(
            "xAI API key: first-class fallback when SuperGrok OAuth is absent or quarantined. "
            "Provider-conventional name; the `TRADER_` form is accepted too."
        ),
        validation_alias=AliasChoices("XAI_API_KEY", "TRADER_XAI_API_KEY"),
    )
    xai_token_key: str = setting(
        "",
        category=CREDENTIAL,
        section="LLM endpoints",
        description=(
            "SuperGrok device-code OAuth: Fernet key that encrypts the token store. Set it, "
            'then run `trader xai login`. Generate with `python -c "from cryptography.fernet '
            'import Fernet; print(Fernet.generate_key().decode())"`. Unset, OAuth is '
            "disabled and the provider resolves to `XAI_API_KEY` alone."
        ),
    )
    xai_token_path: str = setting(
        "~/.config/trader/xai_token.enc",
        category=TUNING,
        section="LLM endpoints",
        description="Encrypted SuperGrok OAuth token store path.",
    )

    @field_validator("decide_extra_body", "research_extra_body")
    @classmethod
    def extra_body_is_json_object(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("must be a JSON object") from exc
        if not isinstance(parsed, dict):
            raise ValueError("must be a JSON object")
        return text

    # --- Market data / broker credentials ---
    # Provider-conventional env names (no TRADER_ prefix); the TRADER_ form is an alias.
    alpaca_api_key_id: str = setting(
        "",
        category=CREDENTIAL,
        section="Broker / market data credentials",
        description=(
            "Alpaca paper key id. The same keys serve broker and market data. Rate-limit "
            "budget (Basic/free tier): ~200 req/min across data endpoints; crypto data free; "
            "equities = IEX feed only. Fetch-on-tick load is ~2 req/wakeup/symbol, far below "
            "budget."
        ),
        validation_alias=AliasChoices("ALPACA_API_KEY_ID", "TRADER_ALPACA_API_KEY_ID"),
    )
    alpaca_api_secret: str = setting(
        "",
        category=CREDENTIAL,
        section="Broker / market data credentials",
        description="Alpaca paper secret, paired with `ALPACA_API_KEY_ID`.",
        validation_alias=AliasChoices("ALPACA_API_SECRET", "TRADER_ALPACA_API_SECRET"),
    )
    twelve_data_api_key: str = setting(
        "",
        category=CREDENTIAL,
        section="Broker / market data credentials",
        description=(
            "Twelve Data key, equities-quote fallback only (untouched unless Alpaca equities "
            "data fails). Free tier: 8 credits/min, 800/day, 1 credit per quote or "
            "time_series call."
        ),
        validation_alias=AliasChoices("TWELVE_DATA_API_KEY", "TRADER_TWELVE_DATA_API_KEY"),
    )
    fred_api_key: str = setting(
        "",
        category=CREDENTIAL,
        section="Broker / market data credentials",
        description=(
            "FRED key (https://fred.stlouisfed.org/docs/api/api_key.html) for the macro "
            "context line; responses are TTL-cached 1h in-process, so live load is a few "
            "calls/day. Absent, the decide prompt carries no FRED line."
        ),
        validation_alias=AliasChoices("FRED_API_KEY", "TRADER_FRED_API_KEY"),
    )

    def require_auth(self) -> None:
        """Fail closed: a server with no API key and no explicit dev opt-out must not boot."""
        if not self.api_key and not self.auth_disabled:
            raise RuntimeError(
                "TRADER_API_KEY is unset. Set it, or set TRADER_AUTH_DISABLED=true for local dev."
            )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
