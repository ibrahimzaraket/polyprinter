"""Fast lane: which addresses have earned the on-chain path actually
driving their decisions, instead of only logging for comparison
(watch_events.py's default — see that module's docstring on why).

Deliberately ONE function, imported by both watch_poll.py and
watch_events.py, so there is exactly one answer to "who's fast-laned
right now" — never two independently-computed lists that could disagree.
That single-source-of-truth property is the entire safety mechanism
against double-executing a trade: watch_poll.py skips decide()/execute()
for anyone this function returns, watch_events.py is the only one who
calls them for that address. Get this list wrong in one caller but not
the other and a trade gets acted on twice.

Membership: pinned (mirror.pinned_addresses — an explicit "watch this
person") AND holding an active operator mandate with fast_lane=1 (an
explicit "and trust the fast path for them specifically"). Both
conditions matter on purpose — pinning alone just means "always watch",
not "skip the proven path for them"; that second, narrower yes has to be
deliberate.
"""

from __future__ import annotations

import sqlite3

from polyprinter.mirror import watch_poll


def fast_lane_addresses(conn: sqlite3.Connection) -> set[str]:
    pinned = watch_poll._pinned_addresses()
    if not pinned:
        return set()
    placeholders = ",".join("?" * len(pinned))
    rows = conn.execute(
        f"""
        SELECT address FROM mandates
        WHERE address IN ({placeholders})
          AND superseded_by IS NULL
          AND issued_by = 'operator'
          AND fast_lane = 1
          AND expires_at > datetime('now')
        """,
        tuple(pinned),
    ).fetchall()
    return {r["address"] for r in rows}
