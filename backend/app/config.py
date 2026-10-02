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
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/.env — documented in README/`.env.example` and gitignored. Anchored to
# the module so the file is found no matter the process CWD; real OS env vars
# still take precedence over it.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """All knobs for the data layer and the trading loop."""

    model_config = SettingsConfigDict(
        env_prefix="", extra="ignore", env_file=_ENV_FILE
    )

    # ---- Polymarket public APIs (read-only market data) ---------------------
    POLYMARKET_GAMMA_URL: str = "https://gamma-api.polymarket.com"
    POLYMARKET_CLOB_URL: str = "https://clob.polymarket.com"
    POLYMARKET_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    POLYMARKET_DATA_API_URL: str = "https://data-api.polymarket.com"
    # Optional loopback shim for networks that block direct Polymarket
    # egress (see app/polymarket/shim.py and scripts/live_shim.py). Empty =
    # disabled. When set AND ON_BLOCKED is true, a connection-level direct
    # failure (ConnectError / ConnectTimeout only) is replayed once through
    # the shim; HTTP errors and read timeouts never fall back.
    POLYMARKET_SHIM_URL: str = ""
    POLYMARKET_SHIM_ON_BLOCKED: bool = True
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

    # ---- Watch-only wallets (read-only, keyless) ------------------------------
    # Public Polygon JSON-RPC endpoint. ONLY eth_getBalance and
    # eth_getTransactionCount are ever called — no signing, no key material.
    POLYGON_RPC_URL: str = "https://polygon-rpc.com"
    WALLET_ACTIVITY_LIMIT: int = 50  # items fetched per source
    WALLET_ACTIVITY_TTL_SEC: int = 60  # in-process activity cache TTL (0 = off)
    WALLET_LABEL_MAX_CHARS: int = 64

    # ---- Extension scan (prediction only) -------------------------------------
    # Reject a scan whose client clock drifts from the server by more than
    # this (seconds); server time is always the source of truth.
    SCAN_CLOCK_SKEW_SEC: int = 300
    # Hard cap on the untrusted page_state payload size (bytes). It is hashed
    # and redacted, never stored raw, and never trusted for prices.
    SCAN_PAGE_STATE_MAX_BYTES: int = 4096

    # ---- Market guesser (prediction only; RFC-003) -----------------------------
    # Absolute |edge| below which the guesser ABSTAINS instead of guessing.
    PREDICT_ABSTAIN_EDGE: float = 0.10
    # Ensemble size: the same model is asked N times; the median P(YES) wins.
    PREDICT_ENSEMBLE_N: int = 3
    # `/v2/trades` page size for the predictor tape.
    PREDICT_TAPE_LIMIT: int = 100
    # Book depth levels per side used for depth/imbalance features.
    PREDICT_BOOK_LEVELS: int = 5
    # Demo market + whether the demo may hit live upstreams at all.
    PREDICT_DEMO_SLUG: str = "will-bitcoin-hit-100k-in-2026"
    PREDICT_DEMO_LIVE: bool = True

    # ---- Social-media analyzer (social-outcome markets; RFC-003) ---------------
    # The analyzer's numbers are a FEATURE, never the decision.
    SOCIAL_ENABLED: bool = True
    # One shared deadline across sources; a slow source is dropped, not awaited.
    SOCIAL_DEADLINE_SEC: float = 4.0
    # Keyless Bluesky Jetstream v2 websocket (live-verified 2026-09-30).
    # NOTE: the RFC's ".../xrpc/network.bsky.jetstream.subscribeEvents" suffix
    # 404s on this instance; the working live path is the plain /subscribe.
    SOCIAL_JETSTREAM_URL: str = "wss://jetstream2.us-east.bsky.network/subscribe"
    # Bounded connect -> count -> close window (no long-lived subscription).
    SOCIAL_JETSTREAM_WINDOW_SEC: float = 3.0
    SOCIAL_MAX_EVENTS: int = 2000
    # Keyless Reddit JSON is best-effort: it returned 403 in the 2026-09-30
    # live-check, so a refusal degrades to `social.missing` and never blocks.
    SOCIAL_REDDIT_ENABLED: bool = True
    SOCIAL_RSS_MAX_ITEMS: int = 10

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
