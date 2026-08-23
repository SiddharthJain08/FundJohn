"""S_ast_fed_model re-pointed to macro.parquet series (2026-08-23).

Before: 10Y yield from `^TNX` in prices (frozen since 2026-05-21 — no provider)
and a FIXED 2% risk-free ("FRED DGS3MO unavailable in macro DB"). macro.parquet
now carries DGS10 / DGS3MO (FRED keyless stream), exposed by the engine and
aux_data_loader as aux_data['macro'][<series>] (pd.Series, DatetimeIndex,
percent units). The model prefers those, keeps the old inputs as fallbacks,
and says which source it used in signal_params.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.strategies.implementations.S_ast_fed_model import AstFedModel, _monthly_from_macro


def _daily_index(months: int = 40) -> pd.DatetimeIndex:
    return pd.bdate_range(end='2026-08-21', periods=months * 21)


def _prices(with_tnx: bool) -> pd.DataFrame:
    idx = _daily_index()
    rng = np.random.default_rng(7)
    spy = 300 * np.exp(np.cumsum(rng.normal(0.0003, 0.01, len(idx))))
    shy = 84 + np.cumsum(rng.normal(0, 0.01, len(idx)))
    df = pd.DataFrame({'SPY': spy, 'SHY': shy}, index=idx)
    if with_tnx:
        df['^TNX'] = 40 + np.cumsum(rng.normal(0, 0.05, len(idx)))   # ^TNX quotes yield×10
    return df


def _macro() -> dict:
    idx = _daily_index()
    return {
        'DGS10': pd.Series(np.linspace(3.8, 4.7, len(idx)), index=idx),
        'DGS3MO': pd.Series(np.linspace(5.3, 3.9, len(idx)), index=idx),
        'VIX': pd.Series(18.0, index=idx),
    }


def _signal(prices, aux):
    s = AstFedModel()
    sigs = s.generate_signals(prices, {'state': 'LOW_VOL'}, ['SPY', 'SHY'], aux_data=aux)
    assert len(sigs) == 1, sigs
    return sigs[0]


def test_monthly_from_macro_resamples_month_end_and_aligns():
    idx = pd.bdate_range('2026-01-01', '2026-04-30')
    series = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    target = pd.DatetimeIndex(['2026-01-31', '2026-02-28', '2026-03-31', '2026-04-30', '2026-05-31'])
    out = _monthly_from_macro({'DGS10': series}, 'DGS10', target)
    assert list(out.index) == list(target)
    assert out.iloc[0] == series[:'2026-01-31'].iloc[-1]
    assert out.iloc[-1] == series.iloc[-1]          # May has no data → ffill from April
    assert _monthly_from_macro({}, 'DGS10', target) is None
    assert _monthly_from_macro(None, 'DGS10', target) is None
    assert _monthly_from_macro({'DGS10': pd.Series(dtype=float)}, 'DGS10', target) is None


def test_prefers_macro_dgs10_and_dgs3mo_over_tnx_and_fixed_rf():
    sig = _signal(_prices(with_tnx=True), {'macro': _macro()})
    assert sig.signal_params['bond_yield_source'] == 'macro:DGS10'
    assert sig.signal_params['rf_source'] == 'macro:DGS3MO'
    # last-month values are what the model saw (percent → decimal)
    assert abs(sig.signal_params['bond_yield_current'] - 0.047) < 0.002
    assert abs(sig.signal_params['rf_annual_current'] - 0.039) < 0.002


def test_falls_back_to_tnx_and_fixed_rf_without_macro():
    sig = _signal(_prices(with_tnx=True), {})
    assert sig.signal_params['bond_yield_source'] == 'prices:^TNX'
    assert sig.signal_params['rf_source'] == 'fixed:0.02'


def test_falls_back_to_constants_without_macro_or_tnx():
    sig = _signal(_prices(with_tnx=False), None)
    assert sig.signal_params['bond_yield_source'] == 'fixed:0.035'
    assert sig.signal_params['rf_source'] == 'fixed:0.02'


def test_time_varying_rf_changes_the_forecast():
    """A constant rf and a sloping DGS3MO must not produce the same OLS fit."""
    prices = _prices(with_tnx=True)
    macro = _macro()
    a = _signal(prices, {'macro': macro}).signal_params['y_hat']
    flat = dict(macro); flat['DGS3MO'] = pd.Series(2.0, index=macro['DGS3MO'].index)
    b = _signal(prices, {'macro': flat}).signal_params['y_hat']
    assert a != b
