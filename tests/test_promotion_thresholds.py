import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
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
