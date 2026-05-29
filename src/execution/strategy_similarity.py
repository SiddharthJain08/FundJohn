#!/usr/bin/env python3
"""Per-regime strategy x strategy similarity + clustering for orthogonalization.

The transpose of correlation_matrix.py (which is ticker-keyed). Lead signal =
holdings co-firing Jaccard over (ISO-week, ticker, direction) emissions; blended
with return-correlation under a data-adaptive weight that rises from 0 as joint
history accrues. Reuses correlation_matrix's clip/sparse conventions.

Spec: docs/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations

import os
from typing import Optional

REGIME_STATES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
DEFAULT_WINDOW_DAYS = 90
FOLD_THRESHOLD  = float(os.environ.get('OPENCLAW_FOLD_THRESHOLD', '0.85'))
BLOCK_THRESHOLD = float(os.environ.get('OPENCLAW_BLOCK_THRESHOLD', '0.40'))
RETURN_CORR_ALPHA_CEIL = 0.6     # max weight return-corr ever takes in the blend
ALPHA_FULL_OBS = 60              # overlapping observations at which alpha reaches the ceiling
MAX_OFF_DIAGONAL = 0.95
SPARSE_DEFAULT = 0.05


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    u = len(a | b)
    return (len(a & b) / u) if u else 0.0


def overlap_similarity(sets_by_strat: dict[str, set]) -> dict[str, dict[str, float]]:
    """Pairwise Jaccard over co-firing emission sets. Diagonal 1.0; symmetric."""
    strats = sorted(sets_by_strat.keys())
    out: dict[str, dict[str, float]] = {s: {} for s in strats}
    for i, a in enumerate(strats):
        out[a][a] = 1.0
        for b in strats[i + 1:]:
            j = jaccard(sets_by_strat[a], sets_by_strat[b])
            out[a][b] = j
            out[b][a] = j
    return out
