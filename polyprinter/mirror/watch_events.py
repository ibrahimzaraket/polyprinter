"""Phase 4: on-chain event detection (FR-11's second half). Polling stays
the correctness baseline and the SOLE driver of real decisions/positions
for auto-ranked/LLM-mandated traders until the full cutover proves out —
see docs/PRD.md's Phase 4 exit criterion, "diffs clean against phase 2 for
72h". Every detected fill is logged as an `events` row regardless
(message='mirror.chain_trade_detected') for diff_report.py's comparison
against watch_poll.py's real observed_trades, whether or not it's acted on.

For a fast-laned address (mirror/fast_lane.py — pinned AND holding an
active operator mandate with fast_lane=1, an operator's deliberate,
narrower yes), this DOES call decide()/execute(), same as watch_poll.py's
own path. watch_poll.py checks the identical fast_lane_addresses() set and
skips decide()/execute() for exactly these addresses (writing a
FAST_LANE_HANDLED_BY_CHAIN SKIP instead) — see that module's own docstring.
Running both paths live for the SAME address would double-book the paper
portfolio; running each on a disjoint set, decided by one shared function,
is what makes this safe. The chain's log_index is real (not polling's
synthetic "Nth entry we've seen"), so it satisfies observed_trades'
UNIQUE(tx_hash, log_index) exactly as intended.

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
import time
from datetime import datetime, timezone

from polyprinter.mirror import execute
from polyprinter.mirror.decide import decide
from polyprinter.mirror.position_model import TradeLeg, running_position
from polyprinter.mirror.watch_poll import _needs_balance_lookup
from polyprinter.obs.log import Logger
from polyprinter.sources.chain import PolygonChainClient, decode_order_filled_log
from polyprinter.sources.polymarket_data import PolymarketDataClient

CHECKPOINT_MESSAGE = "mirror.chain_checkpoint"
DETECTED_MESSAGE = "mirror.chain_trade_detected"
FAST_LANE_SOURCE = "event"  # observed_trades.source for a chain-driven fast-lane fill
DEFAULT_CONFIRMATIONS = 5  # blocks to lag behind tip — shallow-reorg safety margin, not a guarantee
DEFAULT_LOOKBACK_BLOCKS = 50  # first-run anchor: watch forward from ~first contact, not a backfill
MARKET_ID_LOOKUP_RETRIES = 3  # data-api can lag the chain by a couple seconds; retried, not failed outright


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


def _resolve_market_id(data_client: PolymarketDataClient, *, address: str, tx_hash: str, block_ts_epoch: int) -> str | None:
    """The chain event gives tokenId, not conditionId (positions.market_id
    — see sources/chain.py's module docstring on why deriving this
    on-chain wasn't the right call). One targeted data-api /activity
    lookup, matched by tx_hash, resolves it — the same hybrid design
    chain.py's docstring described from the start. data-api can lag the
    chain by a couple seconds, so this retries a few times rather than
    failing on the first miss; still nowhere near polling's ~60s ceiling.
    """
    for attempt in range(MARKET_ID_LOOKUP_RETRIES):
        entries = data_client.activity(address, limit=25, start=block_ts_epoch - 30, types=["TRADE"])
        for entry in entries:
            if entry.get("transactionHash", "").lower() == tx_hash.lower():
                return entry.get("conditionId")
        if attempt < MARKET_ID_LOOKUP_RETRIES - 1:
            time.sleep(1.5)
    return None


def _already_recorded(conn: sqlite3.Connection, *, tx_hash: str, log_index: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM observed_trades WHERE tx_hash = ? AND log_index = ? LIMIT 1", (tx_hash, log_index)
    ).fetchone()
    return row is not None


def _insert_observed_trade(conn: sqlite3.Connection, fill: dict, *, market_id: str, position_after: float) -> sqlite3.Row:
    cur = conn.execute(
        """
        INSERT INTO observed_trades (
            address, tx_hash, log_index, market_id, token_id, side, shares,
            price, block_ts, detected_at, source, their_position_after
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fill["maker"], fill["tx_hash"], fill["log_index"], market_id, fill["token_id"], fill["side"],
            fill["shares"], fill["price"], fill["block_ts"], fill["detected_at"], FAST_LANE_SOURCE, position_after,
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
            observed_trade_id, decision.get("mandate_id"), _now_iso(), decision["verdict"],
            decision.get("skip_reason_code"), decision.get("skip_reason_text"), decision.get("size_usd"),
            mode, latency_ms,
        ),
    )
    return cur.lastrowid


