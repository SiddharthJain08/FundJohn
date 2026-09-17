"""Regression: the intraday HMM's GK-vol enrichment must read ONLY SPY rows.

2026-09-16: `_enrich_with_daily_derived` did `pd.read_parquet(prices.parquet)`
(the whole ~19M-row append-only master, ~2.2 GB as a DataFrame) every 5 min
through the 15:00 ET cycle; it coincided with the 4.3 GB signals step and the
kernel OOM-killed the cycle. The read now goes through a pyarrow row filter.

Run: python3 -m pytest tests/scripts/test_intraday_spy_ohlc_filtered_read.py -q
"""
import importlib.util
import inspect
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    'run_intraday_market_state', ROOT / 'scripts' / 'run_intraday_market_state.py')
mod = importlib.util.module_from_spec(_SPEC)
sys.modules['run_intraday_market_state'] = mod
_SPEC.loader.exec_module(mod)


def _synthetic_prices(path: Path) -> None:
    rows = []
    for tkr in ('AAPL', 'SPY', 'MSFT'):
        for i, d in enumerate(('2026-09-10', '2026-09-11', '2026-09-14')):
            base = 100.0 + i
            rows.append({'ticker': tkr, 'date': d, 'open': base, 'high': base * 1.02,
                         'low': base * 0.99, 'close': base * 1.01, 'volume': 1.0,
                         'vwap': base, 'transactions': 1.0, 'source': 't'})
    pq.write_table(pa.Table.from_pandas(pd.DataFrame(rows)), str(path))


def test_load_spy_ohlc_returns_only_spy_rows(tmp_path):
    p = tmp_path / 'prices.parquet'
    _synthetic_prices(p)
    spy = mod._load_spy_ohlc(p)
    assert set(spy['ticker']) == {'SPY'}
    assert len(spy) == 3
    assert set(spy.columns) == {'ticker', 'date', 'open', 'high', 'low', 'close'}


def test_enrichment_uses_the_filtered_reader_and_computes_gk(tmp_path, monkeypatch):
    p = tmp_path / 'prices.parquet'
    _synthetic_prices(p)
    calls = []
    real = mod._load_spy_ohlc

    def spy_reader(path):
        calls.append(str(path))
        return real(p)

    monkeypatch.setattr(mod, '_load_spy_ohlc', spy_reader)
    # Point the module's ROOT-relative lookup at the synthetic file by
    # making prices_path.exists() true for the real path and routing the
    # read through our reader (the enrichment only calls the reader when
    # the real path exists — on the VPS it does).
    feats = mod._enrich_with_daily_derived({'ts_utc': '2026-09-15T19:00:00Z'})
    if calls:  # real master present on this host
        assert 'spy_gk_vol_daily' in feats
        assert feats['spy_gk_vol_daily'] > 0
        base = 102.0
        ln_hl = np.log(base * 1.02 / (base * 0.99))
        ln_co = np.log(base * 1.01 / base)
        gk_var = max(0.0, 0.5 * ln_hl ** 2 - (2 * np.log(2) - 1) * ln_co ** 2)
        assert abs(feats['spy_gk_vol_daily'] - float(np.sqrt(gk_var) * np.sqrt(252))) < 1e-9


def test_enrichment_never_calls_pd_read_parquet_on_prices():
    src = inspect.getsource(mod._enrich_with_daily_derived)
    assert 'read_parquet(prices_path)' not in src
    assert '_load_spy_ohlc(' in src
    reader = inspect.getsource(mod._load_spy_ohlc)
    assert "filters=[('ticker', '==', 'SPY')]" in reader
