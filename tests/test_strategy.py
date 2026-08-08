"""scout/strategy.py against a real (temp-file) db and a fake OpenRouter
client — no network, no real spend. Mirrors tests/test_issue.py's shape.
"""

import json
from datetime import datetime, timezone

from polyprinter.db.conn import get_connection
from polyprinter.scout.strategy import maybe_generate_strategy, should_regenerate

ADDRESS = "0xtrader"
MODEL = "fake/model"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FakeClient:
    def __init__(self, response: dict | None = None, *, raises: Exception | None = None):
        self.model = MODEL
        self._response = response
        self._raises = raises
        self.calls = 0

    def complete_json(self, prompt, *, json_schema, schema_name, max_tokens, reasoning_effort="low"):
        self.calls += 1
        if self._raises:
            raise self._raises
        return self._response


def _valid_response(**overrides):
    content = {
        "headline": "Fast crypto scalper, flat sizing, high hold-to-resolution.",
        "summary": "Trades short-duration up/down markets with consistent bet sizes and rarely exits early.",
    }
    content.update(overrides.pop("content", {}))
    base = {
        "content": json.dumps(content),
        "prompt_tokens": 400,
        "completion_tokens": 60,
        "reasoning_tokens": 5,
        "cost_usd": 0.0003,
        "finish_reason": "stop",
    }
    base.update(overrides)
    return base


def _insert_snapshot(conn, *, resolved_positions=50, roi_shrunk=0.22, hold_to_resolution_rate=0.85):
    cur = conn.execute(
        "INSERT INTO trader_snapshots (address, scanned_at, resolved_positions, roi_shrunk, hold_to_resolution_rate) "
        "VALUES (?, ?, ?, ?, ?)",
        (ADDRESS, _now(), resolved_positions, roi_shrunk, hold_to_resolution_rate),
    )
    return conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (cur.lastrowid,)).fetchone()


def _make_db(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, _now()),
    )
    snapshot = _insert_snapshot(conn)
    trader = conn.execute("SELECT * FROM traders WHERE address = ?", (ADDRESS,)).fetchone()
    return conn, trader, snapshot


def _log(conn):
    from polyprinter.obs.log import Logger

    return Logger("test", conn)


def test_generates_and_stores_narrative_on_the_snapshot_row(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())

    outcome = maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)

    assert outcome == "generated"
    updated = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (snapshot["id"],)).fetchone()
    assert updated["strategy_summary"].startswith("Fast crypto scalper")
    assert "consistent bet sizes" in updated["strategy_summary"]

    llm_call = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert llm_call["purpose"] == "strategy"
    assert llm_call["parsed_ok"] == 1


def test_first_snapshot_always_triggers(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    should, reason = should_regenerate(conn, ADDRESS, snapshot)
    assert should is True
    assert "first snapshot" in reason


def test_skips_when_no_material_change_since_last_narrative(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())
    maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)
    assert client.calls == 1

    # a new snapshot with materially the same numbers -> no material change
    snapshot2 = _insert_snapshot(conn, resolved_positions=51, roi_shrunk=0.22, hold_to_resolution_rate=0.85)
    outcome = maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot2, client=client, daily_budget_usd=1.0)

    assert outcome.startswith("skipped:")
    assert client.calls == 1


def test_regenerates_on_material_change(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())
    maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)

    snapshot2 = _insert_snapshot(conn, resolved_positions=60, roi_shrunk=0.45, hold_to_resolution_rate=0.85)
    outcome = maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot2, client=client, daily_budget_usd=1.0)

    assert outcome == "generated"
    assert client.calls == 2


def test_skips_when_budget_exhausted(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())

    outcome = maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=0.0)

    assert outcome == "skipped:daily budget exhausted"
    assert client.calls == 0


def test_mandate_and_strategy_budgets_are_independent(tmp_path):
    """A day's mandate spend must not count against strategy's budget, or
    vice versa (purpose-scoped in llm_calls — see mandate/issue.py's
    today_llm_spend_usd).
    """
    from polyprinter.mandate.issue import today_llm_spend_usd as mandate_spend

    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response(cost_usd=0.5))
    maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)

    assert mandate_spend(conn) == 0  # strategy spend didn't leak into mandate's own budget check


def test_invalid_json_is_logged_not_written_as_a_narrative(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())
    client._response["content"] = "not valid json"

    outcome = maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)

    assert outcome.startswith("invalid:")
    updated = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (snapshot["id"],)).fetchone()
    assert updated["strategy_summary"] is None
    llm_call = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert llm_call["parsed_ok"] == 0


def test_client_exception_is_logged_not_raised(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(raises=RuntimeError("network exploded"))

    outcome = maybe_generate_strategy(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)

    assert outcome == "error:network exploded"
    llm_call = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert llm_call["parsed_ok"] == 0
