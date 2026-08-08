"""Phase 2: polling-mode trade detection (FR-11). watch_events.py (phase 4,
on-chain) will eventually replace this as the primary path, proven against
it by a diff harness first — this file is the correctness baseline it gets
diffed against, not a throwaway.

One cycle: pick a watchlist, poll each watched trader's /activity since
our last-seen cursor for them, and for every new TRADE-type entry, insert
an observed_trades row and write exactly one decisions row (FR-14,
invariant 1 — never skip that pairing). REDEEM/CONVERSION entries are
logged as events but don't get an observed_trades row: the schema's `side`
column is BUY/SELL only, and neither is a trade we can proportionally
mirror (REDEEM is claiming an already-resolved market; CONVERSION is
neg-risk token mechanics the PRD's own risk table says to stay out of
until it's unambiguous — see PRD §8).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from polyprinter.mirror.decide import decide
from polyprinter.mirror.position_model import TradeLeg, running_position
from polyprinter.obs.log import Logger
from polyprinter.sources.polymarket_data import PolymarketDataClient

SOURCE = "poll"
MIRRORABLE_TYPES = ["TRADE", "REDEEM", "CONVERSION"]  # fetched together; only TRADE is mirrored


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_watchlist(conn: sqlite3.Connection, n: int) -> list[str]:
    """Top `n` traders by their latest shrunk ROI snapshot — auto-refreshed
    every cycle from whatever Scout currently has, no static list to
    maintain. Traders with no snapshot yet (roi_shrunk IS NULL) sort last
    and are excluded by the LIMIT before they'd ever be picked, same as
    the dashboard's own traders-page ordering.
    """
    rows = conn.execute(
        """
        SELECT s.address
        FROM trader_snapshots s
        WHERE s.id IN (SELECT MAX(id) FROM trader_snapshots GROUP BY address)
          AND s.roi_shrunk IS NOT NULL
        ORDER BY s.roi_shrunk DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()
    return [r["address"] for r in rows]


def _watch_start_epoch(conn: sqlite3.Connection, address: str) -> int:
    """The epoch second we started watching this trader — persisted as an
    event the first time we see them, not recomputed as "now" on every
    call. That distinction matters: a trader with zero observed trades so
    far has no MAX(block_ts) to anchor on, and naively falling back to
    "now" on *every* cycle would silently blind Mirror to anything that
    happened between two poll cycles, forever, until their first trade
    finally landed. Using the `events` table instead of a new schema
    table: this is exactly what it's for (a queryable log), and Mirror
    doesn't own a "cursors" table per docs/SCHEMA.md invariant 5.
    """
    row = conn.execute(
        """
        SELECT context_json FROM events
        WHERE service = 'mirror' AND message = 'mirror.watch_started' AND context_json LIKE ?
        ORDER BY ts LIMIT 1
        """,
        (f'%"address": "{address}"%',),
    ).fetchone()
    if row is not None:
        return int(json.loads(row["context_json"])["since_epoch"])

    since_epoch = int(datetime.now(timezone.utc).timestamp())
    conn.execute(
        "INSERT INTO events (ts, service, level, message, context_json) VALUES (?, 'mirror', 'INFO', 'mirror.watch_started', ?)",
        (_now_iso(), json.dumps({"address": address, "since_epoch": since_epoch})),
    )
    return since_epoch


def _last_seen_epoch(conn: sqlite3.Connection, address: str) -> int | None:
    """Epoch seconds of the most recent observed trade we already have for
    this trader, or None if we have none yet (in which case the caller
    falls back to _watch_start_epoch, not to "now").
    """
    row = conn.execute(
        "SELECT MAX(block_ts) AS ts FROM observed_trades WHERE address = ?",
        (address,),
    ).fetchone()
    if row["ts"] is None:
        return None
    return int(datetime.fromisoformat(row["ts"]).timestamp())


def _already_recorded(conn: sqlite3.Connection, tx_hash: str, address: str, entry: dict) -> bool:
    """True if this exact fill is already in observed_trades — the real
    idempotency check. Poll windows overlap by design (see _last_seen_epoch
    using `>=`, not `>`, below) so re-seeing the same entry is expected,
    not an error.
    """
    row = conn.execute(
        """
        SELECT 1 FROM observed_trades
        WHERE tx_hash = ? AND address = ? AND token_id = ? AND side = ?
          AND shares = ? AND price = ?
        LIMIT 1
        """,
        (tx_hash, address, entry["asset"], entry["side"], entry["size"], entry["price"]),
    ).fetchone()
    return row is not None


