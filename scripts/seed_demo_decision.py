"""Phase 0 exit-criterion fixture: 'A decision row renders end-to-end with
its reasoning' (PRD §7). This inserts one synthetic trader + observed_trade
+ decision row so the Decisions tab has something to prove the schema,
logging, and dashboard plumbing work together before any real ingestion or
trading logic exists.

This is NOT trading logic — it makes no decision, it inserts a
pre-determined fixture row so the render path can be verified. Safe to run
repeatedly (upserts the demo trader; each run adds one more demo decision).
Delete the row / drop the db to remove it before real data arrives.
"""

from __future__ import annotations

from datetime import datetime, timezone

from polyprinter.db.conn import get_connection

DEMO_ADDRESS = "0x000000000000000000000000000000000000d0"  # obviously fake


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    conn = get_connection()
    now = _now_iso()

    conn.execute(
        """
        INSERT INTO traders (address, alias, first_seen, active, discovery_source)
        VALUES (?, 'demo-fixture', ?, 1, 'lb_day')
        ON CONFLICT(address) DO NOTHING
        """,
        (DEMO_ADDRESS, now),
    )

    cur = conn.execute(
        """
        INSERT INTO observed_trades (
            address, tx_hash, log_index, market_id, token_id, side, shares,
            price, block_ts, detected_at, source
        ) VALUES (?, ?, 0, 'demo-market', 'demo-token', 'BUY', 100.0, 0.55, ?, ?, 'poll')
        """,
        (DEMO_ADDRESS, f"0xdemo{now}", now, now),
    )
    observed_trade_id = cur.lastrowid

    reason_text = (
        "Phase 0 fixture row — no mandate exists yet (Mandates ship in Phase 3). "
        "This row exists only to prove decisions render end-to-end on the dashboard."
    )
    conn.execute(
        """
        INSERT INTO decisions (
            observed_trade_id, mandate_id, decided_at, verdict,
            skip_reason_code, skip_reason_text, size_usd, mode, latency_ms
        ) VALUES (?, NULL, ?, 'SKIP', 'NO_MANDATE', ?, NULL, 'paper', 12)
        """,
        (observed_trade_id, now, reason_text),
    )
    conn.commit()
    print(f"Seeded demo decision for observed_trade_id={observed_trade_id}. "
          f"View at http://127.0.0.1:8765/decisions")


if __name__ == "__main__":
    main()
