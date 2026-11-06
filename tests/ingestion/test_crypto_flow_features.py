from datetime import datetime, timedelta, timezone

import pandas as pd

from ingestion.crypto_flow import features

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _ev(hours_ago, usd, direction, ticker='BTC-USD'):
    return {'ts_utc': NOW - timedelta(hours=hours_ago), 'ticker': ticker, 'amount_usd': usd, 'direction': direction}


def test_netflow_in_minus_out_and_coverage():
    ev = pd.DataFrame([_ev(2, 2e6, 'exchange_in'), _ev(3, 1e6, 'exchange_out'), _ev(4, 5e6, 'unknown')])
    r = features.exchange_netflow(ev, 'BTC-USD', NOW, 24)
    assert r['exch_netflow_usd'] == 1e6 and r['whale_n'] == 3 and r['whale_usd'] == 8e6
    assert r['attribution_coverage'] == 3 / 8


def test_exchange_to_exchange_counts_both_sides_and_nets_zero():
    ev = pd.DataFrame([_ev(1, 4e6, 'exchange_to_exchange')])
    r = features.exchange_netflow(ev, 'BTC-USD', NOW, 24)
    assert r['exch_in_usd'] == 4e6 and r['exch_out_usd'] == 4e6 and r['exch_netflow_usd'] == 0
    assert r['attribution_coverage'] == 1.0


def test_score_bounds_and_components():
    s, d = features.whale_score(exch_netflow_usd_24h=1e7, attribution_coverage=1.0, whale_usd_24h=2e8, whale_usd_30d_mean=1e7,
                                agreement_share=1.0, deriv_confirm=1.0, tape_imbalance_1h=-0.2, netflow_z=2.0)
    assert s == 100 and d == 'distributing'


def test_score_damped_when_coverage_low():
    s, d = features.whale_score(exch_netflow_usd_24h=1e7, attribution_coverage=0.0, whale_usd_24h=0, whale_usd_30d_mean=None,
                                agreement_share=None, deriv_confirm=None, tape_imbalance_1h=None, netflow_z=None)
    assert s == 0 and d == 'neutral'


def test_direction_rules():
    kw = dict(exch_netflow_usd_24h=1e6, attribution_coverage=1.0, whale_usd_24h=0, whale_usd_30d_mean=None,
              agreement_share=None, deriv_confirm=None)
    assert features.whale_score(**kw, tape_imbalance_1h=0.3, netflow_z=1.5)[1] == 'neutral'
    assert features.whale_score(**kw, tape_imbalance_1h=0, netflow_z=-1.2)[1] == 'accumulating'


def test_deleveraging_flag_thresholds():
    assert features.deleveraging_flag(-5.1, -3.0) and not features.deleveraging_flag(-4.9, -3.0) and not features.deleveraging_flag(None, -9)


def test_netflow_zscore_needs_48_points():
    assert features.netflow_zscore(pd.Series(range(47), dtype=float), 100.0) is None
    s = pd.Series([0.0, 2.0] * 30)
    assert features.netflow_zscore(s, 3.0) == (3.0 - 1.0) / s.std()
    assert features.netflow_zscore(pd.Series([1.0] * 60), 3.0) is None


def test_compute_ticker_features_has_fixed_columns():
    r = features.compute_ticker_features('BTC-USD', NOW, pd.DataFrame(), pd.DataFrame(), {}, None)
    assert set(features.FEATURE_COLS) <= set(r)
    assert r['whale_direction'] == 'neutral' and r['oi_chg_1h_pct'] is None and r['whale_score'] == 0


