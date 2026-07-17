"""S_macro_risk_momentum_ip_beta crashed each cycle: _get_ip_growth treated
aux_data['macro'] as a DataFrame (`.empty`, `.columns`), but the engine builds it as
{series_name: pd.Series(date->value)} (a dict). `AttributeError: 'dict' object has no
attribute 'empty'` → the (live) strategy threw and emitted 0 signals every run.

These pin: (1) dict handling + graceful None when the IP series is absent (the current
production reality — macro.parquet has only VIX*, no INDPRO), (2) correct daily
date-alignment when an INDPRO series IS present, (3) generate_signals no longer crashes
on the real VIX-only macro dict and still produces momentum signals.
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

from strategies.implementations.S_macro_risk_momentum_ip_beta import MacroRiskMomentumIPBeta


def _strat():
    return MacroRiskMomentumIPBeta()


def _price_index(n=600):
    return pd.bdate_range(end='2026-06-24', periods=n)


def test_none_or_missing_macro_returns_none():
    s = _strat()
    idx = _price_index(300)
    assert s._get_ip_growth(None, idx) is None
    assert s._get_ip_growth({}, idx) is None
    assert s._get_ip_growth({'macro': {}}, idx) is None


def test_macro_dict_without_indpro_returns_none_no_crash():
    """Production shape: macro is {'VIX': Series, ...} with no INDPRO → graceful None."""
    s = _strat()
    idx = _price_index(300)
    vix = pd.Series(
        np.linspace(15, 22, 400),
        index=pd.bdate_range(end='2026-06-24', periods=400),
    )
    out = s._get_ip_growth({'macro': {'VIX': vix, 'VVIX': vix * 5}}, idx)
    assert out is None   # no IP series → momentum-only fallback, NOT a crash


def test_indpro_present_returns_aligned_daily_series():
    s = _strat()
    idx = _price_index(500)
    # monthly INDPRO levels over ~30 months, spanning the price window
    months = pd.date_range(end='2026-06-01', periods=30, freq='MS')
    indpro = pd.Series(np.linspace(100, 112, 30), index=months)
    out = s._get_ip_growth({'macro': {'INDPRO': indpro}}, idx)
    assert out is not None
    assert len(out) == len(idx)                     # one value per price date
    assert out.notna().all()                        # forward-filled, no NaN
    assert np.isfinite(out.values).all()
    assert (out.values != 0).any()                  # real IP-growth, not all zeros


def test_generate_signals_does_not_crash_on_real_macro_dict():
    """The exact production failure mode: VIX-only macro dict + a LOW_VOL regime."""
    s = _strat()
    n, ntick = 600, 30
    idx = _price_index(n)
    rng = np.random.default_rng(0)
    cols = [f'T{i}' for i in range(ntick)]
    # geometric-ish random-walk prices so momentum ranking is well-defined
    prices = pd.DataFrame(
        100 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, size=(n, ntick)), axis=0)),
        index=idx, columns=cols,
    )
    vix = pd.Series(np.linspace(15, 20, n), index=idx)
    aux = {'macro': {'VIX': vix}}
    # Must not raise (was AttributeError on macro.empty):
    sigs = s.generate_signals(prices, {'state': 'LOW_VOL'}, cols, aux)
    assert isinstance(sigs, list)
    assert len(sigs) > 0   # momentum-only path still emits long/short signals
