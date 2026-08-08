"""Delta-detection (FR-5): only spend an LLM call on a trader whose
dossier has materially changed since their last mandate. Daily
re-evaluation of every watched trader on an unchanged dossier is exactly
the budget burn FR-5 exists to prevent.

Thresholds are deliberately simple and documented, not tuned: >= 5 newly
resolved positions is enough new evidence to be worth another look;
either of the two most decision-relevant metrics (shrunk ROI, since it's
the ranking signal; hold-to-resolution rate, since the PRD itself calls
it the single most predictive field) moving by more than 15% relative is
the other trigger. Both configurable via config.yaml `mandate:` if these
turn out wrong in practice.
"""

from __future__ import annotations

import sqlite3

RESOLVED_POSITIONS_DELTA = 5
METRIC_DELTA_FRACTION = 0.15
DELTA_TRACKED_FIELDS = ("roi_shrunk", "hold_to_resolution_rate")


def _lookup_active_mandate(conn: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM mandates
        WHERE address = ? AND superseded_by IS NULL
        ORDER BY version DESC LIMIT 1
        """,
        (address,),
    ).fetchone()


def should_reevaluate(conn: sqlite3.Connection, address: str, latest_snapshot: sqlite3.Row) -> tuple[bool, str]:
    """Returns (should_call_llm, human-readable reason) — the reason is
    logged either way (see mandate/issue.py) so a "why didn't this trader
    get a mandate today" question has a real answer, not silence.
    """
    active = _lookup_active_mandate(conn, address)
    if active is None:
        return True, "no mandate has ever been issued for this trader"

    prior_snapshot = None
    if active["snapshot_id"] is not None:
        prior_snapshot = conn.execute(
            "SELECT * FROM trader_snapshots WHERE id = ?", (active["snapshot_id"],)
        ).fetchone()
    if prior_snapshot is None:
        return True, "active mandate has no linked snapshot to compare against"

    prior_resolved = prior_snapshot["resolved_positions"] or 0
    latest_resolved = latest_snapshot["resolved_positions"] or 0
    delta_resolved = latest_resolved - prior_resolved
    if delta_resolved >= RESOLVED_POSITIONS_DELTA:
        return True, f"{delta_resolved} newly resolved positions since the active mandate (>= {RESOLVED_POSITIONS_DELTA})"

    for field in DELTA_TRACKED_FIELDS:
        old, new = prior_snapshot[field], latest_snapshot[field]
        if old is None or new is None:
            continue
        if old == 0:
            continue  # can't express a relative move from zero
        relative_move = abs(new - old) / abs(old)
        if relative_move > METRIC_DELTA_FRACTION:
            return True, f"{field} moved {relative_move:.0%} since the active mandate (> {METRIC_DELTA_FRACTION:.0%})"

    return False, "no material change since the active mandate"
