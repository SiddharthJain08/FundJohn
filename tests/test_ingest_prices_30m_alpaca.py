import pandas as pd
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
