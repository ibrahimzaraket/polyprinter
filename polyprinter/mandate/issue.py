"""LLM call, validate, persist (FR-7/FR-8) — the only place mandates
actually get written. Scout owns the `mandates` table (docs/SCHEMA.md
invariant 5) and calls `maybe_issue_mandate` once per trader, right after
writing that trader's snapshot (see scout/run.py) — there is no separate
"mandate service"; this is a step inside Scout's own daily run.

Every attempt is logged to `llm_calls` in full (invariant 4: prompt and
raw_response stored whole, never truncated) whether or not it produces a
valid mandate — a parse failure is exactly the kind of thing that needs
to be inspectable later, not silently retried into invisibility.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from polyprinter.mandate.prompt import build_prompt
from polyprinter.mandate.schema import JSON_SCHEMA, MandateOutput
from polyprinter.mandate.trigger import should_reevaluate
from polyprinter.obs.log import Logger
from polyprinter.sources.openrouter import OpenRouterClient

PURPOSE = "mandate"
ISSUE_MAX_TOKENS = 6000  # 1500 still truncated a real dossier prompt's response mid-JSON
# live (2026-08-08, finish_reason "length") even with reasoning effort "low" — a short
# test prompt underestimated how long a real prose `reasoning` field runs. 4000 cut
# failures to ~2/21 (still occasional very-long outliers); bumped further since cost
# impact is negligible at this model's pricing (~$0.0007/1000 tokens even at max).
# A parse failure past this is logged and skipped, not fatal — see maybe_issue_mandate.
MANDATE_TTL_DAYS = 7  # re-evaluated sooner than this anyway if the dossier moves (trigger.py), longer is just a backstop


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def today_llm_spend_usd(conn: sqlite3.Connection, purpose: str = PURPOSE) -> float:
    """Purpose-scoped, not a blanket sum across all llm_calls — mandate
    issuance and strategy narration (scout/strategy.py, added 2026-08-08)
    are different LLM call types with their own separate daily budgets
    (config.yaml's `mandate:` vs `strategy:`); summing them together would
    let one starve the other's budget on a busy day. Defaults to this
    module's own PURPOSE so existing callers/tests are unaffected.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM llm_calls WHERE purpose = ? AND date(called_at) = date('now')",
        (purpose,),
    ).fetchone()
    return row["total"]


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
) -> int:
    cur = conn.execute(
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
    return cur.lastrowid


def _next_version(conn: sqlite3.Connection, address: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM mandates WHERE address = ?", (address,)
    ).fetchone()
    return row["v"] + 1


def _insert_mandate(
    conn: sqlite3.Connection,
    *,
    address: str,
    snapshot_id: int,
    llm_call_id: int,
    output: MandateOutput,
) -> int:
    version = _next_version(conn, address)
    issued_at = _now_iso()
    expires_at = (_now() + timedelta(days=MANDATE_TTL_DAYS)).isoformat()

    cur = conn.execute(
        """
        INSERT INTO mandates (
            address, version, snapshot_id, llm_call_id, verdict, confidence,
            max_position_usd, categories_allowed_json, categories_blocked_json,
            min_entry_price, max_entry_price, min_market_liquidity, reasoning,
            issued_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            address, version, snapshot_id, llm_call_id, output.verdict, output.confidence,
            output.max_position_usd,
            json.dumps(output.categories_allowed) if output.categories_allowed is not None else None,
            json.dumps(output.categories_blocked) if output.categories_blocked is not None else None,
            output.min_entry_price, output.max_entry_price, output.min_market_liquidity, output.reasoning,
            issued_at, expires_at,
        ),
    )
    new_id = cur.lastrowid

    # Supersede whatever was previously active for this trader — at most
    # one non-superseded mandate per address at a time.
    conn.execute(
        "UPDATE mandates SET superseded_by = ? WHERE address = ? AND id != ? AND superseded_by IS NULL",
        (new_id, address, new_id),
    )
    return new_id


def maybe_issue_mandate(
    conn: sqlite3.Connection,
    log: Logger,
    *,
    trader: sqlite3.Row,
    snapshot: sqlite3.Row,
    client: OpenRouterClient,
    daily_budget_usd: float,
) -> str:
    """Returns a short outcome string for logging/testing:
    "skipped:<reason>" | "issued:<verdict>" | "invalid:<error>" | "error:<exc>".
    Never raises — one trader's LLM failure must not kill Scout's run,
    same discipline as a failed dossier fetch.

    `client` is constructed once by the caller for the whole watchlist
    batch (see scout/run.py) — same dependency-injection shape as
    scout/dossier.compute_dossier(client, address, ...), and it's what
    makes this testable with a fake client, no real network call, no
    real spend (see tests/test_issue.py).
    """
    address = trader["address"]
    model = client.model

    should_call, trigger_reason = should_reevaluate(conn, address, snapshot)
    if not should_call:
        log.info("mandate.skipped", address=address, reason=trigger_reason)
        return f"skipped:{trigger_reason}"

    spent_today = today_llm_spend_usd(conn)
    if spent_today >= daily_budget_usd:
        log.warning(
            "mandate.budget_exhausted",
            address=address,
            spent_today_usd=spent_today,
            daily_budget_usd=daily_budget_usd,
        )
        return "skipped:daily budget exhausted"

    prompt = build_prompt(trader, snapshot)
    start = _now()
    try:
        result = client.complete_json(
            prompt,
            json_schema=JSON_SCHEMA,
            schema_name="mandate",
            max_tokens=ISSUE_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 — network/API failure, log and move on
        latency_ms = int((_now() - start).total_seconds() * 1000)
        _insert_llm_call(
            conn, model=model, prompt=prompt, raw_response="", parsed_ok=False,
            parse_error=str(exc), tokens_in=None, tokens_out=None, cost_usd=None, latency_ms=latency_ms,
        )
        log.error("mandate.call_failed", address=address, error=str(exc))
        return f"error:{exc}"

    latency_ms = int((_now() - start).total_seconds() * 1000)
    # Defense in depth on top of sources/openrouter.py's own None -> "" coercion —
    # a NOT NULL insert crashing the whole batch (seen live 2026-08-08) is a much
    # worse failure mode than one trader's mandate correctly logging as unparseable.
    raw_response = result.get("content") or ""

    try:
        output = MandateOutput.model_validate_json(raw_response)
        parse_error = None
    except (ValidationError, ValueError, TypeError) as exc:
        parse_error = str(exc)
        output = None

    llm_call_id = _insert_llm_call(
        conn,
        model=model,
        prompt=prompt,
        raw_response=raw_response,
        parsed_ok=output is not None,
        parse_error=parse_error,
        tokens_in=result["prompt_tokens"],
        tokens_out=result["completion_tokens"],
        cost_usd=result["cost_usd"],
        latency_ms=latency_ms,
    )

    if output is None:
        log.error("mandate.parse_failed", address=address, error=parse_error, finish_reason=result.get("finish_reason"))
        return f"invalid:{parse_error}"

    _insert_mandate(conn, address=address, snapshot_id=snapshot["id"], llm_call_id=llm_call_id, output=output)
    log.info("mandate.issued", address=address, verdict=output.verdict, confidence=output.confidence, cost_usd=result["cost_usd"])
    return f"issued:{output.verdict}"
