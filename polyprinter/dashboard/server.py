"""Dashboard server. Reads the DB directly (FR-27) — never generate-and-push.
Bound to 127.0.0.1:8765 only; reached over a tunnel, not exposed. Dashboard
is read-only (invariant 5) — no writes happen from this process except its
own heartbeat.
"""

from __future__ import annotations

import json
import sqlite3

from flask import Flask, abort, g, render_template

from polyprinter.db.conn import get_connection
from polyprinter.obs import heartbeat

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

    return render_template(
        "now.html",
        beats=beats,
        stale=stale,
        trader_count=trader_count,
        snapshot_count=snapshot_count,
        decision_count=decision_count,
        open_position_count=open_position_count,
        active_tab="now",
    )


@app.route("/traders")
def traders() -> str:
    conn = get_db()
    rows = _latest_snapshot_per_trader(conn)
    return render_template("traders.html", rows=rows, active_tab="traders")


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
    return render_template(
        "trader_detail.html",
        trader=trader,
        snapshots=snapshots,
        active_mandate=active_mandate,
        active_tab="traders",
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


def main() -> None:
    conn = get_connection()  # ensure schema exists before serving
    conn.close()
    app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    main()
