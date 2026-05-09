"""Application configuration via environment variables."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to the project root (3 levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    """Central configuration loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- App ---
    app_name: str = "Conduit"
    app_env: str = "development"
    debug: bool = False
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # --- Database ---
    database_url: str = "postgresql+asyncpg://conduit:conduit@localhost:5432/conduit"

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"

    # --- LND ---
    lnd_host: str = "localhost"
    lnd_grpc_port: int = 10009
    lnd_rest_port: int = 8080
    lnd_tls_cert_path: Path = Field(default=Path("~/.lnd/tls.cert"))
    lnd_macaroon_path: Path = Field(
        default=Path("~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon")
    )
    lnd_network: str = "mainnet"

    # --- L402 ---
    l402_secret_key: str = "change-me-to-a-random-secret"
    l402_token_expiry_seconds: int = 3600

    # --- Fees ---
    transaction_fee_percent: float = 1.5

    # --- API Key Auth ---
    # Required to start the MCP server. Reject if missing or default.
    conduit_api_key: str = "CHANGE-ME"

    # --- Nostr ---
    # Private key for signing Nostr events (nsec or hex). Auto-generated if empty.
    nostr_private_key: str = ""
    # Comma-separated relay URLs for publishing/discovering skills
    nostr_relays: str = "wss://relay.damus.io,wss://relay.nostr.band,wss://nos.lol"
    # How far back to search for skills on relays (hours)
    nostr_discovery_window_hours: int = 168  # 7 days

    # --- Spending Limits ---
    # Maximum sats for a single outgoing payment (0 = no limit)
    spending_limit_per_payment_sats: int = 10000
    # Maximum total sats spent per hour (0 = no limit)
    spending_limit_hourly_sats: int = 50000
    # Maximum total sats spent per 24h rolling window (0 = no limit)
    spending_limit_daily_sats: int = 200000
    # Payments above this threshold require explicit confirmation
    spending_confirm_above_sats: int = 5000

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def nostr_relay_list(self) -> list[str]:
        """Parse comma-separated relay URLs into a list."""
        return [r.strip() for r in self.nostr_relays.split(",") if r.strip()]


# Singleton instance
settings = Settings()
