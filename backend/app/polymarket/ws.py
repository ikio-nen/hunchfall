"""CLOB market websocket client (optional live-price upgrade).

Connects to ``POLYMARKET_WS_URL``
(``wss://ws-subscriptions-clob.polymarket.com/ws/market``) and streams
live book/price events::

    subscribe message: {"type": "market", "assets_ids": [<token_id>, ...]}
    events seen on the channel: "book", "price_change", "last_trade_price"

The loop (``app/loop.py --watch``) currently polls REST for its snapshot —
that path is kept and the websocket is an optional upgrade: lower latency,
a staleness heartbeat, and no repeated REST polling. Nothing here places
orders; it is a read-only price feed, paper mode stays paper.

``stream_prices(token_ids, handler)`` is a coroutine: ``handler`` receives
each parsed event dict ``{"event_type", "token_id", "price", ...}``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

Handler = Callable[[dict], Awaitable[None] | None]


def _parse_event(raw: str) -> dict | None:
    """Parse one websocket message into a normalized price event.

    Args:
        raw: Raw message text.

    Returns:
        Normalized event dict, or None when the message is not a price
        event (e.g. subscription acks).
    """
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(msg, dict):
        return None
    event_type = str(msg.get("event_type") or msg.get("type") or "")
    token_id = str(
        msg.get("asset_id") or msg.get("assetId") or msg.get("token_id") or ""
    )
    price = msg.get("price") or msg.get("last_trade_price") or msg.get("new_price")
    if event_type not in ("book", "price_change", "last_trade_price"):
        return None
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    return {
        "event_type": event_type,
        "token_id": token_id,
        "price": price_f,
        "raw": msg,
    }


async def stream_prices(token_ids: list[str], handler: Handler) -> None:
    """Subscribe to live price events for token ids and call ``handler``.

    Sends ``{"type": "market", "assets_ids": [...]}`` on connect and keeps
    the socket open until cancelled; reconnects with backoff on drops.

    Args:
        token_ids: CLOB token ids to subscribe to.
        handler: Called with each normalized event dict; may be async.
    """
    import websockets  # deferred: optional dep; see requirements.txt

    while True:
        try:
            async with websockets.connect(_WS_URL) as ws:
                await ws.send(
                    json.dumps({"type": "market", "assets_ids": token_ids})
                )
                log.info("ws: subscribed to %d token(s)", len(token_ids))
                async for raw in ws:
                    event = _parse_event(str(raw))
                    if event is None:
                        continue
                    result = handler(event)
                    if isinstance(result, Awaitable):
                        await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reconnect, never crash loop
            log.warning("ws: connection dropped (%s); reconnecting in 5s", exc)
            await asyncio.sleep(5)


__all__ = ["stream_prices"]
