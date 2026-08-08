"""dashboard/server.py's new write routes (2026-08-08) — Flask's test
client, a real temp-file db. Read routes are exercised live by
driver.sh; these are the ones that changed the "dashboard is read-only"
invariant on purpose, so they get their own direct coverage.
"""

from datetime import datetime, timezone

import pytest

from polyprinter.db.conn import get_connection

ADDRESS = "0xtrader"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import polyprinter.config as config_module
    from polyprinter.dashboard import server

    db_path = tmp_path / "test.db"
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, _now()),
    )
    conn.execute(
        "INSERT INTO trader_snapshots (address, scanned_at, roi_shrunk) VALUES (?, ?, 0.2)",
        (ADDRESS, _now()),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(server, "get_connection", lambda *a, **k: get_connection(db_path))
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", tmp_path / "config-overrides.yaml")

    server.app.config["TESTING"] = True
    with server.app.test_client() as c:
        yield c


def _query(client, sql, params=()):
    from polyprinter.dashboard import server

    conn = server.get_connection()
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


def test_tail_pins_and_redirects(client):
    import polyprinter.config_write as config_write

    resp = client.post(f"/traders/{ADDRESS}/tail")

    assert resp.status_code == 302
    assert resp.headers["Location"] == f"/traders/{ADDRESS}"
    assert config_write.is_pinned(ADDRESS) is True


def test_tail_unknown_address_404s(client):
    resp = client.post("/traders/0xneverseen/tail")
    assert resp.status_code == 404


def test_untail_unpins_and_revokes_mandate(client):
    client.post(f"/traders/{ADDRESS}/tail")
    client.post(f"/traders/{ADDRESS}/mandate", data={"size_multiplier": "1.0"})

    resp = client.post(f"/traders/{ADDRESS}/untail")

    assert resp.status_code == 302
    import polyprinter.config_write as config_write
    assert config_write.is_pinned(ADDRESS) is False
    mandate = _query(client, "SELECT * FROM mandates WHERE address = ? AND superseded_by IS NULL", (ADDRESS,))
    assert mandate["expires_at"] < _now()  # revoked, not active


def test_mandate_requires_tailing_first(client):
    resp = client.post(f"/traders/{ADDRESS}/mandate", data={"size_multiplier": "1.0"})
    assert resp.status_code == 400


def test_mandate_issues_operator_mandate(client):
    client.post(f"/traders/{ADDRESS}/tail")

    resp = client.post(f"/traders/{ADDRESS}/mandate", data={"size_multiplier": "2.0", "fast_lane": "on"})

    assert resp.status_code == 302
    mandate = _query(client, "SELECT * FROM mandates WHERE address = ? AND superseded_by IS NULL", (ADDRESS,))
    assert mandate["issued_by"] == "operator"
    assert mandate["size_multiplier"] == 2.0
    assert mandate["fast_lane"] == 1
    assert mandate["verdict"] == "FOLLOW"


def test_mandate_rejects_non_numeric_multiplier(client):
    client.post(f"/traders/{ADDRESS}/tail")
    resp = client.post(f"/traders/{ADDRESS}/mandate", data={"size_multiplier": "not-a-number"})
    assert resp.status_code == 400


def test_mandate_revoke(client):
    client.post(f"/traders/{ADDRESS}/tail")
    client.post(f"/traders/{ADDRESS}/mandate", data={"size_multiplier": "1.0"})

    resp = client.post(f"/traders/{ADDRESS}/mandate/revoke")

    assert resp.status_code == 302
    mandate = _query(client, "SELECT * FROM mandates WHERE address = ? AND superseded_by IS NULL", (ADDRESS,))
    assert mandate["expires_at"] < _now()


def test_analyze_without_openrouter_configured_returns_503(client, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)

    resp = client.post(f"/traders/{ADDRESS}/analyze")

    assert resp.status_code == 503


def test_analyze_unknown_trader_404s(client):
    resp = client.post("/traders/0xneverseen/analyze")
    assert resp.status_code == 404
