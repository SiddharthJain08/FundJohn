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
(verified 2026-08-24, R1): both of those actually reimplement the LEGACY
gate INLINE from class_thresholds()/dd_leg_passes() rather than calling
qualifies_regime() itself — qualifies_regime is the reference rule and unit
tests exercise it directly, but its only *production* import today is
lifecycle.py's docstring cross-reference.

R1-assigners (2026-08-25, follow-up to R1): the benchmark-relative leg is
now threaded into those two inline reimplementations, plus
strategies.lifecycle.LifecycleStateMachine.can_transition's python
candidate->live guard, via the pure `benchmark_leg_passes()` helper below
(+ the `log_bench_gate_skip` / `log_bench_gate_verdict` logging helpers,
kept alongside it so the `[bench_gate] ...` line text has exactly one
source regardless of which consumer prints it). Each consumer ANDs
`benchmark_leg_passes()`'s result onto its own already-computed
legacy_pass — qualifies_regime's inline benchmark block is untouched and
remains its own (equivalent) copy of this same comparison.

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


def benchmark_leg_passes(sharpe, benchmark_sharpe,
                         instrument_class: Optional[str] = None) -> tuple[bool, str]:
    """R1-assigners (2026-08-25): the benchmark-relative leg ALONE, factored
    out of qualifies_regime so the Python gate consumers that reimplement
    their own legacy sleeve gate inline -- backtest.activation_assigner.
    compute_eligible, backtest.eligibility_assigner.compute_eligible, and
    strategies.lifecycle.LifecycleStateMachine.can_transition's python
    candidate->live guard -- can AND this leg onto their own legacy_pass
    without re-deriving the comparison or re-declaring
    MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS as a literal (this is the ONE
    place besides qualifies_regime itself that does the comparison).

    Pure: no DB access, no logging -- callers own their own observability
    (print()-based CLI tools vs. logging.getLogger library code disagree on
    convention, so a shared helper doing I/O would have to pick one). See
    log_bench_gate_skip / log_bench_gate_verdict below for the exact,
    shared log-line text every caller should use.

    Returns (passes, reason) with reason in {'pass', 'fail',
    'skipped_null_benchmark'}. `passes` is True for BOTH 'pass' and
    'skipped_null_benchmark' (fail-open on missing/non-finite benchmark
    data, mirroring qualifies_regime's own contract) -- callers AND this
    onto their own legacy_pass, so a skip never rescues an otherwise-
    failing sleeve; only a 'fail' reason can additionally tighten it.

    Same strict `>` comparison and same class-keyed threshold as
    qualifies_regime's inline version: sharpe > benchmark_sharpe +
    min_excess_sharpe_vs_benchmark(instrument_class).
    """
    bench_f = None
    if benchmark_sharpe is not None:
        try:
            _cand = float(benchmark_sharpe)
        except (TypeError, ValueError):
            _cand = float('nan')
        if math.isfinite(_cand):
            bench_f = _cand
    if bench_f is None:
        return True, 'skipped_null_benchmark'
    if sharpe is None:
        # Defensive only: every caller's own legacy gate already requires a
        # non-None sharpe before it would even consult this leg. Never
        # silently pass/fail a comparison against None.
        return False, 'fail'
    min_excess = min_excess_sharpe_vs_benchmark(instrument_class)
    passes = float(sharpe) > bench_f + min_excess
    return passes, ('pass' if passes else 'fail')


def log_bench_gate_skip(log_fn, sid: str, regime: str) -> None:
    """`[bench_gate] no benchmark for <sid> <regime>; skipped` -- call when
    benchmark_leg_passes() returns reason='skipped_null_benchmark'.
    `log_fn` is the caller's own print/logger callable (str) -> None, so
    the line reaches wherever that caller's other diagnostics go, while the
    `[bench_gate]` text itself stays identical across every consumer (this
    is the single place the text is spelled out)."""
    log_fn(f'[bench_gate] no benchmark for {sid} {regime}; skipped')


def log_bench_gate_verdict(log_fn, sid: str, regime: str, legacy_pass: bool,
                           bench_passes: bool, sharpe, benchmark_sharpe) -> None:
    """`[bench_gate] <sid> <regime> legacy=PASS bench=FAIL (sharpe=<x>
    bench=<y>)` -- call after ANDing bench_passes onto legacy_pass. Logs
    ONLY when the benchmark leg actually flips the verdict: the leg is
    ANDed on top of legacy_pass (final = legacy_pass and bench_passes), so
    it can only ever TIGHTEN the gate -- legacy_pass=False, bench_passes=
    True can never produce a flip here, by construction of that AND, not
    by a runtime assert inside a promotion gate."""
    if legacy_pass and not bench_passes:
        log_fn(f'[bench_gate] {sid} {regime} legacy=PASS bench=FAIL '
               f'(sharpe={sharpe} bench={benchmark_sharpe})')


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
