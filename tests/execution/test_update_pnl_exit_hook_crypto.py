"""Crypto engine Phase 3 Task 3 (fix round 1): update_pnl's exit-hook glue
hands a crypto-class strategy the CRYPTO regime (load_crypto_regime_state,
at most once per update_pnl call) and every other class the identical
`regime` object. Fakes only (pattern of test_update_pnl_exit_hook.py)."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from execution import engine  # noqa: E402
from strategies.base import BaseStrategy, CANONICAL_REGIMES  # noqa: E402
from strategies.implementations.S_beta_btc import BetaBtc, HOLD_DAYS  # noqa: E402
from tests.execution.test_update_pnl_exit_hook import ENV, _FakeCursor, _closes  # noqa: E402

RUN_DATE = date(2026, 5, 13)


def _panel():
    idx = pd.date_range('2026-05-04', '2026-05-13', freq='D')
    return pd.DataFrame({'AAPL': 100.0, 'BTC-USD': 90000.0, 'ETH-USD': 3000.0}, index=idx)


def _row(sid, ticker, sig_id, entry, stop, tgt, regime_tag='LOW_VOL'):
    return {'id': sig_id, 'strategy_id': sid, 'ticker': ticker, 'direction': 'LONG',
            'entry_price': entry, 'mark_entry_price': entry, 'target_date': date(2026, 5, 6),
            'lifecycle_state': 'FILLED', 'stop_loss': stop, 'target_1': tgt,
            'signal_date': date(2026, 5, 5),
            'signal_params': {'hold_days': HOLD_DAYS, 'regime': regime_tag}}


def _recorder(sid):
    class R(BaseStrategy):
        id = sid
        active_in_regimes = list(CANONICAL_REGIMES)
        exit_hook = True
        seen = []

        def generate_signals(self, prices, regime, universe, aux_data=None):
            return []

        def should_exit(self, position, prices, regime, aux_data=None):
            R.seen.append(regime)
            return None
    return R()


def _classes(mapping):
    return lambda sid, *a, **k: mapping.get(sid, 'equity')


def test_crypto_hooks_get_crypto_regime_loaded_once_and_equity_gets_same_object():
    crypto_a, crypto_b, eq = _recorder('S_cA'), _recorder('S_cB'), _recorder('S_eq')
    rows = [_row('S_cA', 'BTC-USD', 's1', 90000.0, 50000.0, 500000.0),
            _row('S_cB', 'ETH-USD', 's2', 3000.0, 1000.0, 20000.0),
            _row('S_eq', 'AAPL', 's3', 100.0, 50.0, 500.0)]
    eq_regime = {'state': 'CRISIS', 'vix_level': 40.0}
    crypto_regime = {'state': 'LOW_VOL', 'regime_data': {}}
    calls = []

    def _load():
        calls.append(1)
        return crypto_regime
    with patch.dict(os.environ, ENV), \
         patch.object(engine, 'instrument_class_for', _classes({'S_cA': 'crypto', 'S_cB': 'crypto', 'S_eq': 'etp'})), \
         patch.object(engine, 'load_crypto_regime_state', _load):
        engine.update_pnl(_FakeCursor(rows), _panel(), RUN_DATE,
                          strategies=[crypto_a, crypto_b, eq], regime=eq_regime)
    assert type(crypto_a).seen == [crypto_regime] and type(crypto_b).seen == [crypto_regime]
    assert type(crypto_a).seen[0] is crypto_regime
    assert len(type(eq).seen) == 1 and type(eq).seen[0] is eq_regime
    assert len(calls) == 1


def test_no_crypto_hook_never_loads_crypto_regime():
    eq = _recorder('S_eq')
    eq_regime = {'state': 'LOW_VOL'}

    def _boom():
        raise AssertionError('crypto regime must not be loaded for equity-only hooks')
    with patch.dict(os.environ, ENV), \
         patch.object(engine, 'instrument_class_for', _classes({})), \
         patch.object(engine, 'load_crypto_regime_state', _boom):
        engine.update_pnl(_FakeCursor([_row('S_eq', 'AAPL', 's3', 100.0, 50.0, 500.0)]), _panel(),
                          RUN_DATE, strategies=[eq], regime=eq_regime)
    assert type(eq).seen[0] is eq_regime


def _btc_row(regime_tag='LOW_VOL'):
    return _row('S_beta_btc', 'BTC-USD', 'b1', 90000.0, 54000.0, 450000.0, regime_tag)


def test_real_beta_btc_exits_on_crypto_flip_not_equity_state():
    # real manifest routing: instrument_class_for('S_beta_btc') == 'crypto'
    for crypto_state, want in (('HIGH_VOL', 'strategy_exit:regime_exit'), ('LOW_VOL', None)):
        cur = _FakeCursor([_btc_row('LOW_VOL')])
        with patch.dict(os.environ, ENV), \
             patch.object(engine, 'load_crypto_regime_state',
                          lambda: {'state': crypto_state, 'regime_data': {}}):
            # equity regime == entry tag: only the crypto regime can drive the exit
            n, closed = engine.update_pnl(cur, _panel(), RUN_DATE, strategies=[BetaBtc()],
                                          regime={'state': 'LOW_VOL', 'vix_level': 15.0})
        reasons = [r for r, _ in _closes(cur)]
        assert reasons == [want], (crypto_state, reasons)
        assert closed == (['b1'] if want else [])


def test_missing_crypto_regime_holds():
    cur = _FakeCursor([_btc_row('LOW_VOL')])
    with patch.dict(os.environ, ENV), \
         patch.object(engine, 'load_crypto_regime_state', lambda: {'state': None}):
        n, closed = engine.update_pnl(cur, _panel(), RUN_DATE, strategies=[BetaBtc()],
                                      regime={'state': 'CRISIS', 'vix_level': 40.0})
    assert closed == [] and [r for r, _ in _closes(cur)] == [None]
    assert engine.LAST_EXIT_HOOK_STATS['strategy_exit'] == 0
