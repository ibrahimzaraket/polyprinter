"""Turns a decide.py verdict into real bookkeeping (FR-20/21): opens a
`positions` row on TAKE, writes a `position_exits` row (and updates the
parent position) on MIRROR_EXIT.

decide.py only computes what SHOULD happen; this is what actually DOES
it — kept as a separate step (not inlined into decide.py or watch_poll.py)
so decide.py stays a pure decision function, easy to unit test without
touching the DB's write side, and this module's whole job is the write
side, easy to unit test against a real temp db the same way sizing.py's
tests already are.

Found by code review 2026-08-08, not a live incident — no FOLLOW mandate
had fired yet: decide.py could return TAKE $X or MIRROR_EXIT, watch_poll.py
would write the `decisions` row, and nothing downstream of that ever
existed. sizing.py's portfolio/correlation caps (which query `positions`)
would have kept reading $0 exposure forever, and the paper portfolio this
whole project exists to measure (PRD §5) would have silently not existed
the moment a real FOLLOW verdict landed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from polyprinter.mirror.fills import approximate_fill

_ZERO_EPSILON = 1e-9


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_position(
    conn: sqlite3.Connection, *, decision_id: int, trade: sqlite3.Row, size_usd: float, mode: str
) -> int | None:
    """Opens one `positions` row from a TAKE decision. Returns its id, or
    None for a degenerate $0 TAKE (a schema-valid but practically-empty
    mandate, e.g. max_position_usd: 0) — nothing to open, and the
    alternative is approximate_fill() raising, which would abort the rest
    of this trader's trades for the cycle over a decision that wasn't
    real money anyway.

    `trade` is the observed_trades row that triggered the TAKE. Its price
    is the trader's own fill; approximate_fill() uses that as our fill too
    (see fills.py's module docstring on why, and on what replaces this the
    moment a live order book exists). Fees are folded into cost_usd — the
    cost basis sizing.py's caps sum against should reflect capital actually
    committed, not bare notional.
    """
    if size_usd <= 0:
        return None
    fill = approximate_fill(trade["price"], size_usd)
    now = _now_iso()
    cur = conn.execute(
        """
        INSERT INTO positions (
            decision_id, address, market_id, token_id, mode,
            shares_open, shares_total, our_entry_price, their_entry_price,
            cost_usd, opened_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')
        """,
        (
            decision_id, trade["address"], trade["market_id"], trade["token_id"], mode,
            fill.shares, fill.shares, fill.avg_price, trade["price"],
            fill.total_usd, now,
        ),
    )
    return cur.lastrowid


def record_exit(
    conn: sqlite3.Connection, *, decision_id: int, position: sqlite3.Row, trade: sqlite3.Row, fraction: float
) -> None:
    """Writes one `position_exits` row for a MIRROR_EXIT decision and
    updates the parent position's shares_open/status. One row per leg
    (FR-13, proportional mirroring) — a position can take several partial
    exits before it's fully CLOSED, not just one close-everything event.

    `fraction` is decide.decide_exit's fraction-of-THEIR-pre-trade-position
    (position_model.fraction_of), already clamped to [0, 1] there — applied
    here to OUR shares_open, same as decide_exit's own size_usd math, so
    the two stay consistent (decide_exit computes what the exit is worth;
    this is what actually happens to our position because of it).
    """
    shares_sold = position["shares_open"] * fraction
    now = _now_iso()

    if shares_sold <= _ZERO_EPSILON:
        # fraction is clamped to [0, 1] upstream (position_model.fraction_of)
        # and should never actually be 0 for a real trade, but a dust-sized
        # remainder (float drift from a prior partial exit) is possible.
        # approximate_fill() requires size_usd > 0 — rather than raise here
        # and risk a TAKE/MIRROR_EXIT decision row standing with no
        # bookkeeping behind it (the exact bug this module exists to close),
        # a dust exit is a no-op: nothing meaningful to record or close.
        return

    # approximate_fill's Fill fields are named from a BUY's perspective
    # (cost_usd/fee_usd/total_usd), but the notional/fee math is symmetric
    # for a sell: cost_usd is the gross notional of what we're selling,
    # fee_usd the taker fee on it, so proceeds = cost_usd - fee_usd.
    fill = approximate_fill(trade["price"], shares_sold * trade["price"])

    conn.execute(
        """
        INSERT INTO position_exits (
            position_id, decision_id, shares_sold, fraction_of_theirs,
            our_exit_price, their_exit_price, proceeds_usd, exited_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position["id"], decision_id, shares_sold, fraction,
            fill.avg_price, trade["price"], fill.cost_usd - fill.fee_usd, now,
        ),
    )

    shares_open_after = max(position["shares_open"] - shares_sold, 0.0)
    fully_closed = shares_open_after <= _ZERO_EPSILON
    conn.execute(
        """
        UPDATE positions
        SET shares_open = ?, status = ?, closed_at = ?
        WHERE id = ?
        """,
        (
            0.0 if fully_closed else shares_open_after,
            "CLOSED" if fully_closed else "PARTIAL",
            now if fully_closed else position["closed_at"],
            position["id"],
        ),
    )
