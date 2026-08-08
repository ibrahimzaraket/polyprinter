"""Phase 4's exit criterion made measurable: "diffs clean against phase 2
for 72h" (docs/PRD.md §7). Compares watch_events.py's chain-detected fills
(logged as `events` rows — see that module for why they're not written to
observed_trades yet) against watch_poll.py's real observed_trades
(source='poll') over the same window.

Not wired to a schedule or a dashboard tab here — this is the measurement
primitive the 72h call gets made from, not a new UI surface nobody asked
for. `docker compose exec dashboard python -c "..."` (see SKILL.md) is
enough to check it by hand while the clock runs.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from polyprinter.mirror.watch_events import DETECTED_MESSAGE


def _chain_detections(conn: sqlite3.Connection, since_iso: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT context_json FROM events WHERE service = 'mirror' AND message = ? AND ts >= ? ORDER BY ts",
        (DETECTED_MESSAGE, since_iso),
    ).fetchall()
    return [json.loads(r["context_json"]) for r in rows]


def _poll_detections(conn: sqlite3.Connection, since_iso: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM observed_trades WHERE source = 'poll' AND detected_at >= ? ORDER BY detected_at",
        (since_iso,),
    ).fetchall()


def _key(address: str, tx_hash: str, token_id: str, side: str) -> tuple[str, str, str, str]:
    return (address.lower(), tx_hash.lower(), token_id, side)


def compute_diff(conn: sqlite3.Connection, *, since_iso: str) -> dict[str, Any]:
    """Matched on (address, tx_hash, token_id, side) — not price/shares,
    which can legitimately round a little differently between the two
    sources' own computation paths (data-api's own aggregation vs this
    project's raw on-chain decode). A real mismatch there is worth a human
    look, not an automatic fail, so it's reported inside `matched`, not
    used to split a real match into a false miss.

    `chain_only` (detected on-chain, no matching poll row) is the more
    concerning direction — it means polling missed or mis-attributed a
    real trade. `poll_only` can legitimately happen for reasons that
    aren't a chain-detection bug: the confirmations lag (chain.py trails
    the tip on purpose) means very recent poll trades haven't cleared yet,
    and REDEEM/CONVERSION-type activity never produces an OrderFilled log
    at all (poll already excludes those from observed_trades too, so this
    should be rare, but isn't structurally impossible if that ever
    changes).
    """
    chain = {_key(d["maker"], d["tx_hash"], d["token_id"], d["side"]): d for d in _chain_detections(conn, since_iso)}
    poll = {_key(r["address"], r["tx_hash"], r["token_id"], r["side"]): r for r in _poll_detections(conn, since_iso)}

    chain_keys = set(chain)
    poll_keys = set(poll)
    matched_keys = chain_keys & poll_keys

    matched = [
        {
            "address": poll[k]["address"],
            "tx_hash": poll[k]["tx_hash"],
            "chain_shares": chain[k]["shares"],
            "poll_shares": poll[k]["shares"],
            "chain_price": chain[k]["price"],
            "poll_price": poll[k]["price"],
        }
        for k in matched_keys
    ]

    return {
        "n_chain": len(chain_keys),
        "n_poll": len(poll_keys),
        "n_matched": len(matched_keys),
        "chain_only": [chain[k] for k in chain_keys - poll_keys],
        "poll_only": [dict(poll[k]) for k in poll_keys - chain_keys],
        "matched": matched,
    }
