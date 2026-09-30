"""GET /marketplaces — happy counts + empty + legacy-default grouping."""

from fastapi.testclient import TestClient

from app.api.server import create_app
from app.memory.audit import AuditLog
from app.paths import resolve_db_path
from tests.fakes import make_settings


def _client(tmp_path):
    settings = make_settings(tmp_path)
    return TestClient(create_app(settings)), settings


def test_marketplaces_counts(tmp_path):
    client, settings = _client(tmp_path)
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    audit.record("scan", {"marketplace": "polymarket", "slug": "a"})
    audit.record("scan", {"marketplace": "polymarket", "slug": "b"})
    audit.record("signal", {"marketplace": "polymarket", "question": "q1"})
    audit.record("signal", {"marketplace": "polymarket", "question": "q2"})
    audit.record(
        "validation", {"marketplace": "polymarket", "signal_ref": "x", "outcome": "HIT"}
    )

    body = client.get("/marketplaces").json()
    assert body["mode"] == "PAPER"
    assert len(body["marketplaces"]) == 1
    row = body["marketplaces"][0]
    assert row["name"] == "polymarket"
    assert row["scans"] == 2
    assert row["hunches"] == 2
    assert row["validated"] == 1
    assert row["pending_validations"] == 1
    assert row["last_scan_at"]


def test_marketplaces_empty_db_is_honestly_empty(tmp_path):
    client, _settings = _client(tmp_path)
    body = client.get("/marketplaces").json()
    assert body["marketplaces"] == []
    assert body["label"] == "paper predictions only"


def test_marketplaces_legacy_default_and_custom_names(tmp_path):
    client, settings = _client(tmp_path)
    audit = AuditLog(resolve_db_path(settings.DATABASE_PATH))
    audit.record("signal", {"question": "legacy loop signal — no marketplace"})
    audit.record("scan", {"marketplace": "kalshi", "slug": "k"})

    rows = {
        row["name"]: row
        for row in client.get("/marketplaces").json()["marketplaces"]
    }
    assert rows["polymarket"]["hunches"] == 1  # legacy defaulted, never dropped
    assert rows["kalshi"]["scans"] == 1  # unknown names preserved verbatim
