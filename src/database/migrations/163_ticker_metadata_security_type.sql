-- 163: ticker_metadata_snapshots.security_type (universe security type, Phase 1;
-- spec docs/specs/2026-10-04-universe-security-type-spec.md §1, operator-ruled 2026-10-04).
-- ADDITIVE ONLY: one nullable column, no backfill, no rewrite of existing rows.
-- Existing rows keep NULL until the daily ticker_metadata writer next upserts them
-- (from the vendor profile cache: isEtf/isFund/isAdr); consumers read the LATEST
-- non-NULL value per symbol and apply it to every snapshot date.
ALTER TABLE ticker_metadata_snapshots
  ADD COLUMN IF NOT EXISTS security_type TEXT;

COMMENT ON COLUMN ticker_metadata_snapshots.security_type IS
  'etf | fund | adr | stock; NULL = unknown (no/empty vendor profile). Precedence etf > fund > adr > stock. '
  'Static attribute: the latest known (non-NULL) value applies to ALL history, not point-in-time.';
