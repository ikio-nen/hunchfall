"""Polymarket read-only market-data clients.

- gamma.py — Gamma Data API: events, markets, questions, resolution rules
- clob.py — CLOB market-data API: /price, /midpoint, /book, /spread,
  /prices-history, /tick-size (+ batch endpoints). No trading endpoints.
- data_api.py — Data API: /trades, /v2/prices-history, /holders, /oi
- ws.py — CLOB websocket (wss://ws-subscriptions-clob.polymarket.com/ws/market):
  optional live-price upgrade for --watch; read-only price feed.

PAPER TRADING ONLY: all clients are public endpoints, no auth, no orders.
POST /order (and any trading endpoint) is never called anywhere.
"""
