"""Crypto Phase 3 Task 6: the live wrapper's instrument class comes from ANY
contributor — if any contributing strategy is crypto, the order is crypto."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import regime_blended_sizer_live as rbsl  # noqa: E402

_CLASSES = {'S_eq': 'equity', 'S_btc': 'crypto', 'S_opt': 'option'}


def _payload(monkeypatch, order):
    monkeypatch.setenv('OPENCLAW_INSTRUMENT_CLASS_ROUTING', '1')
    monkeypatch.setattr(rbsl, 'instrument_class_for', lambda sid, *a, **k: _CLASSES.get(sid, 'equity'))
    seen = []

    def _apply(o, ic):
        seen.append(ic)
        return o
    monkeypatch.setattr(rbsl, 'apply_instrument_class_sizing', _apply)
    handoff = {'cycle_date': '2026-11-10', 'regime': {'state': 'LOW_VOL'}}
    rbsl._build_sized_payload([order], handoff, equity=100_000.0)
    return seen


def _order(**kw):
    o = {'ticker': 'BTC-USD', 'notional_usd': 1000.0, 'direction': 1,
         'bracket': {'entry_price': 100.0, 'stop_loss': 95.0, 'take_profit_1': 110.0}}
    o.update(kw)
    return o


def test_second_contributor_crypto_makes_order_crypto(monkeypatch):
    seen = _payload(monkeypatch, _order(contributions=[
        {'strategy_id': 'S_eq', 'attribution_weight': 0.6},
        {'strategy_id': 'S_btc', 'attribution_weight': 0.4}]))
    assert seen == ['crypto']


def test_all_equity_contributors_stay_equity(monkeypatch):
    seen = _payload(monkeypatch, _order(contributions=[
        {'strategy_id': 'S_eq'}, {'strategy_id': 'S_eq2'}]))
    assert seen == []


def test_first_contributor_class_kept_when_no_crypto(monkeypatch):
    seen = _payload(monkeypatch, _order(contributions=[
        {'strategy_id': 'S_opt'}, {'strategy_id': 'S_eq'}]))
    assert seen == ['option']


def test_composite_top_level_sid_with_crypto_part(monkeypatch):
    seen = _payload(monkeypatch, _order(strategy_id='S_btc|S_eq'))
    assert seen == ['crypto']
