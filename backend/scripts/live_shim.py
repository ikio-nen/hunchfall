"""Loopback live-data shim for networks that block Polymarket egress.

This machine cannot open direct TCP connections to gamma-api / clob /
data-api.polymarket.com (all requests time out). The repo's documented
read-only fetch-proxy route (``r.jina.ai``) can reach them, but it wraps
JSON bodies in a small markdown envelope. This shim maps

    http://127.0.0.1:<port>/gamma/<path>  ->  https://gamma-api.polymarket.com/<path>
    http://127.0.0.1:<port>/clob/<path>   ->  https://clob.polymarket.com/<path>
    http://127.0.0.1:<port>/data/<path>   ->  https://data-api.polymarket.com/<path>

through the fetch proxy and unwraps the envelope, so the backend clients
see plain JSON.

It is the target of the backend's gated fallback (``app/polymarket/shim.py``):
with ``POLYMARKET_SHIM_URL=http://127.0.0.1:<port>`` set, a connection-level
direct failure is replayed once through the matching mount, while HTTP errors
and read timeouts never fall back. It can also be used directly by pointing
the three ``POLYMARKET_*_URL`` settings at the mounts above.

Read-only by construction: only GET is served, no auth, no keys, no
order placement — paper predictions only.

Run from ``backend/``::

    python scripts/live_shim.py [port]        # default 8011

Then point the backend at it (direct route)::

    POLYMARKET_GAMMA_URL=http://127.0.0.1:8011/gamma
    POLYMARKET_CLOB_URL=http://127.0.0.1:8011/clob
    POLYMARKET_DATA_API_URL=http://127.0.0.1:8011/data

or keep the real hosts and set ``POLYMARKET_SHIM_URL=http://127.0.0.1:8011``
to let the backend fall back only when egress is blocked.
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

UPSTREAMS = {
    "gamma": "https://gamma-api.polymarket.com",
    "clob": "https://clob.polymarket.com",
    "data": "https://data-api.polymarket.com",
}
PROXY_PREFIX = "https://r.jina.ai/"
FETCH_TIMEOUT_SEC = 90


class Shim(BaseHTTPRequestHandler):
    """Forward ``/<upstream>/<path>`` through the fetch proxy as JSON."""

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        parts = urlsplit(self.path)
        head, _, rest = parts.path.lstrip("/").partition("/")
        base = UPSTREAMS.get(head)
        if base is None:
            self.send_error(404, "unknown shim prefix (use /gamma, /clob, /data)")
            return
        target = f"{base}/{rest}"
        if parts.query:
            target = f"{target}?{parts.query}"
        try:
            raw = (
                urlopen(
                    Request(
                        f"{PROXY_PREFIX}{target}",
                        headers={"User-Agent": "hunchfall-live-shim/0.1"},
                    ),
                    timeout=FETCH_TIMEOUT_SEC,
                )
                .read()
                .decode("utf-8", "replace")
            )
        except HTTPError as exc:  # upstream status -> same status
            body = exc.read()
            self.send_response(exc.code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:  # noqa: BLE001
            body = f"shim fetch failed: {type(exc).__name__}: {exc}".encode()
            self.send_response(502)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "Markdown Content:" in raw:  # unwrap the proxy envelope
            raw = raw.split("Markdown Content:", 1)[1]
        body = raw.strip().encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        """Quiet."""


def main(argv: list[str] | None = None) -> int:
    """Serve the shim on 127.0.0.1:<port> until interrupted."""
    port = int((argv or sys.argv[1:] or ["8011"])[0])
    server = ThreadingHTTPServer(("127.0.0.1", port), Shim)
    print(f"live shim: http://127.0.0.1:{port}/{{gamma,clob,data}}  (GET only)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
