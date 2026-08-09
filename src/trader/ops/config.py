"""Runtime configuration (pydantic-settings). Env-driven, validated at startup."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process configuration. See docs/architecture.md for the safety invariants."""

    model_config = SettingsConfigDict(env_prefix="TRADER_", env_file=".env", extra="ignore")

    # --- Surfaces / auth ---
    api_key: str = ""
    auth_disabled: bool = False  # explicit dev opt-out; without it, an unset api_key fails boot

    # --- Store ---
    database_url: str = "postgresql://trader:trader@localhost:5544/trader"

    # --- Safety gates (default: paper only, live impossible) ---
    alpaca_live_enabled: bool = False

    # --- LLM endpoints ---
    nemotron_base_url: str = "http://localhost:8009/v1"

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