def test_compute_ticker_features_values():
    ev = pd.DataFrame([_ev(2, 2e6, 'exchange_in'), _ev(0.5, 1e6, 'exchange_out')])
    hist = pd.DataFrame({'ticker': ['BTC-USD'] * 2, 'ts_utc': [NOW - timedelta(hours=1), NOW - timedelta(hours=24)],
                         'oi_usd': [100.0, 200.0], 'oi_venues': ['a,b', 'a,b']})
    obs = {'oi_usd': 95.0, 'oi_venues': 'a,b', 'funding_8h': 0.0001, 'large_prints_usd_buy': 3.0, 'large_prints_usd_sell': 4.0,
           'ret_1h': -3.5, 'price_usd': 50000.0}
    r = features.compute_ticker_features('BTC-USD', NOW, ev, hist, obs, None)
    assert r['whale_n_1h'] == 1 and r['whale_usd_24h'] == 3e6 and r['exch_netflow_usd_24h'] == 1e6
    assert abs(r['oi_chg_1h_pct'] - (-5.0)) < 1e-9 and abs(r['oi_chg_24h_pct'] - (-52.5)) < 1e-9
    assert r['delev_1h'] is True or r['delev_1h'] == True  # noqa: E712
    assert r['funding_ann'] == 0.0001 * 3 * 365 and r['large_print_usd_1h'] == 7.0
    assert type(r['oi_usd']) is float


def test_oi_change_requires_matching_venues():
    hist = pd.DataFrame({'ticker': ['BTC-USD'], 'ts_utc': [NOW - timedelta(hours=1)], 'oi_usd': [100.0], 'oi_venues': ['a,b,c']})
    r = features.compute_ticker_features('BTC-USD', NOW, pd.DataFrame(), hist, {'oi_usd': 90.0, 'oi_venues': 'a,b'}, None)
    assert r['oi_chg_1h_pct'] is None


def test_deriv_confirm_direction_semantics():
    assert features.deriv_confirm(1, -0.01, 0.8, 0.9) == 1.0
    assert features.deriv_confirm(-1, -0.01, 0.8, 0.9) == 0.0
    assert features.deriv_confirm(1, -0.01, None, 1.2) == 0.5
    assert features.deriv_confirm(1, None, None, None) is None


def test_agreement_share():
    s = {'BTC-USD': 1, 'ETH-USD': 1, 'A': 1, 'B': 1, 'C': -1, 'D': 0}
    assert features.agreement_share(s, 1) == 0.75
    assert features.agreement_share({'BTC-USD': 1, 'A': 1, 'B': 1}, 1) is None


def _pt(z, score=0, direction='neutral', cov=0.9):
    return {'exch_netflow_z_7d': z, 'attribution_coverage': cov, 'whale_score': score, 'whale_direction': direction,
            'oi_usd': 10.0, 'delev_1h': False}


def test_market_features_shock_lines_only_above_thresholds():
    r = features.compute_market_features({'BTC-USD': _pt(3.2), 'ETH-USD': _pt(0.1)}, {})
    assert r['shock_lines'] == ['shock ticker=BTC-USD kind=netflow_z value=3.2']
    assert features.compute_market_features({'BTC-USD': _pt(2.9)}, {})['shock_lines'] == []
    assert r['oi_usd_total'] == 20.0


def test_market_features_other_shocks_and_supply_chg():
    pt = {'BTC-USD': _pt(0.0, 80, 'distributing'), 'ETH-USD': {**_pt(0.0), 'delev_1h': True}}
    r = features.compute_market_features(pt, {'stable_supply_usd': 110.0, 'providers_degraded': ['x']},
                                         flows_hist=pd.DataFrame({'ticker': ['__MARKET__'], 'ts_utc': [NOW - timedelta(days=7)],
                                                                  'stable_supply_usd': [100.0]}), now=NOW)
    assert any('kind=whale_score' in x for x in r['shock_lines']) and any('kind=delev' in x for x in r['shock_lines'])
    assert abs(r['stable_supply_chg_7d_pct'] - 10.0) < 1e-9 and r['providers_degraded'] == ['x']


