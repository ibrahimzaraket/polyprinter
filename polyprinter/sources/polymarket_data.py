"""Client for data-api.polymarket.com — leaderboard, positions, activity,
trades. Endpoint shapes verified live 2026-08-07; see docs/PRD.md §9 and
docs/api-notes.md for the raw responses this was built against.

Every call persists its raw response via sources/raw_store.py BEFORE
returning parsed JSON — that's the structural rule, not an optimization.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

import httpx

from polyprinter.sources.raw_store import store_raw
from polyprinter.sources.retry import with_retry

BASE_URL = "https://data-api.polymarket.com"
SOURCE = "data-api"

TimePeriod = Literal["DAY", "WEEK", "MONTH", "ALL"]
OrderBy = Literal["PNL", "VOL"]


class PolymarketDataClient:
    def __init__(self, conn: sqlite3.Connection, *, timeout: float = 15.0):
        self.conn = conn
        self._client = httpx.Client(base_url=BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PolymarketDataClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        resp = with_retry(lambda: self._client.get(path, params=params))
        store_raw(
            self.conn,
            source=SOURCE,
            url=str(resp.request.url),
            status=resp.status_code,
            body=resp.text,
        )
        resp.raise_for_status()
        return resp.json()

    def leaderboard(
        self,
        *,
        category: str = "OVERALL",
        time_period: TimePeriod = "DAY",
        order_by: OrderBy = "PNL",
        limit: int = 50,
        offset: int = 0,
        user: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET /v1/leaderboard. limit maxes at 50, offset at 1000 server-side —
        see docs/PRD.md §9 for why that caps reachable candidates per window.
        """
        return self._get(
            "/v1/leaderboard",
            {
                "category": category,
                "timePeriod": time_period,
                "orderBy": order_by,
                "limit": limit,
                "offset": offset,
                "user": user,
            },
        )

    def positions(self, user: str, *, limit: int = 500, offset: int = 0) -> list[dict[str, Any]]:
        """GET /positions — currently open positions."""
        return self._get("/positions", {"user": user, "limit": limit, "offset": offset})

    def closed_positions(self, user: str, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """GET /closed-positions — resolved/closed positions. limit maxes at
        50 server-side (smaller than the original spec assumed); page with
        offset (max 100000) for full history.
        """
        return self._get(
            "/closed-positions",
            {"user": user, "limit": limit, "offset": offset, "sortBy": "TIMESTAMP", "sortDirection": "DESC"},
        )

    def activity(
        self,
        user: str,
        *,
        limit: int = 500,
        offset: int = 0,
        types: list[str] | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /activity. `types` filters the `type` enum, e.g.
        ['TRADE', 'REDEEM'] — REDEEM is what tells us a position was held to
        resolution rather than sold; CONVERSION is a neg-risk conversion,
        distinct from a TRADE (resolves the data-api half of Audit F9).
        """
        params: dict[str, Any] = {"user": user, "limit": limit, "offset": offset, "start": start, "end": end}
        if types:
            params["type"] = ",".join(types)
        return self._get("/activity", params)

    def trades(
        self,
        user: str,
        *,
        limit: int = 500,
        offset: int = 0,
        start: int | None = None,
        end: int | None = None,
    ) -> list[dict[str, Any]]:
        """GET /trades. No log_index in this shape — cannot serve the
        (tx_hash, log_index) idempotency key used by observed_trades; that
        table is fed by the on-chain event subscriber (phase 2/4), not this
        HTTP client.
        """
        return self._get(
            "/trades", {"user": user, "limit": limit, "offset": offset, "start": start, "end": end}
        )

    def value(self, user: str) -> float | None:
        """GET /value — current portfolio value in USD. Verified live
        2026-08-08: matches sum(positions[i].currentValue) to within
        ~0.06% (the gap is just timing skew between two separate calls on
        fast-moving 5-minute crypto markets), and every wallet checked had
        $0 raw on-chain USDC.e balance — active traders keep ~zero idle
        cash, so this endpoint alone (not a wallet balance lookup, which
        would've meant a whole new on-chain capability for no benefit) is
        the right proxy for "their current bankroll" that mirror/sizing.py
        needs for balance-matched sizing (mandate/operator.py).

        Returns None if the response shape is unexpected (no rows) rather
        than 0 — callers must treat a genuine $0 balance (a real, common
        state for an active trader mid-rotation) differently from "we
        don't actually know," and 0 is a valid float that would otherwise
        be indistinguishable from "no data".
        """
        rows = self._get("/value", {"user": user})
        if not rows or "value" not in rows[0]:
            return None
        return rows[0]["value"]
