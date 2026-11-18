"""tests/execution/test_trade_handoff_prefilter_retired.py

The handoff EV / p_t1 prefilter was retired 2026-11-18 (operator ruling).
The GBM two-barrier model was direction-blind (shorts got sign-inverted
geometry and swapped barriers) and its drift was mis-scaled, so it was in
practice a 12-month-momentum-sign test. The sizer never read it.

These tests pin the post-retirement contract:
  * compute_features no longer emits ev_gbm / p_t1 / exp_exit_*; hv21 stays
    (OUE baseline), and the geometry is a direction-aware magnitude.
  * build() prefilters ONLY untradable tickers.
  * daily_signal_summary keeps its one-row-per-handoff INSERT with zero
    counters and NULL averages.
  * the d-1 outlier reader keeps working from hv21 alone (baseline 0).

build() is driven with every DB/disk/broker seam stubbed — write_handoff and
psycopg2.connect in particular, so no real file or DB row is touched.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

MOD = 'src.execution.trade_handoff_builder'
hb = __import__(MOD, fromlist=['build'])

RUN_DATE = '2026-11-17'


@pytest.fixture
def px_two_tickers():
    rng = np.random.default_rng(7)
    idx = pd.bdate_range(end=RUN_DATE, periods=300)
    cols = {}
    for t in ('AAA', 'BBB', 'ZZZ', 'SPY'):
        rets = rng.normal(0.0003, 0.01, len(idx))
        cols[t] = 100.0 * np.cumprod(1.0 + rets)
    return pd.DataFrame(cols, index=idx)


@pytest.fixture
def spy_returns(px_two_tickers):
    return px_two_tickers['SPY'].pct_change().dropna()


def _sig(ticker, direction, entry, stop, t1, t2=None, sid='S_x'):
    return {'ticker': ticker, 'strategy_id': sid, 'direction': direction,
            'entry_price': entry, 'stop_loss': stop, 'target_1': t1,
            'target_2': t2, 'signal_params': {}}


def test_short_signal_is_green_with_positive_geometry(px_two_tickers, spy_returns):
    sig = _sig('AAA', 'SHORT', 100.0, 103.0, 94.0, 88.0)
    f = hb.compute_features(sig, px_two_tickers, spy_returns, RUN_DATE)
    assert f['risk_pct'] == pytest.approx(0.03)
    assert f['t1_pct'] == pytest.approx(0.06)
    assert f['t2_pct'] == pytest.approx(0.12)
    assert f['rr1'] == pytest.approx(2.0)
    assert 'ev_gbm' not in f and 'p_t1' not in f
    assert 'exp_exit_days' not in f and 'exp_exit_date' not in f
    assert 'hv21' in f and f['hv21'] is not None


def test_lowercase_short_is_direction_aware(px_two_tickers, spy_returns):
    sig = _sig('AAA', 'short', 100.0, 103.0, 94.0)   # no t2 -> extrapolated
    f = hb.compute_features(sig, px_two_tickers, spy_returns, RUN_DATE)
    assert f['risk_pct'] == pytest.approx(0.03)
    assert f['t1_pct'] == pytest.approx(0.06)
    assert f['t2'] == pytest.approx(88.0)
    assert f['t2_pct'] == pytest.approx(0.12)


def test_long_geometry_unchanged(px_two_tickers, spy_returns):
    sig = _sig('AAA', 'LONG', 100.0, 97.0, 106.0)
    f = hb.compute_features(sig, px_two_tickers, spy_returns, RUN_DATE)
    assert f['risk_pct'] == pytest.approx(0.03)
    assert f['t1_pct'] == pytest.approx(0.06)
    assert f['t2_pct'] == pytest.approx(0.12)
    assert f['rr1'] == pytest.approx(2.0)
    assert 'ev_gbm' not in f and 'p_t1' not in f


class _Cur:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))


class _Conn:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self, *a, **k):
        return _Cur(self.log)

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def run_build(monkeypatch, px_two_tickers):
    """Run the real build() over three signals: a tradable LONG, a tradable
    SHORT with deliberately poor R:R (stop 1 % away, target 0.5 % away), and
    an untradable ticker. Returns (payload, sql_log, stdout)."""
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub-never-connected')
    signals = [
        _sig('AAA', 'LONG', 100.0, 97.0, 106.0),
        _sig('BBB', 'SHORT', 100.0, 101.0, 99.5, sid='S_y'),
        _sig('ZZZ', 'LONG', 100.0, 97.0, 106.0, sid='S_z'),
    ]

    def _go(capsys):
        captured, sql_log = {}, []

        def _capture(run_date, kind, payload):
            captured['payload'] = payload

        with patch(f'{MOD}.load_signals', return_value=signals), \
             patch(f'{MOD}.load_regime', return_value={'state': 'LOW_VOL'}), \
             patch(f'{MOD}.load_portfolio_state', return_value={}), \
             patch(f'{MOD}.load_veto_history', return_value={}), \
             patch(f'{MOD}.load_mastermind_rec', return_value={}), \
             patch(f'{MOD}.load_yesterdays_vetoed', return_value=[]), \
             patch(f'{MOD}._get_sigma_gate', return_value=2.0), \
             patch(f'{MOD}.load_yesterdays_performance_outliers', return_value=([], [])), \
             patch(f'{MOD}._write_performance_outliers', return_value=None), \
             patch(f'{MOD}.load_prices', return_value=px_two_tickers), \
             patch(f'{MOD}.load_tradable_universe', return_value={'AAA', 'BBB', 'SPY'}), \
             patch(f'{MOD}._load_sentiment_for_tickers', return_value={}), \
             patch(f'{MOD}.write_handoff', side_effect=_capture), \
             patch(f'{MOD}.psycopg2.connect', side_effect=lambda *a, **k: _Conn(sql_log)), \
             patch('execution.alpaca_trader._alpaca_session', return_value=object()), \
             patch('execution.alpaca_trader._fetch_account_state',
                   return_value={'fetched': False}):
            hb.build(RUN_DATE)
        return captured['payload'], sql_log, capsys.readouterr().out
    return _go


def test_build_prefilters_only_untradable(run_build, capsys):
    out, _, stdout = run_build(capsys)
    assert {p['reason'] for p in out['prefiltered']} == {'untradable_at_alpaca'}
    assert [p['ticker'] for p in out['prefiltered']] == ['ZZZ']
    assert set(out['prefiltered'][0]) == {'ticker', 'strategy_id', 'direction', 'reason'}
    assert len(out['signals']) == 2
    assert {s['ticker'] for s in out['signals']} == {'AAA', 'BBB'}
    short = next(s for s in out['signals'] if s['ticker'] == 'BBB')
    assert short['risk_pct'] == pytest.approx(0.01)
    assert short['t1_pct'] == pytest.approx(0.005)
    assert not hasattr(hb, 'MIN_EV_GBM') and not hasattr(hb, 'MIN_P_T1')
    assert not hasattr(hb, 'HOLDING_PERIOD') and not hasattr(hb, 'DEFAULT_HP')
    assert ('[handoff] prefilter: 2 green / 1 filtered '
            '(untradable=1, universe_size=3)') in stdout


def test_summary_row_writes_zero_counters_and_null_ev(run_build, capsys):
    _, sql_log, stdout = run_build(capsys)
    inserts = [p for s, p in sql_log if 'INSERT INTO daily_signal_summary' in s]
    assert len(inserts) == 1
    run_date, regime, n, ev_pos, ev_neg, avg_ev, avg_p_t1, high_conv, meta = inserts[0]
    assert run_date == RUN_DATE and regime == 'LOW_VOL'
    assert (ev_pos, ev_neg, high_conv) == (0, 0, 0)
    assert avg_ev is None and avg_p_t1 is None
    assert n == 3
    m = json.loads(meta)
    assert m == {'green': 2, 'prefiltered': 1, 'd1_matches': 0,
                 'ev_gate': 'retired 2026-11-18'}
    assert '[handoff] daily_signal_summary appended: n=3 (ev gate retired)' in stdout


def test_d1_outlier_reader_uses_hv21_baseline_without_ev_gbm():
    """Yesterday's structured handoff no longer carries ev_gbm. The d-1
    outlier reader must key on hv21 and use a 0 baseline, not go silent."""
    y_struct = {'signals': [{'ticker': 'AAA', 'strategy_id': 'S_x',
                             'hv21': 0.20, 'entry': 100.0}]}
    rows = [{'signal_id': 1, 'strategy_id': 'S_x', 'pnl_date': '2026-11-16',
             'close_price': 110.0, 'unrealized_pnl_pct': 0.10,
             'realized_pnl_pct': None, 'days_held': 1, 'status': 'open',
             'close_reason': None, 'ticker': 'AAA', 'direction': 'LONG',
             'entry_price': 100.0}]

    class _DictCur:
        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows

    class _DictConn:
        def cursor(self, *a, **k):
            return _DictCur()

        def close(self):
            pass

    with patch(f'{MOD}.read_handoff', return_value=y_struct), \
         patch(f'{MOD}._get_sigma_gate', return_value=2.0), \
         patch(f'{MOD}.psycopg2.connect', return_value=_DictConn()):
        over, under = hb.load_yesterdays_performance_outliers('postgresql://stub', RUN_DATE)
    assert under == []
    assert len(over) == 1
    assert over[0]['ev_gbm'] == 0.0
    assert over[0]['delta'] == pytest.approx(0.10)
