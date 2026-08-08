"""learner/resolve.py against a real (temp-file) db and a fake gamma
client (no network). Covers both paths into `outcomes` (SCHEMA.md
invariant 5): a fully-exited CLOSED position (no gamma call needed) and
an OPEN position whose market resolves (gamma-api confirms it).
"""

import json
from datetime import datetime, timezone

import pytest

from polyprinter.db.conn import get_connection
from polyprinter.learner import resolve
from polyprinter.mirror import execute
from polyprinter.obs.log import Logger

ADDRESS = "0xtrader"
MODE = "paper"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_db(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, _now()),
    )
    return conn


def _log(conn):
    return Logger("test", conn)


def _insert_trade(conn, *, side="BUY", shares=10.0, price=0.5, market_id="m1", token_id="tok"):
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO observed_trades (
            address, tx_hash, log_index, market_id, token_id, side, shares,
            price, block_ts, detected_at, source
        ) VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 'poll')
        """,
        (ADDRESS, f"0x{market_id}{token_id}{side}{shares}{price}", market_id, token_id, side, shares, price, now, now),
    )
    return conn.execute("SELECT * FROM observed_trades WHERE id = ?", (cur.lastrowid,)).fetchone()


def _insert_decision(conn, trade_id, *, verdict="TAKE", size_usd=None):
    cur = conn.execute(
        "INSERT INTO decisions (observed_trade_id, decided_at, verdict, size_usd, mode) VALUES (?, ?, ?, ?, ?)",
        (trade_id, _now(), verdict, size_usd, MODE),
    )
    return cur.lastrowid


def _open_position(conn, *, price=0.5, size_usd=50.0, market_id="m1", token_id="tok"):
    trade = _insert_trade(conn, price=price, market_id=market_id, token_id=token_id)
    decision_id = _insert_decision(conn, trade["id"], size_usd=size_usd)
    position_id = execute.open_position(conn, decision_id=decision_id, trade=trade, size_usd=size_usd, mode=MODE)
    return conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()


class FakeGammaClient:
    """Stands in for PolymarketGammaClient — same call shape
    (markets_by_condition_ids), serves canned market dicts in gamma-api's
    real shape (outcomes/outcomePrices/clobTokenIds as JSON strings)."""

    def __init__(self, markets_by_condition_id: dict[str, dict]):
        self._markets = markets_by_condition_id

    def markets_by_condition_ids(self, condition_ids, *, closed=None):
        return [self._markets[cid] for cid in condition_ids if cid in self._markets]


def _resolved_market(*, condition_id, outcomes, outcome_prices, token_ids):
    return {
        "conditionId": condition_id,
        "closed": True,
        "outcomes": json.dumps(outcomes),
        "outcomePrices": json.dumps([str(p) for p in outcome_prices]),
        "clobTokenIds": json.dumps(token_ids),
        "closedTime": "2026-08-09T00:00:00Z",
    }


# ─── compute_position_outcome (pure) ─────────────────────────────────

def test_compute_outcome_full_hold_to_resolution_win():
    outcome = resolve.compute_position_outcome(
        cost_usd=51.0, our_entry_price=0.51, their_entry_price=0.50,
        exit_legs=[{"shares": 100.0, "our_price": 1.0, "their_price": 1.0, "proceeds_usd": 100.0}],
    )
    assert outcome["our_pnl_usd"] == pytest.approx(49.0)
    assert outcome["our_roi"] == pytest.approx(49.0 / 51.0)
    assert outcome["copy_tax_entry_cents"] == pytest.approx(1.0)  # (0.51 - 0.50) * 100
    assert outcome["copy_tax_exit_cents"] == pytest.approx(0.0)  # redemption: no execution lag
    assert outcome["copy_tax_total_cents"] == pytest.approx(1.0)
    assert outcome["their_pnl_per_share"] == pytest.approx(0.50)  # 1.0 - 0.50
    assert outcome["their_edge_cents"] == pytest.approx(50.0)


def test_compute_outcome_full_hold_to_resolution_loss():
    outcome = resolve.compute_position_outcome(
        cost_usd=51.0, our_entry_price=0.51, their_entry_price=0.50,
        exit_legs=[{"shares": 100.0, "our_price": 0.0, "their_price": 0.0, "proceeds_usd": 0.0}],
    )
    assert outcome["our_pnl_usd"] == pytest.approx(-51.0)
    assert outcome["our_roi"] == pytest.approx(-1.0)


def test_compute_outcome_blends_partial_exit_and_redemption_shares_weighted():
    # 60 shares sold early at a real price, 40 redeemed at resolution —
    # exit price should be the shares-weighted blend, not a plain mean.
    outcome = resolve.compute_position_outcome(
        cost_usd=51.0, our_entry_price=0.51, their_entry_price=0.50,
        exit_legs=[
            {"shares": 60.0, "our_price": 0.70, "their_price": 0.72, "proceeds_usd": 42.0},
            {"shares": 40.0, "our_price": 1.0, "their_price": 1.0, "proceeds_usd": 40.0},
        ],
    )
    expected_our_exit = (60 * 0.70 + 40 * 1.0) / 100
    expected_their_exit = (60 * 0.72 + 40 * 1.0) / 100
    assert outcome["copy_tax_exit_cents"] == pytest.approx((expected_their_exit - expected_our_exit) * 100)
    assert outcome["our_pnl_usd"] == pytest.approx(82.0 - 51.0)


def test_compute_outcome_returns_none_for_zero_shares():
    assert resolve.compute_position_outcome(cost_usd=10.0, our_entry_price=0.5, their_entry_price=0.5, exit_legs=[]) is None


# ─── close_fully_exited_position (Path 1: CLOSED, no gamma call) ─────

def test_close_fully_exited_position_writes_outcome(tmp_path):
    conn = _make_db(tmp_path)
    position = _open_position(conn, price=0.5, size_usd=50.0)

    exit_trade = _insert_trade(conn, side="SELL", price=0.8)
    exit_decision_id = _insert_decision(conn, exit_trade["id"], verdict="MIRROR_EXIT")
    execute.record_exit(conn, decision_id=exit_decision_id, position=position, trade=exit_trade, fraction=1.0)
    closed_position = conn.execute("SELECT * FROM positions WHERE id = ?", (position["id"],)).fetchone()
    assert closed_position["status"] == "CLOSED"

    outcome_id = resolve.close_fully_exited_position(conn, closed_position)
    conn.commit()

    assert outcome_id is not None
    outcome = conn.execute("SELECT * FROM outcomes WHERE position_id = ?", (position["id"],)).fetchone()
    assert outcome["our_pnl_usd"] == pytest.approx((100 * 0.8 * 0.98) - 51.0)
    assert outcome["resolution"] is None  # closed via trades, not a market resolution


def test_close_fully_exited_position_is_noop_with_no_exit_legs(tmp_path):
    conn = _make_db(tmp_path)
    position = _open_position(conn)  # still OPEN, no exits
    assert resolve.close_fully_exited_position(conn, position) is None
    assert conn.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"] == 0


# ─── resolve_open_position (Path 2: OPEN/PARTIAL, market resolved) ───

def test_resolve_open_position_marks_resolved_and_writes_outcome(tmp_path):
    conn = _make_db(tmp_path)
    position = _open_position(conn, price=0.5, size_usd=50.0, market_id="m1", token_id="tok-yes")
    resolution = {
        "outcomes": ["Yes", "No"],
        "outcome_prices": [1.0, 0.0],
        "clob_token_ids": ["tok-yes", "tok-no"],
        "closed_time": "2026-08-09T00:00:00Z",
    }

    outcome_id = resolve.resolve_open_position(conn, position, resolution)
    conn.commit()

    assert outcome_id is not None
    updated = conn.execute("SELECT * FROM positions WHERE id = ?", (position["id"],)).fetchone()
    assert updated["status"] == "RESOLVED"
    assert updated["shares_open"] == pytest.approx(0.0)
    assert updated["closed_at"] is not None

    outcome = conn.execute("SELECT * FROM outcomes WHERE position_id = ?", (position["id"],)).fetchone()
    assert outcome["resolution"] == "Yes"
    assert outcome["our_pnl_usd"] == pytest.approx(100.0 - 51.0)  # 100 shares redeemed at $1


def test_resolve_open_position_returns_none_when_token_not_in_resolution(tmp_path):
    conn = _make_db(tmp_path)
    position = _open_position(conn, token_id="tok-yes")
    resolution = {"outcomes": ["Yes", "No"], "outcome_prices": [1.0, 0.0], "clob_token_ids": ["some-other-token"]}
    assert resolve.resolve_open_position(conn, position, resolution) is None
    unchanged = conn.execute("SELECT * FROM positions WHERE id = ?", (position["id"],)).fetchone()
    assert unchanged["status"] == "OPEN"


# ─── run_once (end-to-end, both paths, via a fake gamma client) ──────

def test_run_once_resolves_closed_and_open_positions_leaves_unresolved_alone(tmp_path):
    conn = _make_db(tmp_path)

    # CLOSED via mirrored sell — resolvable without any gamma call.
    closed_pos = _open_position(conn, price=0.5, size_usd=50.0, market_id="m-closed", token_id="tok-closed")
    exit_trade = _insert_trade(conn, side="SELL", price=0.8, market_id="m-closed", token_id="tok-closed")
    exit_decision_id = _insert_decision(conn, exit_trade["id"], verdict="MIRROR_EXIT")
    execute.record_exit(conn, decision_id=exit_decision_id, position=closed_pos, trade=exit_trade, fraction=1.0)

    # OPEN, market has resolved.
    resolved_pos = _open_position(conn, price=0.5, size_usd=50.0, market_id="m-resolved", token_id="tok-r-yes")

    # OPEN, market still trading — should be left alone.
    still_open_pos = _open_position(conn, price=0.5, size_usd=50.0, market_id="m-open", token_id="tok-open")

    conn.commit()

    gamma = FakeGammaClient(
        {
            "m-resolved": _resolved_market(
                condition_id="m-resolved", outcomes=["Yes", "No"], outcome_prices=[1.0, 0.0],
                token_ids=["tok-r-yes", "tok-r-no"],
            ),
        }
    )

    result = resolve.run_once(conn, _log(conn), gamma_client=gamma)

    assert result == {"n_closed_resolved": 1, "n_open_resolved": 1, "n_still_open": 1}
    assert conn.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"] == 2

    still_open = conn.execute("SELECT * FROM positions WHERE id = ?", (still_open_pos["id"],)).fetchone()
    assert still_open["status"] == "OPEN"
    resolved = conn.execute("SELECT * FROM positions WHERE id = ?", (resolved_pos["id"],)).fetchone()
    assert resolved["status"] == "RESOLVED"


def test_run_once_is_idempotent_on_a_second_pass(tmp_path):
    conn = _make_db(tmp_path)
    position = _open_position(conn, market_id="m1", token_id="tok-yes")
    conn.commit()
    gamma = FakeGammaClient(
        {"m1": _resolved_market(condition_id="m1", outcomes=["Yes", "No"], outcome_prices=[1.0, 0.0], token_ids=["tok-yes", "tok-no"])}
    )

    first = resolve.run_once(conn, _log(conn), gamma_client=gamma)
    second = resolve.run_once(conn, _log(conn), gamma_client=gamma)

    assert first["n_open_resolved"] == 1
    assert second == {"n_closed_resolved": 0, "n_open_resolved": 0, "n_still_open": 0}  # already resolved -> not a candidate anymore
    assert conn.execute("SELECT COUNT(*) AS n FROM outcomes WHERE position_id = ?", (position["id"],)).fetchone()["n"] == 1
