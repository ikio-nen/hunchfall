"""Batched ``/v2/trades`` and CLOB batch price methods (docs upgrades).

Live-checked 2026-10-02:

* ``/v2/trades?condition=a,b`` accepts up to 20 comma-separated condition ids
  and interleaves their rows — so the batched path splits rows client-side by
  their own ``condition_id``, and drops any row whose id was not requested.
* The CLOB batch endpoints (``/midpoints``, ``/prices``, ``/books``) reject
  **every** input on this deployment with ``{"error": "Invalid payload"}`` —
  including the docs' own example — so ``get_midpoints`` / ``get_prices``
  attempt the batch once and fall back to per-token calls.

Everything here is offline: the fake HTTP layer replays canned responses and
records the exact query params each client sent.
"""

from __future__ import annotations

from app.polymarket.clob import ClobClient
from app.polymarket.data_api import DataApiClient
from tests.fakes import make_settings


class FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.request = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeHttp:
    """Captures calls and replays canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.responses.pop(0)


# --------------------------------------------------------- /v2/trades batching
def _rows_for(condition: str, n: int = 2) -> list[dict]:
    return [
        {
            "condition_id": condition,
            "price": 0.5,
            "size": 10.0,
            "side": "BUY",
            "timestamp": 1000 + i,
            "token_id": "111",
        }
        for i in range(n)
    ]


def test_trades_many_splits_rows_by_their_own_condition_id(tmp_path):
    client = DataApiClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [
            FakeResponse(
                200,
                {
                    "data": _rows_for("0xa") + _rows_for("0xb", 3),
                    "pagination": {"next_cursor": None},
                },
            )
        ]
    )
    out = client.get_trades_v2_many(["0xa", "0xb"])
    assert {key: len(rows) for key, rows in out.items()} == {"0xa": 2, "0xb": 3}
    # one HTTP call for both markets, with both ids comma-joined
    assert len(client._http.calls) == 1
    path, params = client._http.calls[0]
    assert path == "/v2/trades"
    assert params["condition"] == "0xa,0xb"


def test_trades_many_drops_unrequested_rows(tmp_path):
    """A silently-ignored filter must never look like a match."""
    client = DataApiClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [
            FakeResponse(
                200,
                {"data": _rows_for("0xa") + _rows_for("0xZZZ", 5), "pagination": {}},
            )
        ]
    )
    out = client.get_trades_v2_many(["0xa"])
    assert list(out) == ["0xa"]
    assert len(out["0xa"]) == 2  # the unrequested 0xZZZ rows are gone


def test_trades_many_keeps_a_zero_state_key_for_every_market(tmp_path):
    client = DataApiClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [FakeResponse(200, {"data": [], "pagination": {}})]
    )
    out = client.get_trades_v2_many(["0xa", "0xb"])
    assert out == {"0xa": [], "0xb": []}


def test_trades_many_chunks_at_twenty_conditions(tmp_path):
    """The docs cap a call at 20 distinct ids; 21 ids must be two calls."""
    client = DataApiClient(make_settings(tmp_path))
    ids = [f"0x{i:02d}" for i in range(21)]
    client._http = FakeHttp(
        [
            FakeResponse(200, {"data": [], "pagination": {}}),
            FakeResponse(200, {"data": [], "pagination": {}}),
        ]
    )
    client.get_trades_v2_many(ids)
    assert len(client._http.calls) == 2
    first = client._http.calls[0][1]["condition"].split(",")
    second = client._http.calls[1][1]["condition"].split(",")
    assert len(first) == 20
    assert len(second) == 1
    assert set(first) | set(second) == set(ids)


def test_trades_many_collapses_duplicates_and_skips_empty(tmp_path):
    client = DataApiClient(make_settings(tmp_path))
    client._http = FakeHttp([FakeResponse(200, {"data": [], "pagination": {}})])
    out = client.get_trades_v2_many(["0xa", "0xa", "", "0xb"])
    assert list(out) == ["0xa", "0xb"]
    assert client._http.calls[0][1]["condition"] == "0xa,0xb"


def test_trades_many_with_no_conditions_makes_no_call(tmp_path):
    client = DataApiClient(make_settings(tmp_path))
    client._http = FakeHttp([])
    assert client.get_trades_v2_many([]) == {}
    assert client._http.calls == []


def test_single_trade_tape_can_ask_for_cash_rows(tmp_path):
    """filter_type is sent when asked for (documented CASH|TOKENS)."""
    client = DataApiClient(make_settings(tmp_path))
    client._http = FakeHttp([FakeResponse(200, {"data": [], "pagination": {}})])
    client.get_trades_v2(condition="0xa", limit=5, filter_type="CASH")
    assert client._http.calls[0][1]["filter_type"] == "CASH"


# ------------------------------------------------------------- retry contract
def test_retryable_false_is_not_retried_and_carries_the_trace_id(tmp_path):
    """The v2 error body's `retryable` flag is honored, and trace_id is kept."""
    from app.polymarket.data_api import DataApiError

    client = DataApiClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [
            FakeResponse(
                503,
                {"error": "nope", "code": "dependency_unavailable", "retryable": False, "trace_id": "tr-1"},
                headers={"Retry-After": "1"},
            )
        ]
    )
    try:
        client.get_trades_v2(condition="0xa")
        raise AssertionError("expected DataApiError")
    except DataApiError as exc:
        assert exc.retryable is False
        assert exc.trace_id == "tr-1"
        assert "tr-1" in str(exc)
    # non-retryable: exactly one attempt, no sleeping
    assert len(client._http.calls) == 1


