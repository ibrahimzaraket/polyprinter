"""scout/category_score.py — the pure grading formula against plain dicts
(no db, no network, same spirit as test_shrinkage-style unit tests), plus
the cache-warming and dashboard-facing read paths against a real
temp-file db (db/conn.get_connection(tmp_path / "test.db")), same pattern
as test_dossier.py / test_prune.py / test_operator_mandate.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polyprinter.db.conn import get_connection
from polyprinter.scout import category_score
from polyprinter.scout.category_score import (
    CATEGORY_SHRINKAGE_K,
    MIN_CATEGORY_TRADES_FOR_ANY_GRADE,
    MIN_CATEGORY_TRADES_FOR_FULL_GRADE,
    CategoryScore,
    _band_fraction,
    category_of,
    compute_category_score,
    trader_category_scores,
    warm_market_category_cache,
)

ADDRESS = "0xtrader"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pos(avg_price=0.5, total_bought=100.0, realized_pnl=10.0, condition_id="0xcond", event_slug="ev"):
    return {
        "conditionId": condition_id,
        "eventSlug": event_slug,
        "avgPrice": avg_price,
        "totalBought": total_bought,
        "realizedPnl": realized_pnl,
    }


# ─── compute_category_score — pure formula ──────────────────────────────


def test_no_positions_returns_none():
    assert compute_category_score("Sports", [], trader_overall_win_rate=0.5) is None


def test_below_any_grade_floor_has_no_grade():
    positions = [_pos(realized_pnl=10.0), _pos(realized_pnl=-5.0)]  # n=2 < MIN_CATEGORY_TRADES_FOR_ANY_GRADE
    s = compute_category_score("Sports", positions, trader_overall_win_rate=0.5)
    assert s is not None
    assert s.n_resolved == 2
    assert s.grade is None


def test_below_full_grade_floor_caps_at_c():
    # A small, all-winning, well-behaved category — would score high enough
    # for an A/B on the formula alone, but n=5 is below
    # MIN_CATEGORY_TRADES_FOR_FULL_GRADE (8), so it must be capped at C.
    assert MIN_CATEGORY_TRADES_FOR_ANY_GRADE <= 5 < MIN_CATEGORY_TRADES_FOR_FULL_GRADE
    positions = [_pos(avg_price=0.5, total_bought=100.0, realized_pnl=50.0) for _ in range(5)]
    s = compute_category_score("Sports", positions, trader_overall_win_rate=0.9)
    assert s is not None
    assert s.grade == "C"


def test_full_sample_high_quality_positions_score_well():
    # n well above the full-grade floor, all wins, entries in the "good"
    # band, sizes identical (no concentration risk, moderate CV) — should
    # clear the A threshold.
    positions = [_pos(avg_price=0.5, total_bought=100.0, realized_pnl=50.0, condition_id=f"0xc{i}") for i in range(25)]
    s = compute_category_score("Sports", positions, trader_overall_win_rate=0.9)
    assert s is not None
    assert s.n_resolved == 25
    assert s.grade in ("A", "B")  # shrunk win rate pulls slightly below a raw 100%, allow either top grade


def test_all_losses_scores_low():
    positions = [_pos(avg_price=0.5, total_bought=100.0, realized_pnl=-20.0, condition_id=f"0xc{i}") for i in range(15)]
    s = compute_category_score("Crypto", positions, trader_overall_win_rate=0.1)
    assert s is not None
    assert s.win_rate == 0.0
    assert s.grade == "D"


def test_win_rate_shrinkage_uses_trader_overall_as_population_mean():
    # A single win in a category (n=1, below any-grade floor so grade is
    # None, but win_rate_shrunk is still computed) should land closer to
    # the trader's own overall win rate than to the raw 100%.
    s = compute_category_score("Sports", [_pos(realized_pnl=10.0)], trader_overall_win_rate=0.3)
    assert s is not None
    expected = (1 * 1.0 + CATEGORY_SHRINKAGE_K * 0.3) / (1 + CATEGORY_SHRINKAGE_K)
    assert s.win_rate_shrunk == pytest.approx(expected)
    assert s.win_rate_shrunk < 1.0
    assert s.win_rate_shrunk > 0.3


def test_win_rate_shrinkage_falls_back_to_coin_flip_prior_when_overall_unknown():
    s = compute_category_score("Sports", [_pos(realized_pnl=10.0)], trader_overall_win_rate=None)
    expected = (1 * 1.0 + CATEGORY_SHRINKAGE_K * 0.5) / (1 + CATEGORY_SHRINKAGE_K)
    assert s.win_rate_shrunk == pytest.approx(expected)


def test_concentration_top1_uses_only_positive_pnls():
    positions = [
        _pos(realized_pnl=90.0, condition_id="0xa"),
        _pos(realized_pnl=10.0, condition_id="0xb"),
        _pos(realized_pnl=-500.0, condition_id="0xc"),  # a big loss must not enter the concentration calc
    ]
    s = compute_category_score("Sports", positions, trader_overall_win_rate=0.5)
    assert s.concentration_top1 == pytest.approx(90.0 / 100.0)


def test_high_concentration_scores_worse_than_low_concentration():
    concentrated = [
        _pos(avg_price=0.5, total_bought=100, realized_pnl=1000.0, condition_id="0xa"),
        *[_pos(avg_price=0.5, total_bought=100, realized_pnl=1.0, condition_id=f"0xb{i}") for i in range(9)],
    ]
    spread = [_pos(avg_price=0.5, total_bought=100, realized_pnl=100.0, condition_id=f"0xc{i}") for i in range(10)]
    s_concentrated = compute_category_score("Sports", concentrated, trader_overall_win_rate=0.9)
    s_spread = compute_category_score("Sports", spread, trader_overall_win_rate=0.9)
    assert s_concentrated.concentration_top1 > s_spread.concentration_top1
    assert s_concentrated.score < s_spread.score


def test_longshot_entry_prices_score_worse_than_mid_range():
    longshot = [_pos(avg_price=0.03, total_bought=100, realized_pnl=100.0, condition_id=f"0xa{i}") for i in range(10)]
    midrange = [_pos(avg_price=0.5, total_bought=100, realized_pnl=100.0, condition_id=f"0xb{i}") for i in range(10)]
    s_longshot = compute_category_score("Sports", longshot, trader_overall_win_rate=0.9)
    s_midrange = compute_category_score("Sports", midrange, trader_overall_win_rate=0.9)
    assert s_longshot.score < s_midrange.score


def test_score_is_bounded_0_to_100():
    positions = [_pos(avg_price=0.5, total_bought=100, realized_pnl=50.0, condition_id=f"0x{i}") for i in range(30)]
    s = compute_category_score("Sports", positions, trader_overall_win_rate=1.0)
    assert 0.0 <= s.score <= 100.0


# ─── _band_fraction ──────────────────────────────────────────────────────


def test_band_fraction_inside_band_is_one():
    assert _band_fraction(0.5, 0.15, 0.85) == 1.0


def test_band_fraction_decays_below_and_above_band():
    assert _band_fraction(0.03, 0.15, 0.85) == pytest.approx(0.03 / 0.15)
    assert _band_fraction(0.95, 0.15, 0.85) == pytest.approx(0.85 / 0.95)


def test_band_fraction_zero_or_negative_is_zero():
    assert _band_fraction(0.0, 0.15, 0.85) == 0.0
    assert _band_fraction(-1.0, 0.15, 0.85) == 0.0


# ─── category_of — real, verified tag shapes ────────────────────────────


def test_category_of_matches_known_crypto_tag():
    # Real tags array, verified live 2026-08-08 against the Ethereum
    # updown market's event (id 811521) — see category_score.py's own
    # module docstring for the exact live-checked example.
    event = {
        "tags": [
            {"slug": "up-or-down"}, {"slug": "crypto-prices"}, {"slug": "hide-from-new"},
            {"slug": "recurring"}, {"slug": "crypto", "label": "Crypto"}, {"slug": "ethereum"}, {"slug": "15M"},
        ]
    }
    assert category_of(event) == "Crypto"


def test_category_of_matches_known_sports_tag():
    # Real tags array, verified live against a real soccer match's event
    # (Malaga CF, condition_id 0x3184719...).
    event = {"tags": [{"slug": "sports"}, {"slug": "games"}, {"slug": "soccer"}, {"slug": "la-liga-2"}]}
    assert category_of(event) == "Sports"


def test_category_of_prefers_politics_over_later_priority_tags():
    event = {"tags": [{"slug": "sports"}, {"slug": "politics"}]}
    assert category_of(event) == "Politics"


def test_category_of_none_when_no_known_tag_matches():
    event = {"tags": [{"slug": "caitlin-clark"}, {"slug": "virgins"}]}
    assert category_of(event) is None


def test_category_of_handles_missing_tags_key():
    assert category_of({}) is None


# ─── warm_market_category_cache — real temp-file db ─────────────────────


class FakeGammaClient:
    """Stands in for PolymarketGammaClient.events_by_slugs — no I/O, serves
    canned events keyed by slug, same FakeDataClient spirit as
    test_dossier.py.
    """

    def __init__(self, events_by_slug: dict[str, dict]):
        self._events_by_slug = events_by_slug
        self.calls: list[list[str]] = []

    def events_by_slugs(self, slugs: list[str]) -> list[dict]:
        self.calls.append(list(slugs))
        return [self._events_by_slug[s] for s in slugs if s in self._events_by_slug]


def test_warm_cache_inserts_one_row_per_distinct_market(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    gamma = FakeGammaClient({"ev-a": {"slug": "ev-a", "tags": [{"slug": "crypto"}]}})
    positions = [_pos(condition_id="0x1", event_slug="ev-a"), _pos(condition_id="0x2", event_slug="ev-a")]

    n_cached, n_total = warm_market_category_cache(conn, gamma, positions)

    assert n_cached == 2
    assert n_total == 2
    rows = {r["market_id"]: r["category"] for r in conn.execute("SELECT market_id, category FROM market_categories").fetchall()}
    assert rows == {"0x1": "Crypto", "0x2": "Crypto"}


def test_warm_cache_batches_one_gamma_call_per_distinct_event_slug_not_per_market(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    gamma = FakeGammaClient({"ev-a": {"slug": "ev-a", "tags": [{"slug": "sports"}]}})
    # Same event, three different markets (multi-outcome event) — the
    # whole point of caching by market_id but batching by distinct event
    # slug is that this doesn't cost three gamma-api round trips.
    positions = [_pos(condition_id=f"0x{i}", event_slug="ev-a") for i in range(3)]

    warm_market_category_cache(conn, gamma, positions)

    assert gamma.calls == [["ev-a"]]  # one call, one slug, despite 3 markets


def test_warm_cache_skips_already_cached_markets(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO market_categories (market_id, category, fetched_at) VALUES ('0x1', 'Sports', ?)", (_now_iso(),)
    )
    conn.commit()
    gamma = FakeGammaClient({})

    n_cached, n_total = warm_market_category_cache(conn, gamma, [_pos(condition_id="0x1", event_slug="ev-a")])

    assert n_cached == 0
    assert n_total == 1
    assert gamma.calls == []  # never even asked gamma-api — already cached


def test_warm_cache_negative_caches_an_event_gamma_could_not_find(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    gamma = FakeGammaClient({})  # empty — simulates gamma-api returning nothing for this slug
    warm_market_category_cache(conn, gamma, [_pos(condition_id="0x1", event_slug="ev-gone")])

    row = conn.execute("SELECT category FROM market_categories WHERE market_id = '0x1'").fetchone()
    assert row is not None
    assert row["category"] is None  # negative cache: looked, found nothing — not "never looked"


def test_warm_cache_no_positions_is_a_noop(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    gamma = FakeGammaClient({})
    assert warm_market_category_cache(conn, gamma, []) == (0, 0)


# ─── trader_category_scores — dashboard-facing read path ────────────────


def _insert_closed_positions_raw_response(conn, address, positions):
    import json

    url = f"https://data-api.polymarket.com/closed-positions?user={address}&limit=50&offset=0&sortBy=TIMESTAMP&sortDirection=DESC"
    conn.execute(
        "INSERT INTO raw_responses (source, url, fetched_at, status, body, body_hash) VALUES ('data-api', ?, ?, 200, ?, ?)",
        (url, _now_iso(), json.dumps(positions), "h" + address),
    )
    conn.commit()


def test_trader_category_scores_groups_by_cached_category(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')", (ADDRESS, _now_iso())
    )
    conn.execute(
        "INSERT INTO trader_snapshots (address, scanned_at, win_rate) VALUES (?, ?, 0.6)", (ADDRESS, _now_iso())
    )
    now = _now_iso()
    conn.execute("INSERT INTO market_categories (market_id, category, fetched_at) VALUES ('0xa', 'Sports', ?)", (now,))
    conn.execute("INSERT INTO market_categories (market_id, category, fetched_at) VALUES ('0xb', 'Crypto', ?)", (now,))
    conn.commit()

    positions = [
        _pos(condition_id="0xa", realized_pnl=10.0),
        _pos(condition_id="0xa", realized_pnl=20.0),
        _pos(condition_id="0xa", realized_pnl=-5.0),
        _pos(condition_id="0xb", realized_pnl=-15.0),
    ]
    _insert_closed_positions_raw_response(conn, ADDRESS, positions)

    scores = trader_category_scores(conn, ADDRESS)

    by_cat = {s.category: s for s in scores}
    assert set(by_cat) == {"Sports", "Crypto"}
    assert by_cat["Sports"].n_resolved == 3
    assert by_cat["Crypto"].n_resolved == 1
    assert scores == sorted(scores, key=lambda s: s.score, reverse=True)


def test_trader_category_scores_uncategorized_bucket_for_uncached_markets(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')", (ADDRESS, _now_iso())
    )
    conn.commit()
    _insert_closed_positions_raw_response(conn, ADDRESS, [_pos(condition_id="0xnevercached", realized_pnl=5.0)])

    scores = trader_category_scores(conn, ADDRESS)

    assert len(scores) == 1
    assert scores[0].category == category_score.UNCATEGORIZED


def test_trader_category_scores_empty_when_no_archived_positions(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')", (ADDRESS, _now_iso())
    )
    conn.commit()
    assert trader_category_scores(conn, ADDRESS) == []
