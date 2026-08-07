"""Client for gamma-api.polymarket.com — market metadata and resolutions.

Verified live 2026-08-07 (docs/PRD.md §9, docs/api-notes.md). The one trap:
`closed` defaults to false SERVER-SIDE even when `condition_ids` narrows to
one specific market — a resolved market silently returns [] unless you pass
closed=true explicitly. This client always passes it explicitly so that
trap can't recur silently.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import httpx

from polyprinter.sources.raw_store import store_raw
from polyprinter.sources.retry import with_retry

BASE_URL = "https://gamma-api.polymarket.com"
SOURCE = "gamma-api"


class PolymarketGammaClient:
    def __init__(self, conn: sqlite3.Connection, *, timeout: float = 15.0):
        self.conn = conn
        self._client = httpx.Client(base_url=BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PolymarketGammaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any]) -> Any:
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

    def markets_by_condition_ids(
        self, condition_ids: list[str], *, closed: bool | None = None
    ) -> list[dict[str, Any]]:
        """GET /markets?condition_ids=... . Pass closed=None (default) to
        query both open and resolved state in two calls — closed defaults
        to false server-side, so a single call can't see both. Pass True/
        False explicitly to query just one.
        """
        if closed is None:
            open_markets = self._get(
                "/markets", {"condition_ids": condition_ids, "closed": "false"}
            )
            closed_markets = self._get(
                "/markets", {"condition_ids": condition_ids, "closed": "true"}
            )
            return open_markets + closed_markets
        return self._get(
            "/markets", {"condition_ids": condition_ids, "closed": "true" if closed else "false"}
        )

    @staticmethod
    def resolution_of(market: dict[str, Any]) -> dict[str, Any] | None:
        """Extract {outcome, outcome_prices} from a market object if resolved,
        else None. outcomes/outcomePrices come back as JSON-encoded strings,
        not native arrays.
        """
        if not market.get("closed"):
            return None
        try:
            outcomes = json.loads(market.get("outcomes") or "[]")
            outcome_prices = json.loads(market.get("outcomePrices") or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        return {
            "outcomes": outcomes,
            "outcome_prices": [float(p) for p in outcome_prices],
            "closed_time": market.get("closedTime"),
            "uma_resolution_status": market.get("umaResolutionStatus"),
            "neg_risk": market.get("negRisk"),
        }
