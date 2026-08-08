"""mandate/operator.py against a real (temp-file) db — the operator's own
authorization path, parallel to mandate/issue.py's LLM path.
"""

import pytest

from polyprinter.db.conn import get_connection
from polyprinter.mandate import operator

ADDRESS = "0xtrader"


def _make_db(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, "2026-08-08T00:00:00+00:00"),
    )
    return conn


def test_issue_creates_a_follow_mandate(tmp_path):
    conn = _make_db(tmp_path)

    mandate_id = operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=True)

    row = conn.execute("SELECT * FROM mandates WHERE id = ?", (mandate_id,)).fetchone()
    assert row["verdict"] == "FOLLOW"
    assert row["issued_by"] == "operator"
    assert row["sizing_mode"] == "balance_matched"
    assert row["size_multiplier"] == 1.0
    assert row["fast_lane"] == 1
    assert row["superseded_by"] is None


def test_issue_rejects_non_positive_multiplier(tmp_path):
    conn = _make_db(tmp_path)
    with pytest.raises(ValueError):
        operator.issue(conn, address=ADDRESS, size_multiplier=0, fast_lane=False)
    with pytest.raises(ValueError):
        operator.issue(conn, address=ADDRESS, size_multiplier=-1, fast_lane=False)


def test_issue_rejects_bad_sizing_mode(tmp_path):
    conn = _make_db(tmp_path)
    with pytest.raises(ValueError):
        operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=False, sizing_mode="nonsense")


def test_second_issue_supersedes_the_first(tmp_path):
    conn = _make_db(tmp_path)
    first_id = operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=False)
    second_id = operator.issue(conn, address=ADDRESS, size_multiplier=2.0, fast_lane=True)

    first = conn.execute("SELECT * FROM mandates WHERE id = ?", (first_id,)).fetchone()
    second = conn.execute("SELECT * FROM mandates WHERE id = ?", (second_id,)).fetchone()
    assert first["superseded_by"] == second_id
    assert second["superseded_by"] is None
    assert second["version"] == first["version"] + 1


def test_issue_supersedes_an_llm_mandate_too(tmp_path):
    conn = _make_db(tmp_path)
    conn.execute(
        """
        INSERT INTO mandates (address, version, verdict, confidence, issued_at, expires_at, reasoning, issued_by)
        VALUES (?, 1, 'SKIP', 'HIGH', '2026-08-08T00:00:00+00:00', '2026-08-15T00:00:00+00:00', 'LLM said skip', 'llm')
        """,
        (ADDRESS,),
    )
    llm_id = conn.execute("SELECT id FROM mandates").fetchone()["id"]

    operator_id = operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=False)

    llm_row = conn.execute("SELECT * FROM mandates WHERE id = ?", (llm_id,)).fetchone()
    assert llm_row["superseded_by"] == operator_id


def test_revoke_expires_the_active_mandate(tmp_path):
    conn = _make_db(tmp_path)
    mandate_id = operator.issue(conn, address=ADDRESS, size_multiplier=1.0, fast_lane=False)

    result = operator.revoke(conn, address=ADDRESS)

    assert result is True
    row = conn.execute("SELECT * FROM mandates WHERE id = ?", (mandate_id,)).fetchone()
    assert row["expires_at"] <= "2100-01-01"  # sanity: not the original ~10-year TTL anymore
    assert row["superseded_by"] is None  # expired, not superseded — still the "active" row, just inactive


def test_revoke_with_nothing_active_returns_false(tmp_path):
    conn = _make_db(tmp_path)
    assert operator.revoke(conn, address=ADDRESS) is False
