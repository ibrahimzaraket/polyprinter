# PRD — PolyPrinter

*v1.0 · 2026-08-07 · supersedes the v3 architecture spec (the audit doc explains what changed and why)*

---

## 1. Problem

Polymarket publishes a public profit leaderboard. Some of those traders are skilled; most of the top ranks are variance. There is currently no way to tell which is which, and no way to act on the difference.

The prior system (Hermes Oracle v2) attempted this and failed for a reason worth naming precisely: **it had no record of its own reasoning.** It ran, it produced numbers, and its operator could not answer "why did it take that trade, and why did it skip the other 400?" A system whose decisions are invisible cannot be debugged, cannot be trusted, and cannot be improved — the outcome was months of work with no accumulated knowledge.

## 2. Goals

**G1.** Identify Polymarket traders whose profit is more likely skill than luck, using metrics the leaderboard does not expose.
**G2.** Mirror those traders' entries and exits with measurable, honest fill simulation.
**G3.** Make every decision — including every skip — inspectable, with the reasoning attached.
**G4.** Measure whether copy-trading is viable *at all* for this operator, and produce a defensible yes/no inside 60 days.

## 3. Non-goals

- Own-edge forecasting (the former "Mode 2"). Cut.
- Live money in v1.0. Paper only until the exit criteria in §7 are met.
- Any public write-capable surface. Telegram remains the only config-write path.
- Beating latency-competitive actors. We are not racing; we are measuring whether our lag is survivable.

## 4. User

One operator. Reads a dashboard, approves mandate changes over Telegram, does not want to SSH in to find out what happened.

## 5. Success metrics

| Metric | Target | Meaning |
|---|---|---|
| **Round-trip copy tax** (cents) | measured, not targeted | The core viability number. See §6.4 |
| Copy tax vs trader edge | tax < 40% of their per-trade edge | Below this, copying is structurally viable |
| Decision coverage | 100% | Every observed trade produces a `decisions` row, take or skip |
| Detection→decision latency | p95 < 5s (event mode) | |
| Mirror uptime | > 99%, heartbeat-verified | Silence must be distinguishable from inactivity |
| Shadow benchmark delta | > 0 | We beat naive copying of high-volume traders (§6.6) |

### Kill criteria — state these up front

Abandon the project, don't sink more months, if after 60 days:

