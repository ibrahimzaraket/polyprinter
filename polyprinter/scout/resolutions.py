"""Outcome ingestion (PRD FR-6 / Audit F6).

Fetches market resolution state from gamma-api for a set of condition IDs.
This is the general-purpose capability FR-6 calls for; its Phase 2+
consumer — populating the `outcomes` table with OUR position P&L — has no
writer yet because `positions` has no writer until Mirror (phase 2) exists
(see docs/SCHEMA.md invariant 5: Learner owns `outcomes`). Phase 1's dossier
metrics don't need this: closed-positions' `realizedPnl` and activity's
REDEEM type already carry what Scout needs (see dossier.py). This module
exists so that capability isn't missing when phase 2 needs it.
"""

from __future__ import annotations

from typing import Any

from polyprinter.sources.polymarket_gamma import PolymarketGammaClient


def fetch_resolutions(
    gamma: PolymarketGammaClient, condition_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Returns {condition_id: resolution_dict} for every condition_id that
    has resolved. Unresolved / not-found markets are simply absent from the
    result — callers should not assume every input id comes back.
    """
    if not condition_ids:
        return {}
    markets = gamma.markets_by_condition_ids(condition_ids, closed=True)
    resolutions: dict[str, dict[str, Any]] = {}
    for market in markets:
        resolution = PolymarketGammaClient.resolution_of(market)
        if resolution is not None:
            resolutions[market["conditionId"]] = resolution
    return resolutions
