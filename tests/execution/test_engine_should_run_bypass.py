"""ERR-20260721-002: the DB gate (strategy_regime_params via is_eligible) is the
SOLE live eligibility authority. The strategy's own should_run(active_in_regimes)
must NOT veto a regime the DB approved — otherwise a strategy the backtest
qualified in CRISIS (but whose declared active_in_regimes omits CRISIS) is
silently dead live. run_strategies mirrors the backtest's discovery-mode override
and widens the instance's active_in_regimes for the eligible regime.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402


class _RegimeGatedStrat:
    """Duck-types a real strategy: generate_signals early-returns [] when
    should_run(regime) is False — exactly the pattern in 165 of 177 strategies."""
    id = 'S_fake_regime_gated'

    def __init__(self, active):
        self.active_in_regimes = list(active)
        self.calls = []

    def should_run(self, regime_state):
        return regime_state in (self.active_in_regimes or [])

    def generate_signals(self, prices, regime, universe, aux):
        self.calls.append(regime.get('state'))
        if not self.should_run(regime.get('state')):
            return []           # the silent-dead early-return
        return ['SIG']


def _panel():
    idx = pd.to_datetime(['2024-01-05', '2024-01-08'])
    return pd.DataFrame({'AAPL': [100.0, 101.0]}, index=idx)


def _wire(monkeypatch, eligible):
    monkeypatch.delenv('OPENCLAW_EQUITY_TRADING_CALENDAR', raising=False)
    monkeypatch.setattr(engine, 'instrument_class_for', lambda sid: 'equity')
    monkeypatch.setattr(engine, '_apply_regime_overrides_to_signals', lambda *a, **k: None)
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: eligible)


def test_db_eligible_bypasses_should_run(monkeypatch):
    # DB says eligible in LOW_VOL; strategy's active_in_regimes OMITS LOW_VOL.
    # Pre-fix: should_run vetoes -> []. Post-fix: engine widens -> it signals.
    _wire(monkeypatch, eligible=True)
    s = _RegimeGatedStrat(active=['CRISIS'])
    out = engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['AAPL'], {})
    assert out['S_fake_regime_gated'] == ['SIG']          # ran and signaled
    assert 'LOW_VOL' in s.active_in_regimes               # instance widened by engine


def test_db_ineligible_still_skipped(monkeypatch):
    # DB gate remains authoritative the other way: ineligible -> never called,
    # even though the strategy's own active_in_regimes would allow it.
    _wire(monkeypatch, eligible=False)
    s = _RegimeGatedStrat(active=['LOW_VOL'])
    out = engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['AAPL'], {})
    assert 'S_fake_regime_gated' not in out
    assert s.calls == []                                  # generate_signals never invoked


def test_no_widen_when_already_active(monkeypatch):
    # Already active in the regime -> no mutation (byte-identical legacy behavior).
    _wire(monkeypatch, eligible=True)
    s = _RegimeGatedStrat(active=['LOW_VOL', 'CRISIS'])
    before = list(s.active_in_regimes)
    out = engine.run_strategies([s], _panel(), {'state': 'LOW_VOL'}, ['AAPL'], {})
    assert out['S_fake_regime_gated'] == ['SIG']
    assert s.active_in_regimes == before
