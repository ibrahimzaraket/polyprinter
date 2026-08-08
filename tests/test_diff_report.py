"""diff_report.py against a real (temp-file) db — Phase 4's exit criterion
("diffs clean against phase 2 for 72h") made into something checkable.
"""

import json
from datetime import datetime, timedelta, timezone

from polyprinter.db.conn import get_connection
from polyprinter.mirror import diff_report, watch_events

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


def _log_chain_detection(conn, *, tx_hash, token_id, side, shares, price):
    conn.execute(
        "INSERT INTO events (ts, service, level, message, context_json) VALUES (?, 'mirror', 'INFO', ?, ?)",
        (
            _now(),
            watch_events.DETECTED_MESSAGE,
            json.dumps(
                {"maker": ADDRESS, "tx_hash": tx_hash, "token_id": token_id, "side": side, "shares": shares, "price": price}
            ),
        ),
    )


def _insert_poll_trade(conn, *, tx_hash, token_id, side, shares, price):
    conn.execute(
        """
        INSERT INTO observed_trades (
            address, tx_hash, log_index, market_id, token_id, side, shares,
            price, block_ts, detected_at, source
        ) VALUES (?, ?, 0, 'm1', ?, ?, ?, ?, ?, ?, 'poll')
        """,
        (ADDRESS, tx_hash, token_id, side, shares, price, _now(), _now()),
    )


def test_matched_trade_appears_in_matched_not_either_only_list(tmp_path):
    conn = _make_db(tmp_path)
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _log_chain_detection(conn, tx_hash="0xa", token_id="tok1", side="BUY", shares=10.0, price=0.5)
    _insert_poll_trade(conn, tx_hash="0xa", token_id="tok1", side="BUY", shares=10.0, price=0.5)

    result = diff_report.compute_diff(conn, since_iso=since)

    assert result["n_chain"] == 1
    assert result["n_poll"] == 1
    assert result["n_matched"] == 1
    assert result["chain_only"] == []
    assert result["poll_only"] == []


def test_chain_only_trade_is_flagged(tmp_path):
    conn = _make_db(tmp_path)
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _log_chain_detection(conn, tx_hash="0xb", token_id="tok1", side="BUY", shares=10.0, price=0.5)

    result = diff_report.compute_diff(conn, since_iso=since)

    assert result["n_matched"] == 0
    assert len(result["chain_only"]) == 1
    assert result["poll_only"] == []


def test_poll_only_trade_is_flagged(tmp_path):
    conn = _make_db(tmp_path)
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _insert_poll_trade(conn, tx_hash="0xc", token_id="tok1", side="SELL", shares=5.0, price=0.6)

    result = diff_report.compute_diff(conn, since_iso=since)

    assert result["n_matched"] == 0
    assert result["chain_only"] == []
    assert len(result["poll_only"]) == 1


def test_matched_trade_with_different_price_still_matches_but_reports_both(tmp_path):
    """A small rounding difference between the two sources' own computation
    paths isn't a real diff-harness failure — it's still reported inside
    `matched` for a human to glance at, not misfiled as a miss.
    """
    conn = _make_db(tmp_path)
    since = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _log_chain_detection(conn, tx_hash="0xd", token_id="tok1", side="BUY", shares=10.0, price=0.5001)
    _insert_poll_trade(conn, tx_hash="0xd", token_id="tok1", side="BUY", shares=10.0, price=0.5)

    result = diff_report.compute_diff(conn, since_iso=since)

    assert result["n_matched"] == 1
    assert result["matched"][0]["chain_price"] == 0.5001
    assert result["matched"][0]["poll_price"] == 0.5


def test_events_before_since_are_excluded(tmp_path):
    conn = _make_db(tmp_path)
    _log_chain_detection(conn, tx_hash="0xold", token_id="tok1", side="BUY", shares=1.0, price=0.5)
    since = datetime.now(timezone.utc).isoformat()  # after the row above

    result = diff_report.compute_diff(conn, since_iso=since)

    assert result["n_chain"] == 0
