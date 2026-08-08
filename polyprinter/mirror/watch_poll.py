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

from polyprinter.config import load_config
from polyprinter.mirror import execute
from polyprinter.mirror.decide import decide
from polyprinter.mirror.position_model import TradeLeg, running_position
from polyprinter.obs.log import Logger
from polyprinter.sources.polymarket_data import PolymarketDataClient

SOURCE = "poll"
MIRRORABLE_TYPES = ["TRADE", "REDEEM", "CONVERSION"]  # fetched together; only TRADE is mirrored


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pinned_addresses() -> list[str]:
    """De-duplicated, lowercased, order-preserved read of
    mirror.pinned_addresses — the one place this gets parsed out of
    config, shared by select_watchlist, ensure_pinned_traders_exist, and
    mirror/fast_lane.py so there's never a second, independently-parsed
    copy of "who's pinned" to drift out of sync with this one.
    """
    config = load_config()
    # `or []`, not `.get(..., [])` — a YAML key present with nothing under
    # it (`pinned_addresses:` with no list items) parses to None, not a
    # missing key, so the dict .get() default never kicks in. Found live
    # by a test using exactly that shape; without this, a config-overrides.yaml
    # left in that state would crash every Scout/Mirror cycle, not just
    # return an empty pin list.
    raw = config.get("mirror", {}).get("pinned_addresses") or []
    return list(dict.fromkeys(a.lower() for a in raw if a))


def select_watchlist(conn: sqlite3.Connection, n: int) -> list[str]:
    """Pinned traders (config.yaml/config-overrides.yaml's
    `mirror.pinned_addresses` — operator's explicit choice to tail
    someone, 2026-08-08) always take a slot, in the order configured; the
    remaining slots fill with the top traders by latest shrunk ROI
    snapshot, auto-refreshed every cycle from whatever Scout currently
    has. Pinned count toward `n`, not on top of it, so watchlist size (and
    therefore Mirror's poll load / Phase 3's mandate cost) stays
    predictable regardless of how many are pinned. Traders with no
    snapshot yet (roi_shrunk IS NULL) sort last in the auto-fill and are
    excluded by the LIMIT before they'd ever be picked, same as the
    dashboard's own traders-page ordering — this doesn't apply to pinned
    addresses, which are included even with no snapshot at all.

    Pure read — does not guarantee a `traders` row exists for a freshly
    pinned address (observed_trades.address has a FK to traders; see
    ensure_pinned_traders_exist(), which is the write-side counterpart
    callers that actually poll/mandate must call first).
    """
    pinned = _pinned_addresses()

    auto_slots = max(n - len(pinned), 0)
    auto: list[str] = []
    if auto_slots > 0:
        if pinned:
            placeholders = ",".join("?" * len(pinned))
            query = f"""
                SELECT s.address
                FROM trader_snapshots s
                WHERE s.id IN (SELECT MAX(id) FROM trader_snapshots GROUP BY address)
                  AND s.roi_shrunk IS NOT NULL
                  AND s.address NOT IN ({placeholders})
                ORDER BY s.roi_shrunk DESC
                LIMIT ?
            """
            params = (*pinned, auto_slots)
        else:
            query = """
                SELECT s.address
                FROM trader_snapshots s
                WHERE s.id IN (SELECT MAX(id) FROM trader_snapshots GROUP BY address)
                  AND s.roi_shrunk IS NOT NULL
                ORDER BY s.roi_shrunk DESC
                LIMIT ?
            """
            params = (auto_slots,)
        auto = [r["address"] for r in conn.execute(query, params).fetchall()]

    return pinned + auto


def ensure_pinned_traders_exist(conn: sqlite3.Connection) -> None:
    """Write-side counterpart to select_watchlist()'s pinned-address
    handling: a manually pinned address may not have been discovered by
    Scout yet, but observed_trades.address REFERENCES traders(address)
    with foreign keys enforced (PRAGMA foreign_keys=ON, db/conn.py) — so
    polling a pinned trader Scout has never seen would crash on the very
    first insert. Upserts a bare traders row for any pinned address
    missing one; Scout's own discovery/dossier work fills in the rest
    (snapshot, ROI, strategy narrative) on its next run, same as any other
    trader. Idempotent (ON CONFLICT DO NOTHING) — safe to call every cycle
    from both mirror/run.py and scout/run.py, whichever runs first.
    """
    pinned = _pinned_addresses()
    now = datetime.now(timezone.utc).isoformat()
    for address in pinned:
        conn.execute(
            "INSERT INTO traders (address, first_seen, active, discovery_source) "
            "VALUES (?, ?, 1, 'manual_pin') ON CONFLICT(address) DO NOTHING",
            (address, now),
        )


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


