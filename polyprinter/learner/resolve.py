"""Learner (SCHEMA.md invariant 5: "Learner owns outcomes") — the module
that actually resolves a `positions` row into an `outcomes` row. Built
2026-08-08 once Mirror started producing real paper TAKEs (11 open
positions, 0 outcomes): without this, positions can open forever and
never resolve, and Portfolio's realized P&L/equity curve/copy tax stay
empty no matter how long the system runs — nothing in the codebase wrote
to `outcomes` before this file existed.

This is the prerequisite half of PRD §6.6's "Learner (weekly)" — the
calibration table (FR-23) and shadow benchmark (FR-24) both READ
`outcomes` and can't do anything meaningful until real rows exist here.
Deliberately not those: calibrate.py/shadow.py are separate, later,
analytics-shaped modules over data this one produces; this one just
turns a closed book into ground truth, and runs far more often than
"weekly" so Portfolio doesn't sit stale for a week per resolution.

Two independent paths into `outcomes`, both handled by run_once():

1. **Already fully exited** (`positions.status = 'CLOSED'`, shares_open
   = 0 via one or more mirrored sells) — 100% of the economics are
   already known from `position_exits` rows alone. No gamma-api call
   needed; resolvable the instant it closes.
2. **Still open when the market resolves** (`status` OPEN/PARTIAL) — the
   remaining `shares_open` redeem at Polymarket's own resolved price
   (1.0 for the winning outcome, 0.0 otherwise; occasionally something
   else for an unusual resolution like the 50-50 case some markets
   define). Modeled as a synthetic final "exit leg" at that price for
   both sides — see `compute_position_outcome`'s docstring for why that
   leg always contributes exactly 0 copy tax.

Redemption is a claim against the CTF contract, not a taker trade against
an order book — no fee is modeled on it (unlike `fills.py`'s
`approximate_fill`, which does apply one), and there's no execution lag to
be taxed on: both the trader we're copying and we ourselves redeem at
the same on-chain-settled price, whenever either of us gets around to
it. That's a modeling choice, not a verified-live fee schedule — flagged
here the same way `fills.py`'s own unmodeled-book-impact note is.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from polyprinter.scout.resolutions import fetch_resolutions
from polyprinter.sources.polymarket_gamma import PolymarketGammaClient

# Conservative relative to category_score.py's GAMMA_EVENTS_BATCH_SIZE=50
# for /events/keyset (itself already "no published limit, treated
# conservatively anyway" — see that module). condition_ids are full 66-char
# hex strings, meaningfully longer than an event slug; a live 403 was hit
# 2026-08-08 on a 50-slug /events/keyset batch whose query string was
# shorter than 50 condition_ids would produce, so this batches smaller
# rather than assume the same ceiling applies. Not separately
# verified-live at this size — the honest caveat, not a measured limit.
MARKETS_BATCH_SIZE = 20


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def compute_position_outcome(
    *, cost_usd: float, our_entry_price: float, their_entry_price: float, exit_legs: list[dict[str, float]]
) -> dict[str, float | None] | None:
    """Pure function: no db, no network — same spirit as
    category_score.py's compute_category_score, directly unit-testable
    with plain dicts. `exit_legs` is every leg that liquidates this
    position's shares, each `{shares, our_price, their_price,
    proceeds_usd}`: one per `position_exits` row (a mirrored sell) plus,
    for a position still open when its market resolved, one synthetic
    leg for the redeemed remainder (both prices equal — a redemption has
    no execution lag to tax, so that leg always contributes exactly 0 to
    copy_tax_exit_cents, correctly diluting the entry-side tax toward 0
    the more of the position was simply held to resolution).

    Shares-weighted average exit price on each side, not a simple mean
    across legs — a position exited in unequal-sized legs should have
    its exit price weighted by how much was actually sold at each price,
    same principle fills.py's own avg_price already uses for entries.

    Returns None if there's nothing to compute from (no legs, or zero
    total shares) — never a fabricated $0 outcome.
    """
    total_shares = sum(leg["shares"] for leg in exit_legs)
    if total_shares <= 0:
        return None

    total_proceeds = sum(leg["proceeds_usd"] for leg in exit_legs)
    our_exit_price = sum(leg["shares"] * leg["our_price"] for leg in exit_legs) / total_shares
    their_exit_price = sum(leg["shares"] * leg["their_price"] for leg in exit_legs) / total_shares

    our_pnl_usd = total_proceeds - cost_usd
    our_roi = our_pnl_usd / cost_usd if cost_usd else None

    # Sign convention matches docs/SCHEMA.md's own column comments:
    # positive = cost to us, on both the entry and exit side.
    copy_tax_entry_cents = (our_entry_price - their_entry_price) * 100
    copy_tax_exit_cents = (their_exit_price - our_exit_price) * 100
    copy_tax_total_cents = copy_tax_entry_cents + copy_tax_exit_cents

    their_pnl_per_share = their_exit_price - their_entry_price
    their_edge_cents = their_pnl_per_share * 100  # same units as copy_tax_total_cents — the ratio FR-23 gates go-live on

    return {
        "our_pnl_usd": our_pnl_usd,
        "our_roi": our_roi,
        "copy_tax_entry_cents": copy_tax_entry_cents,
        "copy_tax_exit_cents": copy_tax_exit_cents,
        "copy_tax_total_cents": copy_tax_total_cents,
        "their_pnl_per_share": their_pnl_per_share,
        "their_edge_cents": their_edge_cents,
    }


def _exit_legs_from_trades(conn: sqlite3.Connection, position_id: int) -> list[dict[str, float]]:
    rows = conn.execute(
        """
        SELECT shares_sold AS shares, our_exit_price AS our_price,
               their_exit_price AS their_price, proceeds_usd
        FROM position_exits WHERE position_id = ?
        """,
        (position_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _write_outcome(
    conn: sqlite3.Connection, position_id: int, outcome: dict[str, float | None], *, resolved_at: str | None, resolution: str | None
) -> int:
    cur = conn.execute(
        """
        INSERT INTO outcomes (
            position_id, resolved_at, resolution, our_pnl_usd, our_roi,
            their_pnl_per_share, copy_tax_entry_cents, copy_tax_exit_cents,
            copy_tax_total_cents, their_edge_cents
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position_id, resolved_at, resolution, outcome["our_pnl_usd"], outcome["our_roi"],
            outcome["their_pnl_per_share"], outcome["copy_tax_entry_cents"], outcome["copy_tax_exit_cents"],
            outcome["copy_tax_total_cents"], outcome["their_edge_cents"],
        ),
    )
    return cur.lastrowid


def close_fully_exited_position(conn: sqlite3.Connection, position: sqlite3.Row) -> int | None:
    """Path 1 — a CLOSED position (shares_open = 0 via mirrored sells
    alone). No gamma-api call: `position_exits` already has everything.
    Returns the new outcome's id, or None if there was nothing to write
    (a CLOSED position with zero exit legs would be a data-integrity gap
    — execute.py's record_exit always writes one before marking CLOSED —
    not a case to silently paper over with a fabricated outcome).
    """
    legs = _exit_legs_from_trades(conn, position["id"])
    if not legs:
        return None
    outcome = compute_position_outcome(
        cost_usd=position["cost_usd"], our_entry_price=position["our_entry_price"],
        their_entry_price=position["their_entry_price"], exit_legs=legs,
    )
    if outcome is None:
        return None
    return _write_outcome(conn, position["id"], outcome, resolved_at=position["closed_at"], resolution=None)


def resolve_open_position(conn: sqlite3.Connection, position: sqlite3.Row, resolution: dict[str, Any]) -> int | None:
    """Path 2 — an OPEN/PARTIAL position whose market has resolved.
    `resolution` is one value from scout.resolutions.fetch_resolutions()
    (or a plain dict of the same shape in tests) for this position's own
    market_id — the caller (run_once) only invokes this once a
    resolution is confirmed to exist for that market.

    Returns the new outcome's id, or None if `token_id` can't be matched
    against this resolution's `clob_token_ids` (a data-integrity gap —
    logged by the caller, not guessed past here) or there's nothing to
    compute (see compute_position_outcome). On success, also marks the
    position RESOLVED and zeroes shares_open (the remainder redeemed).
    """
    clob_token_ids = resolution.get("clob_token_ids") or []
    if position["token_id"] not in clob_token_ids:
        return None
    idx = clob_token_ids.index(position["token_id"])
    outcome_prices = resolution.get("outcome_prices") or []
    if idx >= len(outcome_prices):
        return None
    resolved_price = outcome_prices[idx]

    legs = _exit_legs_from_trades(conn, position["id"])
    if position["shares_open"] > 0:
        legs.append(
            {
                "shares": position["shares_open"],
                "our_price": resolved_price,
                "their_price": resolved_price,
                "proceeds_usd": position["shares_open"] * resolved_price,
            }
        )
    outcome = compute_position_outcome(
        cost_usd=position["cost_usd"], our_entry_price=position["our_entry_price"],
        their_entry_price=position["their_entry_price"], exit_legs=legs,
    )
    if outcome is None:
        return None

    resolution_label = None
    outcomes_list, prices_list = resolution.get("outcomes") or [], outcome_prices
    if outcomes_list and prices_list:
        winning_idx = max(range(len(prices_list)), key=lambda i: prices_list[i])
        if winning_idx < len(outcomes_list):
            resolution_label = outcomes_list[winning_idx]

    resolved_at = resolution.get("closed_time") or _now_iso()
    outcome_id = _write_outcome(conn, position["id"], outcome, resolved_at=resolved_at, resolution=resolution_label)
    conn.execute(
        "UPDATE positions SET status = 'RESOLVED', shares_open = 0.0, closed_at = COALESCE(closed_at, ?) WHERE id = ?",
        (resolved_at, position["id"]),
    )
    return outcome_id


def run_once(conn: sqlite3.Connection, log: Any, *, gamma_client: PolymarketGammaClient) -> dict[str, int]:
    """One full pass: resolve every CLOSED position with no outcome yet
    (Path 1, no network), then check every still-OPEN/PARTIAL position's
    market for a resolution (Path 2, batched gamma-api calls) and resolve
    whichever have one. Positions whose market hasn't resolved yet are
    left untouched — checked again next run, no different from Mirror
    re-polling a trader who hasn't traded.
    """
    n_closed_resolved = 0
    closed_candidates = conn.execute(
        "SELECT p.* FROM positions p LEFT JOIN outcomes o ON o.position_id = p.id "
        "WHERE p.status = 'CLOSED' AND o.id IS NULL"
    ).fetchall()
    for position in closed_candidates:
        try:
            if close_fully_exited_position(conn, position) is not None:
                n_closed_resolved += 1
        except Exception as exc:  # noqa: BLE001 — one bad position must not kill the run, same discipline as scout/mirror's own tick()
            log.error("resolve.closed_failed", position_id=position["id"], error=str(exc))

    open_candidates = conn.execute(
        "SELECT p.* FROM positions p LEFT JOIN outcomes o ON o.position_id = p.id "
        "WHERE p.status IN ('OPEN', 'PARTIAL') AND o.id IS NULL"
    ).fetchall()
    n_open_resolved = 0
    n_still_open = 0
    market_ids = sorted({p["market_id"] for p in open_candidates})
    resolutions: dict[str, dict[str, Any]] = {}
    for batch in _chunks(market_ids, MARKETS_BATCH_SIZE):
        try:
            resolutions.update(fetch_resolutions(gamma_client, batch))
        except Exception as exc:  # noqa: BLE001 — one failed batch shouldn't block every other batch or Path 1's already-committed work
            log.error("resolve.fetch_resolutions_failed", batch_size=len(batch), error=str(exc))

    for position in open_candidates:
        resolution = resolutions.get(position["market_id"])
        if resolution is None:
            n_still_open += 1
            continue
        try:
            if resolve_open_position(conn, position, resolution) is not None:
                n_open_resolved += 1
            else:
                log.error("resolve.token_not_matched", position_id=position["id"], market_id=position["market_id"])
                n_still_open += 1
        except Exception as exc:  # noqa: BLE001
            log.error("resolve.open_failed", position_id=position["id"], error=str(exc))
            n_still_open += 1

    conn.commit()
    result = {"n_closed_resolved": n_closed_resolved, "n_open_resolved": n_open_resolved, "n_still_open": n_still_open}
    log.info("resolve.done", **result)
    return result
