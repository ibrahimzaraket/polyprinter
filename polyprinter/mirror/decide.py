"""Mandate lookup + caps -> TAKE / SKIP / MIRROR_EXIT (structure doc).

Entries (BUY) consult the trader's active mandate and the sizing caps in
sizing.py. Exits (SELL) never consult a mandate at all — invariant 2:
"Exits are never gated by mandate state." An exit only needs a matching
open position of ours; if the mandate that authorised the entry has since
expired or been withdrawn, we still get out.

Two mandate-driven checks from PRD FR-7 (category allow/block, minimum
market liquidity) are deliberately NOT implemented here: neither a
trade's category nor its market's liquidity is actually available on an
observed_trades row today. scout/dossier.py hit the identical gap
computing category_mix_json / median_market_liquidity — both need a
gamma-api event/tag lookup per market that hasn't been built (see that
file's field comments). Faking a match here would be worse than not
checking it, so this is an explicit, documented gap, same as dossier.py's,
not a silent omission.

None of this fires a real TAKE in practice until Phase 3 (Mandates)
exists to write a FOLLOW verdict — until then every entry resolves to
SKIP/NO_MANDATE before any of the price/sizing checks are even reached.
That's correct, not incomplete: it's what proves 100% decision coverage
(Phase 2's own exit criterion) without a live LLM in the loop yet.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from polyprinter.mirror import position_model, sizing

ENTRY_SIDES = {"BUY"}
EXIT_SIDES = {"SELL"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lookup_active_mandate(conn: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    """Latest non-superseded mandate version, regardless of expiry — same
    query the dashboard uses for "Active mandate" on trader_detail.html.
    Expiry is checked separately so NO_MANDATE and MANDATE_EXPIRED stay
    distinguishable reason codes.
    """
    return conn.execute(
        """
        SELECT * FROM mandates
        WHERE address = ? AND superseded_by IS NULL
        ORDER BY version DESC LIMIT 1
        """,
        (address,),
    ).fetchone()


def _their_position_before(conn: sqlite3.Connection, address: str, token_id: str, before_id: int) -> float:
    rows = conn.execute(
        "SELECT side, shares FROM observed_trades "
        "WHERE address = ? AND token_id = ? AND id < ? ORDER BY id",
        (address, token_id, before_id),
    ).fetchall()
    return position_model.running_position(
        position_model.TradeLeg(r["side"], r["shares"]) for r in rows
    )


def _find_open_position(conn: sqlite3.Connection, address: str, token_id: str, mode: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM positions
        WHERE address = ? AND token_id = ? AND mode = ? AND status IN ('OPEN', 'PARTIAL')
        ORDER BY opened_at LIMIT 1
        """,
        (address, token_id, mode),
    ).fetchone()


def _skip(code: str, text: str, *, mandate_id: int | None = None) -> dict[str, Any]:
    return {
        "verdict": "SKIP",
        "skip_reason_code": code,
        "skip_reason_text": text,
        "size_usd": None,
        "mandate_id": mandate_id,
    }


