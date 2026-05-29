"""Options-flow corroboration of a directional equity signal.

PCR (pc_ratio = put_vol/call_vol) is the PRIMARY gate -- volume-derived and reliable.
IV skew (otm_put_iv - otm_call_iv) is a SOFT SECONDARY that only nudges the score, never the
gate, because this file's vendor IV was flagged low-fidelity in SP-4 (SPY iv30 vs VIX corr 0.375).

Pure / deterministic.
"""
from __future__ import annotations
from typing import Optional, Tuple

PCR_BULL_MAX = 0.85
PCR_BEAR_MIN = 1.05
SKEW_WEIGHT = 0.35


def _pcr_score(direction: str, pcr: float) -> float:
    if direction == 'LONG':
        return max(min((1.0 - pcr) / 0.5, 1.0), -1.0)
    return max(min((pcr - 1.0) / 0.5, 1.0), -1.0)


def _skew_score(direction: str, skew: Optional[float]) -> float:
    if skew is None:
        return 0.0
    s = -skew if direction == 'LONG' else skew
    return max(min(s / 0.05, 1.0), -1.0)


def confirm(direction: str, opts_row: Optional[dict],
            pcr_bull_max: float = PCR_BULL_MAX,
            pcr_bear_min: float = PCR_BEAR_MIN) -> Tuple[bool, float]:
    """Return (passes, score in [-1,1]). False when options data is missing/unusable."""
    if not opts_row:
        return False, 0.0
    pcr = opts_row.get('pc_ratio')
    if pcr is None:
        return False, 0.0
    if direction == 'LONG':
        gate = pcr <= pcr_bull_max
    elif direction == 'SHORT':
        gate = pcr >= pcr_bear_min
    else:
        return False, 0.0
    skew = opts_row.get('skew_20d', opts_row.get('skew'))
    score = (1 - SKEW_WEIGHT) * _pcr_score(direction, pcr) + SKEW_WEIGHT * _skew_score(direction, skew)
    return gate, round(score, 4)
