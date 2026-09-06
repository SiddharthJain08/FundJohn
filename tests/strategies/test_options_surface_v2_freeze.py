"""Every v2 SCALAR_KEYS value on the checked-in chain fixture is reproduced
bit-for-bit by the current module. Spec 2026-09-06 §A.3: v3 adds keys, it
never moves a v2 value. Regenerate the JSON only from a v2 checkout, never
from the module under test."""
from __future__ import annotations
import importlib.util, json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def _build_rows():
    spec = importlib.util.spec_from_file_location('bos', ROOT / 'scripts' / 'build_options_surface.py')
    bos = importlib.util.module_from_spec(spec); spec.loader.exec_module(bos)
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    day = pd.Timestamp('2026-09-03')
    return bos.build_rows(chain.assign(date=pd.to_datetime(chain['date'])),
                          {(t, day): s for t, s in meta['spots'].items()})


def _is_null(v):
    try:
        return v is None or bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def test_current_module_reproduces_frozen_v2_values():
    expected = json.load(open(FIX / 'options_surface_v2_expected.json'))['rows']
    built = _build_rows()
    for ticker, keys in expected.items():
        row = built[built.ticker == ticker].iloc[0]
        for k, want in keys.items():
            got = row[k]
            if want is None:
                assert _is_null(got), (ticker, k, got)
            elif isinstance(want, str):
                assert got == want, (ticker, k, got, want)
            else:
                assert abs(float(got) - float(want)) <= 1e-12 * max(1.0, abs(float(want))), (ticker, k, got, want)


def test_snapshot_covers_every_v2_scalar_key():
    from strategies.options_surface import SCALAR_KEYS
    expected = json.load(open(FIX / 'options_surface_v2_expected.json'))['rows']
    v2_keys = {'spot', 'iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d',
               'ts_ratio', 'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm',
               'call_volume', 'put_volume', 'volume', 'pc_ratio', 'expiry_date',
               'n_expiries_fit', 'n_strikes_30d'}
    assert v2_keys <= set(SCALAR_KEYS)
    for ticker in ('SPY', 'AAPL', 'XOM'):
        assert set(expected[ticker]) == v2_keys, ticker
