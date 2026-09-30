"""Data API v2 client units: envelope, no-offset, Retry-After, zero-state."""

import httpx

from app.polymarket.data_api import DataApiClient
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
