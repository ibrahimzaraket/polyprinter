"""Sizing caps for entries — per-trade, portfolio-wide, and correlation
(FR-16/17/18). v2's known gap was a per-trade cap with no portfolio-wide
cap; v3 carried that bug forward unfixed (PRD FR-16). All three checks
here are independent and additive: a TAKE must clear all of them.

Defaults live in config.yaml under `mirror:` — see that file for why
these particular numbers. None of this fires in practice until Phase 3
(Mandates) starts issuing FOLLOW verdicts; until then every entry is
SKIP/NO_MANDATE before sizing is ever consulted. Built now, tested now,
because `decide.py` needs somewhere real to call the moment mandates
exist — see docs/SCHEMA.md's structural rule about not skipping ahead
applying to *phases*, not to "write the plumbing a phase will need."
"""

from __future__ import annotations

import sqlite3


def per_trade_cap(requested_usd: float, mandate_max_usd: float | None) -> float:
    """The smaller of what we'd otherwise size this trade at and the
    mandate's own per-trade ceiling. No mandate ceiling (None) means no
    per-trade cap from the mandate side — the portfolio/correlation caps
    still apply independently.
    """
    if mandate_max_usd is None:
        return requested_usd
    return min(requested_usd, mandate_max_usd)


def portfolio_exposure_usd(conn: sqlite3.Connection, mode: str) -> float:
    """Total cost basis of every OPEN/PARTIAL position we hold, in this
    mode. Paper and live ledgers never mix (invariant 6) — always filtered
    by mode.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM positions "
        "WHERE mode = ? AND status IN ('OPEN', 'PARTIAL')",
        (mode,),
    ).fetchone()
    return row["total"]


def market_exposure_usd(conn: sqlite3.Connection, market_id: str, mode: str) -> float:
    """Total cost basis of every OPEN/PARTIAL position we hold in this one
    market, across ALL followed traders (FR-17) — ten traders all in one
    market is concentration, not diversification, so this is summed
    per-market, not per-trader.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM positions "
        "WHERE mode = ? AND market_id = ? AND status IN ('OPEN', 'PARTIAL')",
        (mode, market_id),
    ).fetchone()
    return row["total"]


def check_portfolio_cap(
    conn: sqlite3.Connection, mode: str, additional_usd: float, cap_usd: float
) -> bool:
    """True if adding `additional_usd` keeps total portfolio exposure at
    or under `cap_usd`.
    """
    return portfolio_exposure_usd(conn, mode) + additional_usd <= cap_usd


def check_correlation_cap(
    conn: sqlite3.Connection, market_id: str, mode: str, additional_usd: float, cap_usd: float
) -> bool:
    """True if adding `additional_usd` keeps this market's total exposure
    (across all followed traders) at or under `cap_usd`.
    """
    return market_exposure_usd(conn, market_id, mode) + additional_usd <= cap_usd


def available_capital_usd(conn: sqlite3.Connection, mode: str, bankroll_usd: float) -> float:
    """Paper (or live) bankroll minus what's already committed to open
    positions (FR-18) — the paper bankroll is finite, and capital locked
    in a 6-month market isn't available for the next trade.
    """
    return bankroll_usd - portfolio_exposure_usd(conn, mode)


def balance_matched_size(
    *, their_trade_usd: float, their_balance_usd: float | None, our_balance_usd: float, multiplier: float
) -> float | None:
    """Operator mandates' sizing_mode='balance_matched' (mandate/operator.py):
    if they put X% of their current portfolio value into this trade, we put
    X% of ours in too, scaled by an operator-editable multiplier (1.0 =
    exact proportional match; 0.5 = mirror at half conviction; 2.0 =
    amplify). `their_balance_usd` comes from PolymarketDataClient.value()
    (verified live 2026-08-08 to track their real current portfolio value,
    not raw on-chain USDC balance — active traders keep ~zero idle cash,
    so a wallet-balance lookup would be worthless here; see that method's
    own docstring).

    Returns None — not 0, not a fallback guess — when there isn't enough
    to compute a meaningful ratio from: no balance data, a non-positive
    balance, or a non-positive trade. A caller silently sizing $0 or
    falling back to some other cap would hide that this specific mandate
    mode couldn't actually be honored for this trade; None makes that an
    explicit, checkable case (same discipline as fraction_of() in
    position_model.py returning None rather than guessing).
    """
    if their_balance_usd is None or their_balance_usd <= 0:
        return None
    if their_trade_usd <= 0:
        return None
    their_pct = their_trade_usd / their_balance_usd
    return their_pct * our_balance_usd * multiplier
