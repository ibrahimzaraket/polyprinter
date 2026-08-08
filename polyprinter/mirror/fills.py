"""Fill simulation (FR-20/21/22): given an order book snapshot, walk it for
our size and report what we'd actually pay — never assume we got their
price.

Deliberately NOT wired to a live order book source this phase.
docs/SCHEMA.md's repo layout calls for `sources/polymarket_clob.py`
("order book snapshots [port from oracle/book.py]") to feed this — but
`oracle_legacy/` (the v2 code that file was meant to be ported from,
including its fee model) was never actually carried into this repo, and
the CLOB book endpoint itself hasn't been verified live the way every
other endpoint this project touches has (see docs/api-notes.md). Building
an unverified client against a brand-new endpoint, with no reference fee
model to check against, is exactly the kind of guess this project's own
rules exist to prevent (CLAUDE.md: verify live, don't infer a shape).

Phase 3 (Mandates) is live now, though, and the moment it issues a real
FOLLOW verdict a TAKE stops being hypothetical — see mirror/execute.py,
which wires decide.py's verdicts into real `positions`/`position_exits`
rows. It uses `approximate_fill()` below, not `walk_book()`, as its fill
source: `walk_book()` is kept exactly as originally built, a pure function
waiting for a real book snapshot, still with nothing feeding it. Swapping
`approximate_fill` for `walk_book` in execute.py is the entire migration
once a live-verified CLOB client exists — nothing else changes.
"""

from __future__ import annotations

from typing import NamedTuple


class BookLevel(NamedTuple):
    price: float
    size: float  # shares available at this price


class Fill(NamedTuple):
    shares: float
    avg_price: float
    cost_usd: float  # before fees
    fee_usd: float
    total_usd: float  # cost_usd + fee_usd


class InsufficientDepth(Exception):
    """The book doesn't have enough size to fill the requested amount."""


def walk_book(levels: list[BookLevel], size_usd: float, *, fee_bps: float = 200.0) -> Fill:
    """Walk `levels` (best price first — asks ascending for a BUY, bids
    descending for a SELL; the caller picks the right side) spending up to
    `size_usd` notional, crossing the spread level by level. `fee_bps` is
    Polymarket's taker fee in basis points of notional (200 bps = 2%,
    a placeholder until a verified live figure replaces it — see module
    docstring on why that verification hasn't happened yet).

    Raises InsufficientDepth if the book runs out before size_usd is
    spent — a caller silently accepting a partial fill as if it were the
    full size would misreport cost, which is worse than failing loudly.
    """
    if size_usd <= 0:
        raise ValueError("size_usd must be positive")

    remaining_usd = size_usd
    shares = 0.0
    cost_usd = 0.0

    for level in levels:
        if remaining_usd <= 0:
            break
        level_usd = level.price * level.size
        take_usd = min(remaining_usd, level_usd)
        take_shares = take_usd / level.price
        shares += take_shares
        cost_usd += take_usd
        remaining_usd -= take_usd

    if remaining_usd > 1e-9:
        raise InsufficientDepth(
            f"book only covers ${size_usd - remaining_usd:.2f} of the requested ${size_usd:.2f}"
        )

    avg_price = cost_usd / shares if shares else 0.0
    fee_usd = cost_usd * (fee_bps / 10_000.0)
    return Fill(shares=shares, avg_price=avg_price, cost_usd=cost_usd, fee_usd=fee_usd, total_usd=cost_usd + fee_usd)


def approximate_fill(price: float, size_usd: float, *, fee_bps: float = 200.0) -> Fill:
    """Stand-in for walk_book() until a live-verified order book exists —
    see the module docstring. Assumes we fill at the trader's own observed
    price with zero slippage, plus the same taker fee walk_book() charges.

    That's optimistic: a real book walk would very likely show worse
    execution than the price someone else already got. For a system whose
    whole point is measuring whether copy tax is survivable (PRD §5), that's
    the safer direction to be wrong in — it under-, not over-, states the
    tax, so it can't manufacture a false kill signal. It's still a real
    number to build positions/decisions bookkeeping against today, not a
    silent no-op.

    `price` must be > 0 (an observed_trades row's price always is, since
    Polymarket prices are cents-on-the-dollar probabilities) and `size_usd`
    must be positive — same contract as walk_book().
    """
    if price <= 0:
        raise ValueError("price must be positive")
    if size_usd <= 0:
        raise ValueError("size_usd must be positive")

    shares = size_usd / price
    fee_usd = size_usd * (fee_bps / 10_000.0)
    return Fill(shares=shares, avg_price=price, cost_usd=size_usd, fee_usd=fee_usd, total_usd=size_usd + fee_usd)
