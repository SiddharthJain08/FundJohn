"""load_prices' universe pushdown must be output-identical to the old full read.

2026-08-06: load_prices used to read ALL rows/tickers of prices.parquet and
pandas-.pivot() them, then discard ~58% of the columns. Measured peak RSS
2608MB for a frame that needs ~160MB, on an 8GB no-swap box where the signals
step had been OOM-killed 5 times in 10 days. It now pushes the universe filter
into the parquet read and builds the wide frame by numpy scatter (2080MB).

The rewrite is only safe if it is EXACTLY equivalent, so that is what this
pins: same shape, columns, index, dtype, NaN placement and values -- plus the
last_parquet_max_date invariant, which must still report the PARQUET's max date
even when the universe filter drops the rows that carried it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from execution.engine import load_prices, _parquet_date_axis


def _write_panel(tmp_path):
    """A panel where the newest date belongs ONLY to an out-of-universe ticker.

    That is the case the metadata fallback exists for: filtering to the
    universe would otherwise silently move last_parquet_max_date backwards and
    mask a failed close-capture from the freshness detector.
    """
    dates = pd.bdate_range('2026-01-01', periods=10).strftime('%Y-%m-%d').tolist()
    rows = []
    for i, d in enumerate(dates):
        for t in ('AAA', 'BBB', 'CCC'):
            rows.append({'ticker': t, 'date': d, 'close': 100.0 + i + hash(t) % 7,
                         'open': 1.0, 'volume': 10})
    # ZZZ (out of universe) is the ONLY ticker trading on the final, latest date.
    latest = '2026-01-20'
    rows.append({'ticker': 'ZZZ', 'date': latest, 'close': 999.0, 'open': 1.0, 'volume': 1})
    p = tmp_path / 'prices.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    return p, latest


def _old_path(path, universe):
    """Verbatim reimplementation of the pre-2026-08-06 read."""
    df = pd.read_parquet(path, columns=['ticker', 'date', 'close'])
    wide = df.pivot(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide.sort_index(inplace=True)
    cols = [c for c in universe if c in wide.columns]
    if cols:
        wide = wide[cols]
    return wide


@pytest.fixture
def panel(tmp_path, monkeypatch):
    p, latest = _write_panel(tmp_path)
    monkeypatch.setattr('execution.engine.ROOT', tmp_path.parent, raising=False)
    master = tmp_path.parent / 'data' / 'master'
    master.mkdir(parents=True, exist_ok=True)
    target = master / 'prices.parquet'
    target.write_bytes(p.read_bytes())
    monkeypatch.delenv('OPENCLAW_CLOSE_PROXY_SNAPSHOT', raising=False)
    return target, latest


def test_pushdown_is_output_identical(panel, monkeypatch):
    target, _ = panel
    universe = ['AAA', 'BBB', 'CCC']
    new = load_prices(universe)
    old = _old_path(target, universe)

    assert list(new.columns) == list(old.columns)
    assert new.index.equals(old.index)
    assert new.shape == old.shape
    assert new.dtypes.unique().tolist() == old.dtypes.unique().tolist()
    nv, ov = new.to_numpy(), old.to_numpy()
    assert np.array_equal(np.isnan(nv), np.isnan(ov)), 'NaN placement diverged'
    assert np.array_equal(nv, ov, equal_nan=True), 'values diverged'


def test_last_parquet_max_date_survives_the_universe_filter(panel):
    """The newest row belongs to ZZZ, which the universe excludes."""
    target, latest = panel
    wide = load_prices(['AAA', 'BBB', 'CCC'])
    assert str(load_prices.last_parquet_max_date) == latest, (
        'last_parquet_max_date must report the PARQUET max, not the filtered '
        "frame's -- otherwise a stale close-capture goes undetected"
    )
    # The date is restored as an all-NaN row, exactly as the old full read had it.
    assert pd.Timestamp(latest) in wide.index
    assert wide.loc[pd.Timestamp(latest)].isna().all()


def test_parquet_date_axis(panel):
    target, latest = panel
    axis = _parquet_date_axis(target)
    assert str(axis.max().date()) == latest
    assert len(axis) == len(set(axis)), 'axis must be deduped'
    assert axis.is_monotonic_increasing
    assert _parquet_date_axis(target, date_col='nope_not_a_column') is None


def test_empty_universe_falls_back_to_unfiltered_read(panel):
    """universe=[] must not silently produce an empty panel."""
    target, _ = panel
    wide = load_prices([])
    assert not wide.empty
    assert {'AAA', 'BBB', 'CCC', 'ZZZ'} <= set(wide.columns)
