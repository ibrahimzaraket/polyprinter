-- Migration 0005 — market -> category cache (workstream §5, category-level
-- Copy Score).
--
-- scout/dossier.py's category_mix_json gap turned out to be real but
-- narrower than originally documented: data-api's position/trade/activity
-- objects still carry no category field (re-verified live 2026-08-08), but
-- gamma-api's per-EVENT `tags` array does (not the market object, and not
-- the market response's own nested copy of its event — see
-- sources/polymarket_gamma.py's module docstring for the real verified
-- shapes). Getting a category means one gamma-api call per distinct event.
--
-- This table exists so that call happens once per distinct market_id ever
-- seen across every trader Scout looks at, not once per (trader, market)
-- pair — a popular market (e.g. a recurring 15-minute crypto updown
-- market) can appear in dozens of traders' closed-positions history, and
-- without this cache each of those would re-fetch the identical category.
--
-- category NULL (row present, value NULL) is a deliberate negative cache:
-- gamma-api's /events/keyset didn't return anything for that market's
-- event slug (renamed/removed event) — recorded so scout/category_score.py
-- doesn't re-query the same dead lookup every run. That's different from
-- "no row at all", which means "never looked up yet".
--
-- Added via a numbered migration file, not schema.sql directly, for the
-- same reason 0002/0003/0004 were: schema.sql is only ever replayed as
-- migration 1 (db/migrate.py), so a table added there would try to
-- CREATE TABLE a second time (harmlessly, since migrate.py rewrites
-- schema.sql's statements with IF NOT EXISTS at replay time) but would
-- misrepresent this table as having existed since the very first schema
-- version, which it didn't — numbered migrations are the actual, honest
-- record of when each piece of schema was added.
CREATE TABLE IF NOT EXISTS market_categories (
    market_id   TEXT PRIMARY KEY,   -- conditionId — same identifier space as
                                     -- observed_trades.market_id and
                                     -- data-api positions'/closed-positions'
                                     -- own conditionId field (verified live,
                                     -- see mirror/watch_poll.py, mirror/
                                     -- watch_events.py's _resolve_market_id)
    category    TEXT,               -- NULL = looked up, gamma-api had no
                                     -- event tags to categorize with (see
                                     -- above); a real value is one of
                                     -- category_score.py's KNOWN_CATEGORY_TAGS
                                     -- labels, or 'Other' if the event had
                                     -- tags but none matched a known
                                     -- top-level category
    fetched_at  TEXT NOT NULL
);