- Median round-trip copy tax ≥ the median per-trade edge of followed traders, across every category; **or**
- Fewer than 30 mirrorable trades occurred (the followed traders aren't active enough to matter); **or**
- The copy portfolio underperforms the shadow benchmark.

## 6. Functional requirements

### 6.1 Scout (daily)

- **FR-1.** Pull leaderboard across all four windows (1d / 7d / 30d / all-time), volume and profit variants where available. Union into a candidate pool.
- **FR-2.** Additionally sample candidates **not selected on profit** — e.g. top traders by volume or by resolved-position count. *Rationale: selecting only on realised profit is the exact bias we're trying to defeat (see audit F7).*
- **FR-3.** For each candidate, fetch full activity history and open positions. Persist raw responses; never re-derive from a mutated source.
- **FR-4.** Compute dossier metrics (§6.2). Write as an **append-only snapshot** — never overwrite. A trader's metric trajectory is signal.
- **FR-5.** Only submit a trader to the LLM when their dossier has **materially changed** since the last mandate (new resolved positions ≥ N, or any metric moved > X%). *Rationale: daily re-analysis of 50 traders burns the LLM budget on unchanged inputs.*
- **FR-6.** Ingest market resolutions daily and populate outcomes. *This loop was missing entirely from the v3 spec.*

### 6.2 Dossier metrics

**Performance (shrunk, not raw)**
- ROI on capital deployed, **shrunk toward the population mean by sample size** — a 300% ROI over 6 trades must not outrank 22% over 400. Use a simple Bayesian shrinkage: `adj = (n·roi + k·pop_mean) / (n + k)`, k ≈ 30.
- Win rate, average win, average loss, and win/loss ratio
- Resolved-position count and total capital deployed
- Profit concentration: share of lifetime P&L from the single best market, and from the top 5

**Copyability**
- **Hold-to-resolution rate** — the single most predictive field. If their edge is exit timing, our lag eats it
- Median and p90 holding period
- Entry price distribution (deciles). Sub-10¢ longshot hunters are uncopyable at a $100 bankroll regardless of expectation
- Sizing pattern: flat vs conviction (coefficient of variation on position size)
- Category mix, with per-category ROI
- Median liquidity of markets entered
- **Scale-in/scale-out frequency** — how often they build or trim a position in pieces

**Liveness**
- Last trade timestamp, trade counts over 7/30d, open position count, `active` boolean

### 6.3 Mandates (LLM output)

- **FR-7.** The LLM receives a dossier — never a raw trade dump — and emits structured JSON: verdict, confidence, size cap, category allow/block, entry-price band, minimum liquidity, and **reasoning in prose**.
- **FR-8.** Mandates carry `issued_at`, `expires_at`, and `version`. Every decision references the mandate ID that authorised it.
- **FR-9.** Mandate expiry **must never orphan an open position.** Exits are mirrored regardless of mandate state. *This was an outright bug in the v3 spec.*
- **FR-10.** If the Scout loop dies, mandates expire and the Mirror silently stops trading — which looks identical to "no trades happened." The dashboard must distinguish *no signal* from *no mandate* from *no heartbeat*.

### 6.4 Mirror (real-time)

- **FR-11.** Detect watched traders' trades. Polling implementation first (correctness baseline), on-chain event subscription second, with a diff harness proving they agree before the polling path is retired.
- **FR-12.** Maintain a running model of each watched trader's position per token, so scale-ins and partial exits are handled.
- **FR-13.** **Proportional mirroring.** If they sell 40% of their holding, we sell 40% of ours. Not full close. *The v3 spec's "find our position and close it" is wrong for any trader who trims.*
- **FR-14.** Every observed trade writes a `decisions` row — TAKE with size, or SKIP with a machine-readable reason code plus human text.
- **FR-15.** **Idempotency.** `(tx_hash, log_index)` unique. A stream reconnect that replays blocks must not double-copy.
- **FR-16.** Portfolio-wide exposure cap, not just per-trade. *v2's known gap, carried into v3 unfixed.*
- **FR-17.** Correlation cap: total exposure to any single market across all followed traders is capped. Ten traders in one market is concentration, not diversification.
- **FR-18.** Capital availability model — the paper bankroll is finite and positions in 6-month markets lock it up. Skips due to insufficient capital are logged as such.
- **FR-19.** Heartbeat every 30s. Stale heartbeat → Telegram alert.

### 6.5 Fill simulation

- **FR-20.** At detection, snapshot the order book. Fill by walking the book for our size, crossing the spread. Apply real fees. Never assume we got their price.
- **FR-21.** Record their fill and our simulated fill side by side on every leg.
- **FR-22.** Note in the schema (not a runtime concern at $5 sizes): our own order's book impact is not modelled.

### 6.6 Learner (weekly)

- **FR-23.** Calibration table: LLM confidence band vs realised ROI. Distinguish two questions with very different sample requirements — **copy tax** converges in tens of trades (it's mechanical); **edge validation** needs hundreds (it's statistical). Do not claim the second from the first.
- **FR-24.** **Shadow benchmark.** Run a parallel paper portfolio that copies a naive rule — e.g. every high-volume trader, no LLM filter. Without this control, "+8%" means nothing.
- **FR-25.** **Out-of-sample discipline.** A trader's mandate is judged only on trades *after* the mandate was issued. In-sample ROI is the thing that got us here.
- **FR-26.** Proposed mandate changes go to Telegram as a diff for operator approval. No auto-apply in v1.0.

### 6.7 Dashboard

- **FR-27.** Server reading the DB directly (not generate-and-push). Bound to localhost; reached over a tunnel.
- **FR-28.** Tabs: **Now** (copy tax headline, heartbeat, open positions, last event received), **Traders** (dossier + metric sparklines + active mandate reasoning in full), **Decisions** (every row incl. skips, click-through to the trade, mandate, LLM call, and fill sim), **Calibration**, **Us vs Them** (our P&L vs theirs on the same trades).
- **FR-29.** Paper/live mode printed in the header of every page and every Telegram digest.

## 7. Phasing — with exit criteria

| Phase | Build | Cannot proceed until |
|---|---|---|
| **0** | Schema, logging, dashboard shell | A decision row renders end-to-end with its reasoning |
| **1** | Scout: ingestion + dossiers, no trading | Ranked trader list with sample-size-shrunk metrics; *useful even if you stop here* |
| **2** | Mirror, polling mode, paper | 100% decision coverage; heartbeat proven |
| **3** | Mandates (LLM) | Mandate JSON validates; cost/day measured against budget |
| **4** | Event stream | Diffs clean against phase 2 for 72h |
| **5** | Learner + shadow benchmark | Calibration table populated |
| **6** | Live | Kill criteria in §5 all cleared, explicitly, in writing |

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Selection bias — leaderboard *is* the bias | **High** | FR-2 non-profit sampling, FR-25 out-of-sample, shrinkage |
| Copy tax exceeds edge | High | Measure first (phase 2), it's the kill criterion |
| Neg-risk market mechanics misread as buys/sells | High | Verify against live data; exclude neg-risk markets in v1.0 if ambiguous |
| Followed traders inactive → no sample | Medium | Liveness gating; 30-trade floor in kill criteria |
| SQLite lock contention across 4 writers | Medium | WAL mode; single-writer discipline; Postgres if it bites |
| LLM budget overrun | Low | FR-5 delta-triggered analysis |

## 9. Polymarket endpoint reality (verified 2026-08-07 against live responses)

The items below were open questions as of v1.0 draft. Verified by hitting the
live APIs directly — see `docs/api-notes.md` for raw sample responses. This
section replaces the original open-items list; struck items are resolved,
one remains genuinely open.

### Data API — `https://data-api.polymarket.com`

- **`GET /v1/leaderboard`** — not `/leaderboard`. Real params: `category`
  (enum, default `OVERALL`), `timePeriod` (enum **`DAY | WEEK | MONTH | ALL`**
  — not `1d/7d/30d/all-time` as originally guessed), `orderBy` (`PNL | VOL`,
  covers the "profit vs volume variant" requirement in FR-1), `limit`
  (max **50** per call — smaller than assumed), `offset` (max **1000**).
  So each (category, timePeriod, orderBy) combination tops out around ~1050
  reachable rows via offset paging. Fine for our candidate-pool size; would
  bite if we ever wanted the full leaderboard.
- **`GET /positions`** (open) and **`GET /closed-positions`** (resolved) —
  both take `user` (required), return proxy-wallet-labeled rows. Real fields
  include `cashPnl`, `percentPnl`, `realizedPnl`, `avgPrice`, `curPrice`,
  `negativeRisk` (bool, present per-position — flags neg-risk exposure
  directly, no separate lookup needed). `/closed-positions` `limit` maxes
  at **50** (not 100+); paginate via `offset` (max 100000).
- **`GET /activity`** (required `user`) — includes a `type` enum with
  **`CONVERSION`** as a value distinct from `TRADE`. This resolves part of
  Audit F9: at the data-api layer, a neg-risk complementary-token conversion
  is *already tagged separately* from a sell — the Scout does not need to
  infer it. (The harder version of F9 — decoding raw on-chain `OrderFilled`
  events during Mirror/phase 4 — is untouched by this and still open, see
  below.)
- **`GET /trades`** — has `proxyWallet`, `side`, `price`, `size`,
  `transactionHash`, but **no `log_index`**. It cannot supply the
  `(tx_hash, log_index)` idempotency key in `observed_trades` — that table is
  fed by the on-chain event subscriber (`chain.py`, phase 2/4), a different
  source entirely. `/trades` is a Scout-side read for dossier history only.
- Every address field across every endpoint is literally named
  **`proxyWallet`**, confirming leaderboard/positions/activity/trades all key
  on Polymarket's per-user proxy contract, not a raw EOA. ~~Whether
  leaderboard addresses are proxy wallets~~ — **confirmed: yes.**

### Gamma API — `https://gamma-api.polymarket.com`

- **`GET /markets?condition_ids=<id>`** — resolutions ARE fetchable, but
  **`closed` defaults to `false` even when `condition_ids` is given** — a
  resolved market silently returns `[]` unless you also pass `closed=true`.
  This is an easy silent-failure trap for FR-6 (resolution ingestion) and is
  now the documented behavior in `resolutions.py`, not an assumption.
  Resolved markets carry `closed: true`, `outcomes` and `outcomePrices` as
  JSON-string arrays (e.g. `outcomePrices: ["0","1"]`), `umaResolutionStatus`,
  `closedTime`, `negRisk`.

### Rate limits (documented values, not assumed)

data-api general 1000 req/10s, `/trades` 200/10s, `/positions` and
`/closed-positions` 150/10s each. gamma-api `/markets` 300/10s. At Scout's
scale (tens to low hundreds of candidates/day) this is not a real
constraint — no backoff strategy needed for Phase 1.

### Resolved 2026-08-08 (Phase 4 build)

- ~~Whether `OrderFilled` events cleanly identify the user address (proxy
  or EOA) and trade direction on-chain, or whether the maker side is the
  matching operator~~ — **resolved, and the naive reading was wrong.**
  The CTF Exchange V2 contract (`0xE111180000d2663C0091e4f400237545B87B996B`,
  migrated from the old `0x4bFb41d5...` address 2026-04-28) emits TWO
  `OrderFilled` events per match — one for the maker order, one for the
  taker's own order — and in the taker's-own-order emission, `taker` is
  literally `address(this)` (the exchange contract), not a second real
  trader. `maker` is always a real trader's own proxy-wallet address in
  every emission, confirmed against the contract's own source
  (github.com/Polymarket/ctf-exchange-v2) and by decoding two real live
  transactions and matching every field against `data-api`'s `/activity`
  response for the same `transactionHash`. See `sources/chain.py`'s module
  docstring for the full writeup. **Rule: always filter/match on `maker`,
  never `taker`.**

### Still genuinely open

- RPC provider free-tier limits — chain.py currently runs against a free
  public endpoint (polygon-bor-rpc.publicnode.com, no API key). Fine for
  the Phase 4 diff-harness stage; worth revisiting if it proves unreliable
  under sustained load once/if event detection is ever cut over to drive
  real decisions.
