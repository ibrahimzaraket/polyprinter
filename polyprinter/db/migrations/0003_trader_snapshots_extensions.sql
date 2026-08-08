-- Migration 0003 — windowed P&L + strategy narrative on trader_snapshots.
--
-- realised_pnl_24h_usd / realised_pnl_7d_usd: realized P&L bucketed by
-- each closed position's own resolution timestamp (scout/dossier.py),
-- computed from data Scout already fetches — no extra API calls.
--
-- strategy_summary: an LLM-generated plain-English explanation of what
-- this trader is actually doing (category mix, hold behavior, sizing
-- pattern), NOT a trading verdict — see scout/strategy.py. Generated for
-- every lifetime-profitable trader Scout keeps, not just Mirror's
-- watchlist, so any trader on /traders can be understood, not just the
-- ~20 being tailed. Nullable: only populated when the delta-trigger
-- (same discipline as mandate/trigger.py) decides it's worth an LLM call.
ALTER TABLE trader_snapshots ADD COLUMN realised_pnl_24h_usd REAL;
ALTER TABLE trader_snapshots ADD COLUMN realised_pnl_7d_usd REAL;
ALTER TABLE trader_snapshots ADD COLUMN strategy_summary TEXT;
