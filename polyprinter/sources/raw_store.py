"""Persist raw responses before anything parses them.

Structural rule (docs/SCHEMA.md): every external call goes through
sources/ and persists its raw response before anything parses it. When an
endpoint shape changes — and it will — you find out from a parse error
against stored data, not from a silently wrong dossier.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_raw(conn: sqlite3.Connection, *, source: str, url: str, status: int, body: str) -> int:
    """Insert one raw_responses row. Returns its id.

    `source` is a short tag identifying the client, e.g. 'data-api',
    'gamma-api' — not the full URL, so raw responses can be grouped without
    parsing `url`.
    """
    cur = conn.execute(
        "INSERT INTO raw_responses (source, url, fetched_at, status, body) VALUES (?, ?, ?, ?, ?)",
        (source, url, _now_iso(), status, body),
    )
    return cur.lastrowid
