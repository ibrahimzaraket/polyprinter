"""Their running position per token (FR-12) — pure functions, no I/O.

Mirror needs to know how big a trader's holding in a given token was
*immediately before* a new trade, for two reasons: it's what `their_position_after`
(observed_trades) records, and it's the denominator for proportional
mirroring (FR-13) — "they sold 40% of their holding" only means something
relative to how much they had.

Kept pure and DB-free so it's trivial to unit test: callers replay a
trader's own chronological observed_trades for one token through
`running_position`, then hand the pre-trade total to `fraction_of`.
"""

from __future__ import annotations

from typing import Iterable, NamedTuple


class TradeLeg(NamedTuple):
    side: str  # 'BUY' | 'SELL'
    shares: float


def running_position(events: Iterable[TradeLeg]) -> float:
    """Net shares held after replaying `events` in chronological order
    (oldest first). BUY adds, SELL subtracts. Never clamped at zero —
    a negative result means our own record is short a BUY we haven't
    observed (e.g. we started polling mid-position), which callers should
    treat as "unknown," not "short," since Polymarket positions can't
    actually go negative.
    """
    position = 0.0
    for leg in events:
        if leg.side == "BUY":
            position += leg.shares
        elif leg.side == "SELL":
            position -= leg.shares
        # any other side value is a data error upstream, not this
        # function's job to police — it just doesn't move the total.
    return position


def fraction_of(position_before: float | None, shares_moved: float) -> float | None:
    """What fraction of their pre-trade position this many shares
    represents — the "40%" in "they sold 40% of their holding."

    Returns None when position_before is missing or non-positive: with no
    known prior position (most likely because our poll window started
    after they'd already built it), a fraction would be meaningless, not
    just imprecise. Callers should treat None as "can't size proportionally
    from this," not as zero.

    Clamped to [0, 1] — real-world float drift or an incomplete observed
    history can otherwise produce a fraction slightly over 1.0 or (for a
    partial-history BUY-side edge case) slightly negative; both are
    reported as the nearest valid fraction rather than propagated.
    """
    if position_before is None or position_before <= 0:
        return None
    return max(0.0, min(1.0, shares_moved / position_before))
