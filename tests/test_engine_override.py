import pytest
from dataclasses import dataclass

@dataclass
class _Sig:
    ticker: str = 'AAA'
    direction: str = 'LONG'
    entry_price: float = 100.0
    stop_loss: float = 98.0
    target_1: float = 104.0
    target_2: float = 0.0
    target_3: float = 0.0
    position_size_pct: float = 1.0

def test_apply_overrides_to_signals_gate_on(monkeypatch):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    import importlib
    import execution.engine as eng
    importlib.reload(eng)
    from execution import regime_param_override as rpo
    monkeypatch.setattr(rpo, 'resolve_override',
                        lambda sid, regime, injected=None: {'stop_pct': 0.02, 'target_pct': 0.20})
    sig = _Sig()
    eng._apply_regime_overrides_to_signals('S_demo', [sig], 'LOW_VOL')
    assert sig.stop_loss == pytest.approx(98.0)   # 100*(1-0.02)
    assert sig.target_1 == pytest.approx(120.0)   # 100*(1+0.20)

def test_apply_overrides_noop_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_BACKTEST_COUPLED_RECS', raising=False)
    import importlib, execution.engine as eng
    importlib.reload(eng)
    sig = _Sig()
    eng._apply_regime_overrides_to_signals('S_demo', [sig], 'LOW_VOL')
    assert sig.stop_loss == 98.0 and sig.target_1 == 104.0
