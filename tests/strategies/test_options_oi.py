from __future__ import annotations
import datetime as dt
import pandas as pd
import pytest

from strategies import options_oi as oi


def _partition(root, session, rows):
    root.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(root / f'date={session}.parquet', index=False)


def _rows(session='2026-09-02', underlying='ZZZT', spot=100.0):
    exp_front = (pd.Timestamp(session) + pd.Timedelta(days=16)).date()
    exp_back = (pd.Timestamp(session) + pd.Timedelta(days=44)).date()
    rows = []
    for exp in (exp_front, exp_back):
        for K, coi, poi in ((90.0, 100, 400), (100.0, 300, 300), (110.0, 500, 50)):
            for typ, o in (('C', coi), ('P', poi)):
                rows.append({'date': dt.date.fromisoformat(session), 'underlying': underlying, 'contract_symbol': 'x',
                             'root': underlying, 'expiry': exp, 'option_type': typ, 'strike': K,
                             'bid': 1.0, 'bid_size': 1, 'ask': 1.2, 'ask_size': 1, 'iv': 0.25 + (0.02 if typ == 'P' else 0.0),
                             'open_interest': float(o), 'volume': 10.0, 'delta': (0.5 if typ == 'C' else -0.5) * (1 if K == 100 else (0.6 if K == 90 else 0.4)),
                             'gamma': 0.02, 'vega': 0.1, 'theta': -0.01, 'rho': 0.0, 'theo': 1.1, 'last_trade_price': 1.1,
                             'last_trade_time': None, 'prev_day_close': 1.0, 'underlying_price': spot,
                             'feed_timestamp': f'{session} 17:00:00', 'source': 'cboe'})
    return rows


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / 'cboe_chains'
    _partition(r, '2026-09-01', _rows('2026-09-01'))
    _partition(r, '2026-09-02', _rows('2026-09-02'))
    monkeypatch.setenv('OPENCLAW_CBOE_CHAINS_ROOT', str(r))
    oi.clear_cache()
    return r


def test_session_lookup_is_strictly_before_as_of(root):
    assert oi.cboe_session_for('2026-09-03') == dt.date(2026, 9, 2)
    assert oi.cboe_session_for('2026-09-02') == dt.date(2026, 9, 1)
    assert oi.cboe_session_for('2026-09-01') is None
    assert oi.cboe_session_for('2026-09-08') == dt.date(2026, 9, 2)


def test_oi_features_values(root):
    rows = oi.load_cboe_session(dt.date(2026, 9, 2), ['ZZZT'])
    f = oi.oi_features_for_day(rows, '2026-09-03')
    assert f['oi_session'] == '2026-09-02'
    assert f['open_interest_by_strike'] == {90.0: 500.0, 100.0: 600.0, 110.0: 550.0}
    # max pain: payout minimised at 100 (symmetric OI); front expiry only
    assert f['max_pain'] == 100.0
    assert f['contracts_liquid'] == 6
    assert f['pcr_oi'] == pytest.approx((400 + 300 + 50) * 2 / ((100 + 300 + 500) * 2))
    gex_expected = (0.02 * (100 + 300 + 500) - 0.02 * (400 + 300 + 50)) * 100
    assert f['gex'] == pytest.approx(gex_expected)
    assert f['iv_centroid_delta'] is not None and f['surface_premium'] is not None


def test_ticker_helper_and_builder_lookup(root):
    f = oi.oi_features_for_ticker('ZZZT', '2026-09-03')
    assert f['gex'] is not None and f['oi_session'] == '2026-09-02'
    assert oi.oi_features_for_ticker('NOPE', '2026-09-03')['gex'] is None
    look = oi.oi_lookup_factory()
    d = look('ZZZT', pd.Timestamp('2026-09-03'))
    assert 'open_interest_by_strike' not in d and d['max_pain'] == 100.0
    assert look('ZZZT', pd.Timestamp('2026-09-01')) is None       # no session strictly before


