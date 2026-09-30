"""Regression: v2 normalizers must read the real (snake_case) OpenAPI fields.

The original normalizers were written against guessed aliases and the offline
fakes mirrored those guesses, so CI could not see that ``usdc_size`` /
``transaction_hash`` (activity) and ``current_size`` / ``realized_pnl``
(positions) were ignored. The fixtures below are shaped like the Data API v2
Activity and Position schemas; the legacy ``size`` values are deliberately
different so a regression cannot pass by coincidence.
"""

from app.wallets import _normalize_activity, _normalize_positions

TX_HASH = "0x" + "cd" * 32

#: One Activity item, shaped like a `/v2/activity` response row.
ACTIVITY_ITEM = {
    "proxy_wallet": "0xabc",
    "timestamp": 1759224000,
    "condition_id": "0xcondition",
    "type": "TRADE",
    "size": 240.0,  # shares
    "usdc_size": 120.0,  # USD notional — the value size_usd must report
    "price": 0.62,
    "asset": "111",
    "side": "BUY",
    "outcome": "Yes",
    "title": "Will it rain tomorrow?",
    "slug": "will-it-rain-tomorrow",
    "transaction_hash": TX_HASH,
}

#: One open Position item, shaped like a `/v2/positions` response row.
POSITION_ITEM = {
    "proxy_wallet": "0xabc",
    "asset": "111",
    "condition_id": "0xcondition",
    "size": 0.0,  # legacy/guessed field — current_size must win
    "avg_price": 0.42,
    "initial_value": 105.0,
    "current_value": 137.5,
    "total_bought": 250.0,
    "realized_pnl": 12.5,
    "percent_realized_pnl": 5.0,
    "current_size": 250.0,
    "title": "Will it rain tomorrow?",
    "slug": "will-it-rain-tomorrow",
    "outcome": "Yes",
}


def test_activity_size_usd_prefers_usdc_size_over_shares():
    [row] = _normalize_activity([ACTIVITY_ITEM])
    assert row["size_usd"] == 120.0


def test_activity_tx_hash_reads_transaction_hash():
    [row] = _normalize_activity([ACTIVITY_ITEM])
    assert row["tx_hash"] == TX_HASH


def test_activity_legacy_aliases_still_resolve():
    [row] = _normalize_activity(
        [{"type": "TRADE", "usdcSize": 10.0, "transactionHash": "0xlegacy"}]
    )
    assert row["size_usd"] == 10.0
    assert row["tx_hash"] == "0xlegacy"


def test_activity_falls_back_to_size_when_usdc_size_absent():
    [row] = _normalize_activity([{"type": "TRADE", "size": 42.0}])
    assert row["size_usd"] == 42.0


def test_positions_size_prefers_current_size():
    [row] = _normalize_positions([POSITION_ITEM])
    assert row["size"] == 250.0


def test_positions_pnl_reads_realized_pnl():
    [row] = _normalize_positions([POSITION_ITEM])
    assert row["pnl"] == 12.5


def test_positions_pnl_prefers_realized_over_unrealized_and_legacy():
    [row] = _normalize_positions(
        [{**POSITION_ITEM, "unrealized_pnl": 20.0, "cashPnl": 99.0}]
    )
    assert row["pnl"] == 12.5


def test_positions_legacy_cash_pnl_still_resolves():
    [row] = _normalize_positions([{"cashPnl": 3.5, "size": 1.0}])
    assert row["pnl"] == 3.5
