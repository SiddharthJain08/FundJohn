from datetime import datetime, timezone, timedelta
import json
import numpy as np
import pytest
import pandas as pd
from src.ingestion.crypto_flow import store, models

def _ev(h='aa', idx=0, usd=2e6):
    return models.WhaleEvent(ts_utc=datetime(2026,11,6,1,0,tzinfo=timezone.utc), source='btc_onchain', chain='bitcoin',
        symbol='BTC', ticker='BTC-USD', amount=25.0, amount_usd=usd, from_addr='bc1a', from_owner=None, from_owner_type=None,
        to_addr='bc1b', to_owner='binance', to_owner_type='exchange', tx_type='transfer', tx_hash=h, out_index=idx, direction='exchange_in')

def test_write_events_replay_is_idempotent(tmp_path):
    p = tmp_path / 'ev.parquet'
    assert store.write_events([_ev('aa'), _ev('bb')], path=p) == 2
    assert store.write_events([_ev('aa'), _ev('cc', usd=3e6)], path=p) == 1
    df = pd.read_parquet(p)
    assert len(df) == 3 and set(df.columns) >= set(models.WhaleEvent.__dataclass_fields__)

def test_write_flows_replace_same_hour(tmp_path):
    p = tmp_path / 'fl.parquet'
    ts = pd.Timestamp('2026-11-06 01:00', tz='UTC')
    a = pd.DataFrame([{'ticker':'BTC-USD','ts_utc':ts,'whale_usd_1h':1.0}])
    b = pd.DataFrame([{'ticker':'BTC-USD','ts_utc':ts,'whale_usd_1h':2.0}])
    store.write_flows(a, path=p); n = store.write_flows(b, path=p)
    df = pd.read_parquet(p)
    assert n == 1 and df.loc[0,'whale_usd_1h'] == 2.0

def test_read_events_window_filters_since_and_tickers(tmp_path):
    p = tmp_path / 'ev.parquet'
    old = _ev('old'); old.ts_utc = datetime(2026,11,1,tzinfo=timezone.utc)
    store.write_events([old, _ev('new')], path=p)
    df = store.read_events_window(datetime(2026,11,5,tzinfo=timezone.utc), path=p, tickers=['BTC-USD'])
    assert list(df['tx_hash']) == ['new']

class _Cur:
    def __init__(s): s.sql=[]; s.rowcount=1
    def execute(s, q, p=None): s.sql.append((q, p))
    def __enter__(s): return s
    def __exit__(s, *a): pass
class _Conn:
    def __init__(s): s.cur=_Cur(); s.committed=0; s.rolled=0
    def cursor(s): return s.cur
    def commit(s): s.committed += 1
    def rollback(s): s.rolled += 1

def test_write_snapshot_one_insert_per_ticker_on_conflict_do_nothing():
    c = _Conn(); ts = datetime(2026,11,6,1,tzinfo=timezone.utc)
    n = store.write_snapshot(c, ts, {'BTC-USD': {'whale_score': 71}, models.MARKET_TICKER: {'fear_greed': 50}})
    assert n == 2 and c.committed == 1
    assert all('ON CONFLICT' in q for q, _ in c.cur.sql) and all('crypto_flow_snapshot' in q for q, _ in c.cur.sql)

def test_write_snapshot_sanitises_nan_inf():
    c = _Conn(); ts = datetime(2026,11,6,1,tzinfo=timezone.utc)
    store.write_snapshot(c, ts, {'BTC-USD': {'a': float('nan'), 'b': float('inf'), 'c': 1.5}})
    assert c.cur.sql[0][1][2] == '{"a": null, "b": null, "c": 1.5}'

def test_write_events_live_prunes_seven_days():
    c = _Conn(); store.write_events_live(c, [_ev('aa')])
    assert any('DELETE FROM crypto_whale_events_live' in q and "7 days" in q for q, _ in c.cur.sql)
    assert c.committed == 1

def test_events_to_frame_empty_and_write_events_empty(tmp_path):
    df = store.events_to_frame([])
    assert len(df) == 0 and set(df.columns) == set(models.WhaleEvent.__dataclass_fields__)
    p = tmp_path / 'x.parquet'
    assert store.write_events([], path=p) == 0 and not p.exists()
    assert store.read_events_window(datetime(2026,1,1,tzinfo=timezone.utc), path=p).empty

