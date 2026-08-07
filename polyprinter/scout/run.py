"""Scout entrypoint. One call to `run_once()` = one full scan: discover
candidates, fetch + compute a dossier per candidate, shrink ROI against the
batch's population mean, write append-only trader_snapshots rows.

`main()` loops it daily when run as a service (`docker compose up scout`);
`run_once()` alone is what tests and the seed script call.
"""

from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime, timezone

from polyprinter.config import load_config
from polyprinter.db.conn import get_connection
from polyprinter.obs import heartbeat
from polyprinter.obs.log import Logger
from polyprinter.scout.discover import discover_candidates, upsert_traders
from polyprinter.scout.dossier import compute_dossier
from polyprinter.scout.shrinkage import population_mean, shrink
from polyprinter.sources.polymarket_data import PolymarketDataClient

SERVICE = "scout"
SHRINKAGE_K = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_snapshot(conn: sqlite3.Connection, m, roi_shrunk: float | None) -> None:
    conn.execute(
        """
        INSERT INTO trader_snapshots (
            address, scanned_at,
            roi_raw, roi_shrunk, capital_deployed_usd, realised_pnl_usd,
            resolved_positions, win_rate, avg_win_usd, avg_loss_usd, win_loss_ratio,
            concentration_top1, concentration_top5,
            hold_to_resolution_rate, median_hold_hours, p90_hold_hours,
            entry_price_p10, entry_price_median, entry_price_p90,
            sizing_cv, scale_frequency, median_market_liquidity, category_mix_json,
            trades_7d, trades_30d, open_positions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            m.address, _now_iso(),
            m.roi_raw, roi_shrunk, m.capital_deployed_usd, m.realised_pnl_usd,
            m.resolved_positions, m.win_rate, m.avg_win_usd, m.avg_loss_usd, m.win_loss_ratio,
            m.concentration_top1, m.concentration_top5,
            m.hold_to_resolution_rate, m.median_hold_hours, m.p90_hold_hours,
            m.entry_price_p10, m.entry_price_median, m.entry_price_p90,
            m.sizing_cv, m.scale_frequency, m.median_market_liquidity, m.category_mix_json,
            m.trades_7d, m.trades_30d, m.open_positions,
        ),
    )


def run_once(conn: sqlite3.Connection, log: Logger, *, per_window_limit: int | None = None) -> int:
    """Returns the number of traders snapshotted. per_window_limit, if given,
    overrides config.yaml's scout.leaderboard_limit (which itself defaults
    to 50) — mainly useful to shrink a verification run.
    """
    config = load_config()
    if per_window_limit is None:
        per_window_limit = config.get("scout", {}).get("leaderboard_limit", 50)

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

    rois = [m.roi_raw for m in dossiers if m.roi_raw is not None]
    pop_mean = population_mean(rois)
    log.info("shrinkage.population_mean", pop_mean=pop_mean, n=len(rois))

    for m in dossiers:
        roi_shrunk = None
        if m.roi_raw is not None:
            roi_shrunk = shrink(m.resolved_positions, m.roi_raw, pop_mean, k=SHRINKAGE_K)
        _insert_snapshot(conn, m, roi_shrunk)
        conn.execute(
            "UPDATE traders SET active = ?, last_trade_at = COALESCE(?, last_trade_at) WHERE address = ?",
            (1 if m.active else 0, m.last_trade_at, m.address),
        )
    conn.commit()

    log.info("run.done", n_snapshotted=len(dossiers))
    return len(dossiers)


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
    args = parser.parse_args()

    conn = get_connection()
    log = Logger(SERVICE, conn)

    def tick() -> None:
        heartbeat.beat(conn, SERVICE, phase="starting")
        try:
            n = run_once(conn, log, per_window_limit=args.leaderboard_limit)
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
