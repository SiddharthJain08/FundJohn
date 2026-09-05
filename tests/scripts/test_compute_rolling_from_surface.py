# tests/scripts/test_compute_rolling_from_surface.py
from __future__ import annotations
import importlib.util
import logging
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


def _mod():
    spec = importlib.util.spec_from_file_location('crof', ROOT / 'scripts' / 'compute_rolling_options_fields.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def test_build_panel_from_surface_rows():
    m = _mod()
    from strategies.aux_data_loader import FIELDS
    idx = pd.bdate_range('2026-05-01', periods=70)
    surf = pd.DataFrame({'ticker': 'ZZZT', 'date': idx.date, 'spot': 100.0, 'iv30': np.linspace(0.2, 0.3, 70),
                         'iv90': 0.28, 'iv_25d_put_30d': 0.27, 'iv_25d_call_30d': 0.22, 'skew_25d_30d': 0.02,
                         'rr_25d_30d': 0.05, 'ts_ratio': 0.9, 'term_slope': 0.03, 'iv_spread': 0.0,
                         'gamma_atm': 0.01, 'theta_atm': -0.02, 'call_volume': 100.0, 'put_volume': 160.0,
                         'volume': 260.0, 'pc_ratio': 1.6, 'expiry_date': '2026-06-19', 'n_expiries_fit': 4,
                         'n_strikes_30d': 20, 'options_features_version': 2, 'built_at': 'x'})
    closes = pd.DataFrame({'ticker': 'ZZZT', 'date': pd.bdate_range('2026-03-01', periods=115).date,
                           'close': 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 115)))})
    panel = m.build_panel(surf, closes)
    for f in FIELDS:
        assert f in panel.columns, f
    last = panel.sort_values('date').iloc[-1]
    assert last['iv_front'] == last['iv30'] and last['skew_20d'] == last['skew_25d_30d']
    assert last['iv_rank'] == 100.0 and last['unusual_flow'] == 1
    assert isinstance(last['iv_rank_history'], list) and len(last['iv_rank_history']) == 20
    assert panel['iv_rank'].isna().sum() == 19            # first 19 rows below IV_RANK_MIN_OBS
    assert 0.0 < last['rv_20'] < 1.0 and abs(last['vrp'] - (last['iv30'] - last['rv_20'])) < 1e-12


def test_build_panel_warns_once_on_a_minimal_surface_frame(caplog):
    """A surface frame that carries only iv30/pc_ratio/options_features_version is
    missing every other SCALAR_KEYS column a real surface master always writes
    (build_options_surface.py::build_rows) — that gap must be visible, not silent,
    but the panel must still carry every aux_data_loader.FIELDS column (defaulted
    to None, never fabricated)."""
    m = _mod()
    from strategies.aux_data_loader import FIELDS
    idx = pd.bdate_range('2026-05-01', periods=25)
    surf = pd.DataFrame({'ticker': 'ZZZT', 'date': idx.date, 'iv30': np.linspace(0.2, 0.3, 25),
                        'pc_ratio': 1.1, 'options_features_version': 2})
    closes = pd.DataFrame({'ticker': 'ZZZT', 'date': pd.bdate_range('2026-03-01', periods=70).date,
                           'close': 100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.01, 70)))})
    with caplog.at_level(logging.WARNING):
        panel = m.build_panel(surf, closes)
    for f in FIELDS:
        assert f in panel.columns, f
    assert panel['spot'].isna().all() and panel['gamma_atm'].isna().all()   # defaulted None, not fabricated
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any('iv90' in msg for msg in warnings), warnings


def test_build_panel_emits_no_missing_column_warning_on_a_full_surface_frame(caplog):
    """The existing full-column fixture (every SCALAR_KEYS column present) must
    not trigger the missing-column warning added above."""
    m = _mod()
    idx = pd.bdate_range('2026-05-01', periods=70)
    surf = pd.DataFrame({'ticker': 'ZZZT', 'date': idx.date, 'spot': 100.0, 'iv30': np.linspace(0.2, 0.3, 70),
                         'iv90': 0.28, 'iv_25d_put_30d': 0.27, 'iv_25d_call_30d': 0.22, 'skew_25d_30d': 0.02,
                         'rr_25d_30d': 0.05, 'ts_ratio': 0.9, 'term_slope': 0.03, 'iv_spread': 0.0,
                         'gamma_atm': 0.01, 'theta_atm': -0.02, 'call_volume': 100.0, 'put_volume': 160.0,
                         'volume': 260.0, 'pc_ratio': 1.6, 'expiry_date': '2026-06-19', 'n_expiries_fit': 4,
                         'n_strikes_30d': 20, 'options_features_version': 2, 'built_at': 'x'})
    closes = pd.DataFrame({'ticker': 'ZZZT', 'date': pd.bdate_range('2026-03-01', periods=115).date,
                           'close': 100 * np.exp(np.cumsum(np.random.default_rng(1).normal(0, 0.01, 115)))})
    with caplog.at_level(logging.WARNING):
        m.build_panel(surf, closes)
    assert not any('lacks' in r.getMessage() for r in caplog.records)
