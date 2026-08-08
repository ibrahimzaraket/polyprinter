"""execute.py against a real (temp-file) db — the write side decide.py
deliberately doesn't do itself (see execute.py's module docstring for the
gap this closes).
"""

from datetime import datetime, timezone

import pytest

from polyprinter.db.conn import get_connection
from polyprinter.mirror import execute

ADDRESS = "0xtrader"
MODE = "paper"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_db(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, _now()),
    )
    return conn


def _insert_trade(conn, *, side="BUY", shares=10.0, price=0.5, market_id="m1", token_id="tok"):
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO observed_trades (
            address, tx_hash, log_index, market_id, token_id, side, shares,
            price, block_ts, detected_at, source
        ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 'poll')
        """,
        (ADDRESS, f"0x{market_id}{token_id}{side}{shares}{price}", market_id, token_id, side, shares, price, now, now),
    )
    return conn.execute("SELECT * FROM observed_trades WHERE id = ?", (cur.lastrowid,)).fetchone()


def _insert_decision(conn, trade_id, *, verdict="TAKE", size_usd=None):
    cur = conn.execute(
        "INSERT INTO decisions (observed_trade_id, decided_at, verdict, size_usd, mode) VALUES (?, ?, ?, ?, ?)",
        (trade_id, _now(), verdict, size_usd, MODE),
    )
    return cur.lastrowid


def test_open_position_books_shares_and_cost(tmp_path):
    conn = _make_db(tmp_path)
    trade = _insert_trade(conn, price=0.5)
    decision_id = _insert_decision(conn, trade["id"], size_usd=50.0)

    position_id = execute.open_position(conn, decision_id=decision_id, trade=trade, size_usd=50.0, mode=MODE)

    position = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    assert position["shares_open"] == pytest.approx(100.0)  # $50 / $0.50
    assert position["shares_total"] == pytest.approx(100.0)
    assert position["our_entry_price"] == pytest.approx(0.5)
    assert position["their_entry_price"] == pytest.approx(0.5)
    assert position["cost_usd"] == pytest.approx(51.0)  # $50 notional + 2% fee
    assert position["status"] == "OPEN"


def test_open_position_is_noop_for_zero_size(tmp_path):
    conn = _make_db(tmp_path)
    trade = _insert_trade(conn, price=0.5)
    decision_id = _insert_decision(conn, trade["id"], size_usd=0.0)

    result = execute.open_position(conn, decision_id=decision_id, trade=trade, size_usd=0.0, mode=MODE)

    assert result is None
    assert conn.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"] == 0


def test_record_exit_partial_leaves_position_open(tmp_path):
    conn = _make_db(tmp_path)
    entry_trade = _insert_trade(conn, side="BUY", price=0.5)
    entry_decision_id = _insert_decision(conn, entry_trade["id"], size_usd=50.0)
    position_id = execute.open_position(conn, decision_id=entry_decision_id, trade=entry_trade, size_usd=50.0, mode=MODE)
    position = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()

    exit_trade = _insert_trade(conn, side="SELL", price=0.6)
    exit_decision_id = _insert_decision(conn, exit_trade["id"], verdict="MIRROR_EXIT")
    execute.record_exit(conn, decision_id=exit_decision_id, position=position, trade=exit_trade, fraction=0.4)

    updated = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    assert updated["shares_open"] == pytest.approx(60.0)  # 100 - 40%
    assert updated["status"] == "PARTIAL"
    assert updated["closed_at"] is None

    exits = conn.execute("SELECT * FROM position_exits WHERE position_id = ?", (position_id,)).fetchall()
    assert len(exits) == 1
    assert exits[0]["shares_sold"] == pytest.approx(40.0)
    assert exits[0]["fraction_of_theirs"] == pytest.approx(0.4)
    assert exits[0]["proceeds_usd"] == pytest.approx(40 * 0.6 * 0.98)  # notional minus 2% fee


def test_record_exit_full_closes_position(tmp_path):
    conn = _make_db(tmp_path)
    entry_trade = _insert_trade(conn, side="BUY", price=0.5)
    entry_decision_id = _insert_decision(conn, entry_trade["id"], size_usd=50.0)
    position_id = execute.open_position(conn, decision_id=entry_decision_id, trade=entry_trade, size_usd=50.0, mode=MODE)
    position = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()

    exit_trade = _insert_trade(conn, side="SELL", price=0.6)
    exit_decision_id = _insert_decision(conn, exit_trade["id"], verdict="MIRROR_EXIT")
    execute.record_exit(conn, decision_id=exit_decision_id, position=position, trade=exit_trade, fraction=1.0)

    updated = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    assert updated["shares_open"] == pytest.approx(0.0)
    assert updated["status"] == "CLOSED"
    assert updated["closed_at"] is not None


def test_record_exit_is_noop_for_dust_fraction(tmp_path):
    conn = _make_db(tmp_path)
    entry_trade = _insert_trade(conn, side="BUY", price=0.5)
    entry_decision_id = _insert_decision(conn, entry_trade["id"], size_usd=50.0)
    position_id = execute.open_position(conn, decision_id=entry_decision_id, trade=entry_trade, size_usd=50.0, mode=MODE)
    position = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()

    exit_trade = _insert_trade(conn, side="SELL", price=0.6)
    exit_decision_id = _insert_decision(conn, exit_trade["id"], verdict="MIRROR_EXIT")
    execute.record_exit(conn, decision_id=exit_decision_id, position=position, trade=exit_trade, fraction=0.0)

    assert conn.execute("SELECT COUNT(*) AS n FROM position_exits").fetchone()["n"] == 0
    unchanged = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    assert unchanged["status"] == "OPEN"
