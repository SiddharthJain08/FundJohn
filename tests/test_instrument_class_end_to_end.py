import sys, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from strategies.lifecycle import LifecycleStateMachine, StrategyState
from strategies.instrument_class import instrument_class_for
from execution.instrument_class_sizer import apply_instrument_class_sizing


def _tmp_manifest(tmp_path, sid, ic):
    p = tmp_path / 'manifest.json'
    p.write_text(json.dumps({'strategies': {sid: {
        'state': 'candidate', 'state_since': '2026-05-01T00:00:00Z',
        'metadata': {}, 'history': [], 'instrument_class': ic}}}))
    return p


def test_etp_full_path(tmp_path):
    p = _tmp_manifest(tmp_path, 'synthetic_etp', 'etp')
    assert instrument_class_for('synthetic_etp', manifest_path=p) == 'etp'
    order = {'ticker': 'GLD', 'notional_usd': 1000.0,
             'contributions': [{'strategy_id': 'synthetic_etp'}]}
    assert apply_instrument_class_sizing(order, 'etp')['notional_usd'] == 1000.0
    sm = LifecycleStateMachine.from_manifest(p)
    ok, _ = sm.can_transition('synthetic_etp', StrategyState.LIVE,
                              {'sharpe': 0.6, 'max_drawdown': 0.15})
    assert ok is True


def test_crypto_class_is_identity_through_dispatcher():
    # SP-3.1 Phase A: crypto is now a notional pass-through (was reserved/raises in SP-3).
    out = apply_instrument_class_sizing({'ticker': 'BTC-USD', 'notional_usd': 1.0}, 'crypto')
    assert out['notional_usd'] == 1.0


def test_synthetic_fixture_emits_signal():
    import pandas as pd
    sys.path.insert(0, str(ROOT / 'tests' / 'fixtures'))
    from synthetic_etp_strategy import SyntheticEtpStrategy
    strat = SyntheticEtpStrategy()

    # Minimal wide-format prices DataFrame: date × ticker closes
    dates = pd.date_range('2026-01-01', periods=30, freq='B')
    prices = pd.DataFrame({'GLD': [185.0 + i * 0.1 for i in range(30)]}, index=dates)

    regime = {'state': 'LOW_VOL'}
    universe = ['GLD']

    signals = strat.generate_signals(prices, regime, universe)
    assert isinstance(signals, list) and len(signals) >= 1
    sig = signals[0]
    assert sig.ticker == 'GLD'
    assert sig.direction == 'LONG'
