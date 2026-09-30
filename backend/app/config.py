"""Environment-driven configuration for hunchfall.

Every external endpoint and numeric threshold comes from env vars (with
sane defaults here) — nothing is hardcoded inside the clients.
``DATABASE_PATH`` and ``CORS_ORIGINS`` are workspace-relative / simple
strings; the runtime resolves them.

Usage::

    from app.config import get_settings
    settings = get_settings()
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All knobs for the data layer and the trading loop."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # ---- Polymarket public APIs (read-only market data) ---------------------
    POLYMARKET_GAMMA_URL: str = "https://gamma-api.polymarket.com"
    POLYMARKET_CLOB_URL: str = "https://clob.polymarket.com"
    POLYMARKET_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    POLYMARKET_DATA_API_URL: str = "https://data-api.polymarket.com"
    GDELT_DOC_URL: str = "https://api.gdeltproject.org/api/v2/doc/doc"

    # ---- Jev decision model (TypeSafe hosted API) ---------------------------
    # Live endpoint: POST {JEV_API_URL}/v1/systemone with Bearer JEV_API_KEY.
    # JEV_MODEL is pinned to a versioned ID — never "jev-latest".
    JEV_API_URL: str = "https://api.typesafe.ai"
    JEV_API_KEY: str = ""
    JEV_MODEL: str = "jev-1.13.0"
    JEV_MOCK: bool = True  # True => deterministic seeded mock, no network

    # ---- Strategy gates ------------------------------------------------------
    EDGE_THRESHOLD: float = 0.10  # min fee-adjusted edge to act (0.10 = 10%)
    KELLY_FRACTION: float = 0.25  # quarter-Kelly multiplier on f*
    MAX_PER_MARKET_FRAC: float = 0.10  # per-market cap, fraction of bankroll
    MAX_TOTAL_EXPOSURE_FRAC: float = 0.40  # total exposure cap, fraction
    MAX_CONCURRENT_POSITIONS: int = 5  # veto beyond this many open positions
    KILL_DRAWDOWN_PCT: float = 15.0  # auto kill-switch at this paper drawdown %
    SPREAD_VETO_CENTS: float = 0.03  # veto if bid-ask spread > this (USD cents)
    MIN_TOP_DEPTH_USD: float = 50.0  # veto if top-of-book depth < this (USD)
    MIN_HOURS_TO_RESOLUTION: float = 2.0  # veto if market resolves sooner
    # Veto if the news snapshot is older than the price snapshot by more
    # than this (seconds): we never trade on news that is stale relative
    # to the book.
    MAX_NEWS_PRICE_SKEW_SEC: int = 900
    # NegRisk complementary-token prices must sum to ~1; outside this band
    # the book is crossed/broken -> veto.
    NEGRISK_SUM_MIN: float = 0.98
    NEGRISK_SUM_MAX: float = 1.02
    JEV_CONFIDENCE_MIN: float = 0.6  # veto if Jev choice confidence < this

    # ---- Auth0 (dashboard login; empty = auth disabled) ---------------------
    # Fill from the Auth0 dashboard when wiring login. The API will validate
    # the Auth0-issued JWT; the SPA uses the VITE_AUTH0_* frontend variables.
    AUTH0_DOMAIN: str = ""
    AUTH0_CLIENT_ID: str = ""
    AUTH0_CLIENT_SECRET: str = ""
    AUTH0_AUDIENCE: str = ""
    # Normally https://<AUTH0_DOMAIN>/; set explicitly for custom domains.
    AUTH0_ISSUER: str = ""

    # ---- Paper portfolio -----------------------------------------------------
    PAPER_BANKROLL_USD: float = 10000.0
    # Taker fee rate override (per-market payload authoritative when present;
    # see loop.py TODO on unverified payload field names). Default falls back
    # to category rates in app/execution/paper.py FEE_RATES.
    FEE_RATE_OVERRIDE: float = 0.0  # 0 = disabled, use category rates

    # ---- Story sources ---------------------------------------------------------
    REDDIT_CLIENT_ID: str = ""
    REDDIT_CLIENT_SECRET: str = ""
    REDDIT_USER_AGENT: str = ""
    GDELT_ENABLED: bool = True
    TWITTER_ENABLED: bool = False  # always False — X is excluded by design

    # ---- Runtime ---------------------------------------------------------------
    LOOP_INTERVAL_SEC: int = 300
    DATABASE_PATH: str = Field(default="data/hunchfall.db")
    API_HOST: str = "127.0.0.1"
    API_PORT: int = 8000
    CORS_ORIGINS: str = "http://localhost:5173"

    # Fields that must never be exposed via GET /config (secrets/keys).
    _SECRET_FIELDS: tuple = (
        "JEV_API_KEY",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
    )

    def safe_subset(self) -> dict:
        """Return config for GET /config with secrets stripped out.

        Only non-secret operational values (URLs, thresholds, flags) are
        included — never API keys, client secrets, or bearer tokens.
        """
        return {
            name: getattr(self, name)
            for name in self.model_fields
            if name not in self._SECRET_FIELDS
        }


@lru_cache
def get_settings() -> Settings:
    """Cached Settings singleton (reads env once per process)."""
    return Settings()
