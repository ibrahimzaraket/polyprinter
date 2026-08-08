"""Learner entrypoint. `main()` loops it every `--interval-seconds` when
run as a service (`docker compose up learner`); `run_once()` (resolve.py)
alone is what tests and the driver call.

Same shape as scout/run.py and mirror/run.py on purpose: one heartbeat per
tick, one bad position must not kill the cycle (see resolve.run_once's own
per-position try/except), same --loop CLI surface. Interval defaults much
shorter than Scout's 24h (positions can resolve any time; Portfolio
shouldn't sit stale for a day waiting to find out) but doesn't need
Mirror's ~60s cadence either — a market resolving 5 minutes later than
this checks for it costs nothing.
"""

from __future__ import annotations

import argparse
import time

from polyprinter.db.conn import get_connection
from polyprinter.learner.resolve import run_once
from polyprinter.obs import heartbeat
from polyprinter.obs.log import Logger
from polyprinter.sources.polymarket_gamma import PolymarketGammaClient

SERVICE = "learner"
DEFAULT_INTERVAL_SECONDS = 600  # 10 min — see module docstring


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="run forever, once per interval")
    parser.add_argument("--interval-seconds", type=int, default=DEFAULT_INTERVAL_SECONDS)
    args = parser.parse_args()

    conn = get_connection()
    log = Logger(SERVICE, conn)

    def tick() -> None:
        heartbeat.beat(conn, SERVICE, phase="starting")
        try:
            with PolymarketGammaClient(conn) as gamma:
                result = run_once(conn, log, gamma_client=gamma)
            heartbeat.beat(conn, SERVICE, phase="idle", **result)
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
