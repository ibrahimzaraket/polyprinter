"""Persist raw responses before anything parses them.

Structural rule (docs/SCHEMA.md): every external call goes through
sources/ and persists its raw response before anything parses it. When an
endpoint shape changes — and it will — you find out from a parse error
against stored data, not from a silently wrong dossier.

Deduplicated on (source, url, body_hash): Scout has no incremental cursor
the way Mirror's watch_poll.py does, so re-running it refetches each
trader's full activity history from scratch, byte-for-byte identical to
last time for any trader with no new activity. Storing that again is pure
waste, not a bigger audit trail — found live 2026-08-08, 14,671 exact
duplicates, ~2.4GB. This never drops a genuinely new response, only a
repeat of one already on file.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_raw(conn: sqlite3.Connection, *, source: str, url: str, status: int, body: str) -> int:
    """Insert one raw_responses row, or return an existing identical one's id.

    `source` is a short tag identifying the client, e.g. 'data-api',
    'gamma-api' — not the full URL, so raw responses can be grouped without
    parsing `url`. A row is "identical" to a prior one iff `source`, `url`,
    and `body` all match exactly (checked via body_hash, not raw `body`,
    since the column isn't indexed).
    """
    body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT id FROM raw_responses WHERE source = ? AND url = ? AND body_hash = ?",
        (source, url, body_hash),
    ).fetchone()
    if existing is not None:
        return existing[0]
    cur = conn.execute(
        "INSERT INTO raw_responses (source, url, fetched_at, status, body, body_hash) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (source, url, _now_iso(), status, body, body_hash),
    )
    return cur.lastrowid
