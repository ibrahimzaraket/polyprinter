"""Sizing caps against a real (temp-file) db — these are SQL-backed sums,
not pure functions, so they're tested against actual inserted rows rather
than mocked.
"""

from datetime import datetime, timezone

from polyprinter.db.conn import get_connection
from polyprinter.mirror import sizing

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


def _open_position(conn, *, market_id: str, cost_usd: float, mode: str = MODE):
    """Inserts the minimal chain a `positions` row's foreign keys require:
    one observed_trade, one decision, then the position itself.
    """
    now = _now()
    obs = conn.execute(
        """
        INSERT INTO observed_trades (
            address, tx_hash, log_index, market_id, token_id, side, shares,
            price, block_ts, detected_at, source
        ) VALUES (?, ?, 0, ?, 'tok', 'BUY', 10, 0.5, ?, ?, 'poll')
        """,
        (ADDRESS, f"0x{market_id}{cost_usd}", market_id, now, now),
    )
    dec = conn.execute(
        """
        INSERT INTO decisions (observed_trade_id, decided_at, verdict, size_usd, mode)
        VALUES (?, ?, 'TAKE', ?, ?)
        """,
        (obs.lastrowid, now, cost_usd, mode),
    )
    conn.execute(
        """
        INSERT INTO positions (
            decision_id, address, market_id, token_id, mode,
            shares_open, shares_total, our_entry_price, their_entry_price,
            cost_usd, opened_at, status
        ) VALUES (?, ?, ?, 'tok', ?, 10, 10, 0.5, 0.5, ?, ?, 'OPEN')
        """,
        (dec.lastrowid, ADDRESS, market_id, mode, cost_usd, now),
    )


def test_portfolio_exposure_sums_open_and_partial_only(tmp_path):
    conn = _make_db(tmp_path)
    _open_position(conn, market_id="m1", cost_usd=100)
    _open_position(conn, market_id="m2", cost_usd=50)
    assert sizing.portfolio_exposure_usd(conn, MODE) == 150


def test_portfolio_exposure_ignores_other_mode(tmp_path):
    conn = _make_db(tmp_path)
    _open_position(conn, market_id="m1", cost_usd=100, mode="paper")
    _open_position(conn, market_id="m2", cost_usd=999, mode="live")
    assert sizing.portfolio_exposure_usd(conn, "paper") == 100


def test_market_exposure_sums_only_that_market(tmp_path):
    conn = _make_db(tmp_path)
    _open_position(conn, market_id="m1", cost_usd=100)
    _open_position(conn, market_id="m2", cost_usd=50)
    assert sizing.market_exposure_usd(conn, "m1", MODE) == 100


def test_check_portfolio_cap(tmp_path):
    conn = _make_db(tmp_path)
    _open_position(conn, market_id="m1", cost_usd=400)
    assert sizing.check_portfolio_cap(conn, MODE, 50, cap_usd=500) is True
    assert sizing.check_portfolio_cap(conn, MODE, 200, cap_usd=500) is False


def test_check_correlation_cap(tmp_path):
    conn = _make_db(tmp_path)
    _open_position(conn, market_id="m1", cost_usd=100)
    assert sizing.check_correlation_cap(conn, "m1", MODE, 40, cap_usd=150) is True
    assert sizing.check_correlation_cap(conn, "m1", MODE, 60, cap_usd=150) is False
    # a different market has its own, independent cap
    assert sizing.check_correlation_cap(conn, "m2", MODE, 140, cap_usd=150) is True


def test_available_capital(tmp_path):
    conn = _make_db(tmp_path)
    _open_position(conn, market_id="m1", cost_usd=300)
    assert sizing.available_capital_usd(conn, MODE, bankroll_usd=1000) == 700


def test_per_trade_cap_no_mandate_ceiling():
    assert sizing.per_trade_cap(75.0, None) == 75.0


def test_per_trade_cap_clamps_to_mandate_ceiling():
    assert sizing.per_trade_cap(75.0, 50.0) == 50.0
    assert sizing.per_trade_cap(30.0, 50.0) == 30.0
