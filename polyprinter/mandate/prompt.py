"""Dossier -> prompt (FR-7). One trader's latest snapshot, formatted with
enough explanation that a model with no prior context on this project can
weigh it correctly — an early live test (2026-08-08) showed a terse,
jargon-only prompt ("22% shrunk ROI, 400 trades...") gets misread as
confusing rather than informative. Every term is defined inline, in the
same plain language as the dashboard's own Definitions tab, not assumed.
"""

from __future__ import annotations

import sqlite3

PROMPT_TEMPLATE = """You are issuing a trading mandate for a copy-trading system that mirrors real Polymarket traders with paper money (no real capital is at risk yet). You will be shown one trader's latest performance dossier. Decide whether we should FOLLOW them (mirror their future trades), WATCH them (not enough evidence either way yet), or SKIP them (do not mirror).

Background on what these numbers mean:
- "Shrunk ROI" is their return on investment after a correction that pulls it toward the population average, more strongly for traders with fewer trades — it exists specifically so a lucky 6-trade streak can't outrank a proven 400-trade track record. Trust this number over "raw ROI".
- "Hold-to-resolution rate" is the single most predictive field here: it's the share of their closed positions they held until the market resolved, rather than selling early. If their edge comes from exact exit timing, we structurally cannot copy it — our detection and reaction always lag theirs by some amount. A trader who mostly holds to resolution has an edge that survives that lag; a trader who mostly exits early does not.
- "Profit concentration (top-1/top-5)" is the share of their total profit that came from just their single best trade, or their five best. High concentration means their track record is mostly one lucky (or skilled, you cannot tell from this number alone) result carrying everything else.
- "Entry price" distribution shows what price range (1-99 cents, roughly a probability) they typically buy into. Traders who mostly buy sub-10-cent longshots are effectively uncopyable at a small paper bankroll — the position sizes and number of losing bets needed to make that style work don't fit a $1,000 paper account.
- "Sizing pattern (CV)" — coefficient of variation of position size. Low means they bet the same amount every time (flat sizing, no extra signal in bet size); high means they size up when confident (conviction sizing, worth paying attention to).

Trader dossier:
- Address: {address}
- Alias: {alias}
- First seen: {first_seen}
- Active: {active}

Performance:
- Shrunk ROI: {roi_shrunk}
- Raw ROI: {roi_raw}
- Win rate: {win_rate}
- Avg win / avg loss (USD): {avg_win_usd} / {avg_loss_usd}
- Win/loss ratio: {win_loss_ratio}
- Resolved positions: {resolved_positions}
- Capital deployed (USD): {capital_deployed_usd}
- Profit concentration top-1 / top-5: {concentration_top1} / {concentration_top5}

Copyability:
- Hold-to-resolution rate: {hold_to_resolution_rate}
- Median / p90 holding period (hours): {median_hold_hours} / {p90_hold_hours}
- Entry price p10 / median / p90: {entry_price_p10} / {entry_price_median} / {entry_price_p90}
- Sizing pattern (CV): {sizing_cv}
- Scale-in/out frequency: {scale_frequency}

Liveness:
- Trades in last 7d / 30d: {trades_7d} / {trades_30d}
- Open positions: {open_positions}

Note: category mix and market liquidity are not available for this trader (a known data gap in this system — see the dossier's own documentation) — do not assume a category allow/block or minimum-liquidity constraint will actually be enforced; whatever you put there is advisory only right now.

Issue a mandate: a verdict (FOLLOW/WATCH/SKIP), a confidence level (LOW/MED/HIGH), and reasoning in prose that references the actual numbers above, not generic advice. If you set max_position_usd, min_entry_price, or max_entry_price, they will be enforced exactly as given by the trading system, so only set them if you mean it."""


def _fmt(value: object) -> str:
    if value is None:
        return "unknown (not enough data yet)"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def build_prompt(trader: sqlite3.Row, snapshot: sqlite3.Row) -> str:
    """trader: a `traders` row. snapshot: that trader's latest
    `trader_snapshots` row. Both required — a trader with no snapshot yet
    has nothing to issue a mandate from (mandate/trigger.py never calls
    this for one)."""
    fields = {
        "address": trader["address"],
        "alias": trader["alias"] or "(none)",
        "first_seen": trader["first_seen"],
        "active": "yes" if trader["active"] else "no",
        "roi_shrunk": _fmt(snapshot["roi_shrunk"]),
        "roi_raw": _fmt(snapshot["roi_raw"]),
        "win_rate": _fmt(snapshot["win_rate"]),
        "avg_win_usd": _fmt(snapshot["avg_win_usd"]),
        "avg_loss_usd": _fmt(snapshot["avg_loss_usd"]),
        "win_loss_ratio": _fmt(snapshot["win_loss_ratio"]),
        "resolved_positions": _fmt(snapshot["resolved_positions"]),
        "capital_deployed_usd": _fmt(snapshot["capital_deployed_usd"]),
        "concentration_top1": _fmt(snapshot["concentration_top1"]),
        "concentration_top5": _fmt(snapshot["concentration_top5"]),
        "hold_to_resolution_rate": _fmt(snapshot["hold_to_resolution_rate"]),
        "median_hold_hours": _fmt(snapshot["median_hold_hours"]),
        "p90_hold_hours": _fmt(snapshot["p90_hold_hours"]),
        "entry_price_p10": _fmt(snapshot["entry_price_p10"]),
        "entry_price_median": _fmt(snapshot["entry_price_median"]),
        "entry_price_p90": _fmt(snapshot["entry_price_p90"]),
        "sizing_cv": _fmt(snapshot["sizing_cv"]),
        "scale_frequency": _fmt(snapshot["scale_frequency"]),
        "trades_7d": _fmt(snapshot["trades_7d"]),
        "trades_30d": _fmt(snapshot["trades_30d"]),
        "open_positions": _fmt(snapshot["open_positions"]),
    }
    return PROMPT_TEMPLATE.format(**fields)
