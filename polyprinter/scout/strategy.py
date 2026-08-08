"""Generates a plain-English strategy narrative for a trader's snapshot
(FR-3/FR-4's dossier work, extended — not a new phase: this is read-only
explanation on top of data Scout already computes, touches nothing in
Mirror/decide/execute).

On-demand only (changed 2026-08-08, operator's explicit correction): the
first version of this ran automatically for every trader Scout kept, every
run. That burned real token spend on traders nobody had asked about — the
only caller now is the dashboard's /traders/<address>/analyze route,
passing force=True. There is no automatic per-Scout-run pass anymore. The
delta-trigger (should_regenerate, same discipline as mandate/trigger.py's
FR-5) still exists and still guards a non-forced call, but nothing in this
codebase makes a non-forced call today — it's there for a future caller
that isn't purely operator-driven, not exercised by the current one.

Same audit discipline as mandate/issue.py too: every attempt logged to
llm_calls in full (invariant 4), purpose='strategy' so its budget
(config.yaml `strategy.daily_budget_usd`) never mixes with mandate's own
cap (see mandate/issue.py's today_llm_spend_usd). Never raises — one
trader's LLM failure logs and moves on, same discipline as a failed
dossier fetch or a failed mandate call.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pydantic import ValidationError

from polyprinter.mandate.issue import today_llm_spend_usd
from polyprinter.obs.log import Logger
from polyprinter.scout.strategy_prompt import build_prompt
from polyprinter.scout.strategy_schema import JSON_SCHEMA, StrategyOutput
from polyprinter.sources.openrouter import OpenRouterClient

PURPOSE = "strategy"
MAX_TOKENS = 6000  # same value mandate/issue.py's ISSUE_MAX_TOKENS converged on, for the same
# reason: this is a reasoning model, and its reasoning tokens are emitted (and billed, and
# count against max_tokens) BEFORE the actual answer — 1500 truncated real output mid-JSON
# live (2026-08-08, finish_reason "length"), the exact failure mode mandate/issue.py already
# documented hitting at that same threshold. Cost impact is negligible either way (~$0.0003/
# call observed live) — see that module's own note on why headroom costs nothing here.

# Same thresholds as mandate/trigger.py, kept as an independent copy (not
# imported) since strategy narration and mandate re-evaluation are
# different call types that may want different tuning later — today
# they're deliberately equal.
RESOLVED_POSITIONS_DELTA = 5
METRIC_DELTA_FRACTION = 0.15
DELTA_TRACKED_FIELDS = ("roi_shrunk", "hold_to_resolution_rate")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def should_regenerate(conn: sqlite3.Connection, address: str, latest_snapshot: sqlite3.Row) -> tuple[bool, str]:
    """Returns (should_call_llm, reason) — compares `latest_snapshot`
    against the PRIOR snapshot for this address (not the one just
    inserted). No prior snapshot, or a prior with no narrative yet, always
    triggers.
    """
    prior = conn.execute(
        """
        SELECT * FROM trader_snapshots
        WHERE address = ? AND id != ?
        ORDER BY scanned_at DESC LIMIT 1
        """,
        (address, latest_snapshot["id"]),
    ).fetchone()
    if prior is None:
        return True, "first snapshot for this trader"
    if prior["strategy_summary"] is None:
        return True, "no narrative generated yet"

    prior_resolved = prior["resolved_positions"] or 0
    latest_resolved = latest_snapshot["resolved_positions"] or 0
    delta_resolved = latest_resolved - prior_resolved
    if delta_resolved >= RESOLVED_POSITIONS_DELTA:
        return True, f"{delta_resolved} newly resolved positions since the last narrative (>= {RESOLVED_POSITIONS_DELTA})"

    for field in DELTA_TRACKED_FIELDS:
        old, new = prior[field], latest_snapshot[field]
        if old is None or new is None:
            continue
        if old == 0:
            continue
        relative_move = abs(new - old) / abs(old)
        if relative_move > METRIC_DELTA_FRACTION:
            return True, f"{field} moved {relative_move:.0%} since the last narrative (> {METRIC_DELTA_FRACTION:.0%})"

    return False, "no material change since the last narrative"


def _insert_llm_call(
    conn: sqlite3.Connection,
    *,
    model: str,
    prompt: str,
    raw_response: str,
    parsed_ok: bool,
    parse_error: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    cost_usd: float | None,
    latency_ms: int,
) -> None:
    conn.execute(
        """
        INSERT INTO llm_calls (
            purpose, model, prompt, raw_response, parsed_ok, parse_error,
            tokens_in, tokens_out, cost_usd, latency_ms, called_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            PURPOSE, model, prompt, raw_response, int(parsed_ok), parse_error,
            tokens_in, tokens_out, cost_usd, latency_ms, _now_iso(),
        ),
    )


