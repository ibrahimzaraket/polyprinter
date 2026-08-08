"""The mandate LLM output — a pydantic model (hard validation, FR-7) plus
a matching JSON Schema dict for OpenRouter's strict structured-output
mode (sources/openrouter.py). Kept as two representations on purpose:
the JSON Schema constrains what the model is even allowed to generate
(model-side enforcement); the pydantic model is the second, independent
check on our side that what came back actually parses and satisfies our
own constraints (e.g. price bounds) — a model can emit schema-valid JSON
that's still semantically wrong (min_entry_price > max_entry_price), and
only the second layer catches that.

Field set mirrors the `mandates` table columns exactly (db/schema.sql) —
this is what becomes one row.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Verdict = Literal["FOLLOW", "SKIP", "WATCH"]
Confidence = Literal["LOW", "MED", "HIGH"]


class MandateOutput(BaseModel):
    verdict: Verdict
    confidence: Confidence
    reasoning: str = Field(min_length=1)

    max_position_usd: float | None = Field(default=None, gt=0)
    categories_allowed: list[str] | None = None
    categories_blocked: list[str] | None = None
    min_entry_price: float | None = Field(default=None, ge=0.0, le=1.0)
    max_entry_price: float | None = Field(default=None, ge=0.0, le=1.0)
    min_market_liquidity: float | None = Field(default=None, ge=0)

    @field_validator("max_entry_price")
    @classmethod
    def _price_band_is_ordered(cls, max_entry_price: float | None, info) -> float | None:
        min_entry_price = info.data.get("min_entry_price")
        if min_entry_price is not None and max_entry_price is not None and max_entry_price < min_entry_price:
            raise ValueError(
                f"max_entry_price ({max_entry_price}) is below min_entry_price ({min_entry_price})"
            )
        return max_entry_price


# JSON Schema for OpenRouter's strict structured-output mode. Every
# property must be listed in "required" under strict mode — optionality
# is expressed as a type union with "null", not by omission (verified
# live against this project's configured model, 2026-08-08).
JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["FOLLOW", "SKIP", "WATCH"]},
        "confidence": {"type": "string", "enum": ["LOW", "MED", "HIGH"]},
        "reasoning": {
            "type": "string",
            "description": "Prose explanation, shown verbatim to the operator. Explain the verdict in terms of the dossier's actual numbers.",
        },
        "max_position_usd": {"type": ["number", "null"], "description": "Per-trade cap in USD, or null for no cap beyond the portfolio/correlation caps Mirror already enforces."},
        "categories_allowed": {"type": ["array", "null"], "items": {"type": "string"}},
        "categories_blocked": {"type": ["array", "null"], "items": {"type": "string"}},
        "min_entry_price": {"type": ["number", "null"], "description": "0-1, a market probability/price floor for entries."},
        "max_entry_price": {"type": ["number", "null"], "description": "0-1, a market probability/price ceiling for entries."},
        "min_market_liquidity": {"type": ["number", "null"]},
    },
    "required": [
        "verdict",
        "confidence",
        "reasoning",
        "max_position_usd",
        "categories_allowed",
        "categories_blocked",
        "min_entry_price",
        "max_entry_price",
        "min_market_liquidity",
    ],
    "additionalProperties": False,
}
