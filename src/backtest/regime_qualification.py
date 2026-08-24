"""regime_qualification.py — the per-regime promotion/activation rule.

Operator policy 2026-07-13 v2: a strategy qualifies IN A REGIME when that
regime's sleeve of its latest primary backtest has
    sharpe STRICTLY > min_sharpe (0.0 — "positive Sharpe"),
    max_dd_pct of the SLEEVE <= the class ceiling (equity/etp 20, option 30,
        crypto 70 — same values as before, now judged per regime),
    trade_count of the SLEEVE >= min_trades (100).

This is the single python source of the rule; the JS twin is
judgeRegimeSleeve in src/lib/promotion_service.js — keep them in sync.
Consumers: backtest.activation_assigner (live sizer eligibility, where the
dashboard's activation min-Sharpe slider adds a further sharpe floor on top)
and backtest.eligibility_assigner (manifest eligible_regimes hint). NOTE
(verified 2026-08-24, R1): both of those actually reimplement the gate
INLINE from class_thresholds()/dd_leg_passes() rather than calling
qualifies_regime() itself — qualifies_regime is the reference rule and unit
tests exercise it directly, but its only *production* import today is
lifecycle.py's docstring cross-reference. The R1 benchmark-relative
criterion added below therefore binds live traffic solely via the JS
promotion path (src/lib/promotion_service.js judgeRegimeSleeve) until the
assigners are threaded to call qualifies_regime() or read benchmark_sharpe
themselves — see task-R1-report.md for the follow-up.
Threshold VALUES come from strategies.lifecycle.PROMOTION_THRESHOLDS
(fractional max_drawdown there → percent here, matching the
strategy_backtest_regimes.max_dd_pct unit), plus (R1, 2026-08-24)
strategies.lifecycle.MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS for the new
excess-Sharpe-over-benchmark leg.
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from strategies.lifecycle import (_promotion_threshold,  # noqa: E402
                                   min_excess_sharpe_vs_benchmark)

logger = logging.getLogger(__name__)


def class_thresholds(instrument_class: Optional[str]) -> dict:
    """Per-class gate thresholds in sleeve units: {min_sharpe, max_dd_pct,
    min_trades, min_calmar, dd_hard_cap_pct, min_excess_sharpe_vs_benchmark}.
    Unknown/None class falls back to equity (same as the JS
    getPromotionThreshold)."""
    thr = _promotion_threshold(instrument_class or 'equity')
    return {
        'min_sharpe': float(thr['min_sharpe']),
        'max_dd_pct': float(thr['max_drawdown']) * 100.0,
        'min_trades': int(thr['min_trades']),
        'min_calmar': float(thr.get('min_calmar', 0.5)),
        'dd_hard_cap_pct': float(thr.get('dd_hard_cap', 0.50)) * 100.0,
        # R1 (2026-08-24): excess-Sharpe-over-benchmark floor. NOT part of
        # strategies.lifecycle.PROMOTION_THRESHOLDS (see that module's
        # MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS header for why) — sourced
        # from its own class-keyed dict instead.
        'min_excess_sharpe_vs_benchmark': float(min_excess_sharpe_vs_benchmark(instrument_class)),
    }


def dd_leg_passes(max_dd_pct, calmar, thr: dict) -> bool:
    """Drawdown leg of the sleeve gate (2026-07-27 Calmar escape hatch).

    Passes when dd <= class ceiling, OR when the sleeve's Calmar clears the
    class floor AND dd stays under the catastrophic hard cap. Max drawdown is
    a running-max extreme — it deepens mechanically with backtest duration and
    breadth, so the flat ceiling alone systematically deactivated long-history
    sleeves (momentum_12_1 LOW_VOL: Sharpe 2.62 / 4,759 trades / DD 26%).
    Calmar (annualized return / max DD) is self-normalizing across horizons.
    Missing calmar → legacy ceiling-only behavior (never a silent pass)."""
    if max_dd_pct is None:
        return False
    dd = float(max_dd_pct)
    if dd <= thr['max_dd_pct']:
        return True
    return (calmar is not None
            and float(calmar) >= thr['min_calmar']
            and dd <= thr['dd_hard_cap_pct'])


def qualifies_regime(sharpe, trade_count, max_dd_pct,
                     instrument_class: Optional[str] = 'equity',
                     calmar=None,
                     benchmark_sharpe=None,
                     sid: str = '?', regime: str = '?') -> bool:
    """True iff one regime sleeve clears all gates. Any missing metric fails
    closed (mirrors judgeRegimeSleeve's no_backtest behavior); a missing
    calmar only forfeits the DD escape hatch, not the whole sleeve.

    R1 (2026-08-24, five-repo-adoptions): benchmark-relative promotion
    criterion. When `benchmark_sharpe` is a finite number (the sleeve's
    persisted strategy_backtest_regimes.benchmark_sharpe — see
    backtest.benchmark_baseline.regime_benchmark_sharpe), ALSO require
    `sharpe > benchmark_sharpe + thr['min_excess_sharpe_vs_benchmark']`.
    None/NaN/inf benchmark_sharpe -> the criterion is SKIPPED (fail-open on
    missing benchmark data) and the legacy rules alone decide; this is
    logged either way (see below).

    Because the benchmark leg is applied ON TOP of (ANDed with) the legacy
    result, it can only ever TIGHTEN the gate, never loosen it — a sleeve
    that already fails the legacy rules cannot be rescued by clearing the
    benchmark leg. The reverse flip (legacy FAIL -> bench PASS) is therefore
    unreachable by construction; see the `bench_pass = legacy_pass and ...`
    line below rather than a runtime assert (an AssertionError inside a
    promotion gate would be worse than this comment).
    """
    thr = class_thresholds(instrument_class)
    if sharpe is None or trade_count is None or max_dd_pct is None:
        return False
    legacy_pass = (float(sharpe) > thr['min_sharpe']
                   and dd_leg_passes(max_dd_pct, calmar, thr)
                   and int(trade_count) >= thr['min_trades'])

    bench_f = None
    if benchmark_sharpe is not None:
        try:
            _cand = float(benchmark_sharpe)
        except (TypeError, ValueError):
            _cand = float('nan')
        if math.isfinite(_cand):
            bench_f = _cand
    if bench_f is None:
        # [bench_gate] no benchmark for <regime>; skipped -- fail-open: NULL
        # (or a non-finite persisted value, e.g. Decimal('NaN')) means the
        # legacy rules alone decide. Logged every evaluation, per the R1
        # "First-Sunday observability" requirement.
        logger.info('[bench_gate] no benchmark for %s; skipped', regime)
        return legacy_pass

    bench_pass = legacy_pass and (float(sharpe) > bench_f + thr['min_excess_sharpe_vs_benchmark'])
    _msg = ('[bench_gate] %s %s legacy=%s bench=%s (sharpe=%s bench=%s)'
            % (sid, regime, 'PASS' if legacy_pass else 'FAIL',
               'PASS' if bench_pass else 'FAIL', sharpe, bench_f))
    if legacy_pass != bench_pass:
        # Benchmark tightened the outcome -- the operator-visible line.
        logger.info(_msg)
    else:
        # Same "log BOTH verdicts wherever it evaluates" line, quieter: no
        # divergence this time, still observable for the first-Sunday audit.
        logger.debug(_msg)
    return bench_pass
