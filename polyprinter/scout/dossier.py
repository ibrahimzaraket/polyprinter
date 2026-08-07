"""Dossier metric computation (PRD §6.2).

Computes one trader_snapshots row's worth of metrics from data-api
responses. Field-by-field provenance and honest gaps are documented inline
— several §6.2 metrics need data Polymarket's public API doesn't expose
cheaply (or at all); those are left None with a comment rather than
approximated silently.

Pagination is bounded (not exhaustive) to keep one trader's fetch to a
handful of requests: closed-positions up to CLOSED_POSITIONS_MAX_PAGES
pages of 50, activity up to ACTIVITY_MAX_PAGES pages of 500 (500*10=5000
matches the endpoint's own offset cap — beyond that you'd need start/end
windowing, not implemented here). For very high-frequency traders this
undercounts resolved_positions/trades — acceptable for a ranking signal,
not for exact accounting.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from polyprinter.sources.polymarket_data import PolymarketDataClient

CLOSED_POSITIONS_MAX_PAGES = 10  # 10 * 50 = 500 resolved positions
ACTIVITY_MAX_PAGES = 10  # 10 * 500 = 5000, matches /activity's offset cap


@dataclass
class DossierMetrics:
    address: str
    roi_raw: float | None = None
    capital_deployed_usd: float | None = None
    realised_pnl_usd: float | None = None
    resolved_positions: int = 0
    win_rate: float | None = None
    avg_win_usd: float | None = None
    avg_loss_usd: float | None = None
    win_loss_ratio: float | None = None
    concentration_top1: float | None = None
    concentration_top5: float | None = None

    hold_to_resolution_rate: float | None = None
    median_hold_hours: float | None = None
    p90_hold_hours: float | None = None
    entry_price_p10: float | None = None
    entry_price_median: float | None = None
    entry_price_p90: float | None = None
    sizing_cv: float | None = None
    scale_frequency: float | None = None
    median_market_liquidity: float | None = None  # gap: needs a gamma-api
    # lookup per unique market; deferred, would multiply request count.
    category_mix_json: str | None = None  # gap: no category field on
    # data-api position/trade/activity objects (verified) — would need a
    # gamma-api event/tag lookup per market, deferred.

    trades_7d: int = 0
    trades_30d: int = 0
    open_positions: int = 0
    last_trade_at: str | None = None
    active: bool = False


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile. pct in [0, 100]."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (pct / 100.0)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _fetch_all_closed_positions(client: PolymarketDataClient, address: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(CLOSED_POSITIONS_MAX_PAGES):
        batch = client.closed_positions(address, limit=50, offset=page * 50)
        out.extend(batch)
        if len(batch) < 50:
            break
    return out


def _fetch_all_activity(client: PolymarketDataClient, address: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(ACTIVITY_MAX_PAGES):
        batch = client.activity(
            address, limit=500, offset=page * 500, types=["TRADE", "REDEEM", "CONVERSION"]
        )
        out.extend(batch)
        if len(batch) < 500:
            break
    return out


def compute_dossier(
    client: PolymarketDataClient, address: str, *, now: datetime | None = None
) -> DossierMetrics:
    now = now or datetime.now(timezone.utc)

    positions = client.positions(address, limit=500)
    closed = _fetch_all_closed_positions(client, address)
    activity = _fetch_all_activity(client, address)

    m = DossierMetrics(address=address)
    m.open_positions = len(positions)
    m.resolved_positions = len(closed)

    # ─── Performance ───
    realized_pnls = [p["realizedPnl"] for p in closed if p.get("realizedPnl") is not None]
    open_realized = [p["realizedPnl"] for p in positions if p.get("realizedPnl") is not None]
    m.realised_pnl_usd = sum(realized_pnls) + sum(open_realized) if (realized_pnls or open_realized) else None

    capital = [p.get("totalBought") or 0.0 for p in closed] + [p.get("totalBought") or 0.0 for p in positions]
    m.capital_deployed_usd = sum(capital) if capital else None

    if m.capital_deployed_usd and m.realised_pnl_usd is not None:
        m.roi_raw = m.realised_pnl_usd / m.capital_deployed_usd

    wins = [p for p in realized_pnls if p > 0]
    losses = [p for p in realized_pnls if p < 0]
    if realized_pnls:
        m.win_rate = len(wins) / len(realized_pnls)
    if wins:
        m.avg_win_usd = statistics.mean(wins)
    if losses:
        m.avg_loss_usd = statistics.mean(losses)
    if m.avg_win_usd is not None and m.avg_loss_usd:
        m.win_loss_ratio = m.avg_win_usd / abs(m.avg_loss_usd)

    if wins:
        total_positive = sum(wins)
        top_sorted = sorted(wins, reverse=True)
        m.concentration_top1 = top_sorted[0] / total_positive
        m.concentration_top5 = sum(top_sorted[:5]) / total_positive

    # ─── Copyability ───
    redeemed_assets = {a["asset"] for a in activity if a.get("type") == "REDEEM" and a.get("asset")}
    closed_assets = {p["asset"] for p in closed if p.get("asset")}
    if closed_assets:
        m.hold_to_resolution_rate = len(closed_assets & redeemed_assets) / len(closed_assets)

    first_buy_ts: dict[str, int] = {}
    trade_events_by_asset: dict[str, int] = {}
    for a in activity:
        if a.get("type") != "TRADE" or not a.get("asset"):
            continue
        asset = a["asset"]
        trade_events_by_asset[asset] = trade_events_by_asset.get(asset, 0) + 1
        if a.get("side") == "BUY":
            ts = a.get("timestamp")
            if ts is not None and (asset not in first_buy_ts or ts < first_buy_ts[asset]):
                first_buy_ts[asset] = ts

    hold_hours: list[float] = []
    for p in closed:
        asset = p.get("asset")
        close_ts = p.get("timestamp")
        if asset in first_buy_ts and close_ts is not None:
            delta_hours = (close_ts - first_buy_ts[asset]) / 3600.0
            if delta_hours >= 0:
                hold_hours.append(delta_hours)
    if hold_hours:
        m.median_hold_hours = statistics.median(hold_hours)
        m.p90_hold_hours = _percentile(hold_hours, 90)

    entry_prices = [p["avgPrice"] for p in (closed + positions) if p.get("avgPrice") is not None]
    if entry_prices:
        m.entry_price_p10 = _percentile(entry_prices, 10)
        m.entry_price_median = _percentile(entry_prices, 50)
        m.entry_price_p90 = _percentile(entry_prices, 90)

    sizes = [p["totalBought"] for p in (closed + positions) if p.get("totalBought")]
    if len(sizes) > 1 and statistics.mean(sizes) > 0:
        m.sizing_cv = statistics.pstdev(sizes) / statistics.mean(sizes)

    if trade_events_by_asset:
        scaled = sum(1 for count in trade_events_by_asset.values() if count > 1)
        m.scale_frequency = scaled / len(trade_events_by_asset)

    # ─── Liveness ───
    trade_timestamps = [a["timestamp"] for a in activity if a.get("type") == "TRADE" and a.get("timestamp")]
    if trade_timestamps:
        latest = max(trade_timestamps)
        m.last_trade_at = datetime.fromtimestamp(latest, tz=timezone.utc).isoformat()
        now_ts = now.timestamp()
        m.trades_7d = sum(1 for ts in trade_timestamps if now_ts - ts <= 7 * 86400)
        m.trades_30d = sum(1 for ts in trade_timestamps if now_ts - ts <= 30 * 86400)
    m.active = m.trades_30d > 0 or m.open_positions > 0

    return m
