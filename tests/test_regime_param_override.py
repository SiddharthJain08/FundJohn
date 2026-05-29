import os
import pytest
from execution import regime_param_override as rpo

def test_apply_override_long_replaces_bracket():
    stop, target = rpo.apply_override(
        entry_price=100.0, direction=1, stop_loss=98.0, target_1=104.0,
        override={'stop_pct': 0.05, 'target_pct': 0.10})
    assert stop == pytest.approx(95.0)
    assert target == pytest.approx(110.0)

def test_apply_override_short_replaces_bracket():
    stop, target = rpo.apply_override(
        entry_price=100.0, direction=-1, stop_loss=102.0, target_1=96.0,
        override={'stop_pct': 0.05, 'target_pct': 0.10})
    assert stop == pytest.approx(105.0)
    assert target == pytest.approx(90.0)

def test_apply_override_none_passthrough():
    stop, target = rpo.apply_override(
        entry_price=100.0, direction=1, stop_loss=98.0, target_1=104.0, override=None)
    assert stop == 98.0 and target == 104.0

def test_apply_override_partial_keeps_other_leg():
    stop, target = rpo.apply_override(
        entry_price=100.0, direction=1, stop_loss=98.0, target_1=104.0,
        override={'stop_pct': 0.05})
    assert stop == pytest.approx(95.0)
    assert target == 104.0

def test_resolve_override_gate_off_returns_none(monkeypatch):
    monkeypatch.delenv('OPENCLAW_BACKTEST_COUPLED_RECS', raising=False)
    assert rpo.resolve_override('S_x', 'LOW_VOL', injected=None) is None

def test_resolve_override_injected_map_used(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    inj = {'LOW_VOL': {'stop_pct': 0.06, 'target_pct': 0.09}}
    assert rpo.resolve_override('S_x', 'LOW_VOL', injected=inj) == {'stop_pct': 0.06, 'target_pct': 0.09}
    assert rpo.resolve_override('S_x', 'HIGH_VOL', injected=inj) is None


@pytest.mark.skipif(not os.environ.get('POSTGRES_URI'), reason='needs DB + prices')
def test_injected_override_changes_backtest(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    from backtest import unified_backtest as ub
    import json, pathlib
    man = json.loads(pathlib.Path('src/strategies/manifest.json').read_text())
    strats = man.get('strategies', {})
    live = [sid for sid, e in strats.items() if e.get('state') == 'live'] if isinstance(strats, dict) \
           else [s['id'] for s in strats if s.get('state') == 'live']
    if not live:
        pytest.skip('no live strategies')
    sid = live[0]
    _b, base_m = ub.run_backtest(sid, commit=False, return_metrics=True)
    _t, tight_m = ub.run_backtest(sid, commit=False, return_metrics=True, param_override={
        r: {'stop_pct': 0.01, 'target_pct': 0.50} for r in
        ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')})
    assert 'sharpe' in base_m and 'sharpe' in tight_m
    assert (base_m['sharpe'] != tight_m['sharpe']) or (base_m['total_trades'] != tight_m['total_trades'])
