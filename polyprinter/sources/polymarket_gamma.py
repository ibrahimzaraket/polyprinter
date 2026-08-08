"""Client for gamma-api.polymarket.com — market metadata, resolutions, and
(2026-08-08, workstream §5) event-level category tags.

Verified live 2026-08-07 (docs/PRD.md §9, docs/api-notes.md). The one trap:
`closed` defaults to false SERVER-SIDE even when `condition_ids` narrows to
one specific market — a resolved market silently returns [] unless you pass
closed=true explicitly. This client always passes it explicitly so that
trap can't recur silently.

### Category tags — verified live 2026-08-08

`scout/dossier.py` has carried an open gap since Phase 1: "no category
field on data-api's position/trade/activity objects." Re-verified live
2026-08-08 — still true, data-api's `/positions`, `/closed-positions`, and
`/activity` responses carry no category/tag field of any kind. But
gamma-api's `/markets?condition_ids=` response ALSO has no `category` or
`tags` field at the market level, nor on the `events[]` array nested
inside a market response (checked both, real response, real market
`0x67a872c836...` — "Ethereum Up or Down"). The category data lives one
hop further: on the EVENT object fetched directly (`/events?id=<id>` or
`/events?slug=<slug>`, not `/markets`' nested copy of it), as a `tags`
array of `{id, label, slug}` objects. Real example for that same
market's event (id 811521, "Ethereum Up or Down - August 8,
12:15PM-12:30PM ET"):
`[{"slug": "up-or-down", ...}, {"slug": "crypto-prices", ...},
{"slug": "hide-from-new", ...}, {"slug": "recurring", ...},
{"slug": "crypto", "label": "Crypto", "id": "21"}, {"slug": "ethereum",
...}, {"slug": "15M", ...}]` — several tags per event, most of them
narrow (this specific 15-minute recurring market, the specific asset),
one of them (`crypto`, real id `21`) matching Polymarket's own top-level
site-nav category. See `category_of()` below for how that's picked out.

Both data-api's `/positions`/`/closed-positions`/`/activity` entries
already carry an `eventSlug` field alongside `conditionId` (verified live
in the same responses `scout/dossier.py` already fetches — no extra
data-api call needed to get it), so the category lookup is one gamma-api
hop per distinct event, not two.

`/events?id=`/`/events?slug=` both still work live but respond with
`deprecation: true`, `warning: 299 - "use /events/keyset"`, and
`sunset: Fri, 01 May 2026 00:00:00 GMT` headers (verified live
2026-08-08) — and that sunset date is already in the past relative to
today, meaning the old endpoint could be pulled at any time with no
further warning. `/events/keyset?slug=...` (repeatable) is the endpoint
this client actually uses: same `tags` array per event, verified live to
carry none of those deprecation headers, wraps the list in
`{"events": [...]}` instead of returning a bare list. Repeating `?slug=`
across multiple values is honored server-side (verified live: two slugs
in one call returned both events, each with its own distinct tags) —
lets `category_score.py` batch many markets into few HTTP calls.

No published rate limit was found for `/events/keyset` (unlike `/markets`
at 300/10s, PRD §9) — no `x-ratelimit-*` response headers were present on
a live check either. Treated conservatively anyway: `category_score.py`
batches lookups (`GAMMA_EVENTS_BATCH_SIZE`) rather than firing one request
per market, and every call still goes through `with_retry` like every
other client in this module.
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

    def events_by_slugs(self, slugs: list[str]) -> list[dict[str, Any]]:
        """GET /events/keyset?slug=<a>&slug=<b>&... — batched event lookup
        by eventSlug (see this module's docstring for why this endpoint,
        not /events?slug=/id=). Returns whatever events the API found;
        callers should not assume every input slug comes back (a slug that
        no longer resolves — renamed/removed event — is simply absent).
        """
        if not slugs:
            return []
        data = self._get("/events/keyset", {"slug": slugs})
        return data.get("events", [])

    @staticmethod
    def resolution_of(market: dict[str, Any]) -> dict[str, Any] | None:
        """Extract {outcome, outcome_prices} from a market object if resolved,
        else None. outcomes/outcomePrices come back as JSON-encoded strings,
        not native arrays.

        `clob_token_ids` (added for learner/resolve.py, 2026-08-08) is the
        array parallel to `outcomes`/`outcome_prices` that a `positions`
        row's own `token_id` needs to be matched against to know which
        index's resolved price is actually ours — verified live against a
        real market (`clobTokenIds` field, same JSON-encoded-string-of-array
        shape as outcomes/outcomePrices). Kept on this same dict rather than
        a second lookup: a caller already has one market object in hand by
        the time it needs either piece.
        """
        if not market.get("closed"):
            return None
        try:
            outcomes = json.loads(market.get("outcomes") or "[]")
            outcome_prices = json.loads(market.get("outcomePrices") or "[]")
            clob_token_ids = json.loads(market.get("clobTokenIds") or "[]")
        except (json.JSONDecodeError, TypeError):
            return None
        return {
            "outcomes": outcomes,
            "outcome_prices": [float(p) for p in outcome_prices],
            "clob_token_ids": clob_token_ids,
            "closed_time": market.get("closedTime"),
            "uma_resolution_status": market.get("umaResolutionStatus"),
            "neg_risk": market.get("negRisk"),
        }
