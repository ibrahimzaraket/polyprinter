"""Heartbeat writer. Per PRD FR-10/FR-19 and Audit F10: the dashboard must
be able to tell 'no signal' (service up, nothing happened) apart from
'no heartbeat' (service is dead). One row per service, upserted.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def beat(conn: sqlite3.Connection, service: str, **detail: Any) -> None:
    """Upsert this service's heartbeat row. Call on every loop tick, not
    just on success — a service that's alive but erroring should still
    show a fresh heartbeat with the error in `detail`.
    """
    conn.execute(
        """
        INSERT INTO heartbeats (service, last_beat, detail_json)
        VALUES (?, ?, ?)
        ON CONFLICT(service) DO UPDATE SET
            last_beat = excluded.last_beat,
            detail_json = excluded.detail_json
        """,
        (service, _now_iso(), json.dumps(detail, default=str)),
    )


# The services this project actually runs today. Telegram isn't built yet
# (no phase has shipped it), so it's deliberately not on this list — adding
# it here would make the dashboard permanently report a "dead" service that
# was simply never supposed to exist yet.
EXPECTED_SERVICES = ("scout", "mirror", "dashboard")


def stale_services(conn: sqlite3.Connection, *, max_age_seconds: int = 120) -> list[dict[str, Any]]:
    """Services whose last heartbeat is older than max_age_seconds, OR that
    have never beaten at all. Used by the dashboard's Now tab.

    The "never beaten" half used to be a docstring promise this function
    didn't keep: it only ever looked at rows already in `heartbeats`, so a
    service that crashes before its first beat() call (an import-time
    failure, say, or a container that never started) was invisible here —
    "dead before it ran" and "ran fine, nothing to report" looked
    identical. Found by code review 2026-08-08, not a live incident.
    Checked against EXPECTED_SERVICES now, so a missing row is reported
    the same way a stale one is, with `last_beat`/`age_seconds` as None
    (there's no timestamp to report — it never happened).
    """
    rows = {r["service"]: r for r in conn.execute("SELECT service, last_beat, detail_json FROM heartbeats").fetchall()}
    now = datetime.now(timezone.utc)
    stale = []
    for service, row in rows.items():
        last_beat = datetime.fromisoformat(row["last_beat"])
        age = (now - last_beat).total_seconds()
        if age > max_age_seconds:
            stale.append({"service": service, "last_beat": row["last_beat"], "age_seconds": age})
    for service in EXPECTED_SERVICES:
        if service not in rows:
            stale.append({"service": service, "last_beat": None, "age_seconds": None})
    return stale