def _handle_fast_lane_fill(
    conn: sqlite3.Connection, log: Logger, fill: dict, *, data_client: PolymarketDataClient, mode: str, mirror_config: dict
) -> None:
    """The fast-lane counterpart to watch_poll.poll_trader's per-entry
    body — same decide()/execute() call, same invariant-1 discipline
    (exactly one decisions row per observed_trades row), different trade
    source (a decoded chain fill, not an /activity entry) and a real
    log_index instead of a synthetic one.
    """
    address = fill["maker"]
    if _already_recorded(conn, tx_hash=fill["tx_hash"], log_index=fill["log_index"]):
        return  # already acted on this exact fill — a re-run over the same block range must not double-act

    detect_start = datetime.now(timezone.utc)
    market_id = _resolve_market_id(
        data_client,
        address=address,
        tx_hash=fill["tx_hash"],
        block_ts_epoch=int(datetime.fromisoformat(fill["block_ts"]).timestamp()),
    )
    if market_id is None:
        log.error("mirror.chain.market_id_unresolved", address=address, tx_hash=fill["tx_hash"])
        return  # not acted on this cycle; the next confirmed block range will retry via the same detection

    prior_rows = conn.execute(
        "SELECT side, shares FROM observed_trades WHERE address = ? AND token_id = ? ORDER BY id",
        (address, fill["token_id"]),
    ).fetchall()
    position_before = running_position(TradeLeg(r["side"], r["shares"]) for r in prior_rows)
    position_after = position_before + fill["shares"] if fill["side"] == "BUY" else position_before - fill["shares"]

    trade_row = _insert_observed_trade(conn, fill, market_id=market_id, position_after=position_after)

    their_balance_usd = None
    if fill["side"] == "BUY" and _needs_balance_lookup(conn, address):
        their_balance_usd = data_client.value(address)

    decision = decide(conn, trade_row, mode=mode, mirror_config=mirror_config, their_balance_usd=their_balance_usd)
    latency_ms = int((datetime.now(timezone.utc) - detect_start).total_seconds() * 1000)
    decision_id = _insert_decision(conn, trade_row["id"], decision, mode=mode, latency_ms=latency_ms)

    if decision["verdict"] == "TAKE":
        execute.open_position(conn, decision_id=decision_id, trade=trade_row, size_usd=decision["size_usd"], mode=mode)
    elif decision["verdict"] == "MIRROR_EXIT":
        position = conn.execute("SELECT * FROM positions WHERE id = ?", (decision["position_id"],)).fetchone()
        execute.record_exit(conn, decision_id=decision_id, position=position, trade=trade_row, fraction=decision["fraction"])

    log.info(
        "mirror.chain.decision",
        address=address, verdict=decision["verdict"], reason=decision.get("skip_reason_code"),
        size_usd=decision.get("size_usd"), latency_ms=latency_ms,
    )


def process_range(
    conn: sqlite3.Connection,
    log: Logger,
    client,
    *,
    watchlist: list[str],
    confirmations: int = DEFAULT_CONFIRMATIONS,
    fast_laned: frozenset[str] = frozenset(),
    data_client: PolymarketDataClient | None = None,
    mode: str = "paper",
    mirror_config: dict | None = None,
) -> int:
    """The actual cycle logic, taking an already-constructed `client` (a
    real PolygonChainClient, or a fake in tests — same split as
    watch_poll.py's poll_trader/run_once, for the same reason: this is
    what's worth unit testing without a network call).

    Fetches new (confirmed) OrderFilled logs for `watchlist` since the
    last checkpoint, decodes each, logs it as a detection event (always),
    advances the checkpoint. For a fill whose `maker` is in `fast_laned`,
    ALSO calls decide()/execute() via _handle_fast_lane_fill — everyone
    else stays log-only, per this module's docstring. `data_client` is
    required whenever `fast_laned` is non-empty (needs it to resolve
    market_id and, sometimes, balance); tests that never exercise the
    fast lane can omit it.

    Returns the number of fills detected (fast-laned or not).
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
    n_fast_lane_acted = 0
    for raw in raw_logs:
        fill = decode_order_filled_log(raw)
        fill["block_ts"] = client.block_timestamp_iso(fill["block_number"])
        fill["detected_at"] = _now_iso()
        _log_detection(conn, fill)
        n_detected += 1

        if fill["maker"] in fast_laned:
            if data_client is None:
                log.error("mirror.chain.fast_lane_missing_data_client", address=fill["maker"])
            else:
                try:
                    _handle_fast_lane_fill(
                        conn, log, fill, data_client=data_client, mode=mode, mirror_config=mirror_config or {}
                    )
                    n_fast_lane_acted += 1
                except Exception as exc:  # noqa: BLE001 — one bad fill must not kill the rest of the batch
                    log.error("mirror.chain.fast_lane_failed", address=fill["maker"], tx_hash=fill["tx_hash"], error=str(exc))

    _set_checkpoint(conn, target)
    log.info(
        "mirror.chain.run.done", n_detected=n_detected, n_fast_lane_acted=n_fast_lane_acted,
        from_block=from_block, to_block=target,
    )
    return n_detected


def run_once(
    conn: sqlite3.Connection,
    log: Logger,
    *,
    rpc_url: str,
    watchlist: list[str],
    confirmations: int = DEFAULT_CONFIRMATIONS,
    fast_laned: frozenset[str] = frozenset(),
    mode: str = "paper",
    mirror_config: dict | None = None,
) -> int:
    """Real-client wrapper around process_range() — what mirror/run.py
    actually calls. Never raises past this function for a single bad
    cycle: same discipline as watch_poll.run_once's per-trader guard and
    scout/run.py's per-candidate guard, an RPC hiccup should cost one
    cycle, not take Mirror's whole tick down with it — enforced by the
    caller wrapping this the same way it already wraps watch_poll.run_once.
    """
    with PolygonChainClient(conn, rpc_url=rpc_url) as client, PolymarketDataClient(conn) as data_client:
        return process_range(
            conn, log, client, watchlist=watchlist, confirmations=confirmations,
            fast_laned=fast_laned, data_client=data_client, mode=mode, mirror_config=mirror_config,
        )
