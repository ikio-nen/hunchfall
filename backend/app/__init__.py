"""hunchfall backend data layer — Polymarket paper-trading agent.

PAPER TRADING ONLY: no real money, no wallet signing, no API-key trading
endpoints, no private keys anywhere in this codebase.

Packages:
- app.config: environment-driven Settings
- app.polymarket: Gamma Data API + CLOB/Data API clients (market data only)
- app.scraper: story intake (Reddit, news, GDELT, Twitter interface stub)
- app.jev: Jev decision-model client (typed noul/choice/score calls)

Honesty rule: every mock or sample is labeled MOCK/SAMPLE in code and
docstrings. Never fake fills, P&L, or win rates.
"""
