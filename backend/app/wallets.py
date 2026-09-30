"""Watch-only wallet registry, secret screening, and read-only activity.

PAPER TRADING ONLY. This module never requests, stores, or returns private
keys, mnemonics, or signing material. The registry accepts watch-only EVM
addresses; anything resembling a secret is rejected with 400 and the
submitted value is never echoed back or written to the audit log.

Activity assembly is read-only: Polymarket Data API v2 (public, verified
params) plus a public Polygon JSON-RPC endpoint (``eth_getBalance`` /
``eth_getTransactionCount``). Partial failures degrade gracefully and are
reported in ``degraded``; when every source fails the caller answers 502 —
nothing is ever fabricated.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.polymarket.data_api import DataApiClient
from app.polygon.rpc import PolygonRpcClient

SECRET_REJECT_MESSAGE = (
    "watch-only addresses only — private keys, mnemonics, and signing "
    "material are never accepted or stored"
)

_HEX64_WHOLE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")
_HEX64_TOKEN = re.compile(r"(?:^|\s)(0x)?[0-9a-fA-F]{64}(?=\s|$)")
_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_KEYWORD_RE = re.compile(
    r"(xprv|xpub|mnemonic|seed\s*phrase|private\s*key|privatekey|keystore|-----begin)",
    re.IGNORECASE,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MNEMONIC_MIN_WORDS = 12


class InvalidAddress(ValueError):
    """Raised when a string is not a 0x-prefixed 40-hex EVM address."""


def screen_for_secrets(text: str) -> bool:
    """True when a value resembles key material / a mnemonic.

    Conservative by design — the wallet registry is watch-only, so a false
    positive only asks the user for a different label while a false
    negative would accept a secret.

    Args:
        text: Raw address or label value.

    Returns:
        True when the value looks like a secret and must be rejected.
    """
    raw = str(text or "")
    if not raw.strip():
        return False
    if _CONTROL_RE.search(raw):
        return True
    stripped = raw.strip()
    if _HEX64_WHOLE.match(stripped):
        return True
    if _HEX64_TOKEN.search(stripped):
        return True
    if _KEYWORD_RE.search(raw):
        return True
    words = stripped.split()
    return len(words) >= _MNEMONIC_MIN_WORDS and all(w.isalpha() for w in words)


def normalize_address(raw: str) -> str:
    """Validate and lowercase a watch-only EVM address.

    The EIP-55 checksum is intentionally not verified (no keccak
    dependency); addresses are stored lowercase.

    Args:
        raw: Candidate address.

    Returns:
        Lowercase 0x-prefixed 40-hex address.

    Raises:
        InvalidAddress: when the value is not a valid address shape.
    """
    value = str(raw or "").strip()
    if not _ADDRESS_RE.match(value):
        raise InvalidAddress(value)
    return value.lower()


class WalletRegistry:
    """Watch-only address registry backed by the shared SQLite file.

    This is the one mutable table in the database (a registry by nature);
    it contains no secret columns and never will.
    """

    def __init__(self, db_path: str) -> None:
        """Create the database/table if needed.

        Args:
            db_path: SQLite database path (same file as the audit log).
        """
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS watch_wallets(
                       address    TEXT PRIMARY KEY COLLATE NOCASE,
                       label      TEXT NOT NULL,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL)"""
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def get(self, address: str) -> dict | None:
        """Fetch one registered wallet (case-insensitive).

        Args:
            address: Watch-only address.

        Returns:
            ``{address, label, watch_only, created_at, updated_at}`` or None.
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT address, label, created_at, updated_at "
                "FROM watch_wallets WHERE address = ? COLLATE NOCASE",
                (str(address),),
            ).fetchone()
        if row is None:
            return None
        return {
            "address": row["address"],
            "label": row["label"],
            "watch_only": True,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def upsert(self, address: str, label: str) -> tuple[dict, bool]:
        """Insert or update a watch-only registration.

        Args:
            address: Normalized (lowercase) address.
            label: Human label.

        Returns:
            ``(row, created)`` — created is True on first registration.
        """
        addr = str(address).lower()
        now = self._now()
        existing = self.get(addr)
        with sqlite3.connect(self.db_path) as conn:
            if existing is not None:
                conn.execute(
                    "UPDATE watch_wallets SET label = ?, updated_at = ? "
                    "WHERE address = ? COLLATE NOCASE",
                    (label, now, addr),
                )
                return {**existing, "label": label, "updated_at": now}, False
            conn.execute(
                "INSERT INTO watch_wallets(address, label, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (addr, label, now, now),
            )
        return {
            "address": addr,
            "label": label,
            "watch_only": True,
            "created_at": now,
            "updated_at": now,
        }, True


# ---------------------------------------------------------------- activity --
_ACTIVITY_CACHE: dict[str, tuple[float, dict]] = {}
_ACTIVITY_CACHE_LOCK = threading.Lock()


def clear_activity_cache() -> None:
    """Drop the in-process activity cache (test helper)."""
    with _ACTIVITY_CACHE_LOCK:
        _ACTIVITY_CACHE.clear()


def _cache_get(address: str, ttl: float) -> dict | None:
    if ttl <= 0:
        return None
    now = time.monotonic()
    with _ACTIVITY_CACHE_LOCK:
        hit = _ACTIVITY_CACHE.get(address)
    if hit is None:
        return None
    stored_at, payload = hit
    if now - stored_at > ttl:
        return None
    return dict(payload)


def _cache_put(address: str, payload: dict) -> None:
    with _ACTIVITY_CACHE_LOCK:
        _ACTIVITY_CACHE[address] = (time.monotonic(), dict(payload))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_iso(value: Any) -> str | None:
    """Normalize an epoch-seconds/millis or ISO timestamp to ISO-8601 UTC."""
    if value is None:
        return None
    numeric = None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is not None:
        ts = numeric / 1000.0 if numeric > 1e12 else numeric
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    return text or None


def _first(item: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        value = item.get(key)
        if value is not None:
            return value
    return default


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_activity(items: Any) -> list[dict]:
    """Normalize Data API v2 activity items (defensive key handling)."""
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        side = _first(item, ("side", "takerSide"))
        out.append(
            {
                "source": "polymarket-data-api-v2",
                "type": str(_first(item, ("type", "activityType", "activity_type"), "ACTIVITY")).upper(),
                "ts": _to_iso(_first(item, ("timestamp", "ts", "time", "created_at", "createdAt"))),
                "market": _first(
                    item,
                    ("title", "question", "market", "marketQuestion", "slug", "condition_id", "conditionId", "condition"),
                ),
                "side": str(side).upper() if side is not None else None,
                "outcome": _first(item, ("outcome", "asset")),
                "size_usd": _num(
                    _first(item, ("usdc_size", "usdcSize", "sizeUsd", "size", "amount"))
                ),
                "price": _num(_first(item, ("price",))),
                "tx_hash": _first(
                    item,
                    ("transaction_hash", "transactionHash", "txHash", "tx_hash", "hash"),
                ),
            }
        )
    return out


def _normalize_positions(items: Any) -> list[dict]:
    """Normalize Data API v2 position items (defensive key handling)."""
    out: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "source": "polymarket-data-api-v2",
                "market": _first(item, ("title", "question", "market", "slug", "condition_id", "conditionId", "condition")),
                "outcome": _first(item, ("outcome", "asset")),
                "size": _num(_first(item, ("current_size", "size", "shares"))),
                "avg_price": _num(_first(item, ("avgPrice", "averagePrice", "avg_price"))),
                "current_price": _num(_first(item, ("curPrice", "currentPrice", "current_price", "price"))),
                "pnl": _num(
                    _first(
                        item,
                        (
                            "realized_pnl",
                            "total_pnl",
                            "unrealized_pnl",
                            "cashPnl",
                            "cash_pnl",
                            "pnl",
                            "realizedPnl",
                        ),
                    )
                ),
            }
        )
    return out


def collect_activity(
    settings: Settings,
    wallet: dict,
    *,
    data_api: Any = None,
    rpc: Any = None,
) -> dict:
    """Assemble read-only wallet activity from Data API v2 + Polygon RPC.

    Args:
        settings: App settings (URLs, limits, cache TTL).
        wallet: Registry row (``address``, ``label``).
        data_api: Optional DataApiClient-like override (tests).
        rpc: Optional PolygonRpcClient-like override (tests).

    Returns:
        Response payload including an internal ``ok`` flag (True when at
        least one source succeeded).
    """
    address = str(wallet.get("address") or "")
    ttl = float(getattr(settings, "WALLET_ACTIVITY_TTL_SEC", 0) or 0)
    cached = _cache_get(address, ttl)
    if cached is not None:
        return cached

    limit = int(getattr(settings, "WALLET_ACTIVITY_LIMIT", 50) or 50)
    sources_ok: list[str] = []
    degraded: list[str] = []
    activity: list[dict] = []
    positions: list[dict] = []
    chain: dict | None = None

    api = data_api if data_api is not None else DataApiClient(settings)
    try:
        try:
            activity = _normalize_activity(api.get_activity_v2(user=address, limit=limit))
            sources_ok.append("polymarket-data-api-v2")
        except Exception:  # noqa: BLE001 - degraded, reported honestly
            degraded.append("polymarket-data-api-v2/activity")
        try:
            positions = _normalize_positions(
                api.get_positions_v2(user=address, status="OPEN", limit=limit)
            )
            if "polymarket-data-api-v2" not in sources_ok:
                sources_ok.append("polymarket-data-api-v2")
        except Exception:  # noqa: BLE001 - degraded, reported honestly
            degraded.append("polymarket-data-api-v2/positions")
    finally:
        if data_api is None:
            try:
                api.close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    rpc_client = rpc if rpc is not None else PolygonRpcClient(settings)
    try:
        try:
            balance_wei = rpc_client.get_balance_wei(address)
            nonce = rpc_client.get_transaction_count(address)
            chain = {
                "source": "polygon-rpc",
                "rpc_url": str(getattr(settings, "POLYGON_RPC_URL", "")),
                "balance_wei": str(balance_wei),
                "balance_pol": round(float(balance_wei) / 1e18, 6),
                "nonce": int(nonce),
            }
            sources_ok.append("polygon-rpc")
        except Exception:  # noqa: BLE001 - degraded, reported honestly
            degraded.append("polygon-rpc")
    finally:
        if rpc is None:
            try:
                rpc_client.close()
            except Exception:  # noqa: BLE001 - best effort
                pass

    payload: dict = {
        "mode": "PAPER",
        "wallet": {
            "address": address,
            "label": wallet.get("label", ""),
            "watch_only": True,
        },
        "sources": sources_ok,
        "degraded": degraded,
        "activity": activity,
        "positions": positions,
        "chain": chain,
        "fetched_at": _now_iso(),
        "note": (
            "no activity found (address may not be a Polymarket proxy wallet)"
            if not activity and not positions
            else ""
        ),
        "label": "watch-only · read-only",
        "ok": bool(sources_ok),
    }
    if payload["ok"]:
        _cache_put(address, payload)
    return payload
