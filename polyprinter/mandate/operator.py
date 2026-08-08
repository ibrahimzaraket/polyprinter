"""Operator-issued mandates — a second, equally real way to authorize a
FOLLOW, parallel to mandate/issue.py's LLM path (migrations/0004's own
docstring covers the schema rationale). Called from the dashboard's write
routes (dashboard/server.py), never from Scout/Mirror themselves — this is
the operator's own explicit choice landing in the database, not a scan
result.

Same supersession mechanic as the LLM path (at most one non-superseded
mandate per address), same versioning, same table — decide.py doesn't
need to know or care which path issued the mandate it's reading; only
sizing_mode/fast_lane change what happens once the mandate is active.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

OPERATOR_MANDATE_TTL_DAYS = 3650  # effectively indefinite — an operator's own
# choice doesn't go stale the way an LLM's dossier-grounded opinion does;
# it stays active until the operator explicitly revokes it (revoke()
# below), not until a clock runs out.

VALID_SIZING_MODES = {"fixed_cap", "balance_matched"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _next_version(conn: sqlite3.Connection, address: str) -> int:
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS v FROM mandates WHERE address = ?", (address,)).fetchone()
    return row["v"] + 1


def _supersede_active(conn: sqlite3.Connection, address: str, new_id: int) -> None:
    conn.execute(
        "UPDATE mandates SET superseded_by = ? WHERE address = ? AND id != ? AND superseded_by IS NULL",
        (new_id, address, new_id),
    )


def issue(
    conn: sqlite3.Connection,
    *,
    address: str,
    size_multiplier: float,
    fast_lane: bool,
    sizing_mode: str = "balance_matched",
    max_position_usd: float | None = None,
    min_entry_price: float | None = None,
    max_entry_price: float | None = None,
    reasoning: str = "Operator-issued: manually tailed via dashboard.",
) -> int:
    """Issues a new operator mandate for `address`, superseding whatever
    was previously active (LLM- or operator-issued — an operator tailing
    someone overrides an LLM SKIP the same way a new LLM mandate would).
    Returns the new mandate's id.

    `size_multiplier` must be positive — 1.0 is an exact proportional
    match to their own bet-as-%-of-balance (mirror/sizing.py's
    balance_matched_size), not a default meaning "no opinion".
    """
    if sizing_mode not in VALID_SIZING_MODES:
        raise ValueError(f"sizing_mode must be one of {VALID_SIZING_MODES}, got {sizing_mode!r}")
    if size_multiplier <= 0:
        raise ValueError("size_multiplier must be positive")

    version = _next_version(conn, address)
    issued_at = _now_iso()
    expires_at = (_now() + timedelta(days=OPERATOR_MANDATE_TTL_DAYS)).isoformat()

    cur = conn.execute(
        """
        INSERT INTO mandates (
            address, version, verdict, confidence, reasoning,
            max_position_usd, min_entry_price, max_entry_price,
            issued_at, expires_at, issued_by, sizing_mode, size_multiplier, fast_lane
        ) VALUES (?, ?, 'FOLLOW', 'HIGH', ?, ?, ?, ?, ?, ?, 'operator', ?, ?, ?)
        """,
        (
            address, version, reasoning, max_position_usd, min_entry_price, max_entry_price,
            issued_at, expires_at, sizing_mode, size_multiplier, int(fast_lane),
        ),
    )
    new_id = cur.lastrowid
    _supersede_active(conn, address, new_id)
    return new_id


def revoke(conn: sqlite3.Connection, *, address: str) -> bool:
    """Supersedes the active mandate for `address` with nothing — stops
    new entries (decide_entry finds no active mandate, same as
    NO_MANDATE) without touching any open position: invariant 2 (exits
    are never gated by mandate state) means an already-tailed position
    still exits cleanly regardless. Returns False if there was nothing
    active to revoke.
    """
    active = conn.execute(
        "SELECT id FROM mandates WHERE address = ? AND superseded_by IS NULL ORDER BY version DESC LIMIT 1",
        (address,),
    ).fetchone()
    if active is None:
        return False
    # Superseded-by-nothing: mark it superseded by itself is wrong (the FK
    # would point at a "successor" that isn't one); instead expire it,
    # which decide_entry already treats as inactive (MANDATE_EXPIRED)
    # without needing a new concept. Backdated by a full day, not set to
    # the exact current instant: SQLite's datetime('now') truncates to
    # whole seconds while Python's isoformat() doesn't, so "expires_at =
    # now" read back via `expires_at > datetime('now')` (mirror/
    # fast_lane.py's query) could evaluate as NOT YET expired for a
    # mandate revoked within the same second — found by its own test,
    # deterministically, not a rare race. Backdating sidesteps the
    # precision mismatch entirely rather than depending on exactly how a
    # caller compares it.
    revoked_at = (_now() - timedelta(days=1)).isoformat()
    conn.execute("UPDATE mandates SET expires_at = ? WHERE id = ?", (revoked_at, active["id"]))
    return True
