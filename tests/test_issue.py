"""mandate/issue.py against a real (temp-file) db and a fake OpenRouter
client — no network, no real spend. The fake matches
sources.openrouter.OpenRouterClient.complete_json's return shape exactly.
"""

import json
from datetime import datetime, timezone

from polyprinter.db.conn import get_connection
from polyprinter.mandate.issue import maybe_issue_mandate, today_llm_spend_usd

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
        "verdict": "FOLLOW",
        "confidence": "HIGH",
        "reasoning": "Large sample, high hold-to-resolution rate.",
        "max_position_usd": None,
        "categories_allowed": None,
        "categories_blocked": None,
        "min_entry_price": None,
        "max_entry_price": None,
        "min_market_liquidity": None,
    }
    content.update(overrides.pop("content", {}))
    base = {
        "content": json.dumps(content),
        "prompt_tokens": 500,
        "completion_tokens": 80,
        "reasoning_tokens": 10,
        "cost_usd": 0.0002,
        "finish_reason": "stop",
    }
    base.update(overrides)
    return base


def _make_db(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    conn.execute(
        "INSERT INTO traders (address, first_seen, discovery_source) VALUES (?, ?, 'lb_day')",
        (ADDRESS, _now()),
    )
    cur = conn.execute(
        "INSERT INTO trader_snapshots (address, scanned_at, resolved_positions, roi_shrunk, hold_to_resolution_rate) "
        "VALUES (?, ?, 50, 0.22, 0.85)",
        (ADDRESS, _now()),
    )
    snapshot_id = cur.lastrowid
    trader = conn.execute("SELECT * FROM traders WHERE address = ?", (ADDRESS,)).fetchone()
    snapshot = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    return conn, trader, snapshot


def test_issues_mandate_on_valid_response(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())

    outcome = maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)

    assert outcome == "issued:FOLLOW"
    mandate = conn.execute("SELECT * FROM mandates WHERE address = ?", (ADDRESS,)).fetchone()
    assert mandate is not None
    assert mandate["verdict"] == "FOLLOW"
    assert mandate["version"] == 1
    assert mandate["superseded_by"] is None

    llm_call = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert llm_call["parsed_ok"] == 1
    assert llm_call["cost_usd"] == 0.0002


def test_second_mandate_supersedes_the_first(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())
    maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)
    first = conn.execute("SELECT * FROM mandates WHERE address = ?", (ADDRESS,)).fetchone()

    # force a re-evaluation by inserting a materially-changed snapshot
    cur = conn.execute(
        "INSERT INTO trader_snapshots (address, scanned_at, resolved_positions, roi_shrunk, hold_to_resolution_rate) "
        "VALUES (?, ?, 60, 0.22, 0.85)",
        (ADDRESS, _now()),
    )
    snapshot2 = conn.execute("SELECT * FROM trader_snapshots WHERE id = ?", (cur.lastrowid,)).fetchone()
    maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot2, client=client, daily_budget_usd=1.0)

    first_after = conn.execute("SELECT * FROM mandates WHERE id = ?", (first["id"],)).fetchone()
    second = conn.execute("SELECT * FROM mandates WHERE version = 2").fetchone()
    assert first_after["superseded_by"] == second["id"]
    assert second["superseded_by"] is None


def test_skips_when_no_material_change(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())
    maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)
    assert client.calls == 1

    # same trader, same snapshot again -> no material change -> no second call
    outcome = maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)
    assert outcome.startswith("skipped:")
    assert client.calls == 1


def test_skips_when_budget_exhausted(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())

    outcome = maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=0.0)
    assert outcome == "skipped:daily budget exhausted"
    assert client.calls == 0


def test_today_llm_spend_sums_todays_calls(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response(cost_usd=0.00042))
    maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)
    assert today_llm_spend_usd(conn) == 0.00042


def test_invalid_json_is_logged_not_written_as_a_mandate(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(_valid_response())
    # override content directly with something that doesn't parse
    client._response["content"] = "not valid json"

    outcome = maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)

    assert outcome.startswith("invalid:")
    assert conn.execute("SELECT COUNT(*) AS n FROM mandates").fetchone()["n"] == 0
    llm_call = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert llm_call["parsed_ok"] == 0
    assert llm_call["parse_error"] is not None


def test_client_exception_is_logged_not_raised(tmp_path):
    conn, trader, snapshot = _make_db(tmp_path)
    client = FakeClient(raises=RuntimeError("network exploded"))

    outcome = maybe_issue_mandate(conn, _log(conn), trader=trader, snapshot=snapshot, client=client, daily_budget_usd=1.0)

    assert outcome == "error:network exploded"
    assert conn.execute("SELECT COUNT(*) AS n FROM mandates").fetchone()["n"] == 0
    llm_call = conn.execute("SELECT * FROM llm_calls").fetchone()
    assert llm_call["parsed_ok"] == 0
    assert "network exploded" in llm_call["parse_error"]


def _log(conn):
    from polyprinter.obs.log import Logger

    return Logger("test", conn)
