"""watch_poll.py against a real (temp-file) db and a fake data client (no
network). Focused on the two things that matter most for Phase 2's own
exit criterion: 100% decision coverage (invariant 1) and idempotency
(FR-15) — a re-poll must not double-write.
"""

from datetime import datetime, timezone

from polyprinter.db.conn import get_connection
from polyprinter.obs.log import Logger
from polyprinter.mirror.watch_poll import poll_trader, select_watchlist

ADDRESS = "0xtrader"
MODE = "paper"
MIRROR_CONFIG = {
    "watchlist_size": 20,
    "paper_bankroll_usd": 1000,
    "portfolio_exposure_cap_usd": 500,
    "correlation_cap_usd": 150,
}


def _now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


class FakeClient:
    """Stands in for PolymarketDataClient.activity() — same call shape,
    serves a fixed list regardless of the start/offset it's asked for
    (poll_trader only ever asks for one page in these tests).
    """

    def __init__(self, entries: list[dict]):
        self.entries = entries
        self.calls = 0

    def activity(self, user, *, limit=500, offset=0, types=None, start=None, end=None):
        self.calls += 1
        return list(self.entries)


def _make_db(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, datetime.now(timezone.utc).isoformat()),
    )
    return conn


def _log(conn):
    return Logger("test", conn)


def _trade_entry(*, tx_hash, ts, type_="TRADE", side="BUY", size=10.0, price=0.5, asset="tok"):
    return {
        "transactionHash": tx_hash,
        "timestamp": ts,
        "conditionId": "cond1",
        "type": type_,
        "side": side,
        "size": size,
        "price": price,
        "asset": asset,
    }


def test_poll_trader_writes_one_decision_per_trade_entry(tmp_path):
    conn = _make_db(tmp_path)
    now = _now_epoch()
    entries = [
        _trade_entry(tx_hash="0xa", ts=now - 20, side="BUY", size=10, price=0.5),
        _trade_entry(tx_hash="0xb", ts=now - 10, type_="REDEEM", side="", size=5, price=0),
        _trade_entry(tx_hash="0xc", ts=now, side="SELL", size=4, price=0.6),
    ]
    client = FakeClient(entries)

    n = poll_trader(conn, client, _log(conn), ADDRESS, mode=MODE, mirror_config=MIRROR_CONFIG)

    # REDEEM is logged but doesn't get an observed_trades/decisions row —
    # only the two TRADE entries do.
    assert n == 2
    observed = conn.execute("SELECT * FROM observed_trades").fetchall()
    assert len(observed) == 2

    # invariant 1: every observed_trades row has exactly one decisions row
    for row in observed:
        decisions = conn.execute(
            "SELECT * FROM decisions WHERE observed_trade_id = ?", (row["id"],)
        ).fetchall()
        assert len(decisions) == 1


def test_poll_trader_is_idempotent_on_rerun(tmp_path):
    conn = _make_db(tmp_path)
    now = _now_epoch()
    entries = [_trade_entry(tx_hash="0xa", ts=now, side="BUY", size=10, price=0.5)]
    client = FakeClient(entries)

    n1 = poll_trader(conn, client, _log(conn), ADDRESS, mode=MODE, mirror_config=MIRROR_CONFIG)
    n2 = poll_trader(conn, client, _log(conn), ADDRESS, mode=MODE, mirror_config=MIRROR_CONFIG)

    assert n1 == 1
    assert n2 == 0  # already recorded, not a new trade
    assert conn.execute("SELECT COUNT(*) AS n FROM observed_trades").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"] == 1


def test_poll_trader_records_correct_running_position(tmp_path):
    conn = _make_db(tmp_path)
    now = _now_epoch()
    entries = [
        _trade_entry(tx_hash="0xa", ts=now - 20, side="BUY", size=10, price=0.5),
        _trade_entry(tx_hash="0xb", ts=now - 10, side="BUY", size=5, price=0.5),
        _trade_entry(tx_hash="0xc", ts=now, side="SELL", size=6, price=0.6),
    ]
    client = FakeClient(entries)

    poll_trader(conn, client, _log(conn), ADDRESS, mode=MODE, mirror_config=MIRROR_CONFIG)

    rows = conn.execute(
        "SELECT their_position_after FROM observed_trades ORDER BY id"
    ).fetchall()
    assert [r["their_position_after"] for r in rows] == [10, 15, 9]


def test_select_watchlist_orders_by_shrunk_roi_desc(tmp_path):
    conn = _make_db(tmp_path)
    for addr, roi in [("0xa", 0.10), ("0xb", 0.30), ("0xc", 0.20)]:
        conn.execute(
            "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
            (addr, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO trader_snapshots (address, scanned_at, roi_shrunk) VALUES (?, ?, ?)",
            (addr, datetime.now(timezone.utc).isoformat(), roi),
        )

    watchlist = select_watchlist(conn, 2)
    assert watchlist == ["0xb", "0xc"]  # top 2 by roi_shrunk, descending


def test_select_watchlist_excludes_traders_with_no_snapshot(tmp_path):
    conn = _make_db(tmp_path)  # ADDRESS has no snapshot at all
    assert select_watchlist(conn, 20) == []
