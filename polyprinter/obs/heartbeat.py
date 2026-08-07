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


def stale_services(conn: sqlite3.Connection, *, max_age_seconds: int = 120) -> list[dict[str, Any]]:
    """Services whose last heartbeat is older than max_age_seconds, or that
    have never beaten at all. Used by the dashboard's Now tab.
    """
    rows = conn.execute("SELECT service, last_beat, detail_json FROM heartbeats").fetchall()
    now = datetime.now(timezone.utc)
    stale = []
    for row in rows:
        last_beat = datetime.fromisoformat(row["last_beat"])
        age = (now - last_beat).total_seconds()
        if age > max_age_seconds:
            stale.append({"service": row["service"], "last_beat": row["last_beat"], "age_seconds": age})
    return stale
