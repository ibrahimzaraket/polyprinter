# Polymarket API — verified raw responses

*Captured 2026-08-07 by hitting the live APIs directly, not from memory or
old specs. Backs the corrections in PRD.md §9. Re-verify if `sources/`
starts throwing parse errors — that's the canary, per SCHEMA.md's
structural rule.*

Public docs (with a real OpenAPI spec) live at `https://docs.polymarket.com`;
`https://docs.polymarket.com/llms-full.txt` dumps the whole reference in one
file and `https://docs.polymarket.com/api-spec/data-openapi.yaml` /
`gamma-openapi.yaml` are the machine-readable specs. Worth re-pulling those
before trusting this file blindly — specs drift.

---

## Leaderboard — `GET https://data-api.polymarket.com/v1/leaderboard`

Params: `category` (OVERALL default), `timePeriod` (`DAY|WEEK|MONTH|ALL`),
`orderBy` (`PNL|VOL`), `limit` (max 50), `offset` (max 1000), `user`,
`userName`.

`timePeriod=DAY&orderBy=PNL&limit=3`:

```json
[{"rank":"1","proxyWallet":"0x3dfb153c197d4c19d3b31c1ecd2c7b6860eeabaf","userName":"0x3DFb153c197D4C19D3B31c1ecD2c7B6860eeabAf-1722957908185","xUsername":"","verifiedBadge":false,"vol":694303.721228,"pnl":151065.51808632887,"profileImage":""},
 {"rank":"2","proxyWallet":"0x6d20c35f65d9899b6d6b74f8466e824580f9a165","userName":"Djdjdjekekek","xUsername":"","verifiedBadge":false,"vol":4264965.589240001,"pnl":140198.329058409,"profileImage":""}]
```

`timePeriod=WEEK&orderBy=VOL&limit=2`:

```json
[{"rank":"1","proxyWallet":"0xfe787d2da716d60e8acff57fb87eb13cd4d10319","userName":"ferrariChampions2026","xUsername":"","verifiedBadge":false,"vol":52656603.277074985,"pnl":406988.5809159999,"profileImage":""},
 {"rank":"2","proxyWallet":"0x2005d16a84ceefa912d4e380cd32e7ff827875ea","userName":"RN1","xUsername":"RN1polymarket","verifiedBadge":false,"vol":26644164.223832004,"pnl":303702.9884043636,"profileImage":"https://polymarket-upload.s3.us-east-2.amazonaws.com/profile-image-1567750-84001aec-310a-470f-9584-67dfcaa61267.png"}]
```

Notably: `pnl`/`vol` differ across DAY vs WEEK vs MONTH vs ALL for the same
address — confirms these are genuinely different windowed aggregates, not
the same figure relabeled.

## Positions (open) — `GET /positions?user=<addr>`

```json
[{"proxyWallet":"0x204f72f35326db932158cba6adff0b9a1da95e14","asset":"409607...","conditionId":"0x90b657...","size":29583.8133,"avgPrice":0.0043,"initialValue":129.015,"currentValue":14.7919,"cashPnl":-114.2231,"percentPnl":-88.5347,"totalBought":36455.2681,"realizedPnl":1954.3929,"percentRealizedPnl":-90.6958,"curPrice":0.0005,"redeemable":false,"mergeable":true,"title":"Will Beijing Guoan FC win on 2026-08-07?","slug":"chi-bgu-xin-2026-08-07-bgu","eventId":"753218","eventSlug":"chi-bgu-xin-2026-08-07","outcome":"No","outcomeIndex":1,"oppositeOutcome":"Yes","oppositeAsset":"400986...","endDate":"2026-08-07","negativeRisk":true}]
```

## Closed positions — `GET /closed-positions?user=<addr>`

