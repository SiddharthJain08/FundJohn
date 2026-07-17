"""tests/test_crypto_regime_gating.py — SP-3.1 Phase C per-strategy regime selection."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import engine  # noqa: E402


def _strat(sid):
    s = MagicMock(); s.id = sid
    s.generate_signals.return_value = []
    return s


def test_crypto_strategy_gated_on_crypto_regime():
    equity_regime = {'state': 'LOW_VOL'}
    crypto_regime = {'state': 'CRISIS'}
    seen = {}
    def fake_eligible(sid, regime_state):
        seen[sid] = regime_state
        return True
    def fake_ic(sid, *a, **k):
        return 'crypto' if sid == 'S_btc' else 'equity'
    with patch.object(engine, 'is_eligible', side_effect=fake_eligible), \
         patch.object(engine, 'instrument_class_for', side_effect=fake_ic), \
         patch.object(engine, 'load_crypto_regime_state', return_value=crypto_regime):
        engine.run_strategies([_strat('S_spy'), _strat('S_btc')], {}, equity_regime, [], None)
    assert seen['S_spy'] == 'LOW_VOL'
    assert seen['S_btc'] == 'CRISIS'