def _needs_balance_lookup(conn: sqlite3.Connection, address: str) -> bool:
    """True if `address` has an active balance_matched mandate — the only
    case decide_entry() actually consults their_balance_usd for. Checked
    before calling client.value() so the other ~all trades (fixed_cap
    LLM/operator mandates, or none at all) never pay for an API call they
    don't need.
    """
    row = conn.execute(
        "SELECT 1 FROM mandates WHERE address = ? AND superseded_by IS NULL "
        "AND sizing_mode = 'balance_matched' AND expires_at > datetime('now') LIMIT 1",
        (address,),
    ).fetchone()
    return row is not None


def poll_trader(
    conn: sqlite3.Connection,
    client: PolymarketDataClient,
    log: Logger,
    address: str,
    *,
    mode: str,
    mirror_config: dict,
    fast_laned: frozenset[str] = frozenset(),
) -> int:
    """Poll one trader, ingest any new trades, write a decision for each.
    Returns the number of new decisions written.

    `fast_laned` (mirror/fast_lane.py's single source of truth, computed
    once per run_once() cycle, not per trader) — for an address in this
    set, watch_events.py is the one actually deciding/executing; polling
    still records the observed_trades row (keeps the poll cursor correct,
    keeps a redundant audit trail) but writes a SKIP decision instead of
    calling decide()/execute() itself, so the trade is never acted on
    twice.
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

        if address in fast_laned:
            decision = {
                "verdict": "SKIP",
                "skip_reason_code": "FAST_LANE_HANDLED_BY_CHAIN",
                "skip_reason_text": "This address is fast-laned — watch_events.py (on-chain) drives its decisions, not polling.",
                "size_usd": None,
                "mandate_id": None,
            }
        else:
            their_balance_usd = None
            if entry["side"] == "BUY" and _needs_balance_lookup(conn, address):
                their_balance_usd = client.value(address)
            decision = decide(
                conn, trade_row, mode=mode, mirror_config=mirror_config, their_balance_usd=their_balance_usd
            )
        latency_ms = int((datetime.now(timezone.utc) - detect_start).total_seconds() * 1000)
        decision_id = _insert_decision(conn, trade_row["id"], decision, mode=mode, latency_ms=latency_ms)

        # decide() only computes what SHOULD happen; execute.py is what
        # actually books it — see that module's docstring for why this
        # step used to not exist at all.
        if decision["verdict"] == "TAKE":
            execute.open_position(
                conn, decision_id=decision_id, trade=trade_row, size_usd=decision["size_usd"], mode=mode
            )
        elif decision["verdict"] == "MIRROR_EXIT":
            position = conn.execute(
                "SELECT * FROM positions WHERE id = ?", (decision["position_id"],)
            ).fetchone()
            execute.record_exit(
                conn, decision_id=decision_id, position=position, trade=trade_row, fraction=decision["fraction"]
            )

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
    from polyprinter.mirror import fast_lane  # local import: fast_lane imports this module (watch_poll)

    ensure_pinned_traders_exist(conn)
    watchlist = select_watchlist(conn, mirror_config["watchlist_size"])
    fast_laned = frozenset(fast_lane.fast_lane_addresses(conn))
    log.info("mirror.watchlist", n=len(watchlist), n_fast_laned=len(fast_laned))

    total_new = 0
    with PolymarketDataClient(conn) as client:
        for address in watchlist:
            try:
                total_new += poll_trader(
                    conn, client, log, address, mode=mode, mirror_config=mirror_config, fast_laned=fast_laned
                )
            except Exception as exc:  # noqa: BLE001 — one bad trader must not kill the cycle
                log.error("mirror.poll_trader.failed", address=address, error=str(exc))

    log.info("mirror.run.done", n_new_decisions=total_new, watchlist_size=len(watchlist))
    return total_new
