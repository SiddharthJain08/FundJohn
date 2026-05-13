-- 088_correlation_adjustments_per_regime.sql
-- Phase 2H: per-regime correlation matrices + state-probability-weighted blend.
-- Extends the 2G `correlation_adjustments` sidecar log with two JSONB columns
-- that record (a) the blend weights actually used per cycle and (b) the
-- coverage classification per regime (real | fallback_global | stress_prior).
--
-- Append-only per CLAUDE.md NEVER-DELETE invariant. ADD COLUMN with NULL
-- default so 2G-era rows remain valid (no row-level migration needed).
--
-- Spec: docs/superpowers/specs/2026-05-13-regime-blended-sizer-phase-2h-design.md

ALTER TABLE correlation_adjustments
    ADD COLUMN IF NOT EXISTS regime_blend_weights JSONB,
    ADD COLUMN IF NOT EXISTS regime_coverage      JSONB;