def test_oi_change_prefers_snapshot_history_exact_hour():
    flows = pd.DataFrame({'ticker': ['BTC-USD'], 'ts_utc': [NOW - timedelta(hours=1)], 'oi_usd': [50.0], 'oi_venues': ['a,b']})
    snap = pd.DataFrame({'ticker': ['BTC-USD'] * 3,
                         'ts_utc': [NOW - timedelta(minutes=75), NOW - timedelta(minutes=60), NOW - timedelta(hours=24, minutes=20)],
                         'oi_usd': [80.0, 100.0, 200.0], 'oi_venues': ['a,b'] * 3, 'price_usd': [None] * 3})
    r = features.compute_ticker_features('BTC-USD', NOW, pd.DataFrame(), flows, {'oi_usd': 95.0, 'oi_venues': 'a,b'}, None,
                                         snap_hist=snap)
    assert abs(r['oi_chg_1h_pct'] - (-5.0)) < 1e-9          # the -60 min snapshot row, not the hour-floored flows row
    assert abs(r['oi_chg_24h_pct'] - (-52.5)) < 1e-9        # within ±30 min of t-24h


def test_oi_change_snapshot_requires_same_venues_and_tolerance():
    snap = pd.DataFrame({'ticker': ['BTC-USD'] * 2, 'ts_utc': [NOW - timedelta(minutes=60), NOW - timedelta(minutes=70)],
                         'oi_usd': [100.0, 100.0], 'oi_venues': ['a,b,c', 'a,b'], 'price_usd': [None, None]})
    flows = pd.DataFrame({'ticker': ['BTC-USD'], 'ts_utc': [NOW - timedelta(hours=1)], 'oi_usd': [50.0], 'oi_venues': ['a,b']})
    r = features.compute_ticker_features('BTC-USD', NOW, pd.DataFrame(), flows, {'oi_usd': 95.0, 'oi_venues': 'a,b'}, None,
                                         snap_hist=snap)
    assert r['oi_chg_1h_pct'] is None  # snapshot rows exist for the ticker → no fallback to flows_hist


def test_ret_1h_from_snapshot_prices_overrides_carried_value():
    # snapshot price_usd rows were written from the per-tick perp mark (mark_px), not the hourly spot
    snap = pd.DataFrame({'ticker': ['BTC-USD'] * 2, 'ts_utc': [NOW - timedelta(minutes=62), NOW - timedelta(hours=24, minutes=20)],
                         'oi_usd': [None] * 2, 'oi_venues': [None] * 2, 'price_usd': [50000.0, 40000.0]})
    obs = {'mark_px': 48000.0, 'price_usd': 47000.0, 'ret_1h': 1.0, 'ret_24h': 2.0}
    r = features.compute_ticker_features('BTC-USD', NOW, pd.DataFrame(), pd.DataFrame(), obs, None, snap_hist=snap)
    assert abs(r['ret_1h'] - (-4.0)) < 1e-9 and abs(r['ret_24h'] - 20.0) < 1e-9
    assert r['price_usd'] == 48000.0 and r['price_spot_usd'] == 47000.0
    r2 = features.compute_ticker_features('BTC-USD', NOW, pd.DataFrame(), pd.DataFrame(), obs, None,
                                          snap_hist=snap.assign(ts_utc=[NOW - timedelta(minutes=70), NOW - timedelta(hours=25)]))
    assert r2['ret_1h'] == 1.0 and r2['ret_24h'] == 2.0  # no snapshot price in tolerance → macro_sent values


def test_price_usd_prefers_mark_px_and_keeps_spot():
    f = lambda o: features.compute_ticker_features('BTC-USD', NOW, pd.DataFrame(), pd.DataFrame(), o, None)  # noqa: E731
    r = f({'mark_px': 70100.0, 'price_usd': 70000.0})
    assert r['price_usd'] == 70100.0 and r['price_spot_usd'] == 70000.0
    for bad in (None, 0.0, -1.0, float('nan'), float('inf'), 'x'):
        r = f({'mark_px': bad, 'price_usd': 70000.0})
        assert r['price_usd'] == 70000.0 and r['price_spot_usd'] == 70000.0
    r = f({'mark_px': 70100.0})
    assert r['price_usd'] == 70100.0 and r['price_spot_usd'] is None
    i = features.FEATURE_COLS.index('price_usd')
    assert features.FEATURE_COLS[i + 1] == 'price_spot_usd'
