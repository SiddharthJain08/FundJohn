-- Migration 116: Fix market_news constraint drift
--
-- Migration 009 declared `uuid TEXT UNIQUE NOT NULL` but the live database
-- has the UNIQUE constraint on `primary_ticker` instead (cause unknown — pre-
-- 2026-04 schema drift or operator-applied DDL not in git). The primary_ticker
-- constraint silently rejected most INSERTs after the first per-ticker article,
-- which is why the table grew to only 354 rows over ~6 months.
--
-- This migration deduplicates by uuid (keeping the earliest-inserted copy),
-- drops the bogus primary_ticker uniqueness, and adds the correct uuid
-- uniqueness so SP-1 Task 15a's alpaca_news.py dual-write can ON CONFLICT (uuid)
-- DO NOTHING idempotently.
--
-- Note: market_news is NOT under the append-only master-data invariant
-- (CLAUDE.md core invariant lists execution_signals, signal_pnl,
-- alpaca_submissions, data_coverage, data_columns — market_news is omitted
-- and already had a 30-day prune in runNewsCollection). Dedup of duplicate
-- uuid rows is therefore allowed.

BEGIN;

-- Step 1: Dedupe by uuid, keeping the row with the lowest id (earliest insert).
DELETE FROM market_news a
USING market_news b
WHERE a.uuid = b.uuid AND a.id > b.id;

-- Step 2: Drop the misnamed/misplaced UNIQUE constraint on primary_ticker.
-- IF EXISTS so this migration is idempotent on a freshly-built schema where
-- the constraint was never created.
ALTER TABLE market_news DROP CONSTRAINT IF EXISTS market_news_primary_ticker_key;

-- Step 3: Add the intended UNIQUE constraint on uuid.
ALTER TABLE market_news ADD CONSTRAINT market_news_uuid_key UNIQUE (uuid);

COMMIT;
