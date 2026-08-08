"""Phase 4: on-chain event detection (FR-11's second half). Polling stays
the correctness baseline and the SOLE driver of real decisions/positions
until this proves out — see docs/PRD.md's Phase 4 exit criterion, "diffs
clean against phase 2 for 72h".

Deliberately does NOT call decide()/execute(). Running both paths live
would double-book the paper portfolio: poll's log_index is synthetic
("nth entry we've seen for this tx", starting at 0) while the chain's is
the real on-chain value, so they essentially never collide on
observed_trades' UNIQUE(tx_hash, log_index) — both would happily insert
their own row for the SAME real trade, and both would fire decide() +
execute() for it. Instead, every detected fill is logged as an `events`
row (message='mirror.chain_trade_detected') for later comparison against
watch_poll.py's real observed_trades — see diff_report.py. Cutting event
detection over to actually drive decisions is a distinct, later, explicit
step, not something that happens silently once 72h of clean diffs
accumulate.

Checkpointing follows watch_poll.py's own precedent (`_watch_start_epoch`):
no dedicated cursor table (docs/SCHEMA.md invariant 5 — Mirror doesn't own
one), state lives in `events` instead. Unlike that one-time value, this
checkpoint moves every cycle, so it's a fresh INSERT each time (not an
upsert) and resumed via `ORDER BY id DESC LIMIT 1` — cheap, and it doubles
as its own history of how far behind the chain tip this has ever been.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from polyprinter.obs.log import Logger
from polyprinter.sources.chain import PolygonChainClient, decode_order_filled_log

CHECKPOINT_MESSAGE = "mirror.chain_checkpoint"
DETECTED_MESSAGE = "mirror.chain_trade_detected"
DEFAULT_CONFIRMATIONS = 5  # blocks to lag behind tip — shallow-reorg safety margin, not a guarantee
DEFAULT_LOOKBACK_BLOCKS = 50  # first-run anchor: watch forward from ~first contact, not a backfill


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_checkpoint(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT context_json FROM events WHERE service = 'mirror' AND message = ? ORDER BY id DESC LIMIT 1",
        (CHECKPOINT_MESSAGE,),
    ).fetchone()
    if row is None:
        return None
    return int(json.loads(row["context_json"])["block"])


def _set_checkpoint(conn: sqlite3.Connection, block: int) -> None:
    conn.execute(
        "INSERT INTO events (ts, service, level, message, context_json) VALUES (?, 'mirror', 'INFO', ?, ?)",
        (_now_iso(), CHECKPOINT_MESSAGE, json.dumps({"block": block})),
    )


def _log_detection(conn: sqlite3.Connection, fill: dict) -> None:
    conn.execute(
        "INSERT INTO events (ts, service, level, message, context_json) VALUES (?, 'mirror', 'INFO', ?, ?)",
        (_now_iso(), DETECTED_MESSAGE, json.dumps(fill, default=str)),
    )


def process_range(
    conn: sqlite3.Connection,
    log: Logger,
    client,
    *,
    watchlist: list[str],
    confirmations: int = DEFAULT_CONFIRMATIONS,
) -> int:
    """The actual cycle logic, taking an already-constructed `client` (a
    real PolygonChainClient, or a fake in tests — same split as
    watch_poll.py's poll_trader/run_once, for the same reason: this is
    what's worth unit testing without a network call).

    Fetches new (confirmed) OrderFilled logs for `watchlist` since the
    last checkpoint, decodes each, logs it as a detection event, advances
    the checkpoint. Returns the number of fills detected.
    """
    latest = client.latest_block()
    target = latest - confirmations
    if target < 0:
        return 0

    checkpoint = _get_checkpoint(conn)
    from_block = (checkpoint + 1) if checkpoint is not None else max(target - DEFAULT_LOOKBACK_BLOCKS, 0)

    if from_block > target:
        return 0  # nothing new and confirmed yet

    raw_logs = client.get_order_filled_logs(from_block=from_block, to_block=target, maker_addresses=watchlist)

    n_detected = 0
    for raw in raw_logs:
        fill = decode_order_filled_log(raw)
        fill["block_ts"] = client.block_timestamp_iso(fill["block_number"])
        fill["detected_at"] = _now_iso()
        _log_detection(conn, fill)
        n_detected += 1

    _set_checkpoint(conn, target)
    log.info("mirror.chain.run.done", n_detected=n_detected, from_block=from_block, to_block=target)
    return n_detected


def run_once(
    conn: sqlite3.Connection,
    log: Logger,
    *,
    rpc_url: str,
    watchlist: list[str],
    confirmations: int = DEFAULT_CONFIRMATIONS,
) -> int:
    """Real-client wrapper around process_range() — what mirror/run.py
    actually calls. Never raises past this function for a single bad
    cycle: same discipline as watch_poll.run_once's per-trader guard and
    scout/run.py's per-candidate guard, an RPC hiccup should cost one
    cycle, not take Mirror's whole tick down with it — enforced by the
    caller wrapping this the same way it already wraps watch_poll.run_once.
    """
    with PolygonChainClient(conn, rpc_url=rpc_url) as client:
        return process_range(conn, log, client, watchlist=watchlist, confirmations=confirmations)
