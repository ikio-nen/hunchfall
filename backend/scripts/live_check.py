"""Live checks for the RFC-003 predictor against the real Polymarket APIs.

Why this exists: the build network blackholes every Polymarket host
(gamma/clob/data-api, HTTP and HTTPS, IPv4 and IPv6 all time out while
unrelated hosts answer in <1.5s), so the predictor's live surfaces could not
be checked from here by a plain HTTP client. This script makes the checks
reproducible and records them, and it can route every upstream call through a
public **read-only fetch proxy** (``--proxy r.jina.ai``) when direct egress is
blocked.

Trust boundary: with ``--proxy`` the payloads are still first-party
Polymarket data, but a third party is in the transport path. Every endpoint
used here is public and keyless (no secret ever leaves the machine), and the
results are labelled as proxy-transported. Re-run without ``--proxy`` on an
unblocked network to reproduce them first-party.

Checks:
  1. ``/v2/trades?condition=…`` — the real row key set (USD = size x price).
  2. Gamma condition-id resolution — ``/markets/{condition_id}`` is rejected,
     ``/markets?condition_ids=`` is the working lookup.
  3. End-to-end ``POST /predict`` on a real open market, then
     ``POST /predict/{id}/resolve``, then ``GET /predict/accuracy``.

Run (from ``backend/``):

    python scripts/live_check.py --proxy r.jina.ai
    python scripts/live_check.py                      # direct egress
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

UPSTREAMS = {
    "gamma": "https://gamma-api.polymarket.com",
    "clob": "https://clob.polymarket.com",
    "data": "https://data-api.polymarket.com",
}

#: The market used for the end-to-end run (open, liquid, non-social).
DEFAULT_SLUG = "xi-jinping-out-before-2027"
DEFAULT_CONDITION = "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7"
FETCH_TIMEOUT = 90


class ProxyShim(BaseHTTPRequestHandler):
    """Loopback HTTP shim: ``/gamma|clob|data/<path>`` -> the real host."""

    proxy_prefix = ""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Forward one GET to the mapped upstream and return its body."""
        parts = urlsplit(self.path)
        head, _, rest = parts.path.lstrip("/").partition("/")
        base = UPSTREAMS.get(head)
        if base is None:
            self.send_error(404, "unknown shim prefix")
            return
        target = f"{base}/{rest}"
        if parts.query:
            target = f"{target}?{parts.query}"
        url = f"{self.proxy_prefix}{target}" if self.proxy_prefix else target
        try:
            raw = urlopen(
                Request(url, headers={"User-Agent": "hunchfall-livecheck/0.1"}),
                timeout=FETCH_TIMEOUT,
            ).read().decode("utf-8", "replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").encode("utf-8")
            self.send_response(exc.code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:  # noqa: BLE001 - surfaced as a 502 to the client
            message = f"shim fetch failed: {type(exc).__name__}: {exc}".encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)
            return
        if self.proxy_prefix:
            marker = "Markdown Content:"
            if marker in raw:
                raw = raw.split(marker, 1)[1]
        body = raw.strip().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Keep the shim quiet."""


def start_shim(proxy_prefix: str) -> tuple[ThreadingHTTPServer, int]:
    """Start the forwarding shim on an ephemeral port.

    Args:
        proxy_prefix: Fetch-proxy prefix (e.g. ``https://r.jina.ai/``) or "".

    Returns:
        ``(server, port)``.
    """
    handler = type("BoundShim", (ProxyShim,), {"proxy_prefix": proxy_prefix})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, int(server.server_address[1])


def main(argv: list[str] | None = None) -> int:
    """Run the three live checks and print a JSON report."""
    parser = argparse.ArgumentParser(description="hunchfall predictor live checks")
    parser.add_argument("--proxy", default=None, help="fetch-proxy host, e.g. r.jina.ai")
    parser.add_argument("--slug", default=DEFAULT_SLUG, help="market slug for the run")
    parser.add_argument("--condition", default=DEFAULT_CONDITION, help="condition id")
    args = parser.parse_args(argv)

    from fastapi.testclient import TestClient

    from app.api.server import create_app
    from app.config import Settings
    from app.polymarket.data_api import DataApiClient
    from app.polymarket.gamma import GammaClient

    prefix = f"https://{args.proxy}/" if args.proxy else ""
    server, port = start_shim(prefix)
    base = f"http://127.0.0.1:{port}"
    out: dict = {"mode": "LIVE-CHECK", "proxy": args.proxy or "none (direct egress)"}

    # ignore_cleanup_errors: the audit log keeps its SQLite handle open, which
    # Windows refuses to unlink (harmless; the report still prints).
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        settings = Settings(
            DATABASE_PATH=str(Path(tmp) / "livecheck.db"),
            POLYMARKET_GAMMA_URL=f"{base}/gamma",
            POLYMARKET_CLOB_URL=f"{base}/clob",
            POLYMARKET_DATA_API_URL=f"{base}/data",
            JEV_MOCK=True,
        )

        # ---- 1. the tape's real row shape --------------------------------
        data_api = DataApiClient(settings)
        try:
            rows = data_api.get_trades_v2(condition=args.condition, limit=5)
            out["trades"] = {
                "n_rows": len(rows),
                "row_keys": sorted(rows[0].keys()) if rows else [],
                "first_row": rows[0] if rows else None,
                "has_usdc_size": bool(rows and "usdc_size" in rows[0]),
            }
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            out["trades"] = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            data_api.close()

        # ---- 2. condition-id resolution ----------------------------------
        gamma = GammaClient(settings)
        try:
            market = gamma.get_market_by_condition_id(args.condition)
            out["condition_lookup"] = {
                "resolved": bool(market),
                "id": market.get("id"),
                "slug": market.get("slug"),
                "question": market.get("question"),
                "closed": market.get("closed"),
                "end_date": market.get("endDate"),
            }
        except Exception as exc:  # noqa: BLE001
            out["condition_lookup"] = {"error": f"{type(exc).__name__}: {exc}"}
        finally:
            gamma.close()

        # ---- 3. end-to-end pipeline --------------------------------------
        client = TestClient(create_app(settings))
        resp = client.post("/predict", json={"market_slug": args.slug})
        out["predict"] = {"status": resp.status_code}
        if resp.status_code == 200:
            body = resp.json()
            features = body["snapshot"]["features"]
            out["predict"].update(
                {
                    "market": body["market"],
                    "market_price": body["market_price"],
                    "abstain_threshold": body["abstain_threshold"],
                    "prediction": body["prediction"],
                    "model": body["model"],
                    "label": body["label"],
                    "trade_placed": body["trade_placed"],
                    "snapshot_missing": body["snapshot"]["missing"],
                    "tape": features.get("tape"),
                    "book": {
                        k: features.get(k)
                        for k in (
                            "market_mid",
                            "best_bid",
                            "best_ask",
                            "spread_cents",
                            "bid_depth_usd",
                            "ask_depth_usd",
                            "imbalance",
                        )
                    },
                    "audit_event_id": body["audit_event_id"],
                }
            )
            pid = body["prediction"]["prediction_id"]
            resolved = client.post(f"/predict/{pid}/resolve", json={"outcome": "NO"})
            out["resolve"] = {"status": resolved.status_code, "body": resolved.json()}
            # Default view excludes the mock run (invariant: never derive a
            # claim from MOCK); include_mock shows the row really is logged.
            keys = ("n_logged", "n_resolved", "n_abstained", "brier", "label")
            accuracy = client.get("/predict/accuracy").json()
            out["accuracy"] = {k: accuracy.get(k) for k in keys}
            with_mock = client.get("/predict/accuracy?include_mock=true").json()
            out["accuracy_including_mock"] = {k: with_mock.get(k) for k in keys}
        else:
            out["predict"]["error"] = resp.text[:400]

    server.shutdown()
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["predict"]["status"] == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
