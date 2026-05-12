"""Per-strategy regime-eligibility gate.

Called by `engine.run_strategies()` immediately before invoking each
strategy's `compute_signals()`. If the current regime isn't in the
strategy's `eligible_regimes` field in manifest.json, the strategy is
skipped for the day (no signals generated).

Backward compat: a strategy missing `eligible_regimes` (or with a
malformed value) defaults to all-four regimes — the gate returns True.
However, if the runtime regime_state passed in is itself unknown
(e.g., a typo upstream), the gate returns False — this is the safer
default for invalid input.

Spec: docs/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md §9
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ALL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
MANIFEST_PATH = Path(__file__).resolve().parent / 'manifest.json'


def _load_manifest() -> dict:
    try:
        with open(MANIFEST_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        logger.error('regime_gate: manifest unreadable (%s); defaulting all strategies eligible', e)
        return {'strategies': {}}


def is_eligible(strategy_id: str, regime_state: str) -> bool:
    """True if strategy_id should compute signals under current regime."""
    manifest = _load_manifest()
    strategies = manifest.get('strategies', {}) or {}
    record = strategies.get(strategy_id)

    if record is None:
        # unknown strategy — backward compat default (all regimes eligible)
        if regime_state not in ALL_REGIMES:
            logger.warning('regime_gate: unknown regime_state=%r; defaulting eligible', regime_state)
        return True

    eligible = record.get('eligible_regimes')
    if eligible is None:
        # missing field — backward compat default (all regimes eligible)
        if regime_state not in ALL_REGIMES:
            logger.warning('regime_gate: unknown regime_state=%r; defaulting eligible', regime_state)
        return True

    if not isinstance(eligible, list):
        logger.warning('regime_gate: %s has malformed eligible_regimes=%r; defaulting eligible',
                       strategy_id, eligible)
        if regime_state not in ALL_REGIMES:
            logger.warning('regime_gate: unknown regime_state=%r; defaulting eligible', regime_state)
        return True

    # If regime_state is not valid (not in ALL_REGIMES), reject it even if it's in eligible list
    if regime_state not in ALL_REGIMES:
        logger.warning('regime_gate: unknown regime_state=%r; rejecting', regime_state)
        return False

    # Validate each entry in eligible list; warn on typos but don't fail the whole list.
    for r in eligible:
        if r not in ALL_REGIMES:
            logger.warning('regime_gate: %s has invalid regime %r in eligible_regimes', strategy_id, r)

    return regime_state in eligible
