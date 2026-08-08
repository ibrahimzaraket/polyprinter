"""The strategy-narrative LLM output — same two-representation pattern as
mandate/schema.py (JSON Schema for OpenRouter's strict mode + a pydantic
model as an independent second check), for the same reason: a model can
emit schema-valid JSON that's still semantically empty (e.g. an empty
string), and only the pydantic layer catches that.

This is NOT a trading verdict — no FOLLOW/SKIP, no caps, nothing Mirror
ever reads. It's purely explanatory: "what is this person actually doing,"
in plain English, for a human reading the dashboard. See scout/strategy.py.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class StrategyOutput(BaseModel):
    headline: str = Field(min_length=1, max_length=140)
    summary: str = Field(min_length=1)


JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {
            "type": "string",
            "description": "One short sentence (under ~140 characters) capturing the trader's style — shown in list views. E.g. 'Fast crypto up/down scalper, flat ~$50 bets, rarely exits early.'",
        },
        "summary": {
            "type": "string",
            "description": "2-4 plain-English sentences explaining their actual pattern: what they trade, how they size, how they hold, and what (if anything) their profit concentration says about how repeatable it looks. No jargon left unexplained, no trading recommendation — just an honest description of what the numbers show.",
        },
    },
    "required": ["headline", "summary"],
    "additionalProperties": False,
}
