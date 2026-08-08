import json

import pytest
from pydantic import ValidationError

from polyprinter.mandate.schema import MandateOutput


def _valid_dict(**overrides):
    base = {
        "verdict": "FOLLOW",
        "confidence": "HIGH",
        "reasoning": "Strong hold-to-resolution rate and a large sample size.",
        "max_position_usd": None,
        "categories_allowed": None,
        "categories_blocked": None,
        "min_entry_price": None,
        "max_entry_price": None,
        "min_market_liquidity": None,
    }
    base.update(overrides)
    return base


def test_valid_mandate_parses():
    m = MandateOutput.model_validate(_valid_dict())
    assert m.verdict == "FOLLOW"
    assert m.confidence == "HIGH"


def test_parses_from_raw_json_string():
    raw = json.dumps(_valid_dict(verdict="SKIP", confidence="LOW"))
    m = MandateOutput.model_validate_json(raw)
    assert m.verdict == "SKIP"


def test_rejects_invalid_verdict():
    with pytest.raises(ValidationError):
        MandateOutput.model_validate(_valid_dict(verdict="MAYBE"))


def test_rejects_empty_reasoning():
    with pytest.raises(ValidationError):
        MandateOutput.model_validate(_valid_dict(reasoning=""))


def test_rejects_price_out_of_bounds():
    with pytest.raises(ValidationError):
        MandateOutput.model_validate(_valid_dict(min_entry_price=1.5))


def test_rejects_inverted_price_band():
    with pytest.raises(ValidationError):
        MandateOutput.model_validate(_valid_dict(min_entry_price=0.8, max_entry_price=0.2))


def test_accepts_ordered_price_band():
    m = MandateOutput.model_validate(_valid_dict(min_entry_price=0.2, max_entry_price=0.8))
    assert m.min_entry_price == 0.2
    assert m.max_entry_price == 0.8


def test_rejects_non_positive_max_position():
    with pytest.raises(ValidationError):
        MandateOutput.model_validate(_valid_dict(max_position_usd=0))
