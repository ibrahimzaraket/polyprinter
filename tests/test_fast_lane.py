"""mirror/fast_lane.py — the single source of truth both watch_poll.py and
watch_events.py check before deciding who drives a trade. Wrong here means
wrong in two places that are supposed to always agree.
"""

from datetime import datetime, timezone

from polyprinter.db.conn import get_connection
from polyprinter.mandate import operator
from polyprinter.mirror import fast_lane

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


def _set_pinned(monkeypatch, tmp_path, addresses):
    import polyprinter.config as config_module

    overrides = tmp_path / "config-overrides.yaml"
    lines = ["mirror:", "  pinned_addresses:"] + [f'    - "{a}"' for a in addresses]
    overrides.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(config_module, "OVERRIDES_PATH", overrides)


def test_not_fast_laned_when_not_pinned(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)
    _set_pinned(monkeypatch, tmp_path, [])
    operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=True)

    assert fast_lane.fast_lane_addresses(conn) == set()


def test_not_fast_laned_when_pinned_but_no_mandate(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)
    _set_pinned(monkeypatch, tmp_path, [ADDRESS])

    assert fast_lane.fast_lane_addresses(conn) == set()


def test_not_fast_laned_when_mandate_fast_lane_is_false(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)
    _set_pinned(monkeypatch, tmp_path, [ADDRESS])
    operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=False)

    assert fast_lane.fast_lane_addresses(conn) == set()


def test_fast_laned_when_pinned_and_fast_lane_mandate_active(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)
    _set_pinned(monkeypatch, tmp_path, [ADDRESS])
    operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=True)

    assert fast_lane.fast_lane_addresses(conn) == {ADDRESS}


def test_not_fast_laned_after_revoke(tmp_path, monkeypatch):
    conn = _make_db(tmp_path)
    _set_pinned(monkeypatch, tmp_path, [ADDRESS])
    operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=True)
    operator.revoke(conn, address=ADDRESS)

    assert fast_lane.fast_lane_addresses(conn) == set()


def test_llm_mandate_never_grants_fast_lane(tmp_path, monkeypatch):
    """fast_lane is operator-mandate-only, structurally — an LLM mandate
    (issued_by='llm') can never set it, since mandate/issue.py never
    writes that column to anything but its default.
    """
    conn = _make_db(tmp_path)
    _set_pinned(monkeypatch, tmp_path, [ADDRESS])
    conn.execute(
        """
        INSERT INTO mandates (address, version, verdict, confidence, issued_at, expires_at, reasoning, issued_by)
        VALUES (?, 1, 'FOLLOW', 'HIGH', ?, ?, 'x', 'llm')
        """,
        (ADDRESS, _now(), "2030-01-01T00:00:00+00:00"),
    )

    assert fast_lane.fast_lane_addresses(conn) == set()
