"""Mirror entrypoint (Phase 2, polling mode). `main()` loops it every
`mirror.poll_interval_seconds` when run as a service (`docker compose up
mirror`); `run_once()` alone is what tests and the driver call.

Mirrors scout/run.py's own shape on purpose — same heartbeat-per-tick,
same "one bad item must not kill the cycle" discipline, same --loop/--once
CLI surface — so the two services read as one system, not two styles.
"""

from __future__ import annotations

import argparse
import time

from polyprinter.config import load_config
from polyprinter.db.conn import get_connection
from polyprinter.mirror.watch_poll import run_once
from polyprinter.obs import heartbeat
from polyprinter.obs.log import Logger

SERVICE = "mirror"

DEFAULT_MIRROR_CONFIG = {
    "poll_interval_seconds": 60,
    "watchlist_size": 20,
    "paper_bankroll_usd": 1000,
    "portfolio_exposure_cap_usd": 500,
    "correlation_cap_usd": 150,
}


def _mirror_config() -> dict:
    config = load_config()
    return {**DEFAULT_MIRROR_CONFIG, **config.get("mirror", {})}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="run forever, once per interval")
    parser.add_argument("--interval-seconds", type=int, default=None, help="override config.yaml's mirror.poll_interval_seconds")
    parser.add_argument("--watchlist-size", type=int, default=None, help="override config.yaml's mirror.watchlist_size")
    args = parser.parse_args()

    conn = get_connection()
    log = Logger(SERVICE, conn)
    mirror_config = _mirror_config()
    if args.watchlist_size is not None:
        mirror_config["watchlist_size"] = args.watchlist_size
    interval = args.interval_seconds or mirror_config["poll_interval_seconds"]
    mode = load_config().get("mode", "paper")

    def tick() -> None:
        heartbeat.beat(conn, SERVICE, phase="starting")
        try:
            n = run_once(conn, log, mode=mode, mirror_config=mirror_config)
            heartbeat.beat(conn, SERVICE, phase="idle", last_run_decisions=n)
        except Exception as exc:  # noqa: BLE001
            log.error("mirror.run.failed", error=str(exc))
            heartbeat.beat(conn, SERVICE, phase="error", error=str(exc))

    tick()
    while args.loop:
        time.sleep(interval)
        tick()


if __name__ == "__main__":
    main()