def maybe_generate_strategy(
    conn: sqlite3.Connection,
    log: Logger,
    *,
    trader: sqlite3.Row,
    snapshot: sqlite3.Row,
    client: OpenRouterClient,
    daily_budget_usd: float,
    force: bool = False,
) -> str:
    """Returns a short outcome string for logging/testing:
    "skipped:<reason>" | "generated" | "invalid:<error>" | "error:<exc>".
    On success, UPDATEs `snapshot`'s own strategy_summary column in place
    — this is the one narrow, deliberate exception to trader_snapshots
    being append-only (docs/SCHEMA.md invariant 3): the row was inserted
    moments ago in this same Scout run and has no other reader yet, so
    there's no "trajectory" being rewritten, just finishing what that
    insert started. Every later snapshot still gets its own independent
    narrative, never edited after the fact.

    `force=True` (the dashboard's /traders/<address>/analyze route —
    operator's explicit choice, 2026-08-08: analyze who I choose, not
    everyone, so we don't burn tokens on traders I never accepted)
    bypasses should_regenerate() entirely. The click IS the trigger; there
    is no automatic per-Scout-run pass anymore — see this module's own
    docstring and scout/run.py's history for what used to call this
    unconditionally for every kept trader.
    """
    address = trader["address"]
    model = client.model

    if force:
        should_call, trigger_reason = True, "operator requested (force)"
    else:
        should_call, trigger_reason = should_regenerate(conn, address, snapshot)
    if not should_call:
        log.info("strategy.skipped", address=address, reason=trigger_reason)
        return f"skipped:{trigger_reason}"

    spent_today = today_llm_spend_usd(conn, PURPOSE)
    if spent_today >= daily_budget_usd:
        log.warning("strategy.budget_exhausted", address=address, spent_today_usd=spent_today, daily_budget_usd=daily_budget_usd)
        return "skipped:daily budget exhausted"

    prompt = build_prompt(trader, snapshot)
    start = datetime.now(timezone.utc)
    try:
        result = client.complete_json(prompt, json_schema=JSON_SCHEMA, schema_name="strategy", max_tokens=MAX_TOKENS)
    except Exception as exc:  # noqa: BLE001 — network/API failure, log and move on
        latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
        _insert_llm_call(
            conn, model=model, prompt=prompt, raw_response="", parsed_ok=False,
            parse_error=str(exc), tokens_in=None, tokens_out=None, cost_usd=None, latency_ms=latency_ms,
        )
        log.error("strategy.call_failed", address=address, error=str(exc))
        return f"error:{exc}"

    latency_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)
    raw_response = result.get("content") or ""

    try:
        output = StrategyOutput.model_validate_json(raw_response)
        parse_error = None
    except (ValidationError, ValueError, TypeError) as exc:
        parse_error = str(exc)
        output = None

    _insert_llm_call(
        conn,
        model=model, prompt=prompt, raw_response=raw_response,
        parsed_ok=output is not None, parse_error=parse_error,
        tokens_in=result["prompt_tokens"], tokens_out=result["completion_tokens"],
        cost_usd=result["cost_usd"], latency_ms=latency_ms,
    )

    if output is None:
        log.error("strategy.parse_failed", address=address, error=parse_error, finish_reason=result.get("finish_reason"))
        return f"invalid:{parse_error}"

    summary_text = f"{output.headline}\n\n{output.summary}"
    conn.execute("UPDATE trader_snapshots SET strategy_summary = ? WHERE id = ?", (summary_text, snapshot["id"]))
    log.info("strategy.generated", address=address, cost_usd=result["cost_usd"])
    return "generated"