def test_missing_retryable_flag_keeps_the_status_based_retry(tmp_path, monkeypatch):
    """An unreadable body must not silently stop retrying 429/503."""
    import app.polymarket.data_api as data_api_module

    sleeps: list[float] = []
    monkeypatch.setattr(data_api_module.time, "sleep", lambda s: sleeps.append(s))
    client = DataApiClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [
            FakeResponse(429, {"error": "busy"}, headers={"Retry-After": "2"}),
            FakeResponse(200, {"data": [], "pagination": {}}),
        ]
    )
    assert client.get_trades_v2(condition="0xa") == []
    assert sleeps == [2.0]
    assert len(client._http.calls) == 2


# ------------------------------------------------------------- CLOB batching
def test_midpoints_falls_back_per_token_when_the_batch_is_rejected(tmp_path):
    """Live 2026-10-02: /midpoints answers `Invalid payload` for every input."""
    client = ClobClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [
            FakeResponse(200, {"error": "Invalid payload"}),  # the batch call
            FakeResponse(200, {"mid": "0.25"}),  # per-token fallback
            FakeResponse(200, {"mid": "0.75"}),
        ]
    )
    out = client.get_midpoints(["111", "222"])
    assert out == {"111": 0.25, "222": 0.75}
    assert [path for path, _ in client._http.calls] == [
        "/midpoints",
        "/midpoint",
        "/midpoint",
    ]


def test_midpoints_uses_the_batch_when_it_works(tmp_path):
    client = ClobClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [FakeResponse(200, {"111": "0.25", "222": "0.75"})]
    )
    out = client.get_midpoints(["111", "222"])
    assert out == {"111": 0.25, "222": 0.75}
    assert len(client._http.calls) == 1
    assert client._http.calls[0][1] == {"token_ids": "111,222"}


def test_midpoint_parses_the_real_mid_key(tmp_path):
    """Live 2026-10-02: the payload is {"mid": "0.0255"} — not `midpoint`."""
    client = ClobClient(make_settings(tmp_path))
    client._http = FakeHttp([FakeResponse(200, {"mid": "0.0255"})])
    assert client.get_midpoint("111") == 0.0255


def test_midpoint_raises_a_typed_error_on_an_unusable_payload(tmp_path):
    from app.polymarket.clob import ClobError

    client = ClobClient(make_settings(tmp_path))
    client._http = FakeHttp([FakeResponse(200, {})])
    try:
        client.get_midpoint("111")
        raise AssertionError("expected ClobError")
    except ClobError as exc:
        assert "no price" in str(exc)


def test_prices_batch_sends_aligned_sides_and_falls_back(tmp_path):
    client = ClobClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [
            FakeResponse(200, {"error": "Invalid payload"}),
            FakeResponse(200, {"price": "0.26"}),
            FakeResponse(200, {"price": "0.27"}),
        ]
    )
    out = client.get_prices(["111", "222"], side="BUY")
    assert out == {"111": 0.26, "222": 0.27}
    assert client._http.calls[0] == (
        "/prices",
        {"token_ids": "111,222", "sides": "BUY,BUY"},
    )


def test_prices_batch_parses_the_nested_map_shape(tmp_path):
    client = ClobClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [FakeResponse(200, {"111": {"BUY": 0.26}, "222": {"BUY": 0.27}})]
    )
    assert client.get_prices(["111", "222"]) == {"111": 0.26, "222": 0.27}


def test_batch_helpers_with_no_tokens_make_no_call(tmp_path):
    client = ClobClient(make_settings(tmp_path))
    client._http = FakeHttp([])
    assert client.get_midpoints([]) == {}
    assert client.get_prices([]) == {}
    assert client._http.calls == []


def test_prices_history_uses_the_market_key(tmp_path):
    """Live 2026-10-02: `token_id=` returns an empty history; `market=` works."""
    client = ClobClient(make_settings(tmp_path))
    client._http = FakeHttp(
        [FakeResponse(200, {"history": [{"t": 1, "p": 0.5}]})]
    )
    history = client.get_prices_history("111", interval="1h")
    assert history == [{"t": 1, "p": 0.5}]
    path, params = client._http.calls[0]
    assert path == "/prices-history"
    assert params["market"] == "111"
    assert "token_id" not in params
