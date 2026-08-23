"""CBOE delayed option-chain capture → per-day partitions + aggregates master.

Why (2026-08-23 audit): Alpaca's chain snapshots carry NO open interest, the
options master only starts 2026-04-08 and its greeks are valid from 06-29.
CBOE's delayed chain JSON (no key) carries IV, all five greeks, theo, OI,
volume and the underlying's iv30 — the only free OI source found. Capturing it
daily after the close builds our own OI / IV history forward.
"""
from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pyarrow.parquet as pq
import pytest

from src.ingestion import ingest_cboe_chains as mod


def _opt(sym, **kw):
    base = {'option': sym, 'bid': 1.0, 'bid_size': 10.0, 'ask': 1.2, 'ask_size': 12.0,
            'iv': 0.30, 'open_interest': 100.0, 'volume': 5.0, 'delta': 0.5, 'gamma': 0.01,
            'vega': 0.1, 'theta': -0.02, 'rho': 0.03, 'theo': 1.1, 'change': 0.0,
            'open': 0.0, 'high': 0.0, 'low': 0.0, 'tick': 'up', 'last_trade_price': 1.05,
            'last_trade_time': '2026-08-21T15:43:25', 'percent_change': 0.0,
            'prev_day_close': 1.0}
    base.update(kw)
    return base


PAYLOAD = {
    'timestamp': '2026-08-23 02:13:39',
    'symbol': 'AAPL',
    'data': {
        'symbol': 'AAPL', 'security_type': 'stock', 'current_price': 200.0,
        'iv30': 24.054, 'iv30_change': -0.5, 'iv30_change_percent': -2.0,
        'options': [
            _opt('AAPL260918C00190000', delta=0.70, gamma=0.02, open_interest=1000, volume=50, iv=0.28),
            _opt('AAPL260918C00200000', delta=0.50, gamma=0.03, open_interest=2000, volume=80, iv=0.25),
            _opt('AAPL260918P00200000', delta=-0.50, gamma=0.03, open_interest=1500, volume=40, iv=0.27),
            _opt('AAPL260918P00180000', delta=-0.20, gamma=0.01, open_interest=500, volume=10, iv=0.33),
            _opt('AAPL261218C00135000', delta=0.99, gamma=0.0001, open_interest=698, volume=0, iv=0.61),
        ],
    },
}


def test_parse_occ_symbol_equity_and_index():
    assert mod.parse_occ_symbol('AAPL261218C00135000') == ('AAPL', dt.date(2026, 12, 18), 'C', 135.0)
    assert mod.parse_occ_symbol('SPXW260918P05000000') == ('SPXW', dt.date(2026, 9, 18), 'P', 5000.0)
    assert mod.parse_occ_symbol('BRKB260918C00450500') == ('BRKB', dt.date(2026, 9, 18), 'C', 450.5)
    assert mod.parse_occ_symbol('garbage') is None


def test_session_date_rolls_weekend_and_premarket_stamps_back():
    assert mod.session_date_for('2026-08-23 02:13:39') == dt.date(2026, 8, 21)   # Sunday → Friday
    assert mod.session_date_for('2026-08-22 18:30:09') == dt.date(2026, 8, 21)   # Saturday → Friday
    assert mod.session_date_for('2026-08-21 23:36:27') == dt.date(2026, 8, 21)   # Friday evening
    assert mod.session_date_for('2026-08-24 08:15:00') == dt.date(2026, 8, 21)   # Monday pre-open → Friday
    assert mod.session_date_for('2026-08-24 17:00:00') == dt.date(2026, 8, 24)   # Monday after close


def test_chain_rows_map_every_contract_with_parsed_fields():
    rows = mod.chain_rows(PAYLOAD, 'AAPL', dt.date(2026, 8, 21))
    assert len(rows) == 5
    r = rows[1]
    assert r['date'] == dt.date(2026, 8, 21)
    assert r['underlying'] == 'AAPL'
    assert r['contract_symbol'] == 'AAPL260918C00200000'
    assert r['expiry'] == dt.date(2026, 9, 18)
    assert r['option_type'] == 'C' and r['strike'] == 200.0
    assert r['open_interest'] == 2000 and r['iv'] == 0.25 and r['delta'] == 0.50
    assert r['underlying_price'] == 200.0
    assert r['source'] == 'cboe'


def test_chain_aggregates_pcr_gex_and_atm_iv():
    rows = mod.chain_rows(PAYLOAD, 'AAPL', dt.date(2026, 8, 21))
    agg = mod.chain_aggregates(rows, PAYLOAD, 'AAPL', dt.date(2026, 8, 21))
    assert agg['date'] == dt.date(2026, 8, 21) and agg['underlying'] == 'AAPL'
    assert agg['n_contracts'] == 5
    assert agg['call_oi'] == 1000 + 2000 + 698
    assert agg['put_oi'] == 1500 + 500
    assert agg['pcr_oi'] == pytest.approx(2000 / 3698)
    assert agg['call_volume'] == 130 and agg['put_volume'] == 50
    assert agg['pcr_volume'] == pytest.approx(50 / 130)
    assert agg['iv30'] == pytest.approx(24.054)
    assert agg['underlying_price'] == 200.0
    # dealer-gamma proxy: Σ gamma·OI·100·S²·0.01, calls +, puts −
    exp_gex = (0.02 * 1000 + 0.03 * 2000 + 0.0001 * 698 - 0.03 * 1500 - 0.01 * 500) * 100 * 200.0 ** 2 * 0.01
    assert agg['gex'] == pytest.approx(exp_gex)
    # ATM IV (nearest expiry ≥ 7 DTE, strike closest to spot, call/put mean)
    assert agg['atm_iv'] == pytest.approx((0.25 + 0.27) / 2)
    assert agg['nearest_expiry'] == dt.date(2026, 9, 18)


