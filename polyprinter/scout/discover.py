"""Candidate discovery: leaderboard union across all 4 windows (FR-1) PLUS
non-profit sampling (FR-2 / Audit F4) — selecting only on realised profit
is the exact bias the whole system exists to defeat.

FR-1's "volume and profit variants where available" and FR-2's "sample
candidates not selected on profit... e.g. top traders by volume" turn out to
be the same lever: the leaderboard's `orderBy` param (PNL | VOL, verified
live — docs/PRD.md §9). So each window is pulled twice: orderBy=PNL feeds
the profit-selected pool (FR-1), orderBy=VOL feeds the non-profit-selected
pool (FR-2).

Documented gap: FR-2 also suggests sampling by "resolved-position count."
Polymarket's public API has no endpoint that ranks all users by resolved
position count — verified against the full data-api OpenAPI spec, nothing
like it exists (see docs/api-notes.md). There's no way to sample on that
axis without already having a candidate pool to count within, which is
circular. Volume sampling is the concrete, available instance of FR-2;
resolved-count sampling is not implementable against the real API today.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from polyprinter.sources.polymarket_data import PolymarketDataClient, OrderBy, TimePeriod

WINDOWS: list[TimePeriod] = ["DAY", "WEEK", "MONTH", "ALL"]

_SOURCE_BY_WINDOW_AND_ORDER: dict[tuple[TimePeriod, OrderBy], str] = {
    ("DAY", "PNL"): "lb_day",
    ("WEEK", "PNL"): "lb_week",
    ("MONTH", "PNL"): "lb_month",
    ("ALL", "PNL"): "lb_all",
    ("DAY", "VOL"): "volume_sample",
    ("WEEK", "VOL"): "volume_sample",
    ("MONTH", "VOL"): "volume_sample",
    ("ALL", "VOL"): "volume_sample",
}


@dataclass(frozen=True)
class Candidate:
    address: str
    user_name: str
    discovery_source: str
    vol: float
    pnl: float


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def discover_candidates(
    client: PolymarketDataClient, *, per_window_limit: int = 50
) -> list[Candidate]:
    """Union candidates across all 4 leaderboard windows x {PNL, VOL}
    orderings. De-duplicated by address, first discovery_source wins (an
    address found by lb_day AND volume_sample keeps whichever we saw first
    — the important thing is it's IN the pool, not which label it carries).
    """
    seen: dict[str, Candidate] = {}
    for window in WINDOWS:
        for order_by in ("PNL", "VOL"):
            rows = client.leaderboard(
                time_period=window, order_by=order_by, limit=per_window_limit  # type: ignore[arg-type]
            )
            source = _SOURCE_BY_WINDOW_AND_ORDER[(window, order_by)]  # type: ignore[index]
            for row in rows:
                address = row["proxyWallet"].lower()
                if address not in seen:
                    seen[address] = Candidate(
                        address=address,
                        user_name=row.get("userName") or "",
                        discovery_source=source,
                        vol=row.get("vol") or 0.0,
                        pnl=row.get("pnl") or 0.0,
                    )
    return list(seen.values())


def upsert_traders(conn: sqlite3.Connection, candidates: list[Candidate]) -> None:
    """traders is Scout-owned (invariant 5). INSERT new addresses only —
    discovery_source and first_seen are set once and never overwritten;
    last_trade_at is updated by dossier computation once we know it.
    """
    now = _now_iso()
    for c in candidates:
        conn.execute(
            """
            INSERT INTO traders (address, alias, first_seen, active, discovery_source)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(address) DO NOTHING
            """,
            (c.address, c.user_name or None, now, c.discovery_source),
        )
