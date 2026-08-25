"""Runtime configuration (pydantic-settings). Env-driven, validated at startup."""

from __future__ import annotations

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. See docs/architecture.md for the safety invariants."""

    model_config = SettingsConfigDict(env_prefix="TRADER_", env_file=".env", extra="ignore")

    # --- Surfaces / auth ---
    api_key: str = ""
    auth_disabled: bool = False  # explicit dev opt-out; without it, an unset api_key fails boot
    api_base_url: str = "http://localhost:8080"  # where the CLI finds the running API

    # --- Store ---
    database_url: str = "postgresql://trader:trader@localhost:5544/trader"

    # --- Safety gates (default: paper only, live impossible) ---
    alpaca_live_enabled: bool = False

    # --- Decide loop ---
    # Shadow mode defaults ON: the pipeline runs and journals, and submits nothing.
    shadow_mode: bool = True
    loop_enabled: bool = False  # the background task is an explicit operator opt-in
    loop_tick_seconds: float = 60.0
    loop_symbols: str = "BTC/USD,ETH/USD"
    # Paper account base until the broker lands (EAI-176).
    shadow_equity: float = 100_000.0

    # --- Risk limits (RiskGuard) — code/config only, never prompt-driven ---
    risk_max_position_frac: float = 0.10
    risk_min_notional_usd: float = 5.0
    risk_max_concurrent_positions: int = 5
    risk_max_trades_per_day: int = 20
    risk_daily_loss_limit_frac: float = 0.03
    risk_weekly_drawdown_limit_frac: float = 0.10
    risk_approval_notional_usd: float = 1000.0

    # --- Research Desk ---
    # Days before an unpinned note auto-stales out of compilation (retire stays explicit).
    research_stale_days: int = 30
    # Quiet window after the last note change before the Macro View recompiles.
    research_compile_debounce_seconds: float = 30.0
    # One-way markdown mirror folder (gitignored, regenerable). Empty string disables.
    research_mirror_dir: str = "research"
    # X sentiment (EAI-180). Polling needs an xAI credential; the wake trigger stays OFF
    # until ≥1 week of journaled scores earns the operator's flip (shadow-first).
    sentiment_poll_seconds: float = 3600.0
    sentiment_daily_call_cap: int = 24
    sentiment_trigger_enabled: bool = False
    sentiment_swing_threshold: float = 0.5
    sentiment_x_handles: str = ""  # comma-separated allowlist for x_search; empty = open

    # --- LLM endpoints ---
    nemotron_base_url: str = "http://localhost:8009/v1"
    nemotron_model: str = "nemotron-3-super"  # served model id on the Spark vLLM
    xai_base_url: str = "https://api.x.ai/v1"
    xai_model: str = "grok-4"
    xai_api_key: str = Field(
        default="", validation_alias=AliasChoices("XAI_API_KEY", "TRADER_XAI_API_KEY")
    )
    # SuperGrok OAuth: Fernet key + encrypted store path (TRADER_XAI_TOKEN_KEY unset →
    # OAuth disabled and the provider resolves to XAI_API_KEY alone).
    xai_token_key: str = ""
    xai_token_path: str = "~/.config/trader/xai_token.enc"

    # --- Market data / broker credentials ---
    # Provider-conventional env names (no TRADER_ prefix), as documented in .env.example.
    alpaca_api_key_id: str = Field(
        default="", validation_alias=AliasChoices("ALPACA_API_KEY_ID", "TRADER_ALPACA_API_KEY_ID")
    )
    alpaca_api_secret: str = Field(
        default="", validation_alias=AliasChoices("ALPACA_API_SECRET", "TRADER_ALPACA_API_SECRET")
    )
    twelve_data_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("TWELVE_DATA_API_KEY", "TRADER_TWELVE_DATA_API_KEY"),
    )
    fred_api_key: str = Field(
        default="", validation_alias=AliasChoices("FRED_API_KEY", "TRADER_FRED_API_KEY")
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