def test_write_onchain_daily_replace_same_key(tmp_path):
    p = tmp_path / 'oc.parquet'
    f = pd.Timestamp('2026-11-06', tz='UTC')
    row = lambda v: pd.DataFrame([{'ticker':'BTC-USD','date':'2026-11-05','metric':'active_addresses','value':v,'source':'santiment','fetched_at':f}])
    store.write_onchain_daily(row(1.0), path=p)
    assert store.write_onchain_daily(row(2.0), path=p) == 1
    assert pd.read_parquet(p).loc[0,'value'] == 2.0

def test_sanitize_numpy_scalars():
    d = {'a': np.int64(3), 'b': np.float64('nan'), 'c': np.bool_(True), 'd': np.float32(1.5)}
    assert json.dumps(store._sanitize(d), allow_nan=False) == '{"a": 3, "b": null, "c": true, "d": 1.5}'
    assert store._sanitize(pd.Timestamp('2026-11-06', tz='UTC')).startswith('2026-11-06')

def test_writer_rolls_back_on_exception():
    c = _Conn(); n = {'k': 0}
    orig = c.cur.execute
    def boom(q, p=None):
        n['k'] += 1
        if n['k'] == 2: raise RuntimeError('db down')
        orig(q, p)
    c.cur.execute = boom
    with pytest.raises(RuntimeError):
        store.write_snapshot(c, datetime(2026,11,6,1,tzinfo=timezone.utc), {'A': {'x': 1}, 'B': {'x': 2}})
    assert c.rolled == 1 and c.committed == 0


class _RowsCur(_Cur):
    def __init__(s, rows=None, boom=False):
        super().__init__(); s.rows = rows or []; s.boom = boom
    def execute(s, q, p=None):
        if s.boom:
            raise RuntimeError('no table')
        super().execute(q, p)
    def fetchall(s): return s.rows

def test_read_latest_snapshot_latest_row_per_ticker_dict_or_json():
    c = _Conn(); c.cur = _RowsCur([('BTC-USD', {'price_usd': 70000.0}), ('ETH-USD', '{"price_usd": 3000.0}')])
    out = store.read_latest_snapshot(c)
    assert out == {'BTC-USD': {'price_usd': 70000.0}, 'ETH-USD': {'price_usd': 3000.0}}
    q = c.cur.sql[0][0]
    assert 'DISTINCT ON (ticker)' in q and 'ORDER BY ticker, ts_utc DESC' in q

def test_read_latest_snapshot_fail_open_rolls_back():
    c = _Conn(); c.cur = _RowsCur(boom=True)
    assert store.read_latest_snapshot(c) == {} and c.rolled == 1

def test_read_snapshot_history_extracts_fields():
    ts = datetime(2026,11,6,1,tzinfo=timezone.utc)
    c = _Conn(); c.cur = _RowsCur([('BTC-USD', ts, '1e9', 'binance,okx', '70000')])
    df = store.read_snapshot_history(c, ts - timedelta(days=2))
    q, p = c.cur.sql[0]
    assert 'crypto_flow_snapshot' in q and "features->>'oi_usd'" in q and "features->>'price_usd'" in q
    assert "features->>'oi_venues'" in q and p[0] == ts - timedelta(days=2)
    assert list(df.columns) == ['ticker', 'ts_utc', 'oi_usd', 'oi_venues', 'price_usd']
    assert df.loc[0, 'oi_usd'] == 1e9 and df.loc[0, 'price_usd'] == 70000.0 and df.loc[0, 'oi_venues'] == 'binance,okx'

def test_read_snapshot_history_empty_and_error():
    ts = datetime(2026,11,6,1,tzinfo=timezone.utc)
    c = _Conn(); c.cur = _RowsCur([])
    df = store.read_snapshot_history(c, ts)
    assert df.empty and list(df.columns) == ['ticker', 'ts_utc', 'oi_usd', 'oi_venues', 'price_usd']
    c = _Conn(); c.cur = _RowsCur(boom=True)
    assert store.read_snapshot_history(c, ts).empty and c.rolled == 1
