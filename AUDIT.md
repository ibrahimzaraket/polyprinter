# Audit — Hermes Copy Engine v3 spec

*2026-08-07. Findings against the v3 architecture spec I wrote earlier today. All fixes are already folded into PRD.md and SCHEMA.md; this document records what was wrong and why, so the reasoning isn't lost.*

Severity: **C**ritical (loses money or produces false conclusions) · **H**igh · **M**edium

---

## F1 — "Mirror their exit" doesn't handle partial exits · **C**

The spec says: *SELL → find our matching open position → close it.*

Traders trim. If a trader sells 30% of a holding to lock in gains and rides the rest, we would liquidate 100% — exiting a position they're still in, at a worse price, and inverting the very behaviour we're paying to copy. Over time this systematically caps our upside while keeping full downside.

**Fix:** maintain a running model of *their* position size per token; mirror the **fraction** they sell (FR-12, FR-13, `position_exits` table).

## F2 — Mandate expiry orphans open positions · **C**

Mandates expire after 7 days. The spec never says what happens to a position opened under a mandate that has since lapsed. The natural implementation — check mandate, no mandate, skip — means the sell signal is ignored and the position sits open forever, unmanaged, in a system whose only exit mechanism is mirroring.

**Fix:** exits never consult mandate state. Only entries do (FR-9, invariant 2).

## F3 — The copy tax formula uses the wrong denominator · **C**

Spec: `copy_tax = (our_fill - their_fill) / their_fill`

Prediction market prices *are* probabilities, and the payout is $1. Buying at 55¢ when they got 53¢ is not a "3.8% worse fill" — it is **2 cents of a fixed $1 payout**, permanently gone. If the trader's edge is 4¢/share, that percentage figure hides the fact that you just surrendered half of it. And on the sell side the sign silently inverts.

**Fix:** measure in **absolute cents**, entry and exit separately, and always express it as a ratio against the trader's per-trade edge in cents. That ratio is the go/no-go number.

## F4 — The whole method is built on a selection-bias engine · **C**

This is the deepest problem and the spec doesn't acknowledge it at all.

You sample candidates from the **profit leaderboard** — i.e. you select on realised profit — and then judge skill using metrics derived from the same realised profit. With thousands of active traders, the top 100 by profit is populated largely by the lucky tail of an unskilled distribution. Richer metrics (win/loss ratio, concentration) reduce but do not remove this: they're computed on the same selected sample.

**Fixes, all three needed:**
- **Shrinkage** — adjust ROI toward the population mean by sample size, so 300% over 6 trades ranks below 22% over 400
- **Non-profit sampling** (FR-2) — also pull candidates by volume or resolved-position count, cohorts not selected on the outcome variable
- **Out-of-sample judgement** (FR-25) — a mandate is scored only on trades made *after* it was issued. In-sample ROI is what got us here

## F5 — v2's known portfolio gap was carried forward unfixed · **H**

The v2 export explicitly names it: *"sizing is per-trade only, no portfolio-wide check keeps total open copy exposure under the nominal bankroll."* The v3 spec caps per-position size and never adds a portfolio cap. A known bug survived the rewrite.

Worse, there's a second-order version the spec also misses: **correlation**. Following ten traders who are all long the same election outcome is a 10× concentrated bet dressed as diversification.

**Fix:** FR-16 (portfolio cap) and FR-17 (per-market correlation cap).

## F6 — No producer for the `outcomes` table · **H**

The schema has an `outcomes` table. Nothing in the three loops ever fetches market resolutions. The calibration loop reads from a table nobody writes.

**Fix:** resolution ingestion added to the Scout (FR-6).

## F7 — "Calibration after ~50 resolved copies" conflates two different questions · **H**

Copy tax is *mechanical* — it converges in tens of observations. Edge validation is *statistical* — with prediction-market variance, 50 resolved positions cannot distinguish a 5% edge from zero at any useful confidence.

The spec's phrasing invites exactly the wrong conclusion: 50 copies come back positive, that reads as validation, real money goes in.

**Fix:** separate the two explicitly (FR-23), and add a **shadow benchmark** (FR-24) so results are measured against a control rather than against zero.

## F8 — No idempotency on the event stream · **H**

Websocket RPCs drop and resubscribe; reconnection commonly replays recent blocks. Nothing in the spec prevents the same fill being processed twice, producing duplicate positions and corrupted P&L — quietly.

**Fix:** `UNIQUE (tx_hash, log_index)` (FR-15).

## F9 — Chain decoding is presented as simpler than it is · **H**

*"Filter on watchlisted maker/taker addresses. Decode: which trader, buy or sell."* On Polymarket's exchange, the maker side is frequently the matching operator rather than a user, direction must be inferred from which asset ID sits on which side, and the leaderboard's addresses are proxy wallets which may or may not be what appears in the event. Additionally, **neg-risk markets** support conversions between complementary outcome tokens that can look like a sell but aren't.

**Fix:** polling path first as ground truth, event stream diffed against it for 72h before cutover (FR-11); exclude neg-risk markets in v1.0 if the distinction can't be made cleanly.

## F10 — Scout failure is indistinguishable from market quiet · **M**

If the Scout dies, mandates expire, the Mirror stops taking entries, and the dashboard shows... no trades. Which is also what a quiet week looks like. The v2 failure mode — *"it runs, I can't tell what it does"* — reappears in a new costume.

**Fix:** heartbeat per service, and the dashboard distinguishes *no signal* / *no mandate* / *no heartbeat* (FR-10, FR-19).

## F11 — LLM budget math doesn't survive contact · **M**

50 dossiers analysed daily at ~$15/month is tight, and most of those calls re-analyse traders whose dossiers barely moved.

**Fix:** delta-triggered analysis (FR-5) — call the LLM only when a dossier materially changed. Cuts the great majority of calls with no information loss.

## F12 — `expires_at` and `review_after_n_copies` can contradict · **M**

A mandate expiring in 7 days with a review trigger at 15 copies is undefined for a trader who makes 4 trades a week. Which fires? The spec doesn't say.

**Fix:** expiry is a hard ceiling; the copy trigger fires early if reached. Both explicit in the mandate schema.

## F13 — Four concurrent SQLite writers · **M**

Scout, Mirror, Dashboard, Telegram all writing. Under default journal mode this produces lock contention and `database is locked` errors under exactly the conditions you care about — a burst of trades.

**Fix:** WAL mode, and one-writer-per-table-family discipline (invariant 5). Postgres if it still bites.

## F14 — Capital lockup unmodelled · **M**

A followed trader who enters a market resolving in six months ties up paper bankroll for six months. With a $100 book and $5 positions, twenty such entries and the system is fully allocated and silently skipping everything.

**Fix:** capital availability check with `NO_CAPITAL` as a first-class, logged skip reason (FR-18).

---

## What the v3 spec got right, for the record

- Moving the LLM out of the execution hot path and into cached mandates — that reconciliation of "LLM decides" with "seconds latency" holds up
- Hold-to-resolution rate as the key copyability metric
- Logging skips as first-class decisions
- Honest fill simulation as a precondition for meaningful paper results
- Build order putting observability before trading logic

## The one finding to act on first

**F4.** The others are bugs and will surface in testing. F4 is a methodology error that produces *confident, wrong conclusions* — a system that runs perfectly, reports a positive edge, and is measuring luck. It's the failure mode that survives every other fix.
