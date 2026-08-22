import pandas as pd
from ingestion import ingest_prices_30m_alpaca as ing
from ingestion.ingest_prices_30m_alpaca import _bars_to_df, _merge_append, COLUMNS

def _bar(t, vw=100.0, h=101.0, l=99.0, o=100.0, c=100.5, v=1000, n=50):
    return {'t': t, 'vw': vw, 'h': h, 'l': l, 'o': o, 'c': c, 'v': v, 'n': n}

def test_bars_to_df_maps_schema_and_filters_rth():
    bars = [
        _bar('2026-05-28T08:00:00Z'),   # 04:00 ET pre-market → dropped
        _bar('2026-05-28T13:30:00Z'),   # 09:30 ET → kept (bucket 0)
        _bar('2026-05-28T19:30:00Z'),   # 15:30 ET → kept (last RTH bucket)
        _bar('2026-05-28T20:00:00Z'),   # 16:00 ET → dropped (post-close)
    ]
    df = _bars_to_df(bars, 'ORCL')
    assert list(df.columns) == COLUMNS
    assert len(df) == 2                          # only the two RTH bars survive the filter
    assert (df['ticker'] == 'ORCL').all()
    assert df['vwap'].iloc[0] == 100.0           # vw → vwap
    assert int(df['transactions'].iloc[0]) == 50 # n → transactions
    assert df['date'].iloc[0] == '2026-05-28'    # ET calendar date string (drives _by_date)
    assert str(df['datetime'].dt.tz) == 'UTC'

def test_merge_append_is_append_only_and_dedups():
    existing = _bars_to_df([_bar('2026-05-28T13:30:00Z')], 'AAPL')
    new = _bars_to_df([_bar('2026-05-28T13:30:00Z'), _bar('2026-05-28T14:00:00Z')], 'ORCL')
    dup = _bars_to_df([_bar('2026-05-28T13:30:00Z', vw=999.0)], 'AAPL')  # overlapping AAPL row
    merged = _merge_append(existing, pd.concat([new, dup], ignore_index=True))
    assert (merged['ticker'] == 'AAPL').any()                 # existing ticker preserved
    assert (merged['ticker'] == 'ORCL').sum() == 2            # both new ORCL rows kept
    aapl_1330 = merged[(merged['ticker'] == 'AAPL') &
                       (merged['datetime'] == pd.Timestamp('2026-05-28T13:30:00Z'))]
    assert len(aapl_1330) == 1                                # deduped, not duplicated

def test_merge_append_never_shrinks_existing_tickers():
    existing = _bars_to_df([_bar('2026-05-28T13:30:00Z'), _bar('2026-05-28T14:00:00Z')], 'TSLA')
    new = _bars_to_df([_bar('2026-05-28T13:30:00Z')], 'ORCL')
    merged = _merge_append(existing, new)
    assert set(existing['ticker']) <= set(merged['ticker'])   # no existing ticker dropped
    assert (merged['ticker'] == 'TSLA').sum() == 2            # TSLA rows intact
    assert len(merged) >= len(existing)


# ── batched fetch (2026-08-22: one multi-bars request for the universe) ──────

def test_fetch_many_uses_one_multibars_call_for_the_universe(monkeypatch):
    seen = {}
    def fake_multi(symbols, start, end, **kw):
        seen.update(symbols=list(symbols), start=start, end=end, kw=kw)
        return {'AAPL': [_bar('2026-05-28T13:30:00Z'), _bar('2026-05-28T14:00:00Z')], 'BRK.B': [_bar('2026-05-28T13:30:00Z')], 'GHOST': []}
    monkeypatch.setattr(ing, 'fetch_multi_bars', fake_multi)
    df = ing.fetch_many(['AAPL', 'BRK-B', 'GHOST'], '2026-05-27', '2026-05-28')
    assert seen['symbols'] == ['AAPL', 'BRK.B', 'GHOST']
    assert seen['kw']['timeframe'] == '30Min' and seen['kw']['adjustment'] == 'raw' and seen['kw']['bars_per_day'] == 13
    assert list(df.columns) == COLUMNS
    assert (df['ticker'] == 'AAPL').sum() == 2 and (df['ticker'] == 'BRK-B').sum() == 1, 'rows keyed by the universe ticker'
    assert 'GHOST' not in set(df['ticker'])


def test_fetch_many_skips_malformed_symbols_with_warning(monkeypatch, caplog):
    monkeypatch.setattr(ing, 'fetch_multi_bars', lambda symbols, start, end, **kw: {s: [] for s in symbols})
    df = ing.fetch_many(['AAPL', '^GSPC'], '2026-05-27', '2026-05-28')
    assert len(df) == 0 and list(df.columns) == COLUMNS


def test_fetch_ticker_is_a_thin_wrapper_over_fetch_many(monkeypatch):
    monkeypatch.setattr(ing, 'fetch_many', lambda tickers, start, end: _bars_to_df([_bar('2026-05-28T13:30:00Z')], tickers[0]))
    df = ing.fetch_ticker('ORCL', '2026-05-27', '2026-05-28')
    assert len(df) == 1 and df.iloc[0]['ticker'] == 'ORCL'


def test_backfill_fetches_the_universe_in_one_call(tmp_path, monkeypatch):
    out = tmp_path / 'prices_30m.parquet'
    existing = _bars_to_df([_bar('2026-05-27T13:30:00Z')], 'TSLA')
    existing.to_parquet(out, index=False)
    calls = []
    def fake_many(tickers, start, end):
        calls.append(list(tickers))
        return pd.concat([_bars_to_df([_bar('2026-05-28T13:30:00Z')], t) for t in tickers], ignore_index=True)
    monkeypatch.setattr(ing, 'fetch_many', fake_many)
    before, after = ing.backfill(['TSLA', 'ORCL'], '2026-05-28', '2026-05-28', out_path=str(out))
    assert calls == [['TSLA', 'ORCL']], 'one batched fetch, not one per ticker'
    assert (before, after) == (1, 3)
