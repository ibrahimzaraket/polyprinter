-- Migration 0004 — operator-issued mandates, parallel to the LLM's.
--
-- Every mandate so far came from mandate/issue.py (an LLM call). This adds
-- a second, equally real way to authorize a FOLLOW: the operator, via the
-- dashboard's new write routes (mandate/operator.py) — for a wallet
-- they've manually vetted, not one the LLM decided to follow.
--
-- issued_by distinguishes the two ('llm' default preserves every existing
-- row's meaning unchanged). sizing_mode/size_multiplier let an operator
-- mandate size proportionally to the trader's OWN bet-as-%-of-their-
-- balance (mirror/sizing.py's balance_matched_size), not just a flat cap
-- — 'fixed_cap' is the default so every existing LLM mandate's behavior
-- is byte-for-byte unchanged; only a mandate explicitly created with
-- sizing_mode='balance_matched' uses the new math. fast_lane opts a
-- specific tailed wallet into Phase 4's on-chain path actually driving
-- its decisions (mirror/watch_events.py), instead of only logging for
-- comparison — see that module and mirror/fast_lane.py for the guard
-- that keeps this from ever double-executing a trade.
ALTER TABLE mandates ADD COLUMN issued_by TEXT NOT NULL DEFAULT 'llm';
ALTER TABLE mandates ADD COLUMN sizing_mode TEXT NOT NULL DEFAULT 'fixed_cap';
ALTER TABLE mandates ADD COLUMN size_multiplier REAL;
ALTER TABLE mandates ADD COLUMN fast_lane INTEGER NOT NULL DEFAULT 0;
