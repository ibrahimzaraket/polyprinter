"""Dossier computation against frozen real API responses (captured live
2026-08-07, see docs/api-notes.md) — not synthetic data, so a shape change
in the real API breaks this test before it breaks a live run.
"""

import json
from pathlib import Path

from polyprinter.scout.dossier import compute_dossier

FIXTURES = Path(__file__).parent / "fixtures"
ADDRESS = "0x204f72f35326db932158cba6adff0b9a1da95e14"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


class FakeDataClient:
    """Stands in for PolymarketDataClient: same method signatures, no I/O,
    serves one page of frozen fixture data then an empty page (so the
    dossier's pagination loop terminates naturally).
    """

    def __init__(self):
        self.positions_data = _load("positions_sample.json")
        self.closed_data = _load("closed_positions_sample.json")
        self.activity_data = _load("activity_sample.json")

    def positions(self, user, *, limit=500, offset=0):
        return self.positions_data if offset == 0 else []

    def closed_positions(self, user, *, limit=50, offset=0):
        return self.closed_data if offset == 0 else []

    def activity(self, user, *, limit=500, offset=0, types=None, start=None, end=None):
        return self.activity_data if offset == 0 else []


def test_compute_dossier_runs_end_to_end_against_real_shapes():
    client = FakeDataClient()
    m = compute_dossier(client, ADDRESS)

    assert m.address == ADDRESS
    assert m.open_positions == len(client.positions_data)
    assert m.resolved_positions == len(client.closed_data)
    # every closed position in the fixture has non-null realizedPnl
    assert m.realised_pnl_usd is not None
    assert m.capital_deployed_usd is not None
    assert m.capital_deployed_usd > 0


def test_win_rate_and_ratio_are_internally_consistent():
    client = FakeDataClient()
    m = compute_dossier(client, ADDRESS)

    if m.win_rate is not None:
        assert 0.0 <= m.win_rate <= 1.0
    if m.avg_win_usd is not None:
        assert m.avg_win_usd >= 0
    if m.avg_loss_usd is not None:
        assert m.avg_loss_usd <= 0
    if m.win_loss_ratio is not None:
        assert m.win_loss_ratio >= 0


def test_entry_price_percentiles_are_ordered():
    client = FakeDataClient()
    m = compute_dossier(client, ADDRESS)

    if m.entry_price_p10 is not None:
        assert m.entry_price_p10 <= m.entry_price_median <= m.entry_price_p90


def test_hold_to_resolution_rate_is_a_fraction():
    client = FakeDataClient()
    m = compute_dossier(client, ADDRESS)

    if m.hold_to_resolution_rate is not None:
        assert 0.0 <= m.hold_to_resolution_rate <= 1.0
