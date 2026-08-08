"""Scout entrypoint. One call to `run_once()` = one full scan: discover
candidates, fetch + compute a dossier per candidate, shrink ROI against the
batch's population mean, write append-only trader_snapshots rows for
whoever's lifetime-profitable or already being watched (scout/prune.py
deletes everyone else's data — operator's explicit choice, see that
module), then (Phase 3) issue a mandate for any watched trader whose
dossier materially changed. Scout owns the `mandates` table (docs/SCHEMA.md
invariant 5) — there is no separate mandate service, this is a step in
Scout's own run.

`main()` loops it daily when run as a service (`docker compose up scout`);
`run_once()` alone is what tests and the seed script call.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone

from polyprinter.config import load_config
from polyprinter.db.conn import get_connection
from polyprinter.mandate.issue import maybe_issue_mandate
from polyprinter.mirror.watch_poll import ensure_pinned_traders_exist, select_watchlist
from polyprinter.obs import heartbeat
from polyprinter.obs.log import Logger
from polyprinter.scout.discover import discover_candidates, upsert_traders
from polyprinter.scout.dossier import compute_dossier
from polyprinter.scout import prune
from polyprinter.scout.shrinkage import population_mean, shrink
from polyprinter.sources.openrouter import OpenRouterClient
from polyprinter.sources.polymarket_data import PolymarketDataClient

SERVICE = "scout"
SHRINKAGE_K = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_snapshot(conn: sqlite3.Connection, m, roi_shrunk: float | None) -> int:
    cur = conn.execute(
        """
        INSERT INTO trader_snapshots (
            address, scanned_at,
            roi_raw, roi_shrunk, capital_deployed_usd, realised_pnl_usd,
            realised_pnl_24h_usd, realised_pnl_7d_usd,
            resolved_positions, win_rate, avg_win_usd, avg_loss_usd, win_loss_ratio,
            concentration_top1, concentration_top5,
            hold_to_resolution_rate, median_hold_hours, p90_hold_hours,
            entry_price_p10, entry_price_median, entry_price_p90,
            sizing_cv, scale_frequency, median_market_liquidity, category_mix_json,
            trades_7d, trades_30d, open_positions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            m.address, _now_iso(),
            m.roi_raw, roi_shrunk, m.capital_deployed_usd, m.realised_pnl_usd,
            m.realised_pnl_24h_usd, m.realised_pnl_7d_usd,
            m.resolved_positions, m.win_rate, m.avg_win_usd, m.avg_loss_usd, m.win_loss_ratio,
            m.concentration_top1, m.concentration_top5,
            m.hold_to_resolution_rate, m.median_hold_hours, m.p90_hold_hours,
            m.entry_price_p10, m.entry_price_median, m.entry_price_p90,
            m.sizing_cv, m.scale_frequency, m.median_market_liquidity, m.category_mix_json,
            m.trades_7d, m.trades_30d, m.open_positions,
        ),
    )
    return cur.lastrowid


