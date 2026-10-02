"""Build a REAL labeled playtest set from resolved Polymarket markets.

Every field comes from a live first-party read (2026-10-01):

* ``market_question``  — Gamma ``/markets`` (resolved, ``closed=true``)
* ``odds_snapshot_yes`` — CLOB ``/prices-history?market=<YES token>``: the last
  traded price at or before **T-24h from resolution**, i.e. the market-implied
  YES probability *as known one day before the outcome*.
* ``outcome`` — Gamma ``outcomePrices`` on the closed market (``["1","0"]`` =
  YES, ``["0","1"]`` = NO). The documented ``["0","0"]`` gotcha is treated as
  "no confirmed winner" and the market is skipped, never guessed.
* ``news_snapshot`` — **the market's own Gamma metadata** (question,
  description, resolution source). No historical news archive was available to
  this build, so this field is explicitly NOT news; it is labelled as such in
  ``source`` and in docs/PLAYTESTING.md.

The builder refuses to invent anything: a market is skipped when the winner is
ambiguous, the price history has no point before the cutoff, or the
decision-time price is degenerate (outside 0.02..0.98).

Usage (from ``backend/``)::

    python scripts/build_playtest_set.py --proxy r.jina.ai --want 20
    python scripts/build_playtest_set.py --want 20          # direct egress
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.polymarket.gamma import parse_outcome_prices, parse_token_ids  # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SAMPLES = Path(__file__).resolve().parents[1] / "app" / "playtest" / "samples" / "samples.jsonl"

#: Decision time = this long before the market's endDate.
DECISION_LEAD = timedelta(hours=24)
#: Keep samples whose decision-time price is not degenerate.
PRICE_FLOOR, PRICE_CEIL = 0.02, 0.98
FETCH_TIMEOUT = 90


def proxied(url: str, proxy: str | None) -> object:
    """GET a URL, optionally through a read-only fetch proxy.

    Args:
        url: Absolute target URL.
        proxy: Proxy host (e.g. ``r.jina.ai``) or None for direct egress.

    Returns:
        Parsed JSON body.
    """
    target = f"https://{proxy}/{url}" if proxy else url
    raw = urlopen(
        Request(target, headers={"User-Agent": "hunchfall-playtest-build/0.1"}),
        timeout=FETCH_TIMEOUT,
    ).read().decode("utf-8", "replace")
    if proxy:
        marker = "Markdown Content:"
        if marker in raw:
            raw = raw.split(marker, 1)[1]
    return json.loads(raw.strip())


def as_list(value: object) -> list:
    """Parse a JSON-string list or return the list itself."""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def resolved_outcome(market: dict) -> str | None:
    """``"YES"``/``"NO"`` for a closed market, or None when unconfirmed."""
    winner = (
        market.get("umaResolutionStatus")
        or market.get("resolutionSource")
        or market.get("resolvedBy")
        or ""
    )
    if not bool(market.get("closed")) or not str(winner).strip():
        return None
    prices = parse_outcome_prices(market)
    if len(prices) < 2:
        return None
    if prices[0] >= 0.999 and prices[1] <= 0.001:
        return "YES"
    if prices[1] >= 0.999 and prices[0] <= 0.001:
        return "NO"
    return None  # includes the ["0","0"] gotcha — never guessed


def price_at(history: list, cutoff_ts: float) -> float | None:
    """Last traded price at or before ``cutoff_ts`` (the decision time)."""
    points: list[tuple[float, float]] = []
    for point in history or []:
        if not isinstance(point, dict):
            continue
        try:
            points.append((float(point["t"]), float(point["p"])))
        except (KeyError, TypeError, ValueError):
            continue
    usable = [p for p in points if p[0] <= cutoff_ts]
    if not usable:
        return None
    return max(usable, key=lambda p: p[0])[1]


def news_field(market: dict, decision_price: float, decision_ts: float) -> str:
    """The honest ``news_snapshot``: real market metadata, not news."""
    description = str(market.get("description") or "").replace("\n", " ").strip()
    when = datetime.fromtimestamp(decision_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    parts = [f"[market metadata, not news] {market.get('question')}"]
    if description:
        parts.append(f"Resolution criteria: {description[:280]}")
    source = str(market.get("resolutionSource") or "").strip()
    if source:
        parts.append(f"Resolution source: {source}")
    parts.append(f"Market-implied YES at decision time ({when}): {decision_price:.3f}")
    return " | ".join(parts)


def build_sample(market: dict, proxy: str | None) -> dict | None:
    """Turn one resolved market into a labeled example, or None if unusable."""
    outcome = resolved_outcome(market)
    if outcome is None:
        return None
    tokens = parse_token_ids(market)
    if len(tokens) < 2:
        return None
    end_raw = str(market.get("endDate") or "")
    try:
        end = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    decision_ts = (end - DECISION_LEAD).timestamp()
    try:
        payload = proxied(
            f"{CLOB}/prices-history?market={tokens[0]}&interval=max&fidelity=60", proxy
        )
    except HTTPError as exc:
        if exc.code == 429:
            time.sleep(5.0)  # proxy/venue throttling — back off, never guess
        return None
    except (OSError, ValueError):
        return None
    history = payload.get("history") if isinstance(payload, dict) else None
    price = price_at(history if isinstance(history, list) else [], decision_ts)
    if price is None or not (PRICE_FLOOR <= price <= PRICE_CEIL):
        return None
    return {
        "news_snapshot": news_field(market, price, decision_ts),
        "market_question": str(market.get("question") or ""),
        "odds_snapshot_yes": round(price, 4),
        "outcome": outcome,
        "source": (
            "REAL: polymarket-gamma(/markets closed) + clob(/prices-history T-24h); "
            "news_snapshot = market metadata, not news (see docs/PLAYTESTING.md)"
        ),
        "label": "REAL",
    }


def main(argv: list[str] | None = None) -> int:
    """Fetch resolved markets and write REAL samples (keeping SAMPLE ones)."""
    parser = argparse.ArgumentParser(description="build the REAL playtest set")
    parser.add_argument("--proxy", default=None, help="fetch-proxy host, e.g. r.jina.ai")
    parser.add_argument("--want", type=int, default=20, help="target REAL samples")
    parser.add_argument("--pages", type=int, default=3, help="Gamma pages to scan")
    parser.add_argument("--page-size", type=int, default=12, help="markets per page")
    parser.add_argument("--out", default=str(SAMPLES), help="destination JSONL")
    parser.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = parser.parse_args(argv)

    existing: list[dict] = []
    out_path = Path(args.out)
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                existing.append(json.loads(line))
    kept = [row for row in existing if row.get("label") != "REAL"]
    have = [row for row in existing if row.get("label") == "REAL"]
    seen = {str(row.get("market_question") or "") for row in have}
    print(
        f"existing rows: {len(existing)} "
        f"({len(kept)} non-REAL kept, {len(have)} REAL already built)"
    )

    def checkpoint() -> None:
        """Persist progress after every sample, so long runs are resumable."""
        with open(out_path, "w", encoding="utf-8") as handle:
            for row in kept + have + built:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    if len(have) >= args.want:
        print(f"already have {len(have)} REAL samples; nothing to do")
        return 0

    built: list[dict] = []
    scanned = 0
    for page in range(max(1, args.pages)):
        url = (
            f"{GAMMA}/markets?closed=true&limit={args.page_size}"
            f"&order=volume&ascending=false&offset={page * args.page_size}"
        )
        try:
            payload = proxied(url, args.proxy)
        except HTTPError as exc:
            print(f"page {page}: gamma fetch failed (HTTP {exc.code})", flush=True)
            continue
        except (OSError, ValueError) as exc:
            print(f"page {page}: gamma fetch failed ({type(exc).__name__})", flush=True)
            continue
        markets = payload if isinstance(payload, list) else payload.get("data", [])
        for market in markets:
            if not isinstance(market, dict):
                continue
            question = str(market.get("question") or "")
            if question in seen:
                continue
            scanned += 1
            sample = build_sample(market, args.proxy)
            if sample is None:
                continue
            seen.add(question)
            built.append(sample)
            print(
                f"[{len(have) + len(built)}] {sample['outcome']:3s} "
                f"p={sample['odds_snapshot_yes']:<6} {sample['market_question'][:56]}",
                flush=True,
            )
            if not args.dry_run:
                checkpoint()  # resumable: progress survives a timeout
            if len(have) + len(built) >= args.want:
                break
            time.sleep(0.3)
        if len(have) + len(built) >= args.want:
            break

    total = len(have) + len(built)
    print(f"built {len(built)} new REAL samples from {scanned} scanned markets")
    if args.dry_run:
        return 0 if built else 1
    checkpoint()
    print(f"wrote {len(kept) + total} rows to {out_path} ({total} REAL)")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())
