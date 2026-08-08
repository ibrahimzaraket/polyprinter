"""prune.py against a real (temp-file) db — the operator's explicit choice
to drop everything about a never-mirrored, lifetime-unprofitable trader,
without ever touching anyone who's actually been watched/mandated (see
prune.py's module docstring for the full rationale and the audit guard).
"""

from datetime import datetime, timezone

from polyprinter.db.conn import get_connection
from polyprinter.scout import prune

ADDRESS = "0xtrader"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_db(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, _now()),
    )
    return conn


def test_is_lifetime_profitable():
    assert prune.is_lifetime_profitable(1.0) is True
    assert prune.is_lifetime_profitable(0.0) is False
    assert prune.is_lifetime_profitable(-5.0) is False
    assert prune.is_lifetime_profitable(None) is False  # unknown != profitable


def test_has_been_acted_upon_false_for_untouched_trader(tmp_path):
    conn = _make_db(tmp_path)
    assert prune.has_been_acted_upon(conn, ADDRESS) is False


def test_has_been_acted_upon_true_with_a_mandate(tmp_path):
    conn = _make_db(tmp_path)
    conn.execute(
        """
        INSERT INTO mandates (address, version, verdict, confidence, issued_at, expires_at, reasoning)
        VALUES (?, 1, 'SKIP', 'HIGH', ?, ?, 'x')
        """,
        (ADDRESS, _now(), _now()),
    )
    assert prune.has_been_acted_upon(conn, ADDRESS) is True


def test_has_been_acted_upon_true_with_an_observed_trade(tmp_path):
    conn = _make_db(tmp_path)
    conn.execute(
        """
        INSERT INTO observed_trades (
            address, tx_hash, log_index, market_id, token_id, side, shares,
            price, block_ts, detected_at, source
        ) VALUES (?, '0xa', 0, 'm1', 'tok', 'BUY', 10, 0.5, ?, ?, 'poll')
        """,
        (ADDRESS, _now(), _now()),
    )
    assert prune.has_been_acted_upon(conn, ADDRESS) is True


def test_purge_trader_deletes_snapshots_raw_responses_and_trader_row(tmp_path):
    conn = _make_db(tmp_path)
    conn.execute(
        "INSERT INTO trader_snapshots (address, scanned_at, roi_raw) VALUES (?, ?, -0.1)",
        (ADDRESS, _now()),
    )
    conn.execute(
        "INSERT INTO raw_responses (source, url, fetched_at, status, body, body_hash) "
        "VALUES ('data-api', ?, ?, 200, '[]', 'h1')",
        (f"https://data-api.polymarket.com/activity?user={ADDRESS}&limit=500", _now()),
    )
    # A leaderboard response mentioning many traders (including this one in
    # its body, incidentally) must NOT be deleted — it can't be attributed
    # to one address and isn't the storage problem this exists to fix.
    conn.execute(
        "INSERT INTO raw_responses (source, url, fetched_at, status, body, body_hash) "
        "VALUES ('data-api', 'https://data-api.polymarket.com/v1/leaderboard?timePeriod=DAY', ?, 200, ?, 'h2')",
        (_now(), f'[{{"proxyWallet": "{ADDRESS}"}}]'),
    )
    conn.commit()

    prune.purge_trader(conn, ADDRESS)

    assert conn.execute("SELECT COUNT(*) AS n FROM traders WHERE address = ?", (ADDRESS,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM trader_snapshots WHERE address = ?", (ADDRESS,)).fetchone()["n"] == 0
    remaining = conn.execute("SELECT url FROM raw_responses").fetchall()
    assert len(remaining) == 1
    assert "leaderboard" in remaining[0]["url"]


def test_purge_trader_does_not_touch_a_different_address(tmp_path):
    conn = _make_db(tmp_path)
    other = "0xother"
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (other, _now()),
    )
    conn.execute(
        "INSERT INTO raw_responses (source, url, fetched_at, status, body, body_hash) VALUES "
        "('data-api', ?, ?, 200, '[]', 'h1'), ('data-api', ?, ?, 200, '[]', 'h2')",
        (
            f"https://data-api.polymarket.com/activity?user={ADDRESS}",
            _now(),
            f"https://data-api.polymarket.com/activity?user={other}",
            _now(),
        ),
    )
    conn.commit()

    prune.purge_trader(conn, ADDRESS)

    assert conn.execute("SELECT COUNT(*) AS n FROM traders WHERE address = ?", (other,)).fetchone()["n"] == 1
    remaining = conn.execute("SELECT url FROM raw_responses").fetchall()
    assert len(remaining) == 1
    assert other in remaining[0]["url"]
