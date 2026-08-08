"""Drop everything Scout knows about a trader who isn't lifetime-profitable
(operator's explicit choice, 2026-08-08) — the biggest share of
`raw_responses`' storage growth is full activity/position history for
candidates nobody will ever mirror; keeping it forever for someone who's
net-negative serves no purpose this project has.

This is a deliberate, narrow exception to docs/SCHEMA.md invariant 3
(`trader_snapshots` is append-only, the trajectory is signal) and to the
general "store every raw response" rule — both still apply in full to
anyone who's ever actually been watched or mandated: `has_been_acted_upon`
is the hard guard that keeps this from ever touching decision/audit
history (G3 — every decision inspectable — does not get relaxed by this).
It only ever deletes pure discovery/scoring data for a trader who was
looked at and never acted on.

"Lifetime profitable" = the dossier's own `realised_pnl_usd` (cumulative
closed + open realized P&L, computed fresh every run in scout/dossier.py)
is strictly positive. None (not enough data to compute it at all — e.g. a
brand-new address with no resolved positions yet) is treated as "keep,
not enough evidence to call unprofitable" — this is about removing people
we've confirmed lose money, not people we simply don't know about yet.
"""

from __future__ import annotations

import sqlite3

from polyprinter.config import load_config


def is_lifetime_profitable(realised_pnl_usd: float | None) -> bool:
    return realised_pnl_usd is not None and realised_pnl_usd > 0


def has_been_acted_upon(conn: sqlite3.Connection, address: str) -> bool:
    """True if this trader has ever had a mandate issued, ever been
    watched by Mirror (an observed_trades row exists), or is currently
    manually pinned (mirror.pinned_addresses — an explicit "track this
    person" is just as strong a signal as Mirror having actually traded
    with them, 2026-08-08) — the guard that keeps purge_trader from ever
    deleting audited history or overriding an operator's explicit choice.
    """
    address = address.lower()
    pinned = {a.lower() for a in load_config().get("mirror", {}).get("pinned_addresses", []) if a}
    if address in pinned:
        return True
    for table, column in (
        ("mandates", "address"),
        ("observed_trades", "address"),
    ):
        row = conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ? LIMIT 1", (address,)).fetchone()
        if row is not None:
            return True
    return False


def purge_trader(conn: sqlite3.Connection, address: str) -> None:
    """Deletes every row this project has ever stored about `address`:
    trader_snapshots, data-api raw_responses that mention them, and the
    traders row itself. Caller MUST have already checked
    has_been_acted_upon() is False — this function doesn't check it again,
    to keep it a pure "delete everything" primitive that's easy to reason
    about and easy to unit test in isolation.

    raw_responses has no address column (see sources/raw_store.py) — its
    `url` carries `user=<address>` for every data-api call this project
    makes (leaderboard responses are the one exception: they list many
    traders in one shared response body and are left alone, both because
    they can't be attributed to a single address and because they're a
    small, bounded share of total bytes — see the storage breakdown that
    motivated this file).
    """
    conn.execute(
        "DELETE FROM raw_responses WHERE source = 'data-api' AND url LIKE ('%user=' || ? || '%')",
        (address,),
    )
    conn.execute("DELETE FROM trader_snapshots WHERE address = ?", (address,))
    conn.execute("DELETE FROM traders WHERE address = ?", (address,))
