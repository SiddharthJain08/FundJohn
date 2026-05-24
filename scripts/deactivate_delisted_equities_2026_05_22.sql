-- SP-1 Task 15a operator data change (2026-05-22) — applied via
--   psql $POSTGRES_URI -f scripts/deactivate_delisted_equities_2026_05_22.sql
--
-- Audit found 16 active equities in universe_config returning 0 bars across
-- the most recent 30 days on Alpaca's stocks endpoint. Each corresponds to a
-- merger / acquisition / delisting that completed before 2026-05-22 — Alpaca
-- correctly stopped serving them, but our universe_config never reflected the
-- state change. Without this flip, every cycle wastes 16 API calls + emits 16
-- `bars: null` warnings.
--
-- BK (BNY Mellon) was on the same one-day-probe shortlist but a wider 30-day
-- probe found 21 fresh bars — single-day fluke, NOT delisted. Kept active.
--
-- Per CLAUDE.md core invariant, deactivation is `active = false` on a metadata
-- row (never a DELETE). Historical parquet rows for these tickers remain.
--
-- Notes on each:
--   PXD  Pioneer Natural Resources — acquired by ExxonMobil (closed 2024-05)
--   WRK  WestRock — merged with Smurfit Kappa into Smurfit Westrock (SW, 2024-07)
--   MRO  Marathon Oil — acquired by ConocoPhillips (closed 2024-11)
--   JNPR Juniper Networks — acquired by HPE (closed 2025-07)
--   ANSS Ansys — acquired by Synopsys (closed 2025-07)
--   HES  Hess Corp — acquired by Chevron (closed 2025-07)
--   PARA Paramount Global — merged with Skydance Media → PSKY (2025-08)
--   IPG  Interpublic Group — acquired by Omnicom (closed 2025-11)
--   K    Kellanova — acquired by Mars Inc (closed 2025-12)
--   MMC  Marsh McLennan stale Jan 2026 — confirm with operator before
--        un-flipping; 0 bars in 30 days but corporate website still active.
--   CMA  Comerica — recent stale, 0 bars in 30 days
--   BLL  Ball Corporation — last bar 2026-04-23
--   PKI  PerkinElmer — became Revvity (RVTY) prior to this date
--   HOLX Hologic — recent stale
--   SEE  Sealed Air — recent stale
--   CTRA Coterra Energy — last bar 2026-05-07

BEGIN;

UPDATE universe_config
   SET active = false
 WHERE ticker IN (
   'PXD','WRK','MRO','JNPR','ANSS','HES','PARA','IPG','K',
   'MMC','CMA','BLL','PKI','HOLX','SEE','CTRA'
 )
   AND active = true;

COMMIT;
