"""Read-only Polygon JSON-RPC client (public endpoint, no keys).

ONLY ``eth_getBalance`` and ``eth_getTransactionCount`` exist here. There is
no signing, no ``eth_sendRawTransaction``, no key material, and no method
that can move funds — this is a watch-only read path for the wallet
activity endpoint. Failures raise :class:`PolygonRpcError` naming the
endpoint so the API can degrade honestly.
"""
from __future__ import annotations

import httpx

from app.config import Settings


class PolygonRpcError(RuntimeError):
    """Raised when a Polygon RPC request fails; names the endpoint."""


class PolygonRpcClient:
    """Minimal sync client for a public Polygon JSON-RPC endpoint."""

    def __init__(self, settings: Settings) -> None:
        """Store settings and create a sync httpx client (no auth).

        Args:
            settings: App Settings carrying POLYGON_RPC_URL.
        """
        self.settings = settings
        self._http = httpx.Client(
            base_url=settings.POLYGON_RPC_URL.rstrip("/"), timeout=10.0
        )

    def _call(self, method: str, params: list) -> object:
        """POST one JSON-RPC request and return ``result``.

        Args:
            method: JSON-RPC method name (read-only methods only).
            params: Positional params list.

        Raises:
            PolygonRpcError: on transport, HTTP, or JSON-RPC errors.
        """
        endpoint = f"{self.settings.POLYGON_RPC_URL} [{method}]"
        try:
            resp = self._http.post(
                "",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise PolygonRpcError(
                f"Polygon RPC request failed [{endpoint}]: {exc}"
            ) from exc
        payload = resp.json()
        if not isinstance(payload, dict):
            raise PolygonRpcError(f"Polygon RPC malformed response [{endpoint}]")
        if payload.get("error"):
            raise PolygonRpcError(
                f"Polygon RPC error [{endpoint}]: {payload['error']}"
            )
        if "result" not in payload:
            raise PolygonRpcError(f"Polygon RPC missing result [{endpoint}]")
        return payload["result"]

    @staticmethod
    def _hex_to_int(value: object, endpoint: str) -> int:
        """Parse a hex quantity from JSON-RPC; raise on malformed data."""
        try:
            return int(str(value), 16)
        except (TypeError, ValueError) as exc:
            raise PolygonRpcError(
                f"Polygon RPC returned a non-hex quantity [{endpoint}]: {value!r}"
            ) from exc

    def get_balance_wei(self, address: str) -> int:
        """POL balance in wei for an address.

        Args:
            address: EVM address (already validated by the caller).

        Returns:
            Balance in wei.
        """
        endpoint = f"{self.settings.POLYGON_RPC_URL} [eth_getBalance]"
        result = self._call("eth_getBalance", [address, "latest"])
        return self._hex_to_int(result, endpoint)

    def get_transaction_count(self, address: str) -> int:
        """Outgoing transaction count (nonce) for an address.

        Args:
            address: EVM address (already validated by the caller).

        Returns:
            Transaction count as an int.
        """
        endpoint = f"{self.settings.POLYGON_RPC_URL} [eth_getTransactionCount]"
        result = self._call("eth_getTransactionCount", [address, "latest"])
        return self._hex_to_int(result, endpoint)

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._http.close()
