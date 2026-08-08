"""Dossier -> a prompt asking for a plain-English explanation of what a
trader is actually doing — not whether to follow them (that's
mandate/prompt.py's job, a different question with a different answer
shape). Reuses the same "define every term inline" discipline that
prompt found necessary live (2026-08-08): a jargon-only prompt reads as
confusing, not informative, to a model with no prior context on this
project.
"""

from __future__ import annotations

import sqlite3

PROMPT_TEMPLATE = """You are writing a short, honest, plain-English explanation of one Polymarket trader's actual betting pattern, for a dashboard a non-expert operator reads. Do NOT recommend whether to copy them — that's decided elsewhere. Just explain what the numbers below show, as if to someone who understands "betting on yes/no outcomes" but nothing about this system's own jargon.

What the numbers mean, so you use them correctly:
- "Shrunk ROI" is return on investment after a statistical correction toward the population average (stronger for traders with fewer trades) — it exists so a lucky short streak can't look like a proven track record. Prefer this over "raw ROI" when describing their skill level.
- "Hold-to-resolution rate" is the share of their closed positions they held until the market resolved, instead of selling early. High means their edge (if any) doesn't depend on precise exit timing. Low means it might, which matters for whether their style is even describable from entries alone.
- "Profit concentration (top-1/top-5)" is the share of total profit from just their best trade, or best five. High concentration means "mostly one big result carried everything" — worth saying plainly, not glossing over.
- "Entry price" (1-99 cents, roughly a probability) shows what odds they typically buy into. Consistently sub-10-cent entries means longshot-hunting; entries clustered near 50 means they're trading close calls; entries near 90+ means they're betting on things they think are very likely.
- "Sizing pattern (CV)" is coefficient of variation of position size. Low = same bet size every time (flat sizing). High = they size up when apparently more confident (conviction sizing) — worth naming as a real signal if present.
- "Realized P&L" figures are actual locked-in profit in USD, for the last 24 hours, last 7 days, and lifetime — use these to say how active/recently profitable they've been, not just their ratio-based ROI.

Trader dossier:
- Address: {address}
- Alias: {alias}
- Active: {active}

Performance:
- Shrunk ROI: {roi_shrunk}
- Raw ROI: {roi_raw}
- Win rate: {win_rate}
- Realized P&L — 24h / 7d / lifetime (USD): {realised_pnl_24h_usd} / {realised_pnl_7d_usd} / {realised_pnl_usd}
- Resolved positions: {resolved_positions}
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

Write a `headline` (one short sentence, under ~140 characters, capturing their style) and a `summary` (2-4 plain sentences: what they trade, how they size, how they hold, and whether their profit looks repeatable or concentrated in a few lucky/skilled results). Ground every claim in the actual numbers above — do not invent behavior the dossier doesn't show."""


def _fmt(value: object) -> str:
    if value is None:
        return "unknown (not enough data yet)"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def build_prompt(trader: sqlite3.Row, snapshot: sqlite3.Row) -> str:
    """trader: a `traders` row. snapshot: that trader's latest
    `trader_snapshots` row. Both required, same precondition as
    mandate/prompt.py's build_prompt."""
    fields = {
        "address": trader["address"],
        "alias": trader["alias"] or "(none)",
        "active": "yes" if trader["active"] else "no",
        "roi_shrunk": _fmt(snapshot["roi_shrunk"]),
        "roi_raw": _fmt(snapshot["roi_raw"]),
        "win_rate": _fmt(snapshot["win_rate"]),
        "realised_pnl_24h_usd": _fmt(snapshot["realised_pnl_24h_usd"]),
        "realised_pnl_7d_usd": _fmt(snapshot["realised_pnl_7d_usd"]),
        "realised_pnl_usd": _fmt(snapshot["realised_pnl_usd"]),
        "resolved_positions": _fmt(snapshot["resolved_positions"]),
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
