"""Per-strategy regime-eligibility gate.

Called by `engine.run_strategies()` immediately before invoking each
strategy's `compute_signals()`. Source of truth: the
`strategy_regime_params` table via `execution.regime_param_resolver`.

Backward compat: if the resolver returns no row for a (strategy, regime)
pair — typically a freshly-added strategy not yet seeded — the gate
returns True ("eligible everywhere"). This matches Phase 1 manifest
semantics where a strategy without an `eligible_regimes` field was
treated as eligible in every regime.

Failure semantics: if the resolver raises (DB unavailable), the gate
fails-open (returns True). Skipping a strategy because of an infrastructure
hiccup would be worse than running it; the doctor check
`strategy_regime_params_consistency` + cycle preflight surfaces the
underlying problem.

Spec: docs/archive/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2a-design.md
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure src/ is importable when this module is run/imported from various
# entry points.
_THIS_FILE = Path(__file__).resolve()
_SRC = _THIS_FILE.parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from execution.regime_param_resolver import is_eligible as _resolver_is_eligible  # noqa: E402

logger = logging.getLogger(__name__)

ALL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')


def is_eligible(strategy_id: str, regime_state: str) -> bool:
    """True if strategy_id should compute signals under current regime."""
    if regime_state not in ALL_REGIMES:
        logger.warning('regime_gate: unknown regime_state=%r; rejecting',
                       regime_state)
        return False
    try:
        return _resolver_is_eligible(strategy_id, regime_state)
    except Exception as exc:
        logger.error('regime_gate: resolver failed (%s); failing OPEN', exc)
        return True
