import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import regime_blended_sizer_live as rbsl  # noqa


def _orders_handoff():
    order = {'ticker': 'SPY', 'notional_usd': 1000.0, 'direction': 1, 'delta': 0.5,
             'bracket': {'entry_price': 100.0, 'stop_loss': 95.0, 'take_profit_1': 110.0},
             'contributions': [{'strategy_id': 'opt_strat', 'attribution_weight': 1.0}]}
    handoff = {'cycle_date': '2026-05-26', 'regime': {'state': 'LOW_VOL'}}
    return [order], handoff


def test_gate_off_leaves_option_notional_unchanged(monkeypatch):
    monkeypatch.delenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', raising=False)
    monkeypatch.setattr(rbsl, 'instrument_class_for', lambda *a, **k: 'option')
    orders, handoff = _orders_handoff()
    out = rbsl._build_sized_payload(orders, handoff, equity=100_000.0)
    assert out['orders'][0]['notional_usd'] == 1000.0  # equity path, unscaled


def test_gate_on_scales_option_notional(monkeypatch):
    monkeypatch.setenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', '1')
    monkeypatch.setattr(rbsl, 'instrument_class_for', lambda *a, **k: 'option')
    orders, handoff = _orders_handoff()
    out = rbsl._build_sized_payload(orders, handoff, equity=100_000.0)
    assert out['orders'][0]['notional_usd'] == 500.0  # scaled by |delta|=0.5
