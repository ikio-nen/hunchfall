"""Data API v2 client units: envelope, no-offset, Retry-After, zero-state."""

import httpx
import pytest

from app.polymarket.data_api import DataApiClient, DataApiError
from tests.fakes import make_settings


class FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error",
                request=httpx.Request("GET", "https://data-api.polymarket.com"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


class FakeHttp:
    """Captures calls and replays canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.responses.pop(0)


def _client(tmp_path) -> DataApiClient:
    return DataApiClient(make_settings(tmp_path))


def test_activity_unwraps_envelope_and_sends_no_offset(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp(
        [FakeResponse(200, {"data": [{"a": 1}], "pagination": {"next_cursor": "c"}})]
    )
    out = client.get_activity_v2(condition="0xcond", activity_type="TRADE", limit=5)
    assert out == [{"a": 1}]
    path, params = client._http.calls[0]
    assert path == "/v2/activity"
    assert params["condition"] == "0xcond"
    assert params["type"] == "TRADE"
    assert "offset" not in params


def test_activity_empty_data_is_zero_state(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, {"data": [], "pagination": {}})])
    assert client.get_activity_v2(user="0xabc", limit=5) == []


def test_retry_after_is_honored(monkeypatch, tmp_path):
    sleeps: list[float] = []
    monkeypatch.setattr(
        "app.polymarket.data_api.time.sleep", lambda seconds: sleeps.append(seconds)
    )
    client = _client(tmp_path)
    client._http = FakeHttp(
        [
            FakeResponse(429, headers={"Retry-After": "2"}),
            FakeResponse(200, {"data": [{"b": 2}], "pagination": {}}),
        ]
    )
    out = client.get_activity_v2(user="0xabc", limit=5)
    assert out == [{"b": 2}]
    assert sleeps == [2.0]
    assert len(client._http.calls) == 2


def test_positions_params_user_status_limit(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, {"data": [{"size": 1}], "pagination": {}})])
    out = client.get_positions_v2(user="0xabc", status="OPEN", limit=10)
    path, params = client._http.calls[0]
    assert path == "/v2/positions"
    assert params == {"user": "0xabc", "status": "OPEN", "limit": 10}
    assert out == [{"size": 1}]


def test_trades_v2_uses_condition_and_sends_no_offset(tmp_path):
    """RFC-003 predictor tape: GET /v2/trades?condition=..., never offset."""
    client = _client(tmp_path)
    client._http = FakeHttp(
        [FakeResponse(200, {"data": [{"price": 0.5, "size": 10}], "pagination": {}})]
    )
    out = client.get_trades_v2(condition="0xcond", limit=25)
    path, params = client._http.calls[0]
    assert path == "/v2/trades"
    assert params == {"condition": "0xcond", "limit": 25}
    assert "offset" not in params
    assert out == [{"price": 0.5, "size": 10}]


def test_trades_v2_empty_data_is_zero_state(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, {"data": [], "pagination": {}})])
    assert client.get_trades_v2(condition="0xcond", limit=5) == []


def test_retry_after_503_is_honored_like_429(monkeypatch, tmp_path):
    """A transient 503 is retried, honoring its ``Retry-After`` header."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        "app.polymarket.data_api.time.sleep", lambda seconds: sleeps.append(seconds)
    )
    client = _client(tmp_path)
    client._http = FakeHttp(
        [
            FakeResponse(503, headers={"Retry-After": "3"}),
            FakeResponse(200, {"data": [{"c": 3}], "pagination": {}}),
        ]
    )
    out = client.get_trades_v2(condition="0xcond", limit=5)
    assert out == [{"c": 3}]
    assert sleeps == [3.0]
    assert len(client._http.calls) == 2


def test_503_exhausts_two_retries_with_the_sleep_capped_at_5s(monkeypatch, tmp_path):
    """At most two retries; a long ``Retry-After`` waits 5s, then DataApiError."""
    sleeps: list[float] = []
    monkeypatch.setattr(
        "app.polymarket.data_api.time.sleep", lambda seconds: sleeps.append(seconds)
    )
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(503, headers={"Retry-After": "120"})] * 3)
    with pytest.raises(DataApiError):
        client.get_trades_v2(condition="0xcond", limit=5)
    assert sleeps == [5.0, 5.0]
    assert len(client._http.calls) == 3  # first attempt + two retries, no more


# ------------------------------------------- /v2/oi + /v2/holders (RFC-003)
def test_oi_unwraps_the_envelope_and_returns_the_value(tmp_path):
    """Live-checked shape: ``{"data": [{"condition_id", "value"}]}``."""
    client = _client(tmp_path)
    client._http = FakeHttp(
        [
            FakeResponse(
                200,
                {
                    "data": [
                        {"condition_id": "0xcond", "value": 117919.344026}
                    ]
                },
            )
        ]
    )
    assert client.get_oi("0xcond") == 117919.344026
    path, params = client._http.calls[0]
    assert path == "/v2/oi"
    assert params == {"condition": "0xcond"}
    assert "offset" not in params


def test_oi_empty_or_unparseable_is_none_never_zero(tmp_path):
    """No row, or a junk value, is reported missing — not 0 (never guessed)."""
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, {"data": []})])
    assert client.get_oi("0xcond") is None
    client._http = FakeHttp([FakeResponse(200, {"data": [{"value": "junk"}]})])
    assert client.get_oi("0xcond") is None


def test_holders_returns_the_outcome_groups_unchanged(tmp_path):
    """Live-checked shape: one group per outcome token, holders ranked."""
    payload = {
        "data": [
            {
                "token_id": "111",
                "holders": [
                    {"amount": 39999.98, "outcome_index": 0, "name": "whale"}
                ],
            }
        ],
        "pagination": {"limit": 100, "has_more": False},
    }
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, payload)])
    groups = client.get_holders("0xcond")
    assert groups == payload["data"]
    path, params = client._http.calls[0]
    assert path == "/v2/holders"
    assert params == {"condition": "0xcond"}


def test_holders_empty_data_is_zero_state(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, {"data": [], "pagination": {}})])
    assert client.get_holders("0xcond") == []
