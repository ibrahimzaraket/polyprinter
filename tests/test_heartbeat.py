"""heartbeat.py against a real (temp-file) db — stale_services() must catch
both a service that went quiet AND one that never beat at all (the second
half used to be a docstring promise the code didn't keep; see the function's
own docstring for the incident).
"""

from datetime import datetime, timedelta, timezone

from polyprinter.db.conn import get_connection
from polyprinter.obs import heartbeat


def _make_db(tmp_path):
    return get_connection(tmp_path / "test.db")


def test_fresh_beat_is_not_stale(tmp_path):
    conn = _make_db(tmp_path)
    heartbeat.beat(conn, "scout")
    conn.commit()
    stale = {s["service"] for s in heartbeat.stale_services(conn)}
    # scout beat just now; mirror/dashboard never beat at all -> both stale
    assert "scout" not in stale
    assert "mirror" in stale
    assert "dashboard" in stale


def test_old_beat_is_stale(tmp_path):
    conn = _make_db(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
    conn.execute(
        "INSERT INTO heartbeats (service, last_beat, detail_json) VALUES ('scout', ?, '{}')",
        (old,),
    )
    conn.commit()
    result = {s["service"]: s for s in heartbeat.stale_services(conn, max_age_seconds=120)}
    assert "scout" in result
    assert result["scout"]["age_seconds"] > 120


def test_never_beaten_service_reported_with_no_timestamp(tmp_path):
    conn = _make_db(tmp_path)
    heartbeat.beat(conn, "scout")
    conn.commit()
    result = {s["service"]: s for s in heartbeat.stale_services(conn)}
    assert result["mirror"]["last_beat"] is None
    assert result["mirror"]["age_seconds"] is None


def test_all_expected_services_beating_means_nothing_stale(tmp_path):
    conn = _make_db(tmp_path)
    for service in heartbeat.EXPECTED_SERVICES:
        heartbeat.beat(conn, service)
    conn.commit()
    assert heartbeat.stale_services(conn) == []


def test_scout_long_interval_not_stale_by_default(tmp_path):
    # Scout's own --interval-seconds default is 86400s (24h) — a beat 3h
    # old is mid-cycle, not dead. Found live 2026-08-08: without a
    # per-service threshold this always showed "stale" on the Now tab
    # despite the last run finishing cleanly.
    conn = _make_db(tmp_path)
    three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    conn.execute(
        "INSERT INTO heartbeats (service, last_beat, detail_json) VALUES ('scout', ?, '{}')",
        (three_hours_ago,),
    )
    conn.commit()
    stale = {s["service"] for s in heartbeat.stale_services(conn)}
    assert "scout" not in stale


def test_explicit_max_age_overrides_per_service_default(tmp_path):
    # An explicit max_age_seconds is a caller opting into "flag anything
    # quiet for over N seconds, no exceptions" — it must still catch
    # scout even though scout's own per-service default would forgive it.
    conn = _make_db(tmp_path)
    three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    conn.execute(
        "INSERT INTO heartbeats (service, last_beat, detail_json) VALUES ('scout', ?, '{}')",
        (three_hours_ago,),
    )
    conn.commit()
    stale = {s["service"] for s in heartbeat.stale_services(conn, max_age_seconds=120)}
    assert "scout" in stale


def test_learner_beat_recognized_as_expected_service(tmp_path):
    conn = _make_db(tmp_path)
    heartbeat.beat(conn, "learner")
    conn.commit()
    stale = {s["service"] for s in heartbeat.stale_services(conn)}
    assert "learner" not in stale
