"""One-off live probe for the Polymarket docs upgrades (2026-10-02).

Verifies, through the documented read-only fetch proxy:
  1. CLOB /prices-history param name (market= vs token_id=) + row shape.
  2. CLOB /midpoints batch (token_ids=a,b).
  3. CLOB /prices batch (token_ids=a,b + sides=BUY,BUY).
  4. Data API /v2/trades batch condition=a,b and filter_type=CASH row shape.

Run from ``backend/``::

    python scripts/probe_docs_upgrades.py --proxy r.jina.ai
"""

from __future__ import annotations

import argparse
import json
import sys
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
FETCH_TIMEOUT = 90

#: The live market used across these probes (Xi out before 2027).
CONDITION = "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7"
YES_TOKEN = "32338220190071351435772801779725302244575775216413325951443816017994629993401"
NO_TOKEN = "25659310674993675562345759665114759892400026242514633218387667107987341231962"


class ProxyShim(BaseHTTPRequestHandler):
    """Loopback shim: ``/gamma|clob|data/<path>`` -> the real host."""

    proxy_prefix = ""

    def do_GET(self) -> None:  # noqa: N802
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
                Request(url, headers={"User-Agent": "hunchfall-probe/0.1"}),
                timeout=FETCH_TIMEOUT,
            ).read().decode("utf-8", "replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace").encode()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:  # noqa: BLE001
            message = f"shim fetch failed: {type(exc).__name__}: {exc}".encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)
            return
        if self.proxy_prefix and "Markdown Content:" in raw:
            raw = raw.split("Markdown Content:", 1)[1]
        body = raw.strip().encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Quiet."""


def start_shim(proxy_prefix: str) -> int:
    """Start the shim on an ephemeral port; return the port."""
    handler = type("BoundShim", (ProxyShim,), {"proxy_prefix": proxy_prefix})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return int(server.server_address[1])


def fetch(port: int, path: str, params: dict) -> tuple[int, str]:
    """GET through the shim; return (status, body)."""
    from urllib.parse import urlencode

    url = f"http://127.0.0.1:{port}{path}?{urlencode(params)}"
    try:
        with urlopen(Request(url), timeout=FETCH_TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    """Probe each endpoint and print what actually came back."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proxy", default=None, help="fetch-proxy host, e.g. r.jina.ai")
    args = parser.parse_args(argv)
    prefix = f"https://{args.proxy}/" if args.proxy else ""
    port = start_shim(prefix)
    out: dict = {"proxy": args.proxy or "direct"}

    # 1. prices-history: market= vs token_id=
    for param in ("market", "token_id"):
        status, body = fetch(
            port,
            "/clob/prices-history",
            {param: YES_TOKEN, "interval": "1h", "fidelity": 60},
        )
        try:
            payload = json.loads(body)
        except ValueError:
            payload = body[:200]
        hist = payload.get("history") if isinstance(payload, dict) else None
        out[f"prices_history_{param}"] = {
            "status": status,
            "n_points": len(hist) if isinstance(hist, list) else None,
            "first_point": (hist[0] if isinstance(hist, list) and hist else None),
            "last_point": (hist[-1] if isinstance(hist, list) and hist else None),
            "raw_head": None if hist is not None else str(payload)[:200],
        }

    # 2. midpoints batch
    status, body = fetch(port, "/clob/midpoints", {"token_ids": f"{YES_TOKEN},{NO_TOKEN}"})
    out["midpoints_batch"] = {"status": status, "body": body[:400]}

    # 3. prices batch
    status, body = fetch(
        port, "/clob/prices", {"token_ids": f"{YES_TOKEN},{NO_TOKEN}", "sides": "BUY,BUY"}
    )
    out["prices_batch"] = {"status": status, "body": body[:400]}

    # 4. trades batch by condition (2 ids) and filter_type=CASH
    status, body = fetch(
        port,
        "/data/v2/trades",
        {"condition": f"{CONDITION},{CONDITION}", "limit": 2},
    )
    try:
        payload = json.loads(body)
        rows = payload.get("data") or []
    except ValueError:
        rows = []
    out["trades_batch_condition"] = {
        "status": status,
        "n_rows": len(rows),
        "row_keys": sorted(rows[0].keys()) if rows else [],
        "error": None if rows else body[:300],
    }

    status, body = fetch(
        port,
        "/data/v2/trades",
        {"condition": CONDITION, "limit": 2, "filter_type": "CASH"},
    )
    try:
        payload = json.loads(body)
        rows = payload.get("data") or []
    except ValueError:
        rows = []
    out["trades_filter_type_cash"] = {
        "status": status,
        "n_rows": len(rows),
        "row_keys": sorted(rows[0].keys()) if rows else [],
        "first_row": rows[0] if rows else None,
        "error": None if rows else body[:300],
    }

    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
