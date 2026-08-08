"""decide.py against a real (temp-file) db. Mandates aren't written by any
real service until Phase 3 — these tests insert mandate rows directly,
which is legitimate here: it's testing that decide.py *consumes* a mandate
correctly, not claiming Phase 3 (issuing mandates) is built.
"""

from datetime import datetime, timedelta, timezone

import pytest

from polyprinter.db.conn import get_connection
from polyprinter.mirror.decide import decide

ADDRESS = "0xtrader"
MODE = "paper"
MIRROR_CONFIG = {
    "watchlist_size": 20,
    "paper_bankroll_usd": 1000,
    "portfolio_exposure_cap_usd": 500,
    "correlation_cap_usd": 150,
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_db(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, _iso(_now())),
    )
    return conn


def _insert_trade(conn, *, side="BUY", shares=10.0, price=0.5, market_id="m1", token_id="tok") -> "sqlite3.Row":
    now = _iso(_now())
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


def _insert_mandate(conn, **overrides):
    defaults = dict(
        address=ADDRESS,
        version=1,
        verdict="FOLLOW",
        confidence="HIGH",
        max_position_usd=None,
        min_entry_price=None,
        max_entry_price=None,
        min_market_liquidity=None,
        reasoning="test mandate",
        issued_at=_iso(_now() - timedelta(hours=1)),
        expires_at=_iso(_now() + timedelta(days=1)),
    )
    defaults.update(overrides)
    conn.execute(
        """
        INSERT INTO mandates (
            address, version, verdict, confidence, max_position_usd,
            min_entry_price, max_entry_price, min_market_liquidity,
            reasoning, issued_at, expires_at
        ) VALUES (:address, :version, :verdict, :confidence, :max_position_usd,
                   :min_entry_price, :max_entry_price, :min_market_liquidity,
                   :reasoning, :issued_at, :expires_at)
        """,
        defaults,
    )


def test_entry_with_no_mandate_is_skipped(tmp_path):
    conn = _make_db(tmp_path)
    trade = _insert_trade(conn)
    result = decide(conn, trade, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "SKIP"
    assert result["skip_reason_code"] == "NO_MANDATE"


def test_entry_with_expired_mandate_is_skipped(tmp_path):
    conn = _make_db(tmp_path)
    _insert_mandate(conn, expires_at=_iso(_now() - timedelta(hours=1)))
    trade = _insert_trade(conn)
    result = decide(conn, trade, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "SKIP"
    assert result["skip_reason_code"] == "MANDATE_EXPIRED"


def test_entry_with_non_follow_verdict_is_skipped(tmp_path):
    conn = _make_db(tmp_path)
    _insert_mandate(conn, verdict="WATCH")
    trade = _insert_trade(conn)
    result = decide(conn, trade, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "SKIP"
    assert result["skip_reason_code"] == "MANDATE_NOT_FOLLOW"


def test_entry_below_price_floor_is_skipped(tmp_path):
    conn = _make_db(tmp_path)
    _insert_mandate(conn, min_entry_price=0.6)
    trade = _insert_trade(conn, price=0.5)
    result = decide(conn, trade, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "SKIP"
    assert result["skip_reason_code"] == "PRICE_BAND"


def test_entry_above_price_ceiling_is_skipped(tmp_path):
    conn = _make_db(tmp_path)
    _insert_mandate(conn, max_entry_price=0.4)
    trade = _insert_trade(conn, price=0.5)
    result = decide(conn, trade, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "SKIP"
    assert result["skip_reason_code"] == "PRICE_BAND"


def test_entry_within_all_caps_is_taken(tmp_path):
    conn = _make_db(tmp_path)
    _insert_mandate(conn)
    trade = _insert_trade(conn, shares=10, price=0.5)  # $5 requested
    result = decide(conn, trade, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "TAKE"
    assert result["size_usd"] == pytest.approx(5.0)
    assert result["mandate_id"] is not None


def test_entry_clamped_by_mandate_max_position(tmp_path):
    conn = _make_db(tmp_path)
    _insert_mandate(conn, max_position_usd=2.0)
    trade = _insert_trade(conn, shares=10, price=0.5)  # $5 requested, capped to $2
    result = decide(conn, trade, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "TAKE"
    assert result["size_usd"] == pytest.approx(2.0)


def test_entry_exceeding_bankroll_is_skipped(tmp_path):
    conn = _make_db(tmp_path)
    _insert_mandate(conn)
    tiny_config = {**MIRROR_CONFIG, "paper_bankroll_usd": 1.0}
    trade = _insert_trade(conn, shares=10, price=0.5)  # $5 requested, bankroll is $1
    result = decide(conn, trade, mode=MODE, mirror_config=tiny_config)
    assert result["verdict"] == "SKIP"
    assert result["skip_reason_code"] == "NO_CAPITAL"


def test_exit_with_no_matching_position_is_skipped(tmp_path):
    conn = _make_db(tmp_path)
    trade = _insert_trade(conn, side="SELL")
    result = decide(conn, trade, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "SKIP"
    assert result["skip_reason_code"] == "NO_MATCHING_POSITION"


def test_exit_is_never_gated_by_mandate_state(tmp_path):
    """Invariant 2: exits proceed even with an expired (or absent)
    mandate — only entries consult mandates.
    """
    conn = _make_db(tmp_path)
    # No mandate at all. Build a position of ours directly (bypassing a
    # real entry decision, which would need Phase 3) to exercise the exit
    # path in isolation.
    buy = _insert_trade(conn, side="BUY", shares=100, price=0.5)
    dec = conn.execute(
        "INSERT INTO decisions (observed_trade_id, decided_at, verdict, size_usd, mode) "
        "VALUES (?, ?, 'TAKE', 50, ?)",
        (buy["id"], _iso(_now()), MODE),
    )
    conn.execute(
        """
        INSERT INTO positions (
            decision_id, address, market_id, token_id, mode,
            shares_open, shares_total, our_entry_price, their_entry_price,
            cost_usd, opened_at, status
        ) VALUES (?, ?, 'm1', 'tok', ?, 100, 100, 0.5, 0.5, 50, ?, 'OPEN')
        """,
        (dec.lastrowid, ADDRESS, MODE, _iso(_now())),
    )

    sell = _insert_trade(conn, side="SELL", shares=40, price=0.55)  # they sold 40% of their 100
    result = decide(conn, sell, mode=MODE, mirror_config=MIRROR_CONFIG)
    assert result["verdict"] == "MIRROR_EXIT"
    assert result["mandate_id"] is None
    assert result["fraction"] == pytest.approx(0.4)
    assert result["size_usd"] == pytest.approx(100 * 0.4 * 0.55)  # our shares_open * fraction * their exit price
