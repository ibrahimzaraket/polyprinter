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


def _insert_mandate(conn, snapshot_id, *, issued_by="llm"):
    conn.execute(
        """
        INSERT INTO mandates (
            address, version, snapshot_id, verdict, confidence, reasoning, issued_at, expires_at, issued_by
        ) VALUES (?, 1, ?, 'FOLLOW', 'HIGH', 'test', ?, ?, ?)
        """,
        (ADDRESS, snapshot_id, _now(), _now(), issued_by),
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


def test_operator_mandate_never_reevaluated_even_with_no_snapshot_link(tmp_path):
    # Regression test for a real production incident (2026-08-08):
    # operator-issued mandates never set snapshot_id (mandate/operator.py
    # never links one — there's no dossier behind a manual "tail this
    # wallet" click), which used to fall straight into the "no linked
    # snapshot" branch below and unconditionally trigger a same-day LLM
    # re-evaluation that silently overwrote the operator's own choice.
    conn = _make_db(tmp_path)
    conn.execute(
        """
        INSERT INTO mandates (
            address, version, snapshot_id, verdict, confidence, reasoning,
            issued_at, expires_at, issued_by, sizing_mode, size_multiplier, fast_lane
        ) VALUES (?, 1, NULL, 'FOLLOW', 'HIGH', 'operator test', ?, ?, 'operator', 'balance_matched', 1.5, 1)
        """,
        (ADDRESS, _now(), _now()),
    )
    latest = conn.execute(
        "SELECT * FROM trader_snapshots WHERE id = ?", (_insert_snapshot(conn),)
    ).fetchone()

    should, reason = should_reevaluate(conn, ADDRESS, latest)
    assert should is False
    assert "operator" in reason.lower()


def test_operator_mandate_not_overridden_by_large_metric_move(tmp_path):
    # Even a metric move that would normally trigger re-evaluation must
    # not touch an active operator mandate — the whole point is that only
    # the operator's own actions change it.
    conn = _make_db(tmp_path)
    old_id = _insert_snapshot(conn, resolved_positions=10, roi_shrunk=0.20, hold_to_resolution_rate=0.80)
    _insert_mandate(conn, old_id, issued_by="operator")

    latest_id = _insert_snapshot(conn, resolved_positions=50, roi_shrunk=0.90, hold_to_resolution_rate=0.10)
    latest = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (latest_id,)).fetchone()

    should, reason = should_reevaluate(conn, ADDRESS, latest)
    assert should is False
