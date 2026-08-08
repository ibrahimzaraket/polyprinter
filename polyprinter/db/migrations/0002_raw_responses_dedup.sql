-- Migration 0002 — dedup key for raw_responses.
--
-- Scout has no incremental cursor (unlike Mirror's watch_poll.py), so a
-- re-run refetches each trader's full activity history from scratch. Every
-- refetch of an unchanged page was being stored as a brand new row —
-- 14,671 exact url+body duplicates, ~2.4GB, found live 2026-08-08.
--
-- Adds body_hash (sha256 of body) and an index on it so raw_store.py can
-- check for an existing identical row before inserting. This file only
-- needs to run against databases that already had raw_responses without
-- body_hash — a fresh install gets the column directly from schema.sql
-- (migration 1) and skips this one.
ALTER TABLE raw_responses ADD COLUMN body_hash TEXT;
CREATE INDEX IF NOT EXISTS idx_raw_responses_dedup ON raw_responses(source, url, body_hash);
