"""Muse trend-scan feed: scheduled market snapshots written by the assistant.

Every ~6h a scheduled job scans Polymarket's public Gamma API for the
highest-volume markets and writes ``backend/data/muse-scans/latest.json``
(see ``backend/data/muse-scans/README.md``). This module loads that file
into candidate inputs for the loop.

The feed is a *hint*, never a decision: candidates go through the same
Jev call, risk gate, and paper engine as story-matched markets. A missing,
unreadable, or stale (>24h) file is silently ignored — the loop then runs
on its normal sources.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("hunchfall.muse_scan")

SCAN_DIRNAME = "muse-scans"
SCAN_FILENAME = "latest.json"
MAX_SCAN_AGE_HOURS = 24.0
MAX_CANDIDATES = 5


@dataclass
class MuseScanCandidate:
    """One market hint from a Muse scan, shaped like a DecisionState."""

    market_question: str
    slug: str
    yes_price: float
    volume_24h: float
    news_summary: str
    extra_context: str
    scanned_at: str


def scan_file_path(data_dir: str | Path) -> Path:
    """Absolute path of the scan file under the backend data dir."""
    return Path(data_dir) / SCAN_DIRNAME / SCAN_FILENAME


def _fresh_enough(scanned_at: str, max_age_hours: float) -> bool:
    try:
        ts = datetime.fromisoformat(str(scanned_at).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age_hours = (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    return 0.0 <= age_hours <= max_age_hours


def load_muse_scan(
    data_dir: str | Path,
    max_age_hours: float = MAX_SCAN_AGE_HOURS,
    limit: int = MAX_CANDIDATES,
) -> list[MuseScanCandidate]:
    """Load fresh Muse scan candidates.

    Returns [] when the file is missing, unreadable, stale, or invalid —
    the loop must keep working on its normal sources in that case.
    """
    path = scan_file_path(data_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("muse_scan: unreadable %s: %s", path, exc)
        return []
    if not isinstance(payload, dict):
        log.warning("muse_scan: invalid payload in %s", path)
        return []
    scanned_at = str(payload.get("scanned_at") or "")
    if not _fresh_enough(scanned_at, max_age_hours):
        log.info("muse_scan: stale scan (%s), ignoring", scanned_at)
        return []
    out: list[MuseScanCandidate] = []
    markets = payload.get("markets") or []
    if not isinstance(markets, list):
        return []
    for m in markets:
        if not isinstance(m, dict):
            continue
        try:
            yes_price = float(m["yes_price"])
            if not 0.0 <= yes_price <= 1.0:
                continue
            out.append(
                MuseScanCandidate(
                    market_question=str(m["market_question"]),
                    slug=str(m.get("slug") or ""),
                    yes_price=yes_price,
                    volume_24h=float(m.get("volume_24h") or 0.0),
                    news_summary=str(m.get("news_summary") or ""),
                    extra_context=str(m.get("extra_context") or ""),
                    scanned_at=scanned_at,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
        if len(out) >= limit:
            break
    return out