def _issue_mandates_for_watchlist(
    conn: sqlite3.Connection, log: Logger, *, watchlist_limit: int | None = None
) -> int:
    """Phase 3: one mandate attempt per trader Mirror actually watches (not
    the full ~200-candidate discovery pool — a trader ranked #150 will
    never be mirrored, so a mandate for them is spend with no purpose).
    Reuses mirror.watch_poll's own watchlist selection rather than
    duplicating "top N by shrunk ROI" query logic in two places — Mirror
    should always be mandated for exactly who it's watching, never a
    differently-sized set.

    No-ops (logs why, doesn't crash) if OPENROUTER_API_KEY/MODEL aren't
    set — Phase 3 is opt-in by simply not configuring it, same as every
    other credential-gated capability in this project.

    `watchlist_limit` overrides config.yaml's mirror.watchlist_size for
    this call only — same shape as run_once's own per_window_limit.
    Needed because each mandate call takes real wall-clock time (a real,
    detailed dossier prompt plus a reasoning model's generation is
    seconds, not milliseconds); without this, driver.sh's own "small
    pool, fast" scout smoke test would silently balloon from ~2 minutes
    to 15+ once OPENROUTER_* is configured, since the mandate watchlist
    is independent of --leaderboard-limit.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not api_key or not model:
        log.info("mandate.disabled", reason="OPENROUTER_API_KEY or OPENROUTER_MODEL not set")
        return 0

    config = load_config()
    watchlist_size = watchlist_limit or config.get("mirror", {}).get("watchlist_size", 20)
    daily_budget_usd = config.get("mandate", {}).get("daily_budget_usd", 1.0)

    watchlist = select_watchlist(conn, watchlist_size)
    n_issued = 0
    with OpenRouterClient(conn, api_key=api_key, model=model) as client:
        for address in watchlist:
            trader = conn.execute("SELECT * FROM traders WHERE address = ?", (address,)).fetchone()
            snapshot = conn.execute(
                "SELECT * FROM trader_snapshots WHERE address = ? ORDER BY scanned_at DESC LIMIT 1",
                (address,),
            ).fetchone()
            if trader is None or snapshot is None:
                continue
            try:
                outcome = maybe_issue_mandate(
                    conn, log,
                    trader=trader, snapshot=snapshot,
                    client=client, daily_budget_usd=daily_budget_usd,
                )
            except Exception as exc:  # noqa: BLE001 — one bad mandate call must not kill
                # the rest of the watchlist's mandate re-evaluation. Hit live
                # 2026-08-08: an unguarded exception three traders into a batch
                # silently killed mandate issuance for everyone after it, exactly
                # the class of bug the dossier loop above was already written to
                # prevent. (conn is autocommit — see db/conn.py — so there's no
                # transaction to roll back here; whatever that trader's call
                # already wrote stands, logged, same as any other partial state
                # this schema expects invariant 1's reconciliation query to catch.)
                log.error("mandate.unexpected_failure", address=address, error=str(exc))
                continue
            if outcome.startswith("issued:"):
                n_issued += 1
            conn.commit()
    return n_issued


def run_once(
    conn: sqlite3.Connection,
    log: Logger,
    *,
    per_window_limit: int | None = None,
    mandate_watchlist_limit: int | None = None,
) -> int:
    """Returns the number of traders snapshotted. per_window_limit, if given,
    overrides config.yaml's scout.leaderboard_limit (which itself defaults
    to 50) — mainly useful to shrink a verification run. mandate_watchlist_limit
    does the same for Phase 3's mandate pass (independent of per_window_limit —
    see _issue_mandates_for_watchlist).
    """
    config = load_config()
    if per_window_limit is None:
        per_window_limit = config.get("scout", {}).get("leaderboard_limit", 50)

    # Belt-and-suspenders: Mirror's own run_once already does this before
    # polling, but if Scout's mandate-issuance pass (which reuses Mirror's
    # watchlist selection) runs first, a freshly pinned address needs its
    # traders row to exist before anything can reference it.
    ensure_pinned_traders_exist(conn)

    with PolymarketDataClient(conn) as client:
        log.info("discover.start")
        candidates = discover_candidates(client, per_window_limit=per_window_limit)
        log.info("discover.done", n_candidates=len(candidates))
        upsert_traders(conn, candidates)
        conn.commit()

        dossiers = []
        for i, c in enumerate(candidates):
            try:
                m = compute_dossier(client, c.address)
                dossiers.append(m)
            except Exception as exc:  # noqa: BLE001 — one bad candidate must not kill the run
                log.error("dossier.failed", address=c.address, error=str(exc))
            if (i + 1) % 10 == 0:
                log.info("dossier.progress", done=i + 1, total=len(candidates))
                heartbeat.beat(conn, SERVICE, phase="dossier", done=i + 1, total=len(candidates))
                conn.commit()

    # Population mean is computed from THIS run's freshly-fetched dossiers,
    # not from stored trader_snapshots history — so pruning unprofitable
    # candidates below (which only ever touches *storage*) can't bias this
    # against FR-2's own anti-selection-bias intent: every candidate this
    # run discovered, profitable or not, still counts here.
    rois = [m.roi_raw for m in dossiers if m.roi_raw is not None]
    pop_mean = population_mean(rois)
    log.info("shrinkage.population_mean", pop_mean=pop_mean, n=len(rois))

    n_purged = 0
    kept_addresses: list[str] = []
    for m in dossiers:
        if not prune.is_lifetime_profitable(m.realised_pnl_usd) and not prune.has_been_acted_upon(conn, m.address):
            # Never watched/mandated/pinned and not lifetime-profitable —
            # nothing here is worth keeping. See scout/prune.py for the
            # full rationale and the audit/pin guard this respects.
            prune.purge_trader(conn, m.address)
            n_purged += 1
            continue

        roi_shrunk = None
        if m.roi_raw is not None:
            roi_shrunk = shrink(m.resolved_positions, m.roi_raw, pop_mean, k=SHRINKAGE_K)
        _insert_snapshot(conn, m, roi_shrunk)
        conn.execute(
            "UPDATE traders SET active = ?, last_trade_at = COALESCE(?, last_trade_at) WHERE address = ?",
            (1 if m.active else 0, m.last_trade_at, m.address),
        )
        kept_addresses.append(m.address)
    conn.commit()

    n_mandates = _issue_mandates_for_watchlist(conn, log, watchlist_limit=mandate_watchlist_limit)
    # Strategy narratives are on-demand only now (dashboard's /analyze
    # route, operator's explicit choice, 2026-08-08) — no automatic pass
    # here anymore. See scout/strategy.py's module docstring.

    n_kept = len(kept_addresses)
    log.info(
        "run.done",
        n_snapshotted=n_kept,
        n_purged_unprofitable=n_purged,
        n_mandates_issued=n_mandates,
    )
    return n_kept


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="run forever, once per interval")
    parser.add_argument("--interval-seconds", type=int, default=86400)
    parser.add_argument(
        "--leaderboard-limit",
        type=int,
        default=None,
        help="override config.yaml's scout.leaderboard_limit — mainly for a fast verification run",
    )
    parser.add_argument(
        "--mandate-watchlist-limit",
        type=int,
        default=None,
        help="override config.yaml's mirror.watchlist_size for Phase 3 mandate issuance only — "
        "independent of --leaderboard-limit, mainly for a fast verification run",
    )
    args = parser.parse_args()

    conn = get_connection()
    log = Logger(SERVICE, conn)

    def tick() -> None:
        heartbeat.beat(conn, SERVICE, phase="starting")
        try:
            n = run_once(
                conn, log,
                per_window_limit=args.leaderboard_limit,
                mandate_watchlist_limit=args.mandate_watchlist_limit,
            )
            heartbeat.beat(conn, SERVICE, phase="idle", last_run_traders=n)
        except Exception as exc:  # noqa: BLE001
            log.error("run.failed", error=str(exc))
            heartbeat.beat(conn, SERVICE, phase="error", error=str(exc))
            conn.commit()

    tick()
    while args.loop:
        time.sleep(args.interval_seconds)
        tick()


if __name__ == "__main__":
    main()
