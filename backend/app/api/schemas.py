"""Request models for the additive RFC-001 endpoints.

Responses are plain dicts (consistent with the rest of this API); only the
request bodies are validated here. ``extra="forbid"`` makes typos fail
loudly with 422 instead of being silently ignored.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class ScanRequest(BaseModel):
    """``POST /extension/scan`` body — an untrusted page hint."""

    model_config = ConfigDict(extra="forbid")

    marketplace: str = Field(
        default="polymarket", pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$"
    )
    kind: Literal["event", "market"] = "event"
    slug: str = Field(
        min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._~-]+$"
    )
    url: str | None = Field(default=None, max_length=1024)
    #: Raw page state from the extension. Never trusted: hashed + redacted,
    #: its prices are ignored, and only a sanitized title may reach the model.
    page_state: dict[str, Any] | None = None
    scanned_at: str | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        """Accept only http(s) URLs."""
        if value is None:
            return None
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return value


class ValidateRequest(BaseModel):
    """``POST /signals/{id}/validate`` body."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["HIT", "MISS"]


class PredictRequest(BaseModel):
    """``POST /predict`` body — **exactly one** market identifier.

    Prediction only: the guesser takes a market and returns P(YES) or an
    honest abstention. It never accepts secrets, wallet addresses, or sizing.
    """

    model_config = ConfigDict(extra="forbid")

    market_slug: str | None = Field(
        default=None, min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._~-]+$"
    )
    condition_id: str | None = Field(
        default=None, pattern=r"^0x[0-9a-fA-F]{64}$"
    )

    @model_validator(mode="after")
    def _exactly_one_identifier(self) -> "PredictRequest":
        """Require exactly one of market_slug / condition_id."""
        provided = [
            value for value in (self.market_slug, self.condition_id) if value
        ]
        if len(provided) != 1:
            raise ValueError(
                "provide exactly one of market_slug or condition_id"
            )
        return self


class ResolveRequest(BaseModel):
    """``POST /predict/{prediction_id}/resolve`` body."""

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["YES", "NO"]


class WalletCreateRequest(BaseModel):
    """``POST /wallets`` body — watch-only address + label.

    Deliberately not pattern-validated here: the handler screens for
    secrets first so a submitted private key/mnemonic gets the specific
    ``secret_rejected`` 400 envelope instead of a generic 422.
    """

    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=1, max_length=256)
    label: str = Field(min_length=1, max_length=256)
