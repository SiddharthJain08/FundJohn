import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import json
from strategies.lifecycle import LifecycleStateMachine, StrategyState  # noqa


def _sm(tmp_path, instrument_class):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {'s1': {
        'state': 'candidate', 'state_since': '2026-05-01T00:00:00Z',
        'metadata': {}, 'history': [], 'instrument_class': instrument_class}}}))
    return LifecycleStateMachine.from_manifest(p)


def test_equity_threshold_positive_sharpe(tmp_path):
    # Policy 2026-07-13 v2: sharpe must STRICTLY EXCEED 0.
    sm = _sm(tmp_path, 'equity')
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': 0.0, 'max_drawdown': 0.10})
    assert ok is False
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': -0.2, 'max_drawdown': 0.10})
    assert ok is False
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': 0.01, 'max_drawdown': 0.10})
    assert ok is True


def test_equity_trades_floor(tmp_path):
    # min_trades 100: enforced when the caller supplies 'trades'.
    sm = _sm(tmp_path, 'equity')
    ok, msg = sm.can_transition('s1', StrategyState.LIVE,
                                {'sharpe': 0.6, 'max_drawdown': 0.10, 'trades': 42})
    assert ok is False and 'trades' in msg
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': 0.6, 'max_drawdown': 0.10, 'trades': 100})
    assert ok is True


def test_etp_uses_its_own_row(tmp_path):
    sm = _sm(tmp_path, 'etp')
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': 0.51, 'max_drawdown': 0.19})
    assert ok is True
    ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                              {'sharpe': 0.51, 'max_drawdown': 0.21})
    assert ok is False


# ── R1-assigners (2026-08-25): benchmark-relative leg on the python ─────────
# candidate->live guard. This guard judges caller-supplied AGGREGATE totals
# (no per-regime sleeve, unlike promotion_service.js's judgeRegimeSleeve /
# activation_assigner / eligibility_assigner) via an OPTIONAL
# 'benchmark_sharpe' metadata key -- absent (every pre-existing caller/test
# above) means the leg is skipped and this guard's legacy behavior is
# unchanged, as proven by every test above still passing unmodified.
class TestCandidateToLiveBenchmarkLeg:
    def test_no_benchmark_key_is_a_noop_legacy_stands(self, tmp_path):
        sm = _sm(tmp_path, 'equity')
        ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                                  {'sharpe': 0.6, 'max_drawdown': 0.10})
        assert ok is True

    def test_benchmark_flips_an_otherwise_passing_transition_to_blocked(self, tmp_path):
        sm = _sm(tmp_path, 'equity')
        # Legacy alone passes (0.6 > 0, dd 0.10 <= 0.20); the benchmark leg
        # (0.6 does not exceed 0.9 + 0.0) tightens it shut.
        ok, msg = sm.can_transition('s1', StrategyState.LIVE,
                                    {'sharpe': 0.6, 'max_drawdown': 0.10,
                                     'benchmark_sharpe': 0.9})
        assert ok is False
        assert 'benchmark_sharpe' in msg

    def test_benchmark_leaves_a_clearing_transition_allowed(self, tmp_path):
        sm = _sm(tmp_path, 'equity')
        ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                                  {'sharpe': 1.5, 'max_drawdown': 0.10,
                                   'benchmark_sharpe': 0.9})
        assert ok is True

    def test_benchmark_never_rescues_a_legacy_failure(self, tmp_path):
        sm = _sm(tmp_path, 'equity')
        # sharpe 0.0 fails the legacy "strictly positive" leg outright; a
        # trivially-beaten benchmark must not rescue it.
        ok, msg = sm.can_transition('s1', StrategyState.LIVE,
                                    {'sharpe': 0.0, 'max_drawdown': 0.10,
                                     'benchmark_sharpe': -5.0})
        assert ok is False
        assert 'benchmark_sharpe' not in msg   # blocked on the legacy leg, not this one

    def test_nonfinite_benchmark_skips_and_legacy_stands(self, tmp_path):
        sm = _sm(tmp_path, 'equity')
        ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                                  {'sharpe': 0.01, 'max_drawdown': 0.10,
                                   'benchmark_sharpe': float('nan')})
        assert ok is True

    def test_dual_verdict_log_line_on_flip(self, tmp_path, caplog):
        import logging
        sm = _sm(tmp_path, 'equity')
        with caplog.at_level(logging.INFO):
            ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                                      {'sharpe': 0.6, 'max_drawdown': 0.10,
                                       'benchmark_sharpe': 0.9})
        assert ok is False
        assert any('[bench_gate] s1 aggregate legacy=PASS bench=FAIL '
                   '(sharpe=0.6 bench=0.9)' in r.message for r in caplog.records)

    def test_no_flip_log_line_when_benchmark_also_passes(self, tmp_path, caplog):
        import logging
        sm = _sm(tmp_path, 'equity')
        with caplog.at_level(logging.INFO):
            ok, _ = sm.can_transition('s1', StrategyState.LIVE,
                                      {'sharpe': 1.5, 'max_drawdown': 0.10,
                                       'benchmark_sharpe': 0.9})
        assert ok is True
        assert not any('legacy=PASS bench=FAIL' in r.message for r in caplog.records)