```json
[{"proxyWallet":"0x204f72f35326db932158cba6adff0b9a1da95e14","asset":"128957...","conditionId":"0xe69062...","avgPrice":0.443306,"totalBought":2367206.947438,"realizedPnl":1171844.90857,"curPrice":1,"title":"Will Germany win on 2026-06-25?","slug":"fifwc-ecu-ger-2026-06-25-ger","eventSlug":"fifwc-ecu-ger-2026-06-25","outcome":"No","outcomeIndex":1,"oppositeOutcome":"Yes","oppositeAsset":"101279...","endDate":"2026-06-25T00:00:00Z","timestamp":1782425653}]
```

`limit` max is **50** here (not the 100+ assumed) — paginate with `offset`
(max 100000) for full history.

## Activity — `GET /activity?user=<addr>`

```json
[{"proxyWallet":"0x204f72f35326db932158cba6adff0b9a1da95e14","timestamp":1786118137,"conditionId":"0x5e383c...","type":"TRADE","size":3.3012,"usdcSize":0.561204,"transactionHash":"0x8660ed...","price":0.17,"asset":"278271...","side":"BUY","outcomeIndex":999,"title":"Will Xorazm Fk Urganch win on 2026-08-07?","slug":"uzb1-xor-mas-2026-08-07-xor","outcome":"No","name":"swisstony","pseudonym":"Frail-Possible","bio":"So long, and thanks for all the fish"}]
```

`type` enum includes `CONVERSION` as a value distinct from `TRADE` — neg-risk
complementary-token conversions are tagged separately from trades at this
layer, which resolves the data-api half of Audit F9.

## Trades — `GET /trades?user=<addr>`

```json
[{"proxyWallet":"0x204f72f35326db932158cba6adff0b9a1da95e14","side":"BUY","asset":"524381...","conditionId":"0x2965c9...","size":195.057469,"price":0.8699999998,"timestamp":1786118136,"title":"ITF Koksijde: Tatiana Pieri vs Barbora Palicova","outcome":"Barbora Palicova","outcomeIndex":1,"name":"swisstony","transactionHash":"0x1d35ec..."}]
```

No `log_index` field — cannot serve `observed_trades`'s `(tx_hash, log_index)`
idempotency key. That table belongs to the chain-event subscriber
(phase 2/4), a different data source than this HTTP endpoint.

## Value — `GET /value?user=<addr>`

```json
[{"user":"0x204f72f35326db932158cba6adff0b9a1da95e14","value":176602.5957}]
```

## Market resolution — `GET https://gamma-api.polymarket.com/markets?condition_ids=<id>&closed=true`

**Trap:** `closed` defaults to `false` server-side even when `condition_ids`
narrows to one specific (already-resolved) market. Omitting `closed=true`
silently returns `[]` — looks like "market not found" but actually means
"found, but filtered out by the default." Confirmed by round-tripping a
known-resolved conditionId with and without the flag: `[]` without it,
full market object with it.

Resolved market, abbreviated:

```json
{"id":"1897315","question":"Will Germany win on 2026-06-25?",
 "conditionId":"0xe690620297dfea974d20df84b4cf90460e46a26ec864353482717b90509a3c0b",
 "outcomes":"[\"Yes\", \"No\"]","outcomePrices":"[\"0\", \"1\"]",
 "closed":true,"closedTime":"2026-06-25 22:13:55+00",
 "umaResolutionStatus":"resolved","negRisk":true}
```

`outcomes` / `outcomePrices` are JSON-encoded **strings**, not native arrays —
`json.loads()` twice, or once with the right field.

## Rate limits (from published docs, not measured under load)

| API | Endpoint | Limit |
|---|---|---|
| data-api | general | 1000 req/10s |
| data-api | `/trades` | 200 req/10s |
| data-api | `/positions`, `/closed-positions` | 150 req/10s each |
| gamma-api | `/markets` | 300 req/10s |

Far above what Scout needs at tens-to-low-hundreds of candidates/day *in
aggregate*. In practice a 429 shows up mid-run anyway: Scout fires many
requests in a tight loop across candidates with no throttling, and that
burst pattern can exceed a short 10-second window even when the day's total
is nowhere near the limit. Confirmed by actually running Scout (not by
reading this table) — `sources/retry.py` retries 429/5xx with backoff,
honoring `Retry-After` when present.
