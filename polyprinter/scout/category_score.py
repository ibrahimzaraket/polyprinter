"""Category-level Copy Score (workstream §5).

A trader might be sharp in NBA props and mediocre in crypto; one blended
dossier hides that. This module grades A/B/C/D per trader, per category,
as a plain deterministic formula — NOT an LLM call. The operator was
explicit this session that automatic-for-everyone LLM analysis is off the
table on cost grounds (see scout/strategy.py's module docstring for that
whole story) — but this is free and instant to recompute, so "runs for
every kept trader" is fine here in a way it deliberately isn't for
strategy.py's narratives.

### Where the category comes from

See sources/polymarket_gamma.py's module docstring for the full live
verification writeup. Short version: data-api's position objects have no
category field (re-checked live 2026-08-08, still true), but each one
carries `conditionId` and `eventSlug`, and gamma-api's per-EVENT `tags`
array (fetched via `/events/keyset?slug=<eventSlug>`, batched) has real
category-shaped data — e.g. a real Ethereum updown market's event carries
tags `['up-or-down', 'crypto-prices', 'hide-from-new', 'recurring',
'crypto', 'ethereum', '15M']`. Most of an event's tags are narrow
(specific asset, specific market mechanic); KNOWN_CATEGORY_TAGS below is
the small set that match Polymarket's own top-level site-nav categories —
verified live 2026-08-08 by hitting gamma-api's own `/tags/slug/<slug>`
endpoint for each one and confirming it's a real tag with a low,
early-assigned numeric id (Sports is id "1", Politics is id "2", Crypto is
id "21" — versus the thousands of narrow per-topic tags that also live in
the same /tags table, e.g. "caitlin-clark" is id "1512", "virgins" is id
"100601"). This is real, verified taxonomy data Polymarket itself
maintains and applies to every event — not a title-keyword heuristic this
project invented. An event tagged with none of these known slugs (it
happens — some events only carry narrow tags) falls back to
UNCATEGORIZED, shown as "Other", rather than being guessed at.

### Design choice: on-demand, not persisted per-Scout-run

PRD/task language allowed either "wire into scout/run.py's run_once() for
every kept trader" or "on-demand only... your call, justify it." This
module does BOTH, split by what's actually expensive:

- The gamma-api category LOOKUP is wired into run_once() (warm_market_
  category_cache, called once per run over every kept trader's fetched
  positions) — that's the one genuinely expensive part (network calls),
  and it's naturally batchable across the whole run's worth of traders at
  once, which per-request on-demand computation could never do as
  cheaply.
- The actual A-D GRADE is computed on demand, at dashboard render time
  (trader_category_scores(), called from dashboard/server.py), purely
  from already-archived data: raw_responses' closed-positions JSON
  (same source dashboard/server.py's own _pnl_by_market already reads)
  joined against the market_categories cache. No new table for scores
  themselves, no snapshot-append-only question to resolve (does a
  category grade table get a fresh row every Scout run the way
  trader_snapshots does? unclear it should — a grade is a view over data
  that's already versioned via raw_responses/market_categories, not a new
  independent time series), and it's cheap enough — pure arithmetic over
  at most ~50 already-in-memory positions per trader — to redo on every
  page load rather than caching. This mirrors dashboard/server.py's own
  existing pattern for _recent_trades/_pnl_by_market exactly: derive a
  read-time view from what Scout already archived, no new writer.

### The formula

Five signals, matching the task's brief (itself modeled on a real
competitor's grading rubric): category win rate, experience depth
(sample size), entry price quality, category P&L concentration, and
sizing "conviction" (coefficient of variation). Each signal maps to a
fraction in [0, 1] ("how good is this signal"); the final score is a
weighted average of whichever signals are actually computable (a
category with zero wins yet, e.g., has no concentration_top1 to score —
its weight is simply left out of the denominator rather than penalized
with a fabricated worst case). Weights were chosen, not fit statistically
— see WEIGHT_* constants for the reasoning behind each — and are named so
they can be argued with, not a black box.

Only CLOSED positions count toward every signal (same scope dossier.py's
own overall win_rate/concentration_top1/entry_price_* use) — an open
position has no realized win/loss yet. Same accepted limitation
dashboard/server.py's _pnl_by_market already documents: only the most
recently archived page of closed-positions (up to 50, offset=0) is
available per trader, so a trader with more than 50 resolved positions in
one category won't have every one counted here — a ranking/grading
signal, not exact accounting, same tradeoff dossier.py's own docstring
already accepts project-wide.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from polyprinter.scout.shrinkage import shrink
from polyprinter.sources.polymarket_gamma import PolymarketGammaClient

UNCATEGORIZED = "Other"

# Polymarket's own top-level site-nav category tags, verified live
# 2026-08-08 against gamma-api's real /tags/slug/<slug> endpoint (each is a
# real tag object with the id/label shown, not a guess) — see this module's
# docstring for the full writeup. Priority order: an event commonly carries
# several tags at once (verified live on a real soccer match's event —
# ['sports', 'games', 'soccer', 'la-liga-2'] — and a real LoL esports
# match's event — ['esports', 'league-of-legends', 'games', 'sports']), so
# the FIRST slug below found in an event's own tag set wins. Politics/
# Elections and Sports/Esports are kept as distinct categories rather than
# merged — both pairs are real, separately-verified top-level tags
# (ids "2"/"144" and "1"/"64"), and merging them would be an invented
# simplification this project's "verify, don't guess" discipline argues
# against.
KNOWN_CATEGORY_TAGS: list[tuple[str, str]] = [
    ("politics", "Politics"),
    ("elections", "Elections"),
    ("crypto", "Crypto"),
    ("sports", "Sports"),
    ("esports", "Esports"),
    ("business", "Business"),
    ("finance", "Finance"),
    ("science", "Science"),
    ("tech", "Tech"),
    ("pop-culture", "Culture"),
    ("world", "World"),
]

GAMMA_EVENTS_BATCH_SIZE = 50  # /events/keyset has no published rate limit
# (checked live 2026-08-08 — no x-ratelimit-* headers on a real response);
# batching many event slugs into one HTTP call via repeated ?slug= params
# (verified live to work — see sources/polymarket_gamma.py) keeps Scout's
# gamma-api traffic bounded regardless, one request per 50 distinct events
# instead of one per event.

# ─── Grading thresholds — named, not tuned ──────────────────────────────

MIN_CATEGORY_TRADES_FOR_ANY_GRADE = 3
# Fewer than 3 resolved positions in a category: not enough evidence to
# say ANYTHING, not even a low grade — the category is simply omitted
# from the trader's breakdown (shown as "insufficient sample" if the
# operator explicitly asks, not ranked alongside real grades).

MIN_CATEGORY_TRADES_FOR_FULL_GRADE = 8
# Fewer than 8: a grade IS computed (there's a formula answer) but capped
# at 'C' regardless of score — mirrors the PRD's own stated concern almost
# verbatim ("a 300% ROI over 6 trades must not outrank 22% over 400"),
# scaled down: a category slice naturally sees far fewer resolved
# positions than a trader's lifetime total, so the floor has to be lower
# than shrinkage.py's own SHRINKAGE_K=30 or every category would flatten
# to an uninformative shrink-to-the-mean result.

CATEGORY_SHRINKAGE_K = 8.0
# Reuses scout/shrinkage.py's exact shrink() formula
# (adj = (n*v + k*pop_mean)/(n+k)) conceptually, but shrinks a category's
# win rate toward the TRADER'S OWN overall win rate, not a cross-trader
# category population mean. A literal reading of "shrink toward the
# population mean" would mean computing, at Scout-run time, the win rate
# of every OTHER trader currently being scanned in this same category —
# a much bigger cross-trader aggregation step, out of this workstream's
# scope, and an honest gap called out in the final report rather than
# quietly built partway. Shrinking toward the trader's own broader,
# already-shrunk-at-the-population-level track record is a smaller, more
# defensible reference point that's always available with zero extra
# fetches. k=8 (vs shrinkage.py's k=30) because category samples are
# smaller by construction — 8 resolved positions in a category pulls the
# estimate about halfway to the trader's own overall rate, roughly the
# same relative pull shrinkage.py's k=30 has against a typical
# ~100-position lifetime sample.

EXPERIENCE_DEPTH_TARGET = 20
# n_resolved / EXPERIENCE_DEPTH_TARGET, capped at 1.0, is the "experience
# depth" signal's fraction. 20 resolved positions in one category is
# picked as "enough to call this trader genuinely experienced in this
# category" — arbitrary but named; not tied to any external constant.

ENTRY_PRICE_GOOD_LOW = 0.15
ENTRY_PRICE_GOOD_HIGH = 0.85
# Same "sub-10¢ longshot hunters are uncopyable at a $100 bankroll
# regardless of expectation" logic dossier.py's entry_price_p10 field
# already exists to surface (PRD §6.2) — extended with a symmetric high
# side (>85¢ favorites have the identical thin-tail liquidity problem on
# the other side of the book). Prices inside the band score 1.0; outside
# it, score decays toward (but never quite reaches) 0 — see _band_fraction.

CONVICTION_CV_GOOD_LOW = 0.3
CONVICTION_CV_GOOD_HIGH = 1.5
# Sizing coefficient of variation (dossier.py's sizing_cv, computed here
# per-category) treated as a "conviction" signal: near-zero CV means every
# bet is flat-sized regardless of confidence (no sizing signal to copy —
# proportional mirroring gains nothing from it), very high CV means
# erratic sizing that's hard to size-match confidently. The band
# [0.3, 1.5] is a judgment call, not a discovered fact — picked as
# "noticeably-varying-but-not-wild" and named here so it can be revisited,
# unlike the live-verified API facts elsewhere in this module.

# ─── Signal weights — sum to 100 when every signal is computable ────────

WEIGHT_WIN_RATE = 35        # the headline "are they actually right" signal
WEIGHT_EXPERIENCE = 15      # sample depth — separate from the shrinkage
                             # already folded into win_rate_shrunk, per the
                             # task's own five-signal list
WEIGHT_ENTRY_PRICE = 20     # copyability: extreme entry prices are hard to
                             # size/fill correctly at small bankroll
WEIGHT_CONCENTRATION = 20   # is the category record one lucky market, or
                             # real, repeated skill
WEIGHT_CONVICTION = 10      # smallest weight — sizing pattern is a softer,
                             # more debatable signal than the other four


def _band_fraction(value: float, good_lo: float, good_hi: float) -> float:
    """1.0 when value is inside [good_lo, good_hi]; decays toward 0 below
    good_lo (as value/good_lo) and above good_hi (as good_hi/value).
    Handles both a naturally bounded domain (entry price, capped at 1.0)
    and an unbounded one (sizing CV has no ceiling) with the same
    function — the ratio decay just asymptotes toward 0 instead of
    hitting a hard-coded max for the unbounded case.
    """
    if value <= 0:
        return 0.0
    if good_lo <= value <= good_hi:
        return 1.0
    if value < good_lo:
        return value / good_lo if good_lo > 0 else 0.0
    return good_hi / value


def _grade_from_score(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 50:
        return "C"
    return "D"


@dataclass
class CategoryScore:
    category: str
    n_resolved: int
    win_rate: float | None
    win_rate_shrunk: float | None
    entry_price_median: float | None
    sizing_cv: float | None
    concentration_top1: float | None
    realised_pnl_usd: float | None
    score: float           # 0..100, continuous
    grade: str | None      # 'A' | 'B' | 'C' | 'D', or None below the
                            # any-grade sample floor


def compute_category_score(
    category: str, positions: list[dict[str, Any]], *, trader_overall_win_rate: float | None
) -> CategoryScore | None:
    """Pure function: no db, no network — a list of closed-position dicts
    in data-api's real shape (avgPrice, totalBought, realizedPnl) in, one
    CategoryScore out (or None if there's truly nothing to grade). Kept
    pure so it's directly unit-testable with plain dicts, same spirit as
    scout/shrinkage.py's shrink().
    """
    n = len(positions)
    if n == 0:
        return None

    realized_pnls = [p["realizedPnl"] for p in positions if p.get("realizedPnl") is not None]
    wins = [p for p in realized_pnls if p > 0]
    win_rate = (len(wins) / len(realized_pnls)) if realized_pnls else None

    pop_mean = trader_overall_win_rate if trader_overall_win_rate is not None else 0.5
    # 0.5 fallback: an unbiased coin-flip prior for a trader whose own
    # overall win_rate isn't known yet (e.g. this category IS their only
    # history so far) — deliberately not 0.0 or 1.0, which would bias the
    # very-small-sample case hardest in exactly the direction shrinkage
    # exists to prevent.
    win_rate_shrunk = None
    if win_rate is not None:
        win_rate_shrunk = shrink(len(realized_pnls), win_rate, pop_mean, k=CATEGORY_SHRINKAGE_K)

    entry_prices = [p["avgPrice"] for p in positions if p.get("avgPrice") is not None]
    entry_price_median = statistics.median(entry_prices) if entry_prices else None

    sizes = [p["totalBought"] for p in positions if p.get("totalBought")]
    sizing_cv = None
    if len(sizes) > 1 and statistics.mean(sizes) > 0:
        sizing_cv = statistics.pstdev(sizes) / statistics.mean(sizes)

    positive_pnls = [p for p in realized_pnls if p > 0]
    concentration_top1 = (max(positive_pnls) / sum(positive_pnls)) if positive_pnls else None

    realised_pnl_usd = sum(realized_pnls) if realized_pnls else None

    components: list[tuple[float, float]] = []  # (weight, fraction)
    if win_rate_shrunk is not None:
        components.append((WEIGHT_WIN_RATE, max(0.0, min(1.0, win_rate_shrunk))))
    components.append((WEIGHT_EXPERIENCE, min(n / EXPERIENCE_DEPTH_TARGET, 1.0)))
    if entry_price_median is not None:
        components.append((WEIGHT_ENTRY_PRICE, _band_fraction(entry_price_median, ENTRY_PRICE_GOOD_LOW, ENTRY_PRICE_GOOD_HIGH)))
    if concentration_top1 is not None:
        components.append((WEIGHT_CONCENTRATION, 1.0 - min(concentration_top1, 1.0)))
    if sizing_cv is not None:
        components.append((WEIGHT_CONVICTION, _band_fraction(sizing_cv, CONVICTION_CV_GOOD_LOW, CONVICTION_CV_GOOD_HIGH)))

    weight_sum = sum(w for w, _ in components)
    score = 100.0 * sum(w * f for w, f in components) / weight_sum if weight_sum else 0.0

    grade: str | None = None
    if n >= MIN_CATEGORY_TRADES_FOR_ANY_GRADE:
        grade = _grade_from_score(score)
        if n < MIN_CATEGORY_TRADES_FOR_FULL_GRADE and grade in ("A", "B"):
            grade = "C"  # not enough resolved trades in this category to trust an A/B

    return CategoryScore(
        category=category,
        n_resolved=n,
        win_rate=win_rate,
        win_rate_shrunk=win_rate_shrunk,
        entry_price_median=entry_price_median,
        sizing_cv=sizing_cv,
        concentration_top1=concentration_top1,
        realised_pnl_usd=realised_pnl_usd,
        score=score,
        grade=grade,
    )


def category_of(event: dict[str, Any]) -> str | None:
    """None if the event has tags but none match a known top-level
    category — caller decides how to label that (UNCATEGORIZED)."""
    tag_slugs = {t.get("slug") for t in (event.get("tags") or []) if t.get("slug")}
    for slug, label in KNOWN_CATEGORY_TAGS:
        if slug in tag_slugs:
            return label
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cached_market_ids(conn: sqlite3.Connection, market_ids: list[str]) -> set[str]:
    if not market_ids:
        return set()
    placeholders = ",".join("?" * len(market_ids))
    rows = conn.execute(
        f"SELECT market_id FROM market_categories WHERE market_id IN ({placeholders})",
        tuple(market_ids),
    ).fetchall()
    return {r["market_id"] for r in rows}


def warm_market_category_cache(
    conn: sqlite3.Connection, gamma: PolymarketGammaClient, positions: list[dict[str, Any]]
) -> tuple[int, int]:
    """Ensures market_categories has a row for every distinct conditionId
    among `positions` (closed + open position dicts, data-api's real
    shape — carries conditionId and eventSlug already, see this module's
    docstring). Called once per Scout run over every kept trader's fetched
    positions combined — NOT per trader — so a market shared by many
    traders (a popular recurring market, say) only ever triggers one
    gamma-api lookup total, this run or ever again after it's cached.

    Returns (n_newly_cached, n_total_distinct_markets) for logging.
    """
    market_to_event: dict[str, str] = {}
    for p in positions:
        cid = p.get("conditionId")
        slug = p.get("eventSlug")
        if cid and slug and cid not in market_to_event:
            market_to_event[cid] = slug
    if not market_to_event:
        return 0, 0

    existing = _cached_market_ids(conn, list(market_to_event.keys()))
    missing = {cid: slug for cid, slug in market_to_event.items() if cid not in existing}
    if not missing:
        return 0, len(market_to_event)

    slug_to_markets: dict[str, list[str]] = {}
    for cid, slug in missing.items():
        slug_to_markets.setdefault(slug, []).append(cid)

    slugs = list(slug_to_markets.keys())
    now = _now_iso()
    n_cached = 0
    for i in range(0, len(slugs), GAMMA_EVENTS_BATCH_SIZE):
        batch = slugs[i : i + GAMMA_EVENTS_BATCH_SIZE]
        events = gamma.events_by_slugs(batch)
        found_slugs: set[str] = set()
        for ev in events:
            slug = ev.get("slug")
            if not slug or slug not in slug_to_markets:
                continue
            found_slugs.add(slug)
            category = category_of(ev) or UNCATEGORIZED
            for cid in slug_to_markets[slug]:
                conn.execute(
                    "INSERT OR REPLACE INTO market_categories (market_id, category, fetched_at) VALUES (?, ?, ?)",
                    (cid, category, now),
                )
                n_cached += 1
        for slug in batch:
            if slug in found_slugs:
                continue
            # Negative cache: gamma-api had nothing for this event slug
            # (renamed/removed) — record it so this exact dead lookup
            # isn't repeated on every future Scout run.
            for cid in slug_to_markets[slug]:
                conn.execute(
                    "INSERT OR REPLACE INTO market_categories (market_id, category, fetched_at) VALUES (?, ?, ?)",
                    (cid, None, now),
                )
                n_cached += 1
    conn.commit()
    return n_cached, len(market_to_event)


def _load_archived_closed_positions(conn: sqlite3.Connection, address: str) -> list[dict[str, Any]]:
    """Same raw_responses read dashboard/server.py's _pnl_by_market already
    does for the same table (source of truth: Scout's own archived fetch,
    no new API call) — duplicated here rather than imported from
    dashboard/server.py because that function is a private (underscore)
    helper scoped to rendering, not a shared library call, and this
    module has no dashboard/Flask dependency by design (it's also
    imported by scout/run.py, which shouldn't need to import Flask code
    to warm a cache).
    """
    row = conn.execute(
        """
        SELECT body FROM raw_responses
        WHERE source = 'data-api' AND url LIKE '%/closed-positions%'
          AND url LIKE ? AND url LIKE '%offset=0%'
        ORDER BY fetched_at DESC LIMIT 1
        """,
        (f"%user={address}%",),
    ).fetchone()
    if row is None:
        return []
    try:
        return json.loads(row["body"])
    except (TypeError, ValueError):
        return []


def _trader_overall_win_rate(conn: sqlite3.Connection, address: str) -> float | None:
    row = conn.execute(
        "SELECT win_rate FROM trader_snapshots WHERE address = ? ORDER BY scanned_at DESC LIMIT 1",
        (address,),
    ).fetchone()
    return row["win_rate"] if row is not None else None


def trader_category_scores(conn: sqlite3.Connection, address: str) -> list[CategoryScore]:
    """The dashboard-facing entrypoint: every category this trader has a
    gradable (or at-least-observed) record in, sorted best score first.
    Pure read — no gamma-api call, no write — uses only what Scout has
    already archived (raw_responses) and already cached
    (market_categories). A market whose category was never cached (or
    cached as a negative/None result) falls into the UNCATEGORIZED bucket
    alongside events gamma-api itself couldn't categorize — the dashboard
    doesn't distinguish "never looked up" from "looked up, no known tag"
    for display purposes, only category_score.py's cache-warming code
    needs that distinction (to avoid re-querying dead lookups).
    """
    positions = _load_archived_closed_positions(conn, address)
    if not positions:
        return []

    market_ids = {p["conditionId"] for p in positions if p.get("conditionId")}
    categories: dict[str, str] = {}
    if market_ids:
        placeholders = ",".join("?" * len(market_ids))
        rows = conn.execute(
            f"SELECT market_id, category FROM market_categories WHERE market_id IN ({placeholders})",
            tuple(market_ids),
        ).fetchall()
        categories = {r["market_id"]: (r["category"] or UNCATEGORIZED) for r in rows}

    by_category: dict[str, list[dict[str, Any]]] = {}
    for p in positions:
        cat = categories.get(p.get("conditionId"), UNCATEGORIZED)
        by_category.setdefault(cat, []).append(p)

    trader_win_rate = _trader_overall_win_rate(conn, address)
    scores = []
    for cat, plist in by_category.items():
        s = compute_category_score(cat, plist, trader_overall_win_rate=trader_win_rate)
        if s is not None:
            scores.append(s)
    scores.sort(key=lambda s: s.score, reverse=True)
    return scores
