"""Guards the pyarrow-dictionary read in unified_backtest.load_prices_panels
against the pre-2026-07-16 pandas read: byte-identical close_wide + bars_by_ticker.

The rewrite (read ticker+date as dictionaries, cast OHLC to float32 in Arrow) cut
the read peak 2.05GB → 0.99GB on the 18.7M-row master — measured live on the fleet
re-backtest, which was OOM-killing heavy cross-sectionals inside this read. This
test proves the rewrite changed no VALUE, exercising the traps that make it
non-trivial:
  - multi-row-group dictionary unification (row_group_size forces >1 group),
  - a categorical ticker sort reorders columns vs the lexicographic object sort,
  - a fully-quarantined ticker must not leave a phantom all-NaN pivot column
    (filter runs before the category normalisation).
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import unified_backtest as ub  # noqa: E402
import src.pipeline.quarantine_filter as qf  # noqa: E402

_COLS = ['ticker', 'date', 'open', 'high', 'low', 'close']


def _write_synthetic(path, *, row_group_size=7):
    # first-appearance order MSFT,AAPL,BRK-B,AA,ZTS != sorted; rows shuffled;
    # dash share-class; small row_group_size forces multiple row groups.
    rows = []
    rng = np.random.default_rng(0)
    for t in ['MSFT', 'AAPL', 'BRK-B', 'AA', 'ZTS']:
        for d in ['2020-01-02', '2020-01-03', '2020-01-06', '2020-01-07']:
            base = 100.0 + rng.random() * 50
            rows.append((t, d, base, base + 1.5, base - 1.2, base + 0.3))
    rng.shuffle(rows)
    df = pd.DataFrame(rows, columns=_COLS)
    tbl = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(tbl, str(path), row_group_size=row_group_size,
                   use_dictionary=['ticker', 'date'])


def _old_reference(path):
    """The pre-rewrite pandas read (object ticker/date → float32 → categorical)."""
    p = pd.read_parquet(str(path), columns=_COLS)
    for c in ('open', 'high', 'low', 'close'):
        p[c] = p[c].astype('float32')
    p['ticker'] = p['ticker'].astype('category')
    p['date'] = pd.to_datetime(p['date'])
    p = p.sort_values(['ticker', 'date'])
    cw = p.pivot(index='date', columns='ticker', values='close')
    cw.columns = pd.Index(cw.columns.astype(str), name='ticker')
    cw.index.name = 'date'
    bars = {str(t): g.set_index('date')[['open', 'high', 'low', 'close']]
            for t, g in p.groupby('ticker', observed=True)}
    return cw, bars


def test_arrow_read_matches_old_pandas_read(tmp_path, monkeypatch):
    path = tmp_path / 'prices.parquet'
    _write_synthetic(path)
    assert pq.ParquetFile(str(path)).metadata.num_row_groups > 1, \
        "test must exercise multi-row-group dictionary unification"

    # Neutralise the quarantine filter (avoid a PG dependency + a real prices.parquet
    # quarantine row coincidentally matching a synthetic (ticker, date)); the empty
    # set drives filter_quarantined's allocation-free hot path.
    monkeypatch.setattr(qf, '_cached', lambda master_table: set())
    monkeypatch.setattr(ub, 'PRICES_PARQUET', path)

    cw_new, bars_new = ub.load_prices_panels()          # the PRODUCTION function
    cw_old, bars_old = _old_reference(path)

    # column ORDER sorted despite non-sorted first-appearance; float32 (memory win).
    assert list(cw_new.columns) == ['AA', 'AAPL', 'BRK-B', 'MSFT', 'ZTS']
    assert cw_new.values.dtype == np.float32
    # BIT-EXACT, not allclose: the bracket hit-tests (high>=target, low<=stop) are
    # exact comparisons, so a 1-ULP drift between pandas.astype(float32) and
    # pyarrow.cast(float32) could flip a trade and split the fleet's methodology.
    # DataFrame.equals is NaN-aware and value-exact (== catches 1-ULP), and also
    # enforces identical index + column order + dtype. (pandas vs pyarrow float32
    # confirmed bit-identical on a real 50-ticker prices.parquet slice, 2026-07-16.)
    assert cw_new.equals(cw_old), "close_wide not bit-identical to the old pandas read"

    assert set(bars_new) == set(bars_old)
    for k in bars_old:
        assert bars_new[k].equals(bars_old[k]), f"bars[{k}] not bit-identical"


def test_fully_quarantined_ticker_leaves_no_phantom_column(tmp_path, monkeypatch):
    """If quarantine removes EVERY row of a ticker, it must vanish from close_wide
    (no phantom all-NaN column) — the reason the category normalisation drops
    unused categories after filtering."""
    path = tmp_path / 'prices.parquet'
    _write_synthetic(path)
    # Quarantine every ZTS row.
    zts_dates = {('ZTS', d) for d in ['2020-01-02', '2020-01-03', '2020-01-06', '2020-01-07']}
    monkeypatch.setattr(qf, '_cached', lambda master_table: zts_dates)
    monkeypatch.setattr(ub, 'PRICES_PARQUET', path)

    cw, bars = ub.load_prices_panels()
    assert 'ZTS' not in cw.columns, "fully-quarantined ticker left a phantom column"
    assert 'ZTS' not in bars
    assert list(cw.columns) == ['AA', 'AAPL', 'BRK-B', 'MSFT']
