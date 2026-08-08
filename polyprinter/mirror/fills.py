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

It's also genuinely inert either way: fills only apply to a TAKE, and no
TAKE can happen until Phase 3 (Mandates) exists — see decide.py. So this
is built and unit-tested as a pure function now, ready for whichever
comes first: a live-verified CLOB client, or Phase 3 needing it wired up.
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