def decide_entry(
    conn: sqlite3.Connection,
    trade: sqlite3.Row,
    *,
    mode: str,
    mirror_config: dict,
    their_balance_usd: float | None = None,
) -> dict[str, Any]:
    """`their_balance_usd` is only consulted when the active mandate's
    sizing_mode is 'balance_matched' (mandate/operator.py) — the caller
    (watch_poll.py/watch_events.py) fetches it via
    PolymarketDataClient.value() only for a trader who actually has such
    a mandate, so this function itself stays a pure DB read with no
    network dependency of its own; every other mandate keeps sizing
    exactly as before regardless of what's passed here.
    """
    mandate = _lookup_active_mandate(conn, trade["address"])
    if mandate is None:
        return _skip("NO_MANDATE", "No mandate has ever been issued for this trader (Mandates ship in Phase 3).")

    expires_at = datetime.fromisoformat(mandate["expires_at"])
    if expires_at <= _now():
        return _skip(
            "MANDATE_EXPIRED",
            f"Mandate v{mandate['version']} expired {mandate['expires_at']}.",
            mandate_id=mandate["id"],
        )
    if mandate["verdict"] != "FOLLOW":
        return _skip(
            "MANDATE_NOT_FOLLOW",
            f"Active mandate verdict is {mandate['verdict']}, not FOLLOW.",
            mandate_id=mandate["id"],
        )

    price = trade["price"]
    if mandate["min_entry_price"] is not None and price < mandate["min_entry_price"]:
        return _skip(
            "PRICE_BAND",
            f"Entry price {price} below mandate floor {mandate['min_entry_price']}.",
            mandate_id=mandate["id"],
        )
    if mandate["max_entry_price"] is not None and price > mandate["max_entry_price"]:
        return _skip(
            "PRICE_BAND",
            f"Entry price {price} above mandate ceiling {mandate['max_entry_price']}.",
            mandate_id=mandate["id"],
        )

    requested_usd = trade["shares"] * price
    bankroll = mirror_config["paper_bankroll_usd"]

    if mandate["sizing_mode"] == "balance_matched":
        # Our own total bankroll (free capital + whatever's already
        # deployed) — the same balance concept their side's
        # PolymarketDataClient.value() represents, so the ratio compares
        # like with like.
        our_balance = bankroll
        matched = sizing.balance_matched_size(
            their_trade_usd=requested_usd,
            their_balance_usd=their_balance_usd,
            our_balance_usd=our_balance,
            multiplier=mandate["size_multiplier"] or 1.0,
        )
        if matched is None:
            return _skip(
                "NO_BALANCE_DATA",
                "Mandate is balance-matched but their current portfolio value is unknown or zero — can't size proportionally.",
                mandate_id=mandate["id"],
            )
        size_usd = sizing.per_trade_cap(matched, mandate["max_position_usd"])
    else:
        size_usd = sizing.per_trade_cap(requested_usd, mandate["max_position_usd"])

    if sizing.available_capital_usd(conn, mode, bankroll) < size_usd:
        return _skip(
            "NO_CAPITAL",
            f"${size_usd:.2f} requested; bankroll ${bankroll:.2f} doesn't have that much free.",
            mandate_id=mandate["id"],
        )
    portfolio_cap = mirror_config["portfolio_exposure_cap_usd"]
    if not sizing.check_portfolio_cap(conn, mode, size_usd, portfolio_cap):
        return _skip(
            "PORTFOLIO_CAP",
            f"Would push total exposure past the ${portfolio_cap:.2f} portfolio cap.",
            mandate_id=mandate["id"],
        )
    correlation_cap = mirror_config["correlation_cap_usd"]
    if not sizing.check_correlation_cap(conn, trade["market_id"], mode, size_usd, correlation_cap):
        return _skip(
            "CORRELATION_CAP",
            f"Would push this market's total exposure past the ${correlation_cap:.2f} correlation cap.",
            mandate_id=mandate["id"],
        )

    return {
        "verdict": "TAKE",
        "skip_reason_code": None,
        "skip_reason_text": None,
        "size_usd": size_usd,
        "mandate_id": mandate["id"],
    }


def decide_exit(conn: sqlite3.Connection, trade: sqlite3.Row, *, mode: str) -> dict[str, Any]:
    position = _find_open_position(conn, trade["address"], trade["token_id"], mode)
    if position is None:
        return _skip(
            "NO_MATCHING_POSITION",
            "We hold no open position in this token to exit — we never took the entry.",
        )

    position_before = _their_position_before(conn, trade["address"], trade["token_id"], trade["id"])
    fraction = position_model.fraction_of(position_before, trade["shares"])
    if fraction is None:
        # Can't size the fraction without a known prior position (our poll
        # history doesn't cover their entry). Fully exiting is the safer
        # failure mode than guessing a partial fraction — invariant 2
        # exists precisely to prevent an orphaned position, and holding
        # onto an unknown-sized remainder is a worse outcome than closing
        # early.
        fraction = 1.0

    size_usd = position["shares_open"] * fraction * trade["price"]
    return {
        "verdict": "MIRROR_EXIT",
        "skip_reason_code": None,
        "skip_reason_text": None,
        "size_usd": size_usd,
        "mandate_id": None,  # exits are never mandate-gated (invariant 2)
        "position_id": position["id"],
        "fraction": fraction,
    }


def decide(
    conn: sqlite3.Connection,
    trade: sqlite3.Row,
    *,
    mode: str,
    mirror_config: dict,
    their_balance_usd: float | None = None,
) -> dict[str, Any]:
    """trade: an already-inserted observed_trades row (must have an id —
    decide_exit needs it to look at what came before)."""
    if trade["side"] in ENTRY_SIDES:
        return decide_entry(conn, trade, mode=mode, mirror_config=mirror_config, their_balance_usd=their_balance_usd)
    if trade["side"] in EXIT_SIDES:
        return decide_exit(conn, trade, mode=mode)
    return _skip("UNKNOWN_SIDE", f"observed side {trade['side']!r} is neither BUY nor SELL.")
