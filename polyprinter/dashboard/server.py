"""Dashboard server. Reads the DB directly (FR-27) — never generate-and-push.
Bound to 127.0.0.1:8765 only; reached over a tunnel, not exposed to the
public internet.

Invariant 5 ("dashboard reads only") is deliberately relaxed as of
2026-08-08, for a narrow, named set of operator actions — tail/untail a
trader, issue/revoke an operator mandate, force a strategy narrative (see
the routes below, all POST, all under /traders/<address>/...). This is a
conscious reversal, not scope creep: CLAUDE.md's "no write-capable public
surface" concern was about a PUBLIC hole; this server has never been
public, only reachable over the operator's own private Tailscale tunnel,
and every write here is something the operator explicitly clicked, not
something the dashboard decides on its own. Every other route stays
exactly as read-only as before.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

from flask import Flask, abort, g, redirect, render_template, request, url_for

from polyprinter import config_write
from polyprinter.config import load_config
from polyprinter.db.conn import get_connection
from polyprinter.mandate import operator as operator_mandate
from polyprinter.mirror import sizing as mirror_sizing
from polyprinter.mirror.run import DEFAULT_MIRROR_CONFIG
from polyprinter.mirror.watch_poll import ensure_pinned_traders_exist, select_watchlist
from polyprinter.obs import heartbeat
from polyprinter.obs.log import Logger
from polyprinter.scout import category_score
from polyprinter.scout.strategy import maybe_generate_strategy
from polyprinter.sources.openrouter import OpenRouterClient

# Paper is the only mode this whole build runs in today (live trading is
# explicitly deferred — see the root plan doc); every "our own book" query
# below is filtered by this constant rather than a hardcoded string
# scattered across each query, so the one place that changes on go-live
# day is this line, not a grep-and-replace.
PAPER_MODE = "paper"

SERVICE = "dashboard"
# Bind 0.0.0.0 *inside* the container — Docker's port-forwarding arrives via
# the container's eth0, not its loopback, so 127.0.0.1 here would be
# unreachable even with the port published. The actual "localhost only"
# guarantee lives one layer out, in docker-compose.yml's
# "127.0.0.1:8765:8765" mapping, which restricts the HOST's exposure to
# loopback. Running server.py directly on a host (not in Docker) binds this
# same 0.0.0.0 — pass --host 127.0.0.1 there if you want the same guarantee
# without Docker's port-mapping layer.
HOST = "0.0.0.0"
PORT = 8765

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = get_connection()
    return g.db


@app.teardown_appcontext
def close_db(_exc: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Metrics worth trending as an inline sparkline on the trader detail page,
# alongside a display label and a formatter for the value shown at the
# right-hand end of the chart. Presentation only — no new computation, just
# reshaping trader_snapshots rows that already exist for the chart.
SPARKLINE_METRICS = [
    ("roi_shrunk", "ROI (shrunk)", "pct"),
    ("win_rate", "Win rate", "pct"),
    ("hold_to_resolution_rate", "Hold-to-resolution", "pct"),
    ("sizing_cv", "Sizing CV", "num"),
]


def _format_metric(value: float, kind: str) -> str:
    if kind == "pct":
        return f"{value * 100:.1f}%"
    return f"{value:.2f}"


def _sparkline(snapshots_chronological: list[sqlite3.Row], field: str, kind: str, *, width: int = 176, height: int = 40, pad: float = 5.0) -> dict | None:
    """Build inline-SVG polyline points for one metric across snapshots,
    oldest first. Returns None when there are fewer than 2 non-null
    readings — a single point has no trend to draw (dataviz: not every
    figure is a chart; a lone value is a stat, not a line).
    """
    points_raw = [(i, row[field]) for i, row in enumerate(snapshots_chronological) if row[field] is not None]
    if len(points_raw) < 2:
        return None
    xs = [p[0] for p in points_raw]
    ys = [p[1] for p in points_raw]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    x_span = (x_max - x_min) or 1
    y_span = (y_max - y_min) or 1

    def sx(x: float) -> float:
        return pad + (x - x_min) / x_span * (width - 2 * pad)

    def sy(y: float) -> float:
        # invert: higher value -> higher on screen (smaller svg y)
        return height - pad - (y - y_min) / y_span * (height - 2 * pad)

    coords = [(sx(x), sy(y)) for x, y in points_raw]
    points_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    last_x, last_y = coords[-1]
    return {
        "points": points_str,
        "width": width,
        "height": height,
        "last_x": last_x,
        "last_y": last_y,
        "first_value": _format_metric(ys[0], kind),
        "last_value": _format_metric(ys[-1], kind),
        "n": len(points_raw),
    }


RECENT_TRADES_LIMIT = 30


def _recent_trades(conn: sqlite3.Connection, address: str, *, limit: int = RECENT_TRADES_LIMIT) -> list[dict]:
    """Individual trades/redeems for a trader — read from the raw
    Polymarket /activity response Scout already fetched and archived in
    raw_responses (sources/raw_store.py persists every external call
    before parsing, as an audit trail). No new API call, no new schema:
    this just parses JSON Scout already wrote to disk. offset=0 is the
    first page of /activity, which comes back newest-first (verified
    live against real data, 2026-08-08) — so its first `limit` entries
    are already the trader's most recent activity, no local re-sort
    needed.
    """
    row = conn.execute(
        """
        SELECT body FROM raw_responses
        WHERE source = 'data-api' AND url LIKE '%/activity%'
          AND url LIKE ? AND url LIKE '%offset=0%'
        ORDER BY fetched_at DESC LIMIT 1
        """,
        (f"%user={address}%",),
    ).fetchone()
    if row is None:
        return []
    try:
        entries = json.loads(row["body"])
    except (TypeError, ValueError):
        return []

    out = []
    for e in entries[:limit]:
        ts = e.get("timestamp")
        when = None
        if ts:
            when = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        out.append(
            {
                "when": when,
                "type": e.get("type"),
                "side": e.get("side"),
                "title": e.get("title"),
                "outcome": e.get("outcome"),
                "price": e.get("price"),
                "usdc_size": e.get("usdcSize"),
                "tx_hash": e.get("transactionHash"),
            }
        )
    return out


PNL_BY_MARKET_LIMIT = 40


def _pnl_by_market(conn: sqlite3.Connection, address: str, *, limit: int = PNL_BY_MARKET_LIMIT) -> list[dict]:
    """Per-market (per-slug) P&L for a trader — one row per position, open
    or closed, so "what's actually happening in their book" is visible
    market-by-market instead of only as one aggregate ROI number.

    Sourced from Scout's already-archived /positions (open) and
    /closed-positions (closed) responses in raw_responses — same audit
    trail as _recent_trades, no new API call. Polymarket's API already
    computes realizedPnl/cashPnl per position; that's used directly
    rather than re-deriving P&L from individual trade legs (buy/sell
    matching, cost basis) ourselves, which the API is better placed to
    get right than a client-side reconstruction from partial data.

    Only the most recently archived page of each endpoint is read (open
    positions: one call, limit=500, so this is already everything open;
    closed positions: one page of up to 50, the most recent — a trader
    with more than 50 resolved positions won't have every one listed
    here, by design, same tradeoff _recent_trades makes for the same
    reason: this is "what's happening lately," not a full ledger).
    """
    rows: list[dict] = []

    open_row = conn.execute(
        """
        SELECT body FROM raw_responses
        WHERE source = 'data-api' AND url LIKE '%/positions%' AND url NOT LIKE '%/closed-positions%'
          AND url LIKE ?
        ORDER BY fetched_at DESC LIMIT 1
        """,
        (f"%user={address}%",),
    ).fetchone()
    if open_row is not None:
        try:
            for p in json.loads(open_row["body"]):
                realized = p.get("realizedPnl") or 0.0
                unrealized = p.get("cashPnl") or 0.0
                rows.append(
                    {
                        "slug": p.get("slug"),
                        "title": p.get("title"),
                        "outcome": p.get("outcome"),
                        "status": "OPEN",
                        "realized_pnl": p.get("realizedPnl"),
                        "unrealized_pnl": p.get("cashPnl"),
                        "total_pnl": realized + unrealized,
                        "size": p.get("size"),
                        "avg_price": p.get("avgPrice"),
                        "cur_price": p.get("curPrice"),
                    }
                )
        except (TypeError, ValueError):
            pass

    closed_row = conn.execute(
        """
        SELECT body FROM raw_responses
        WHERE source = 'data-api' AND url LIKE '%/closed-positions%'
          AND url LIKE ? AND url LIKE '%offset=0%'
        ORDER BY fetched_at DESC LIMIT 1
        """,
        (f"%user={address}%",),
    ).fetchone()
    if closed_row is not None:
        try:
            for p in json.loads(closed_row["body"]):
                realized = p.get("realizedPnl") or 0.0
                rows.append(
                    {
                        "slug": p.get("slug"),
                        "title": p.get("title"),
                        "outcome": p.get("outcome"),
                        "status": "CLOSED",
                        "realized_pnl": p.get("realizedPnl"),
                        "unrealized_pnl": None,
                        "total_pnl": realized,
                        "size": p.get("totalBought"),
                        "avg_price": p.get("avgPrice"),
                        "cur_price": p.get("curPrice"),
                    }
                )
        except (TypeError, ValueError):
            pass

    rows.sort(key=lambda r: abs(r["total_pnl"]), reverse=True)
    return rows[:limit]


def _bankroll_usd() -> float:
    """Same merge mirror/run.py's own _mirror_config() does (defaults
    overridden by config.yaml's `mirror:` block) — read here too so the
    Portfolio page shows the exact bankroll figure sizing.py actually
    sizes against, not a second hardcoded guess at it.
    """
    return {**DEFAULT_MIRROR_CONFIG, **load_config().get("mirror", {})}["paper_bankroll_usd"]


def _market_context(conn: sqlite3.Connection, address: str, market_id: str) -> dict:
    """Best-effort title/outcome/slug for one of OUR OWN positions,
    resolved from the same trader's already-archived /activity payload
    _recent_trades already reads (matched on conditionId == market_id,
    the same field watch_poll.py's _insert_observed_trade stores as
    market_id). positions/observed_trades store market_id/token_id only
    — no title column, see docs/SCHEMA.md — so this is presentation-only,
    same "no new API call, reuse what Scout already archived" pattern as
    _recent_trades/_pnl_by_market above. Best-effort: if the trader's
    archived activity has since scrolled past this trade (only the most
    recent RECENT_TRADES_LIMIT-ish page is kept live), this returns {}
    and the caller falls back to showing the raw market_id.
    """
    row = conn.execute(
        """
        SELECT body FROM raw_responses
        WHERE source = 'data-api' AND url LIKE '%/activity%'
          AND url LIKE ? AND url LIKE '%offset=0%'
        ORDER BY fetched_at DESC LIMIT 1
        """,
        (f"%user={address}%",),
    ).fetchone()
    if row is None:
        return {}
    try:
        entries = json.loads(row["body"])
    except (TypeError, ValueError):
        return {}
    for e in entries:
        if e.get("conditionId") == market_id:
            return {"title": e.get("title"), "outcome": e.get("outcome"), "slug": e.get("slug")}
    return {}


def _our_open_positions(conn: sqlite3.Connection, *, mode: str = PAPER_MODE) -> list[dict]:
    """Every position we (paper-)hold right now, newest-opened first."""
    rows = conn.execute(
        "SELECT * FROM positions WHERE mode = ? AND status IN ('OPEN', 'PARTIAL') ORDER BY opened_at DESC",
        (mode,),
    ).fetchall()
    out = []
    for p in rows:
        ctx = _market_context(conn, p["address"], p["market_id"])
        out.append({**dict(p), "title": ctx.get("title"), "outcome": ctx.get("outcome"), "slug": ctx.get("slug")})
    return out


OUR_CLOSED_POSITIONS_LIMIT = 50


def _our_closed_positions(conn: sqlite3.Connection, *, mode: str = PAPER_MODE, limit: int = OUR_CLOSED_POSITIONS_LIMIT) -> list[dict]:
    """Resolved positions with a real outcome — the actual, realized
    record. Positions can also be CLOSED without a resolved outcome yet
    (Learner hasn't run) — that in-between state is deliberately not
    shown here (nothing to report until there's a real P&L number), the
    same way trader_detail's own dossier only shows what's actually
    computed.
    """
    rows = conn.execute(
        """
        SELECT p.*, o.our_pnl_usd, o.our_roi, o.copy_tax_total_cents, o.resolved_at, o.resolution
        FROM positions p JOIN outcomes o ON o.position_id = p.id
        WHERE p.mode = ?
        ORDER BY o.resolved_at DESC LIMIT ?
        """,
        (mode, limit),
    ).fetchall()
    out = []
    for p in rows:
        ctx = _market_context(conn, p["address"], p["market_id"])
        out.append({**dict(p), "title": ctx.get("title"), "outcome": ctx.get("outcome"), "slug": ctx.get("slug")})
    return out


OUR_TRADE_LOG_LIMIT = 50


def _our_trade_log(conn: sqlite3.Connection, *, mode: str = PAPER_MODE, limit: int = OUR_TRADE_LOG_LIMIT) -> list[dict]:
    """Every decision that actually moved our book — TAKE (entries) and
    MIRROR_EXIT (exits) — most recent first. This is the paper trade
    blotter; /decisions shows every decision including skips, which is a
    debugging view, not "what did we actually do."
    """
    rows = conn.execute(
        """
        SELECT d.*, ot.address, ot.market_id, ot.token_id, ot.side, ot.shares,
               ot.price AS their_price, ot.block_ts
        FROM decisions d JOIN observed_trades ot ON ot.id = d.observed_trade_id
        WHERE d.mode = ? AND d.verdict IN ('TAKE', 'MIRROR_EXIT')
        ORDER BY d.decided_at DESC LIMIT ?
        """,
        (mode, limit),
    ).fetchall()
    out = []
    for d in rows:
        ctx = _market_context(conn, d["address"], d["market_id"])
        out.append({**dict(d), "title": ctx.get("title")})
    return out


def _our_summary(conn: sqlite3.Connection, *, mode: str = PAPER_MODE) -> dict:
    """The headline numbers for the Portfolio page. `equity` is the
    starting bankroll plus realized P&L only — it deliberately does NOT
    mark open positions to their current price (nothing in this codebase
    fetches a live price for OUR OWN open token_id on a schedule yet, only
    for traders we're scouting); an open position is carried at cost until
    it resolves or we exit it, same understated-not-overstated bias
    outcomes.our_pnl_usd already uses (only realized P&L is written
    there). `bankroll` itself never grows/shrinks with P&L (config-fixed,
    per the operator's own explicit call) — that's the number sizing.py
    actually sizes new trades against; `equity` is the separate, honest
    "how are we actually doing" figure shown alongside it.
    """
    bankroll = _bankroll_usd()
    open_exposure = mirror_sizing.portfolio_exposure_usd(conn, mode)
    resolved = conn.execute(
        "SELECT o.our_pnl_usd FROM outcomes o JOIN positions p ON p.id = o.position_id WHERE p.mode = ?",
        (mode,),
    ).fetchall()
    realized_pnl = sum(r["our_pnl_usd"] for r in resolved)
    resolved_n = len(resolved)
    win_n = sum(1 for r in resolved if r["our_pnl_usd"] > 0)
    copy_tax_row = conn.execute(
        "SELECT AVG(o.copy_tax_total_cents) AS avg_cents, COUNT(*) AS n FROM outcomes o "
        "JOIN positions p ON p.id = o.position_id WHERE p.mode = ? AND o.copy_tax_total_cents IS NOT NULL",
        (mode,),
    ).fetchone()
    return {
        "bankroll": bankroll,
        "open_exposure": open_exposure,
        "available_capital": bankroll - open_exposure,
        "realized_pnl": realized_pnl,
        "equity": bankroll + realized_pnl,
        "resolved_n": resolved_n,
        "win_rate": (win_n / resolved_n) if resolved_n else None,
        "copy_tax_avg_cents": copy_tax_row["avg_cents"],
        "copy_tax_n": copy_tax_row["n"],
    }


def _equity_curve(conn: sqlite3.Connection, *, mode: str = PAPER_MODE, bankroll: float, width: int = 640, height: int = 160, pad: float = 8.0) -> dict | None:
    """Cumulative realized P&L over time, starting from the bankroll —
    the account's actual equity line. Same "None below 2 points" contract
    as _sparkline (a lone value has no trend to draw), and same
    realized-only caveat as _our_summary's `equity` above: unrealized
    swings on still-open positions aren't in this line.
    """
    rows = conn.execute(
        "SELECT o.resolved_at, o.our_pnl_usd FROM outcomes o "
        "JOIN positions p ON p.id = o.position_id "
        "WHERE p.mode = ? AND o.resolved_at IS NOT NULL ORDER BY o.resolved_at ASC",
        (mode,),
    ).fetchall()
    if len(rows) < 2:
        return None
    running = bankroll
    values = []
    for r in rows:
        running += r["our_pnl_usd"]
        values.append(running)

    x_span = (len(values) - 1) or 1
    y_min, y_max = min(values), max(values)
    y_span = (y_max - y_min) or 1

    def sx(i: int) -> float:
        return pad + i / x_span * (width - 2 * pad)

    def sy(v: float) -> float:
        return height - pad - (v - y_min) / y_span * (height - 2 * pad)

    coords = [(sx(i), sy(v)) for i, v in enumerate(values)]
    points_str = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    last_x, last_y = coords[-1]
    return {
        "points": points_str,
        "width": width,
        "height": height,
        "last_x": last_x,
        "last_y": last_y,
        "first_value": values[0],
        "last_value": values[-1],
        "n": len(values),
        "up": values[-1] >= values[0],
    }


def _latest_snapshot_per_trader(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.*, t.alias, t.discovery_source, t.active AS trader_active, t.first_seen
        FROM trader_snapshots s
        JOIN traders t ON t.address = s.address
        WHERE s.id IN (
            SELECT MAX(id) FROM trader_snapshots GROUP BY address
        )
        ORDER BY (s.roi_shrunk IS NULL), s.roi_shrunk DESC
        """
    ).fetchall()


@app.route("/")
def now() -> str:
    conn = get_db()
    heartbeat.beat(conn, SERVICE)
    conn.commit()

    beats = conn.execute("SELECT * FROM heartbeats ORDER BY service").fetchall()
    stale = heartbeat.stale_services(conn)
    trader_count = conn.execute("SELECT COUNT(*) AS n FROM traders").fetchone()["n"]
    snapshot_count = conn.execute("SELECT COUNT(*) AS n FROM trader_snapshots").fetchone()["n"]
    decision_count = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    open_position_count = conn.execute(
        "SELECT COUNT(*) AS n FROM positions WHERE status != 'CLOSED'"
    ).fetchone()["n"]

    # Copy tax headline (FR-28) — average round-trip copy tax across
    # resolved outcomes so far. None/0 rows until Mirror + Learner (Phase
    # 2+) start populating `outcomes`; the template renders that as "no
    # trades resolved yet", not a fake zero.
    copy_tax_row = conn.execute(
        "SELECT AVG(copy_tax_total_cents) AS avg_cents, COUNT(*) AS n "
        "FROM outcomes WHERE copy_tax_total_cents IS NOT NULL"
    ).fetchone()

    # Last event received (FR-28) — most recent row of the structured
    # event log, distinct from a heartbeat (a heartbeat says a service is
    # alive; an event says something actually happened).
    last_event = conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 1").fetchone()

    stale_services = {s["service"] for s in stale}

    # A service that's never sent a single heartbeat has no `heartbeats`
    # row at all (see heartbeat.stale_services) — synthesize a display row
    # so it shows up in the table as Stale/"never", not silently absent.
    known = {b["service"] for b in beats}
    never_beaten = [
        {"service": s["service"], "last_beat": "never", "detail_json": "no heartbeat recorded yet"}
        for s in stale
        if s["last_beat"] is None and s["service"] not in known
    ]
    beats = sorted([*beats, *never_beaten], key=lambda b: b["service"])

    return render_template(
        "now.html",
        beats=beats,
        stale=stale,
        stale_services=stale_services,
        trader_count=trader_count,
        snapshot_count=snapshot_count,
        decision_count=decision_count,
        open_position_count=open_position_count,
        copy_tax_avg_cents=copy_tax_row["avg_cents"],
        copy_tax_n=copy_tax_row["n"],
        last_event=last_event,
        active_tab="now",
    )


def _current_watchlist(conn: sqlite3.Connection) -> set[str]:
    """Who Mirror is tailing right now — a pure read (select_watchlist
    itself never writes; see mirror/watch_poll.py), so safe to call from
    the read-only dashboard. Used to render the "Tailed" badge on
    /traders and /traders/<address>.
    """
    watchlist_size = load_config().get("mirror", {}).get("watchlist_size", 20)
    return set(select_watchlist(conn, watchlist_size))


def _pinned_addresses() -> list[str]:
    """De-duplicated, lowercased, order-preserved read of
    mirror.pinned_addresses — the operator's own explicit "tail this
    person" list, distinct from `_current_watchlist` (which also
    includes the auto-filled top-ROI slots). Same expression
    mirror/watch_poll.py's own `_pinned_addresses` uses (see that
    module and SKILL.md's YAML-null gotcha for why `or []`, not a
    `.get()` default) — read directly here rather than importing a
    leading-underscore helper from another module.
    """
    raw = load_config().get("mirror", {}).get("pinned_addresses") or []
    return list(dict.fromkeys(a.lower() for a in raw if a))


def _active_operator_mandate(conn: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM mandates
        WHERE address = ? AND superseded_by IS NULL AND issued_by = 'operator'
        ORDER BY version DESC LIMIT 1
        """,
        (address,),
    ).fetchone()


def _my_portfolio(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict]:
    """"My Copy Portfolio" — just the wallets the operator has explicitly
    pinned, each paired with their active operator mandate if one exists
    (size multiplier, fast lane). A pure read reshaping data server.py
    already computes elsewhere (rows = _latest_snapshot_per_trader) plus
    one small per-address mandate lookup (bounded by pinned-list size,
    not the full ~200-trader table) — no new business logic, same
    pattern as _current_watchlist above.
    """
    by_address = {r["address"]: r for r in rows}
    out = []
    for address in _pinned_addresses():
        mandate = _active_operator_mandate(conn, address)
        out.append({"address": address, "snapshot": by_address.get(address), "mandate": mandate})
    return out


CATEGORY_GRADE_ORDER = {"A": 3, "B": 2, "C": 1, "D": 0}


def _category_summary(scores: list) -> dict | None:
    """Best/worst-graded category for the compact /traders column — full
    breakdown lives on the trader's own page. Only scores that actually
    cleared MIN_CATEGORY_TRADES_FOR_ANY_GRADE (grade is not None) are
    eligible for "best"/"worst"; a trader with only ungraded categories
    (too few resolved positions in every one) gets None here, rendered as
    "insufficient sample" by the template rather than a fabricated grade.
    """
    graded = [s for s in scores if s.grade is not None]
    if not graded:
        return None
    best = max(graded, key=lambda s: (CATEGORY_GRADE_ORDER[s.grade], s.score))
    worst = min(graded, key=lambda s: (CATEGORY_GRADE_ORDER[s.grade], s.score))
    return {"best": best, "worst": worst if worst is not best else None, "n_categories": len(graded)}


@app.route("/traders")
def traders() -> str:
    conn = get_db()
    rows = _latest_snapshot_per_trader(conn)
    watchlist = _current_watchlist(conn)
    my_portfolio = _my_portfolio(conn, rows)
    # Only a pinned trader can have fast_lane=1 (it requires an operator
    # mandate, which requires pinning first — mandate/operator.py) — so
    # this set only ever needs to cover the (small) pinned list, not
    # every row, for the /traders "Fast-lane only" filter to work.
    fast_lane_addresses = {p["address"] for p in my_portfolio if p["mandate"] and p["mandate"]["fast_lane"]}
    # Category-level Copy Score (workstream §5) — computed on demand per
    # row, purely from already-archived data (see scout/category_score.py's
    # module docstring for why this isn't a stored column). Bounded cost:
    # one raw_responses lookup + a parse of <=50 already-archived closed
    # positions per trader, same order of magnitude as _pnl_by_market's own
    # per-trader read on the detail page, just spread across every row here.
    category_summaries = {r["address"]: _category_summary(category_score.trader_category_scores(conn, r["address"])) for r in rows}
    return render_template(
        "traders.html",
        rows=rows,
        watchlist=watchlist,
        my_portfolio=my_portfolio,
        fast_lane_addresses=fast_lane_addresses,
        category_summaries=category_summaries,
        active_tab="traders",
    )


@app.route("/traders/<address>")
def trader_detail(address: str) -> str:
    conn = get_db()
    address = address.lower()
    trader = conn.execute("SELECT * FROM traders WHERE address = ?", (address,)).fetchone()
    if trader is None:
        abort(404)
    snapshots = conn.execute(
        "SELECT * FROM trader_snapshots WHERE address = ? ORDER BY scanned_at DESC",
        (address,),
    ).fetchall()
    active_mandate = conn.execute(
        """
        SELECT * FROM mandates
        WHERE address = ? AND superseded_by IS NULL
        ORDER BY version DESC LIMIT 1
        """,
        (address,),
    ).fetchone()

    latest = snapshots[0] if snapshots else None
    category_mix = {}
    if latest is not None and latest["category_mix_json"]:
        try:
            category_mix = json.loads(latest["category_mix_json"])
        except (TypeError, ValueError):
            category_mix = {}

    chronological = list(reversed(snapshots))  # oldest -> newest, for charting
    sparklines = {}
    for field, label, kind in SPARKLINE_METRICS:
        spark = _sparkline(chronological, field, kind)
        if spark is not None:
            sparklines[field] = {**spark, "label": label}

    recent_trades = _recent_trades(conn, address)
    pnl_by_market = _pnl_by_market(conn, address)
    category_scores = category_score.trader_category_scores(conn, address)
    is_tailed = config_write.is_pinned(address)  # pin status, not watchlist membership — see routes below
    strategy_configured = bool(os.environ.get("OPENROUTER_API_KEY", "").strip() and os.environ.get("OPENROUTER_MODEL", "").strip())

    return render_template(
        "trader_detail.html",
        trader=trader,
        snapshots=snapshots,
        latest=latest,
        category_mix=category_mix,
        sparklines=sparklines,
        recent_trades=recent_trades,
        pnl_by_market=pnl_by_market,
        category_scores=category_scores,
        active_mandate=active_mandate,
        is_tailed=is_tailed,
        strategy_configured=strategy_configured,
        active_tab="traders",
    )


# ─── Write routes (2026-08-08) ─────────────────────────────────────────
# See this module's own docstring for why the dashboard has these at all.
# Every one of them is a POST, redirects back to the trader's own page on
# success, and does the smallest possible write for what it's named —
# no route here does more than one of "pin", "unpin", "issue a mandate",
# "revoke a mandate", "generate a narrative".

@app.route("/traders/<address>/tail", methods=["POST"])
def tail_trader(address: str):
    address = address.lower()
    conn = get_db()
    trader = conn.execute("SELECT 1 FROM traders WHERE address = ?", (address,)).fetchone()
    if trader is None:
        abort(404)
    config_write.add_pinned(address)
    ensure_pinned_traders_exist(conn)  # FK-safe before the next Mirror/Scout cycle even runs
    conn.commit()
    return redirect(url_for("trader_detail", address=address))


@app.route("/traders/<address>/untail", methods=["POST"])
def untail_trader(address: str):
    address = address.lower()
    conn = get_db()
    config_write.remove_pinned(address)
    operator_mandate.revoke(conn, address=address)  # stop trading them too, not just stop watching
    conn.commit()
    return redirect(url_for("trader_detail", address=address))


@app.route("/traders/<address>/mandate", methods=["POST"])
def set_mandate(address: str):
    address = address.lower()
    conn = get_db()
    trader = conn.execute("SELECT 1 FROM traders WHERE address = ?", (address,)).fetchone()
    if trader is None:
        abort(404)
    if not config_write.is_pinned(address):
        abort(400, "Tail this trader before setting a mandate.")
    try:
        size_multiplier = float(request.form.get("size_multiplier", "1.0"))
    except ValueError:
        abort(400, "size_multiplier must be a number.")
    fast_lane_on = request.form.get("fast_lane") == "on"
    try:
        operator_mandate.issue(conn, address=address, size_multiplier=size_multiplier, fast_lane=fast_lane_on)
    except ValueError as exc:
        abort(400, str(exc))
    conn.commit()
    return redirect(url_for("trader_detail", address=address))


@app.route("/traders/<address>/mandate/revoke", methods=["POST"])
def revoke_mandate(address: str):
    address = address.lower()
    conn = get_db()
    operator_mandate.revoke(conn, address=address)
    conn.commit()
    return redirect(url_for("trader_detail", address=address))


@app.route("/traders/<address>/analyze", methods=["POST"])
def analyze_trader(address: str):
    address = address.lower()
    conn = get_db()
    trader = conn.execute("SELECT * FROM traders WHERE address = ?", (address,)).fetchone()
    snapshot = conn.execute(
        "SELECT * FROM trader_snapshots WHERE address = ? ORDER BY scanned_at DESC LIMIT 1", (address,)
    ).fetchone()
    if trader is None or snapshot is None:
        abort(404)
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not api_key or not model:
        abort(503, "OPENROUTER_API_KEY/OPENROUTER_MODEL not configured — strategy analysis is unavailable.")
    strategy_budget = load_config().get("strategy", {}).get("daily_budget_usd", 1.0)
    with OpenRouterClient(conn, api_key=api_key, model=model) as client:
        maybe_generate_strategy(
            conn, Logger("dashboard", conn), trader=trader, snapshot=snapshot,
            client=client, daily_budget_usd=strategy_budget, force=True,
        )
    conn.commit()
    return redirect(url_for("trader_detail", address=address))


@app.route("/portfolio")
def portfolio() -> str:
    """Our own paper account: bankroll/exposure/realized P&L, every open
    and resolved position, the trade blotter, and an equity curve. This
    is the "can I actually see what my copy-trading is doing" view that
    /traders' "My Copy Portfolio" section (their dossiers) and /decisions
    (every decision, including skips) don't cover on their own.
    """
    conn = get_db()
    summary = _our_summary(conn)
    open_positions = _our_open_positions(conn)
    closed_positions = _our_closed_positions(conn)
    trade_log = _our_trade_log(conn)
    equity_curve = _equity_curve(conn, bankroll=summary["bankroll"])
    return render_template(
        "portfolio.html",
        summary=summary,
        open_positions=open_positions,
        closed_positions=closed_positions,
        trade_log=trade_log,
        equity_curve=equity_curve,
        active_tab="portfolio",
    )


@app.route("/decisions")
def decisions() -> str:
    conn = get_db()
    rows = conn.execute(
        """
        SELECT d.*, ot.address, ot.market_id, ot.side, ot.shares, ot.price AS their_price,
               ot.block_ts
        FROM decisions d
        JOIN observed_trades ot ON ot.id = d.observed_trade_id
        ORDER BY d.decided_at DESC
        LIMIT 200
        """
    ).fetchall()
    return render_template("decisions.html", rows=rows, active_tab="decisions")


@app.route("/calibration")
def calibration() -> str:
    return render_template("calibration.html", active_tab="calibration")


@app.route("/us-vs-them")
def us_vs_them() -> str:
    return render_template("us_vs_them.html", active_tab="us_vs_them")


@app.route("/definitions")
def definitions() -> str:
    return render_template("definitions.html", active_tab="definitions")


@app.route("/how-it-works")
def how_it_works() -> str:
    return render_template("how_it_works.html", active_tab="how_it_works")


def main() -> None:
    conn = get_connection()  # ensure schema exists before serving
    conn.close()
    # threaded=True matters now that a route can block for a while (the
    # /analyze route's synchronous LLM call has taken up to ~90s observed
    # live) — without it, Flask's dev server is single-threaded and that
    # one request would freeze the entire dashboard, not just that tab,
    # for everyone, for the duration of the call. Each request already
    # gets its own SQLite connection (get_db()'s per-request g.db), and
    # WAL mode (db/conn.py) is built for concurrent readers/writers, so
    # this is safe, not just convenient.
    app.run(host=HOST, port=PORT, threaded=True)


if __name__ == "__main__":
    main()
