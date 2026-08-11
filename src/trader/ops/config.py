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
