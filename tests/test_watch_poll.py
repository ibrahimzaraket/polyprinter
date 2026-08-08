"""watch_poll.py against a real (temp-file) db and a fake data client (no
network). Focused on the two things that matter most for Phase 2's own
exit criterion: 100% decision coverage (invariant 1) and idempotency
(FR-15) — a re-poll must not double-write.
"""

from datetime import datetime, timedelta, timezone

import pytest

from polyprinter.db.conn import get_connection
from polyprinter.obs.log import Logger
from polyprinter.mirror import watch_poll
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


def test_poll_trader_take_actually_opens_a_position(tmp_path):
    """A TAKE decision must not just get logged — it has to open a real
    positions row too (execute.py; see that module's docstring for the
    gap this guards against regressing).
    """
    conn = _make_db(tmp_path)
    conn.execute(
        """
        INSERT INTO mandates (
            address, version, verdict, confidence, issued_at, expires_at, reasoning
        ) VALUES (?, 1, 'FOLLOW', 'HIGH', ?, ?, 'test mandate')
        """,
        (ADDRESS, datetime.now(timezone.utc).isoformat(), (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
    )
    now = _now_epoch()
    entries = [_trade_entry(tx_hash="0xa", ts=now, side="BUY", size=10, price=0.5)]
    client = FakeClient(entries)

    poll_trader(conn, client, _log(conn), ADDRESS, mode=MODE, mirror_config=MIRROR_CONFIG)

    decision = conn.execute("SELECT * FROM decisions").fetchone()
    assert decision["verdict"] == "TAKE"
    positions = conn.execute("SELECT * FROM positions").fetchall()
    assert len(positions) == 1
    assert positions[0]["decision_id"] == decision["id"]
    assert positions[0]["status"] == "OPEN"
    assert positions[0]["shares_open"] > 0


def test_poll_trader_mirror_exit_actually_closes_a_position(tmp_path):
    conn = _make_db(tmp_path)
    conn.execute(
        """
        INSERT INTO mandates (
            address, version, verdict, confidence, issued_at, expires_at, reasoning
        ) VALUES (?, 1, 'FOLLOW', 'HIGH', ?, ?, 'test mandate')
        """,
        (ADDRESS, datetime.now(timezone.utc).isoformat(), (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
    )
    now = _now_epoch()

    entry_client = FakeClient([_trade_entry(tx_hash="0xa", ts=now - 10, side="BUY", size=10, price=0.5)])
    poll_trader(conn, entry_client, _log(conn), ADDRESS, mode=MODE, mirror_config=MIRROR_CONFIG)
    position = conn.execute("SELECT * FROM positions").fetchone()
    assert position["status"] == "OPEN"

    exit_client = FakeClient([_trade_entry(tx_hash="0xb", ts=now, side="SELL", size=10, price=0.6)])
    poll_trader(conn, exit_client, _log(conn), ADDRESS, mode=MODE, mirror_config=MIRROR_CONFIG)

    updated = conn.execute("SELECT * FROM positions WHERE id = ?", (position["id"],)).fetchone()
    assert updated["status"] == "CLOSED"
    assert updated["shares_open"] == pytest.approx(0.0)
    exits = conn.execute("SELECT * FROM position_exits WHERE position_id = ?", (position["id"],)).fetchall()
    assert len(exits) == 1


def _set_pinned(monkeypatch, tmp_path, addresses):
    import polyprinter.config as config_module

    overrides = tmp_path / "config-overrides.yaml"
    lines = ["mirror:", "  pinned_addresses:"] + [f'    - "{a}"' for a in addresses]
    overrides.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", overrides)


def test_select_watchlist_includes_pinned_regardless_of_rank(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)  # ADDRESS ('0xtrader') has no snapshot
    for addr, roi in [("0xa", 0.10), ("0xb", 0.30), ("0xc", 0.20)]:
        conn.execute(
            "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
            (addr, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO trader_snapshots (address, scanned_at, roi_shrunk) VALUES (?, ?, ?)",
            (addr, datetime.now(timezone.utc).isoformat(), roi),
        )
    _set_pinned(monkeypatch, tmp_path, ["0xpinned"])  # never discovered, no snapshot at all

    watchlist = watch_poll.select_watchlist(conn, 2)

    assert watchlist[0] == "0xpinned"
    assert len(watchlist) == 2  # pinned counts toward n, not on top of it
    assert watchlist[1] == "0xb"  # top-ranked auto slot fills the rest


def test_select_watchlist_pinned_excluded_from_auto_ranked_duplicate(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)
    for addr, roi in [("0xa", 0.10), ("0xb", 0.30)]:
        conn.execute(
            "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
            (addr, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO trader_snapshots (address, scanned_at, roi_shrunk) VALUES (?, ?, ?)",
            (addr, datetime.now(timezone.utc).isoformat(), roi),
        )
    _set_pinned(monkeypatch, tmp_path, ["0xb"])  # already the top-ranked auto pick

    watchlist = watch_poll.select_watchlist(conn, 2)

    assert watchlist == ["0xb", "0xa"]  # not duplicated


def test_ensure_pinned_traders_exist_creates_bare_traders_row(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)
    _set_pinned(monkeypatch, tmp_path, ["0xneverseen"])

    watch_poll.ensure_pinned_traders_exist(conn)

    row = conn.execute("SELECT * FROM traders WHERE address = ?", ("0xneverseen",)).fetchone()
    assert row is not None
    assert row["discovery_source"] == "manual_pin"


def test_ensure_pinned_traders_exist_is_idempotent(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)
    _set_pinned(monkeypatch, tmp_path, ["0xneverseen"])

    watch_poll.ensure_pinned_traders_exist(conn)
    watch_poll.ensure_pinned_traders_exist(conn)  # must not raise (ON CONFLICT DO NOTHING)

    assert conn.execute("SELECT COUNT(*) AS n FROM traders WHERE address = ?", ("0xneverseen",)).fetchone()["n"] == 1