def test_gex_is_none_when_front_expiry_has_no_open_interest(root, monkeypatch):
    rows = _rows('2026-09-02', underlying='QQQT')
    df = pd.DataFrame(rows)
    front = df['expiry'] == df['expiry'].min()
    df.loc[front, 'open_interest'] = 0.0            # front expiry unmeasured; back expiry still has OI
    f = oi.oi_features_for_day(df, '2026-09-03')
    # contracts_liquid is None, not 0: with no OI on the front expiry the count
    # is UNMEASURED (final fix wave 2026-09-05 — never a computed zero).
    assert f['gex'] is None and f['max_pain'] is None and f['contracts_liquid'] is None
    assert f['pcr_oi'] is not None                  # whole-chain ratio still measurable from the back expiry


def test_master_dir_root_never_leaks_the_default_root(tmp_path, monkeypatch):
    monkeypatch.delenv('OPENCLAW_CBOE_CHAINS_ROOT', raising=False)
    oi.clear_cache()
    master = tmp_path / 'master'
    _partition(master / 'cboe_chains', '2026-09-10', _rows('2026-09-10'))
    f = oi.oi_features_for_ticker('ZZZT', '2026-09-11', master_dir=master)
    assert f['oi_session'] == '2026-09-10' and f['gex'] is not None
    look = oi.oi_lookup_factory(root=master / 'cboe_chains')
    d = look('ZZZT', pd.Timestamp('2026-09-11'))
    assert d['oi_session'] == '2026-09-10'
    oi.clear_cache()


# ── Final fix wave 2026-09-05, F4a: per-underlying dict instead of an isin scan ──
def test_load_cboe_session_slices_by_underlying(tmp_path, monkeypatch):
    """Same rows as the old whole-table `isin` scan, for one ticker, several
    tickers, none, and all."""
    r = tmp_path / 'cboe_chains'
    rows = _rows('2026-09-02', underlying='AAAT') + _rows('2026-09-02', underlying='BBBT') \
        + _rows('2026-09-02', underlying='CCCT')
    _partition(r, '2026-09-02', rows)
    monkeypatch.setenv('OPENCLAW_CBOE_CHAINS_ROOT', str(r))
    oi.clear_cache()
    try:
        session = dt.date(2026, 9, 2)
        one = oi.load_cboe_session(session, ['BBBT'])
        assert set(one['underlying']) == {'BBBT'} and len(one) == 12
        two = oi.load_cboe_session(session, ['CCCT', 'AAAT'])
        assert set(two['underlying']) == {'AAAT', 'CCCT'} and len(two) == 24
        dupes = oi.load_cboe_session(session, ['AAAT', 'AAAT'])
        assert len(dupes) == 12                              # a repeat is not a duplication
        none = oi.load_cboe_session(session, ['ZZZZ'])
        assert none.empty and list(none.columns) == list(oi._COLS)
        assert len(oi.load_cboe_session(session)) == 36       # tickers=None -> whole session
    finally:
        oi.clear_cache()


def test_load_cboe_session_result_is_not_mutated_by_features(tmp_path, monkeypatch):
    """load_cboe_session hands back a slice of the lru_cached frame; a second
    call must see the original columns (oi_features_for_day copies first)."""
    r = tmp_path / 'cboe_chains'
    _partition(r, '2026-09-02', _rows('2026-09-02', underlying='AAAT'))
    monkeypatch.setenv('OPENCLAW_CBOE_CHAINS_ROOT', str(r))
    oi.clear_cache()
    try:
        session = dt.date(2026, 9, 2)
        first = oi.load_cboe_session(session, ['AAAT'])
        cols_before = list(first.columns)
        oi.oi_features_for_day(first, '2026-09-03', session=session)
        second = oi.load_cboe_session(session, ['AAAT'])
        assert list(second.columns) == cols_before and 'dte' not in second.columns
    finally:
        oi.clear_cache()
