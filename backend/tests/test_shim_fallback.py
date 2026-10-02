"""Blocked-network shim fallback: direct first, gated, exactly one replay.

The direct attempt is faked with ``httpx.MockTransport`` on the client's own
``_http`` instance; the shim replay is faked by injecting a MockTransport
into the shim module's client factory. No network is touched.
"""

from __future__ import annotations

import time

import httpx
import pytest

from app.polymarket import shim as shim_mod
from app.polymarket.clob import ClobClient, ClobError
from tests.fakes import make_settings


def _settings(tmp_path, **overrides):
    """Settings with an absolute temp DB; shim overrides on top."""
    return make_settings(tmp_path, **overrides)


def _raise_connect_error(request: httpx.Request) -> httpx.Response:
    """MockTransport handler: the route is blocked."""
    raise httpx.ConnectError("blocked by network", request=request)


def _raise_connect_timeout(request: httpx.Request) -> httpx.Response:
    """MockTransport handler: the connect itself times out."""
    raise httpx.ConnectTimeout("connect timed out", request=request)


def _client_with_transport(handler) -> httpx.Client:
    """A CLOB-like direct client whose transport is the given handler."""
    return httpx.Client(
        base_url="https://clob.example",
        transport=httpx.MockTransport(handler),
    )


def _forbidden_shim_client():
    """Fail loudly if a test reaches the shim when it must not."""
    raise AssertionError("the shim must not be contacted")


def test_direct_success_never_touches_the_shim(tmp_path, monkeypatch):
    """A working direct route is used as-is; the shim stays untouched."""
    shim_calls: list[str] = []

    def shim_handler(request: httpx.Request) -> httpx.Response:
        shim_calls.append(str(request.url))
        return httpx.Response(200, json={"should": "not be called"})

    monkeypatch.setattr(
        shim_mod,
        "_client",
        lambda: httpx.Client(transport=httpx.MockTransport(shim_handler)),
    )
    client = ClobClient(
        _settings(tmp_path, POLYMARKET_SHIM_URL="http://127.0.0.1:8011")
    )
    client._http = _client_with_transport(
        lambda request: httpx.Response(200, json={"bids": [], "asks": []})
    )
    assert client.get_orderbook("t1") == {"bids": [], "asks": []}
    assert shim_calls == []


def test_connect_error_replays_exactly_once_through_the_shim(tmp_path, monkeypatch):
    """A blocked direct call is replayed exactly once through the shim."""
    shim_requests: list[httpx.Request] = []

    def shim_handler(request: httpx.Request) -> httpx.Response:
        shim_requests.append(request)
        return httpx.Response(
            200, json={"bids": [{"price": "0.49", "size": "10"}], "asks": []}
        )

    monkeypatch.setattr(
        shim_mod,
        "_client",
        lambda: httpx.Client(transport=httpx.MockTransport(shim_handler)),
    )
    client = ClobClient(
        # Trailing slash is an operator typo, not a different URL.
        _settings(tmp_path, POLYMARKET_SHIM_URL="http://127.0.0.1:8011/")
    )
    client._http = _client_with_transport(_raise_connect_error)
    book = client.get_orderbook("t1")
    assert book["bids"][0]["price"] == "0.49"
    assert len(shim_requests) == 1
    assert str(shim_requests[0].url) == (
        "http://127.0.0.1:8011/clob/book?token_id=t1"
    )


def test_connect_timeout_without_a_shim_url_raises_as_before(tmp_path, monkeypatch):
    """No shim URL -> the old contract: retried, then ClobError. Never shims."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(shim_mod, "_client", _forbidden_shim_client)
    client = ClobClient(_settings(tmp_path))
    client._http = _client_with_transport(_raise_connect_timeout)
    with pytest.raises(ClobError) as err:
        client.get_orderbook("t1")
    assert "CLOB API request failed" in str(err.value)


def test_http_500_never_falls_back_to_the_shim(tmp_path, monkeypatch):
    """An HTTP answer is a real upstream answer, even a bad one."""
    monkeypatch.setattr(time, "sleep", lambda seconds: None)
    monkeypatch.setattr(shim_mod, "_client", _forbidden_shim_client)
    client = ClobClient(
        _settings(tmp_path, POLYMARKET_SHIM_URL="http://127.0.0.1:8011")
    )
    client._http = _client_with_transport(
        lambda request: httpx.Response(500, json={"error": "boom"})
    )
    with pytest.raises(ClobError) as err:
        client.get_orderbook("t1")
    assert "500" in str(err.value)


def test_shim_failure_names_both_errors(tmp_path, monkeypatch):
    """If the replay itself fails, the error names the direct and shim errors."""
    def shim_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("shim unreachable", request=request)

    monkeypatch.setattr(
        shim_mod,
        "_client",
        lambda: httpx.Client(transport=httpx.MockTransport(shim_handler)),
    )
    client = ClobClient(
        _settings(tmp_path, POLYMARKET_SHIM_URL="http://127.0.0.1:8011")
    )
    client._http = _client_with_transport(_raise_connect_error)
    with pytest.raises(ClobError) as err:
        client.get_orderbook("t1")
    message = str(err.value)
    assert "direct call failed" in message
    assert "shim replay failed" in message