def test_chain_aggregates_empty_chain_is_safe():
    payload = {'timestamp': '2026-08-21 23:00:00', 'symbol': 'ZZZ', 'data': {'current_price': 3.0, 'options': []}}
    agg = mod.chain_aggregates([], payload, 'ZZZ', dt.date(2026, 8, 21))
    assert agg['n_contracts'] == 0 and agg['pcr_oi'] is None and agg['atm_iv'] is None


def test_partition_writer_streams_batches_atomically_and_skips_existing(tmp_path):
    rows = mod.chain_rows(PAYLOAD, 'AAPL', dt.date(2026, 8, 21))
    out = mod.partition_path(dt.date(2026, 8, 21), root=tmp_path)
    assert out.name == 'date=2026-08-21.parquet'
    with mod.PartitionWriter(out) as w:
        w.write_rows(rows[:2])
        w.write_rows(rows[2:])
    assert out.exists() and not out.with_suffix('.parquet.tmp').exists()
    t = pq.read_table(out)
    assert t.num_rows == 5
    assert set(['date', 'underlying', 'contract_symbol', 'expiry', 'option_type', 'strike',
                'iv', 'open_interest', 'delta', 'gamma', 'vega', 'theta', 'rho']).issubset(t.column_names)
    assert mod.partition_exists(dt.date(2026, 8, 21), root=tmp_path)


def test_aggregates_master_append_replaces_same_key(tmp_path):
    master = tmp_path / 'cboe_chain_aggregates.parquet'
    rows = mod.chain_rows(PAYLOAD, 'AAPL', dt.date(2026, 8, 21))
    a1 = mod.chain_aggregates(rows, PAYLOAD, 'AAPL', dt.date(2026, 8, 21))
    mod.write_aggregates([a1], master_path=master)
    a2 = dict(a1); a2['iv30'] = 99.0
    mod.write_aggregates([a2], master_path=master)
    df = pd.read_parquet(master)
    assert len(df) == 1 and df.iloc[0]['iv30'] == 99.0


def test_cboe_url_prefixes_index_symbols():
    assert mod.cboe_url('AAPL').endswith('/options/AAPL.json')
    assert mod.cboe_url('_SPX').endswith('/options/_SPX.json')


def test_run_counts_failures_and_keeps_going(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, 'PARTITION_ROOT', tmp_path / 'cboe_chains')
    monkeypatch.setattr(mod, 'AGGREGATES_PATH', tmp_path / 'agg.parquet')

    def fake_fetch(symbol):
        if symbol == 'BAD':
            raise RuntimeError('503')
        p = json.loads(json.dumps(PAYLOAD)); p['symbol'] = symbol; p['data']['symbol'] = symbol
        return p
    monkeypatch.setattr(mod, 'fetch_chain', fake_fetch)
    stats = mod.run(['AAPL', 'BAD', 'MSFT'], session=dt.date(2026, 8, 21), workers=1)
    assert stats['tickers_ok'] == 2 and stats['tickers_failed'] == 1
    assert stats['contracts'] == 10
    assert (tmp_path / 'cboe_chains' / 'date=2026-08-21.parquet').exists()
    assert len(pd.read_parquet(tmp_path / 'agg.parquet')) == 2


def test_fetch_with_retry_backs_off_on_429_then_succeeds(monkeypatch):
    calls, sleeps = [], []

    class Resp:
        def __init__(self, status, retry_after=None):
            self.status_code = status
            self.headers = {'Retry-After': retry_after} if retry_after else {}

    class HTTPError(Exception):
        def __init__(self, status, retry_after=None):
            super().__init__(f'{status} Client Error')
            self.response = Resp(status, retry_after)

    def fake_fetch(sym):
        calls.append(sym)
        if len(calls) < 3:
            raise HTTPError(429, retry_after='2')
        return {'timestamp': '2026-08-21 23:00:00', 'data': {'options': []}}

    monkeypatch.setattr(mod, 'fetch_chain', fake_fetch)
    out = mod.fetch_with_retry('AAPL', attempts=4, sleep=sleeps.append)
    assert out['timestamp'].startswith('2026-08-21')
    assert len(calls) == 3
    assert sleeps == [2.0, 2.0]          # honoured Retry-After both times


def test_fetch_with_retry_gives_up_after_attempts(monkeypatch):
    def always_429(sym):
        raise RuntimeError('429 Client Error: Too Many Requests')
    monkeypatch.setattr(mod, 'fetch_chain', always_429)
    sleeps = []
    with pytest.raises(RuntimeError):
        mod.fetch_with_retry('AAPL', attempts=3, sleep=sleeps.append)
    assert len(sleeps) == 2 and sleeps[0] < sleeps[1]      # exponential when no Retry-After
