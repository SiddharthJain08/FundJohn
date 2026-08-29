"""D1 (2026-08-29 spec): SPY regime Sharpe is a SIZING input only. No gate —
promotion, activation, eligibility — may read benchmark_sharpe."""
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest import regime_qualification as rq          # noqa: E402
from strategies import lifecycle as lc                     # noqa: E402


def test_qualifies_regime_has_no_benchmark_kwarg():
    params = inspect.signature(rq.qualifies_regime).parameters
    assert 'benchmark_sharpe' not in params
    assert set(params) == {'sharpe', 'trade_count', 'max_dd_pct', 'instrument_class', 'calmar'}


def test_sleeve_below_market_still_qualifies():
    # 1.2 < SPY LOW_VOL 2.03 — irrelevant to the gate now.
    assert rq.qualifies_regime(1.2, 150, 10.0, 'equity') is True


def test_class_thresholds_have_no_excess_key():
    assert 'min_excess_sharpe_vs_benchmark' not in rq.class_thresholds('equity')


def test_lifecycle_has_no_benchmark_constants():
    for name in ('MIN_EXCESS_SHARPE_VS_BENCHMARK', 'MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS',
                 'min_excess_sharpe_vs_benchmark'):
        assert not hasattr(lc, name), name
    for name in ('benchmark_leg_passes', 'log_bench_gate_skip', 'log_bench_gate_verdict'):
        assert not hasattr(rq, name), name


def test_can_transition_ignores_benchmark_metadata(tmp_path):
    import json
    from strategies.lifecycle import LifecycleStateMachine, StrategyState
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {'s1': {
        'state': 'candidate', 'state_since': '2026-05-01T00:00:00Z',
        'metadata': {}, 'history': [], 'instrument_class': 'equity'}}}))
    sm = LifecycleStateMachine.from_manifest(p)
    ok, msg = sm.can_transition('s1', StrategyState.LIVE,
                                {'sharpe': 0.6, 'max_drawdown': 0.10, 'benchmark_sharpe': 9.0})
    assert ok is True, msg