def _next_log_index(conn: sqlite3.Connection, tx_hash: str) -> int:
    """observed_trades' idempotency key is (tx_hash, log_index), but the
    polling source (/activity) doesn't expose a real log_index — see
    sources/polymarket_data.py's trades() docstring, same gap. Most
    transactions produce exactly one activity entry, so index 0 covers
    the common case; a genuine multi-leg fill sharing one tx_hash gets
    the next index in the order /activity returned them. This is only
    reached for entries _already_recorded() didn't recognize, so it can't
    collide with a re-poll of the same fill.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM observed_trades WHERE tx_hash = ?", (tx_hash,)
    ).fetchone()
    return row["n"]


def _insert_observed_trade(
    conn: sqlite3.Connection, address: str, entry: dict, *, position_after: float
) -> sqlite3.Row:
    tx_hash = entry["transactionHash"]
    log_index = _next_log_index(conn, tx_hash)
    block_ts = datetime.fromtimestamp(entry["timestamp"], tz=timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO observed_trades (
            address, tx_hash, log_index, market_id, token_id, side, shares,
            price, block_ts, detected_at, source, their_position_after
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            address,
            tx_hash,
            log_index,
            entry.get("conditionId", ""),
            entry["asset"],
            entry["side"],
            entry["size"],
            entry["price"],
            block_ts,
            _now_iso(),
            SOURCE,
            position_after,
        ),
    )
    return conn.execute("SELECT * FROM observed_trades WHERE id = ?", (cur.lastrowid,)).fetchone()


def _insert_decision(conn: sqlite3.Connection, observed_trade_id: int, decision: dict, *, mode: str, latency_ms: int) -> int:
    cur = conn.execute(
        """
        INSERT INTO decisions (
            observed_trade_id, mandate_id, decided_at, verdict,
            skip_reason_code, skip_reason_text, size_usd, mode, latency_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observed_trade_id,
            decision.get("mandate_id"),
            _now_iso(),
            decision["verdict"],
            decision.get("skip_reason_code"),
            decision.get("skip_reason_text"),
            decision.get("size_usd"),
            mode,
            latency_ms,
        ),
    )
    return cur.lastrowid


def poll_trader(
    conn: sqlite3.Connection,
    client: PolymarketDataClient,
    log: Logger,
    address: str,
    *,
    mode: str,
    mirror_config: dict,
) -> int:
    """Poll one trader, ingest any new trades, write a decision for each.
    Returns the number of new decisions written.
    """
    since = _last_seen_epoch(conn, address)
    if since is None:
        # No observed trade for them yet — anchor on when we STARTED
        # watching (persisted, see _watch_start_epoch), not "now" fresh
        # every cycle. Mirror is real-time detection (FR-11), not a
        # backfill, so a never-before-seen trader still watches forward
        # from first contact, not their full history.
        since = _watch_start_epoch(conn, address)

    entries = client.activity(address, limit=500, start=since, types=MIRRORABLE_TYPES)
    entries.sort(key=lambda e: e["timestamp"])  # chronological, oldest first — position replay needs this order

    n_new = 0
    for entry in entries:
        if entry.get("type") != "TRADE":
            log.info("mirror.activity.skipped_non_trade", address=address, type=entry.get("type"))
            continue

        tx_hash = entry["transactionHash"]
        if _already_recorded(conn, tx_hash, address, entry):
            continue  # poll-window overlap, not a new trade

        detect_start = datetime.now(timezone.utc)

        prior_rows = conn.execute(
            "SELECT side, shares FROM observed_trades WHERE address = ? AND token_id = ? ORDER BY id",
            (address, entry["asset"]),
        ).fetchall()
        position_before = running_position(TradeLeg(r["side"], r["shares"]) for r in prior_rows)
        position_after = position_before + entry["size"] if entry["side"] == "BUY" else position_before - entry["size"]

        trade_row = _insert_observed_trade(conn, address, entry, position_after=position_after)
        decision = decide(conn, trade_row, mode=mode, mirror_config=mirror_config)
        latency_ms = int((datetime.now(timezone.utc) - detect_start).total_seconds() * 1000)
        _insert_decision(conn, trade_row["id"], decision, mode=mode, latency_ms=latency_ms)

        log.info(
            "mirror.decision",
            address=address,
            verdict=decision["verdict"],
            reason=decision.get("skip_reason_code"),
            size_usd=decision.get("size_usd"),
        )
        n_new += 1

    return n_new


def run_once(conn: sqlite3.Connection, log: Logger, *, mode: str, mirror_config: dict) -> int:
    watchlist = select_watchlist(conn, mirror_config["watchlist_size"])
    log.info("mirror.watchlist", n=len(watchlist))

    total_new = 0
    with PolymarketDataClient(conn) as client:
        for address in watchlist:
            try:
                total_new += poll_trader(conn, client, log, address, mode=mode, mirror_config=mirror_config)
            except Exception as exc:  # noqa: BLE001 — one bad trader must not kill the cycle
                log.error("mirror.poll_trader.failed", address=address, error=str(exc))

    log.info("mirror.run.done", n_new_decisions=total_new, watchlist_size=len(watchlist))
    return total_new
