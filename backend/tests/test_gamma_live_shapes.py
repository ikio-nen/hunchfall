"""Gamma shapes that only a live check can settle — now pinned.

Live-checked 2026-10-01 against the real API:

1. ``GET /markets/{condition_id}`` is **rejected** ("id is invalid"), while
   ``GET /markets?condition_ids=<id>`` returns the matching market. The
   prediction path used to send a raw condition id to ``/markets/{id}``, so
   ``POST /predict {"condition_id": …}`` could never have worked.
2. ``clobTokenIds`` arrives as a **JSON array encoded as a string**
   (``'["3233…", "2565…"]'``), not as a bare comma-separated list. Splitting
   it on "," produced ids wrapped in ``["`` … ``"]``; CLOB then answered
   ``{"error": …}`` with no bids, breaking every book-dependent path.

Both bugs were invisible offline because the fixture mirrored the guessed
shape. The fixture now uses the real shape and these tests pin the parsers.
"""

from __future__ import annotations

from app.loop import _market_tokens
from app.polymarket.gamma import GammaClient, parse_token_ids
from tests.fakes import make_settings

#: The real ``clobTokenIds`` value for one live market (Xi out before 2027).
LIVE_YES = "32338220190071351435772801779725302244575775216413325951443816017994629993401"
LIVE_NO = "25659310674993675562345759665114759892400026242514633218387667107987341231962"


class FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")

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


def _client(tmp_path) -> GammaClient:
    return GammaClient(make_settings(tmp_path))


# ------------------------------------------------------- condition-id lookup
CID = "0xc7528fbf34f6690317786bb55ba14f6af6a388721f34cbecedaf0d1254e34e8e"


def _row(slug: str, condition_id: str = CID) -> dict:
    return {"slug": slug, "conditionId": condition_id, "question": slug}


def test_condition_id_lookup_uses_the_condition_ids_filter(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, [_row("will-it-rain")])])
    market = client.get_market_by_condition_id(CID)
    path, params = client._http.calls[0]
    assert path == "/markets"  # NOT "/markets/0xc752…" — that form is rejected
    assert params == {"condition_ids": CID, "limit": 1}
    assert market["slug"] == "will-it-rain"
    assert len(client._http.calls) == 1  # open-market hit needs no retry


def test_condition_id_lookup_retries_with_closed_for_a_resolved_market(tmp_path):
    """Live 2026-10-01: the filter returns open markets only by default."""
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, []), FakeResponse(200, [_row("settled")])])
    assert client.get_market_by_condition_id(CID)["slug"] == "settled"
    assert client._http.calls[1][1] == {
        "condition_ids": CID,
        "limit": 1,
        "closed": "true",
    }


def test_condition_id_lookup_lowercases_before_querying(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, [_row("lower")])])
    assert client.get_market_by_condition_id(CID.upper())["slug"] == "lower"
    assert client._http.calls[0][1]["condition_ids"] == CID


def test_condition_id_lookup_rejects_a_non_matching_row(tmp_path):
    """Gamma ignores unknown params and serves a default page (see market_ids).

    A row whose ``conditionId`` is not what we asked for must never be used as
    a prediction target.
    """
    client = _client(tmp_path)
    client._http = FakeHttp(
        [
            FakeResponse(200, [_row("someone-else", "0x" + "9" * 64)]),
            FakeResponse(200, [_row("someone-else", "0x" + "9" * 64)]),
        ]
    )
    assert client.get_market_by_condition_id(CID) == {}


def test_condition_id_lookup_returns_empty_when_nothing_matches(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, []), FakeResponse(200, [])])
    assert client.get_market_by_condition_id(CID) == {}


def test_condition_id_lookup_skips_http_for_an_empty_id(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, [_row("nope")])])
    assert client.get_market_by_condition_id("   ") == {}
    assert client._http.calls == []


def test_condition_id_lookup_accepts_a_data_envelope(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, {"data": [_row("wrapped")]})])
    assert client.get_market_by_condition_id(CID)["slug"] == "wrapped"


def test_market_by_numeric_id_still_uses_the_path_form(tmp_path):
    client = _client(tmp_path)
    client._http = FakeHttp([FakeResponse(200, {"slug": "numeric"})])
    assert client.get_market("559651")["slug"] == "numeric"
    assert client._http.calls[0][0] == "/markets/559651"


# ---------------------------------------------------------- clobTokenIds
def test_parse_token_ids_reads_the_real_json_array_string():
    ids = parse_token_ids({"clobTokenIds": f'["{LIVE_YES}", "{LIVE_NO}"]'})
    assert ids == [LIVE_YES, LIVE_NO]


def test_parse_token_ids_never_returns_brackets_quotes_or_spaces():
    """Regression: the comma split produced ``'["3233…"'`` for every id."""
    for token_id in parse_token_ids({"clobTokenIds": '["111", "222"]'}):
        assert not set(token_id) & set('[]"\' ')


def test_parse_token_ids_keeps_accepting_legacy_shapes():
    assert parse_token_ids({"clobTokenIds": "111,222"}) == ["111", "222"]
    assert parse_token_ids({"clobTokenIds": ["111", "222"]}) == ["111", "222"]


def test_parse_token_ids_fails_closed_on_missing_or_broken_values():
    assert parse_token_ids({}) == []
    assert parse_token_ids({"clobTokenIds": None}) == []
    assert parse_token_ids({"clobTokenIds": "[not json"}) == []


def test_loop_market_tokens_shares_the_same_parser():
    market = {"clobTokenIds": '["111", "222"]', "outcomes": '["Yes", "No"]'}
    assert _market_tokens(market) == ("111", "222")
    assert _market_tokens({"clobTokenIds": "[not json"}) is None
