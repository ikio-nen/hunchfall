"""POST /wallets and GET /wallets/{address}/activity (offline)."""

import json
import sqlite3

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from app.polymarket.data_api import DataApiError
from app.polygon.rpc import PolygonRpcError
from tests.fakes import make_settings

ADDRESS = "0x" + "aB" * 20  # mixed case, valid shape
PRIVATE_KEY = "0x" + "ab" * 32
MNEMONIC = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima"


class FakeWalletApi:
    """Data API stub for the wallet activity route."""

    instances = 0

    def __init__(self, settings, fail: bool = False) -> None:
        type(self).instances += 1
        self.fail = fail

    def get_activity_v2(self, **kwargs):
        if self.fail:
            raise DataApiError("data-api down")
        return [
            {
                "type": "TRADE",
                "side": "BUY",
                "size": 240.0,  # shares
                "usdc_size": 120.0,  # USD notional (the value the API reports)
                "price": 0.62,
                "timestamp": 1759224000,
                "title": "Will it rain tomorrow?",
                "transaction_hash": "0xtx",
            }
        ]

    def get_positions_v2(self, **kwargs):
        if self.fail:
            raise DataApiError("data-api down")
        return [
            {
                "condition_id": "0xcondition",
                "title": "Will it rain tomorrow?",
                "outcome": "Yes",
                "size": 0.0,  # legacy/guessed field — current_size must win
                "current_size": 250.0,
                "avg_price": 0.42,
                "current_price": 0.55,
                "realized_pnl": 12.5,
            }
        ]

    def close(self) -> None:
        pass


class FakeRpcClient:
    """Polygon RPC stub for the wallet activity route."""

    instances = 0

    def __init__(self, settings, fail: bool = False) -> None:
        type(self).instances += 1
        self.fail = fail

    def get_balance_wei(self, address: str) -> int:
        if self.fail:
            raise PolygonRpcError("rpc down")
        return 1234000000000000000

    def get_transaction_count(self, address: str) -> int:
        if self.fail:
            raise PolygonRpcError("rpc down")
        return 7

    def close(self) -> None:
        pass


def _client(tmp_path):
    settings = make_settings(tmp_path)
    return TestClient(create_app(settings)), settings


# ------------------------------------------------------------- POST /wallets
def test_wallet_happy_path_upsert(tmp_path):
    client, settings = _client(tmp_path)
    resp = client.post("/wallets", json={"address": ADDRESS, "label": "Ikio main"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is True
    assert body["wallet"]["address"] == ADDRESS.lower()
    assert body["wallet"]["watch_only"] is True
    assert "never accepts or stores keys" in body["note"]

    again = client.post("/wallets", json={"address": ADDRESS, "label": "Ikio main 2"})
    assert again.status_code == 200
    assert again.json()["created"] is False
    assert again.json()["wallet"]["label"] == "Ikio main 2"

    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    assert len(audit.query("wallet_registered")) == 2


def test_wallet_rejects_private_key_without_echo(tmp_path):
    client, settings = _client(tmp_path)
    resp = client.post("/wallets", json={"address": PRIVATE_KEY, "label": "nope"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "secret_rejected"
    # the submitted value is never echoed back
    assert PRIVATE_KEY not in resp.text
    assert PRIVATE_KEY[2:] not in resp.text
    with sqlite3.connect(resolve_db_path(settings.DATABASE_PATH)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM watch_wallets").fetchone()[0]
    assert count == 0
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    rejections = audit.query("wallet_secret_rejected")
    assert len(rejections) == 1
    assert PRIVATE_KEY not in json.dumps(rejections)


def test_wallet_rejects_mnemonic_label(tmp_path):
    client, settings = _client(tmp_path)
    resp = client.post("/wallets", json={"address": ADDRESS, "label": MNEMONIC})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "secret_rejected"
    assert MNEMONIC not in resp.text
    with sqlite3.connect(resolve_db_path(settings.DATABASE_PATH)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM watch_wallets").fetchone()[0]
    assert count == 0


def test_wallet_rejects_malformed_address(tmp_path):
    client, _settings = _client(tmp_path)
    resp = client.post("/wallets", json={"address": "0x123", "label": "fine"})
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "invalid_address"


# ------------------------------------------------- GET /wallets/{addr}/activity
def test_wallet_activity_happy_path(tmp_path, monkeypatch):
    client, settings = _client(tmp_path)
    client.post("/wallets", json={"address": ADDRESS, "label": "Ikio main"})
    monkeypatch.setattr("app.wallets.DataApiClient", FakeWalletApi)
    monkeypatch.setattr("app.wallets.PolygonRpcClient", FakeRpcClient)

    resp = client.get(f"/wallets/{ADDRESS}/activity")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "PAPER"
    assert body["wallet"]["watch_only"] is True
    assert body["sources"] == ["polymarket-data-api-v2", "polygon-rpc"]
    assert body["degraded"] == []
    assert body["chain"]["balance_pol"] == 1.234
    assert body["chain"]["nonce"] == 7
    assert body["activity"][0]["type"] == "TRADE"
    assert body["activity"][0]["side"] == "BUY"
    assert body["activity"][0]["size_usd"] == 120.0  # not 240.0 shares
    assert body["activity"][0]["tx_hash"] == "0xtx"
    assert body["positions"][0]["size"] == 250.0  # not the 0.0 legacy size
    assert body["positions"][0]["pnl"] == 12.5
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    assert len(audit.query("wallet_activity_request")) == 1


def test_wallet_activity_unregistered_404_without_outbound_calls(tmp_path, monkeypatch):
    client, _settings = _client(tmp_path)
    FakeWalletApi.instances = 0
    FakeRpcClient.instances = 0
    monkeypatch.setattr("app.wallets.DataApiClient", FakeWalletApi)
    monkeypatch.setattr("app.wallets.PolygonRpcClient", FakeRpcClient)
    resp = client.get(f"/wallets/{ADDRESS}/activity")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "wallet_not_registered"
    assert FakeWalletApi.instances == 0
    assert FakeRpcClient.instances == 0


def test_wallet_activity_partial_failure_degrades(tmp_path, monkeypatch):
    client, _settings = _client(tmp_path)
    client.post("/wallets", json={"address": ADDRESS, "label": "Ikio main"})
    monkeypatch.setattr("app.wallets.DataApiClient", FakeWalletApi)
    monkeypatch.setattr(
        "app.wallets.PolygonRpcClient", lambda s: FakeRpcClient(s, fail=True)
    )
    resp = client.get(f"/wallets/{ADDRESS}/activity")
    assert resp.status_code == 200
    body = resp.json()
    assert "polygon-rpc" in body["degraded"]
    assert body["chain"] is None
    assert body["activity"]  # data-api side still present


def test_wallet_activity_total_failure_is_502(tmp_path, monkeypatch):
    client, _settings = _client(tmp_path)
    client.post("/wallets", json={"address": ADDRESS, "label": "Ikio main"})
    monkeypatch.setattr("app.wallets.DataApiClient", lambda s: FakeWalletApi(s, fail=True))
    monkeypatch.setattr(
        "app.wallets.PolygonRpcClient", lambda s: FakeRpcClient(s, fail=True)
    )
    resp = client.get(f"/wallets/{ADDRESS}/activity")
    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "upstream_error"


def test_wallet_activity_secret_like_path_is_400(tmp_path):
    client, _settings = _client(tmp_path)
    resp = client.get(f"/wallets/{PRIVATE_KEY}/activity")
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "secret_rejected"
    assert PRIVATE_KEY not in resp.text
