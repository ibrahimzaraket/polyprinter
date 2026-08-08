from datetime import datetime, timezone

from polyprinter.db.conn import get_connection
from polyprinter.mandate.trigger import should_reevaluate

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


def _insert_snapshot(conn, *, resolved_positions=10, roi_shrunk=0.20, hold_to_resolution_rate=0.80):
    cur = conn.execute(
        "INSERT INTO trader_snapshots (address, scanned_at, resolved_positions, roi_shrunk, hold_to_resolution_rate) "
        "VALUES (?, ?, ?, ?, ?)",
        (ADDRESS, _now(), resolved_positions, roi_shrunk, hold_to_resolution_rate),
    )
    return cur.lastrowid


def _insert_mandate(conn, snapshot_id):
    conn.execute(
        """
        INSERT INTO mandates (
            address, version, snapshot_id, verdict, confidence, reasoning, issued_at, expires_at
        ) VALUES (?, 1, ?, 'FOLLOW', 'HIGH', 'test', ?, ?)
        """,
        (ADDRESS, snapshot_id, _now(), _now()),
    )


def test_no_mandate_yet_always_triggers(tmp_path):
    conn = _make_db(tmp_path)
    latest = conn.execute(
        "SELECT * FROM trader_snapshots WHERE id = ?", (_insert_snapshot(conn),)
    ).fetchone()
    should, reason = should_reevaluate(conn, ADDRESS, latest)
    assert should is True
    assert "no mandate" in reason


def test_no_material_change_does_not_trigger(tmp_path):
    conn = _make_db(tmp_path)
    old_id = _insert_snapshot(conn, resolved_positions=10, roi_shrunk=0.20, hold_to_resolution_rate=0.80)
    _insert_mandate(conn, old_id)

    latest_id = _insert_snapshot(conn, resolved_positions=11, roi_shrunk=0.205, hold_to_resolution_rate=0.79)
    latest = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (latest_id,)).fetchone()

    should, reason = should_reevaluate(conn, ADDRESS, latest)
    assert should is False


def test_enough_new_resolved_positions_triggers(tmp_path):
    conn = _make_db(tmp_path)
    old_id = _insert_snapshot(conn, resolved_positions=10, roi_shrunk=0.20, hold_to_resolution_rate=0.80)
    _insert_mandate(conn, old_id)

    latest_id = _insert_snapshot(conn, resolved_positions=16, roi_shrunk=0.20, hold_to_resolution_rate=0.80)
    latest = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (latest_id,)).fetchone()

    should, reason = should_reevaluate(conn, ADDRESS, latest)
    assert should is True
    assert "resolved positions" in reason


def test_large_roi_move_triggers(tmp_path):
    conn = _make_db(tmp_path)
    old_id = _insert_snapshot(conn, resolved_positions=10, roi_shrunk=0.20, hold_to_resolution_rate=0.80)
    _insert_mandate(conn, old_id)

    # 0.20 -> 0.30 is a 50% relative move, well past the 15% threshold
    latest_id = _insert_snapshot(conn, resolved_positions=10, roi_shrunk=0.30, hold_to_resolution_rate=0.80)
    latest = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (latest_id,)).fetchone()

    should, reason = should_reevaluate(conn, ADDRESS, latest)
    assert should is True
    assert "roi_shrunk" in reason


def test_large_hold_to_resolution_move_triggers(tmp_path):
    conn = _make_db(tmp_path)
    old_id = _insert_snapshot(conn, resolved_positions=10, roi_shrunk=0.20, hold_to_resolution_rate=0.80)
    _insert_mandate(conn, old_id)

    latest_id = _insert_snapshot(conn, resolved_positions=10, roi_shrunk=0.20, hold_to_resolution_rate=0.40)
    latest = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (latest_id,)).fetchone()

    should, reason = should_reevaluate(conn, ADDRESS, latest)
    assert should is True
    assert "hold_to_resolution_rate" in reason
