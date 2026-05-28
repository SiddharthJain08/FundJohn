import pandas as pd
import pytest
from src.strategies.implementations.s15_insider_opportunistic_short import (
    classify_insider,
    qualifying_sales,
)


AS_OF = pd.Timestamp('2026-05-15')


def _make_txn(date, value=1_000_000, ttype='S-Sale'):
    return {
        'transactionDate': date,
        'transactionType': ttype,
        'value': value,
        'reportingName': 'Test Insider',
        'role': 'officer: VP',
        'sharesOwnedAfter': 100_000,
        'shares': 5_000,
        'pricePerShare': 200.0,
    }


def test_classify_insider_routine_regular_quarterly():
    """Insider selling every quarter for 12mo → routine."""
    history = [
        _make_txn('2025-03-15'),
        _make_txn('2025-04-15'),
        _make_txn('2025-07-15'),
        _make_txn('2025-10-15'),
        _make_txn('2026-01-15'),
    ]
    assert classify_insider(history, AS_OF) == 'routine'


def test_classify_insider_opportunistic_single_large_sale():
    """Insider with one sale in window → opportunistic."""
    history = [
        _make_txn('2025-09-15', value=10_000_000),
    ]
    assert classify_insider(history, AS_OF) == 'opportunistic'


def test_classify_insider_opportunistic_new_insider():
    """Insider with zero txns in the window → opportunistic (default high signal)."""
    history = []
    assert classify_insider(history, AS_OF) == 'opportunistic'


def test_classify_insider_ignores_outside_window():
    """Txns outside t-15 to t-3 must not count toward quarter buckets."""
    history = [
        _make_txn('2026-03-15'),
        _make_txn('2026-04-15'),
        _make_txn('2026-05-10'),
    ]
    assert classify_insider(history, AS_OF) == 'opportunistic'


def test_classify_insider_routine_at_threshold():
    """Exactly 3 distinct quarters in window → routine (boundary)."""
    history = [
        _make_txn('2025-05-15'),
        _make_txn('2025-09-15'),
        _make_txn('2025-12-15'),
    ]
    assert classify_insider(history, AS_OF) == 'routine'


def test_classify_insider_opportunistic_at_threshold():
    """Exactly 2 distinct quarters in window → opportunistic (boundary)."""
    history = [
        _make_txn('2025-05-15'),
        _make_txn('2025-09-15'),
    ]
    assert classify_insider(history, AS_OF) == 'opportunistic'


def test_classify_insider_counts_bare_s_type():
    """Bare 'S' transactionType (alternate SEC form) is counted same as S-Sale."""
    # 3 distinct quarters using bare 'S' → routine
    history = [
        _make_txn('2025-05-15', ttype='S'),
        _make_txn('2025-09-15', ttype='S'),
        _make_txn('2025-12-15', ttype='S'),
    ]
    assert classify_insider(history, AS_OF) == 'routine'


def test_qualifying_sales_only_keeps_s_sale_and_s():
    """Filter must keep S-Sale and S, drop everything else."""
    mixed = [
        {'transactionType': 'S-Sale',   'value': 1_000_000},
        {'transactionType': 'S',        'value': 2_000_000},
        {'transactionType': 'M-Exempt', 'value': 3_000_000},
        {'transactionType': 'F-InKind', 'value': 4_000_000},
        {'transactionType': 'G-Gift',   'value': 5_000_000},
        {'transactionType': 'D',        'value': 6_000_000},
        {'transactionType': 'A-Award',  'value': 7_000_000},
        {'transactionType': 'J-Other',  'value': 8_000_000},
        {'transactionType': 'P-Purchase', 'value': 9_000_000},
    ]
    out = qualifying_sales(mixed)
    assert len(out) == 2
    assert {t['value'] for t in out} == {1_000_000, 2_000_000}


def test_qualifying_sales_case_insensitive():
    """Match on uppercase form so casing variants are handled."""
    txns = [
        {'transactionType': 's-sale', 'value': 100},
        {'transactionType': 'S-sale', 'value': 200},
        {'transactionType': 's',      'value': 300},
    ]
    out = qualifying_sales(txns)
    assert len(out) == 3


def test_qualifying_sales_handles_missing_type():
    """Txns with missing/None transactionType are dropped silently."""
    txns = [
        {'transactionType': 'S-Sale', 'value': 1},
        {'transactionType': None,     'value': 2},
        {'value': 3},
    ]
    out = qualifying_sales(txns)
    assert len(out) == 1
    assert out[0]['value'] == 1


from src.strategies.implementations.s15_insider_opportunistic_short import (
    cluster_gate,
)


def _txn(name, value=1_000_000, ttype='S-Sale', date='2026-05-10'):
    return {
        'transactionDate': date,
        'transactionType': ttype,
        'reportingName':   name,
        'value':           value,
        'shares':          5000,
        'sharesOwnedAfter': 50_000,
        'role':            'officer: VP',
    }


def test_cluster_gate_passes_3_insiders_5m_zero_buys():
    sales = [
        _txn('A', 2_000_000), _txn('B', 2_000_000), _txn('C', 2_000_000),
    ]
    buys = []
    ok, meta = cluster_gate(sales, buys, min_insiders=3, min_net_value=5_000_000)
    assert ok is True
    assert meta['distinct_insiders'] == 3
    assert meta['net_sell_value'] == 6_000_000


def test_cluster_gate_fails_2_insiders():
    sales = [_txn('A', 5_000_000), _txn('B', 5_000_000)]
    ok, meta = cluster_gate(sales, [], min_insiders=3, min_net_value=5_000_000)
    assert ok is False
    assert meta['distinct_insiders'] == 2


def test_cluster_gate_fails_under_value_threshold():
    sales = [_txn('A', 1_000_000), _txn('B', 1_000_000), _txn('C', 1_000_000)]
    ok, meta = cluster_gate(sales, [], min_insiders=3, min_net_value=5_000_000)
    assert ok is False
    assert meta['net_sell_value'] == 3_000_000


def test_cluster_gate_fails_with_any_buy():
    sales = [_txn('A', 2_000_000), _txn('B', 2_000_000), _txn('C', 2_000_000)]
    buys = [{'transactionType': 'P-Purchase', 'value': 100_000, 'reportingName': 'Z'}]
    ok, meta = cluster_gate(sales, buys, min_insiders=3, min_net_value=5_000_000)
    assert ok is False
    assert meta['buy_count'] == 1


def test_cluster_gate_distinct_insider_counting():
    """Same insider name counted once even with multiple txns."""
    sales = [
        _txn('A', 2_000_000), _txn('A', 2_000_000), _txn('A', 2_000_000),
        _txn('B', 2_000_000), _txn('C', 2_000_000),
    ]
    ok, meta = cluster_gate(sales, [], min_insiders=3, min_net_value=5_000_000)
    assert meta['distinct_insiders'] == 3
    assert meta['net_sell_value'] == 10_000_000


from src.strategies.implementations.s15_insider_opportunistic_short import (
    conviction_filter,
)


def test_conviction_filter_passes_on_personal_stake():
    """Single seller sold 10%+ of prior holdings."""
    sales = [{
        'reportingName': 'A',
        'role': 'officer: VP',
        'shares': 50_000,
        'sharesOwnedAfter': 400_000,
        'value': 5_000_000,
    }]
    ok, meta = conviction_filter(sales, min_personal_stake_pct=0.10)
    assert ok is True
    assert meta['top_seller_pct_of_holdings'] > 0.10
    assert meta['c_suite_present'] is False


def test_conviction_filter_passes_on_c_suite():
    """No personal-stake pass but CEO present → passes via role test."""
    sales = [
        {'reportingName': 'A', 'role': 'officer: CEO and Director',
         'shares': 1_000, 'sharesOwnedAfter': 100_000, 'value': 200_000},
    ]
    ok, meta = conviction_filter(sales, min_personal_stake_pct=0.10)
    assert ok is True
    assert meta['c_suite_present'] is True


def test_conviction_filter_passes_on_cfo():
    sales = [
        {'reportingName': 'B', 'role': 'officer: Chief Financial Officer',
         'shares': 100, 'sharesOwnedAfter': 10_000, 'value': 20_000},
    ]
    ok, _ = conviction_filter(sales, min_personal_stake_pct=0.10)
    assert ok is True


def test_conviction_filter_passes_on_chair():
    sales = [
        {'reportingName': 'C', 'role': 'director: Chairman of the Board',
         'shares': 100, 'sharesOwnedAfter': 10_000, 'value': 20_000},
    ]
    ok, _ = conviction_filter(sales, min_personal_stake_pct=0.10)
    assert ok is True


def test_conviction_filter_fails_when_all_low_stake_and_no_c_suite():
    """No seller >=10% AND no C-suite → fail."""
    sales = [
        {'reportingName': 'A', 'role': 'officer: VP Engineering',
         'shares': 1_000, 'sharesOwnedAfter': 100_000, 'value': 200_000},
        {'reportingName': 'B', 'role': 'officer: SVP Sales',
         'shares': 2_000, 'sharesOwnedAfter': 200_000, 'value': 400_000},
    ]
    ok, meta = conviction_filter(sales, min_personal_stake_pct=0.10)
    assert ok is False
    assert meta['c_suite_present'] is False


def test_conviction_filter_missing_shares_owned_after():
    """Missing sharesOwnedAfter → that seller skipped for stake test but role still checked."""
    sales = [
        {'reportingName': 'CEO Person', 'role': 'officer: CEO',
         'shares': 1_000, 'sharesOwnedAfter': None, 'value': 50_000},
    ]
    ok, meta = conviction_filter(sales, min_personal_stake_pct=0.10)
    assert ok is True
    assert meta['c_suite_present'] is True


import os
import numpy as np
from src.strategies.implementations.s15_insider_opportunistic_short import (
    OpportunisticInsiderShort,
)


def _make_prices(tickers=('AAA',), days=30):
    idx = pd.date_range('2026-04-01', periods=days, freq='D')
    return pd.DataFrame({t: np.linspace(100, 110, days) for t in tickers}, index=idx)


def test_strategy_metadata():
    s = OpportunisticInsiderShort()
    assert s.id == 'S15_insider_opportunistic_short'
    assert s.tier == 2
    assert s.active_in_regimes == ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    assert s.signal_frequency == 'daily'


def test_strategy_default_parameters():
    s = OpportunisticInsiderShort()
    p = s.default_parameters()
    assert p['min_insiders'] == 3
    assert p['min_net_sell_value'] == 5_000_000
    assert p['min_opportunistic_count'] == 2
    assert p['min_personal_stake_pct'] == 0.10
    assert p['base_size_pct'] == 0.015
    assert p['max_concurrent_positions'] == 20
    assert p['wide_stop_pct'] == 0.15
    assert p['cooldown_after_stop_days'] == 30
    assert p['short_lookback_days'] == 30


def test_generate_signals_empty_when_gate_off(monkeypatch):
    """No env var → empty signals."""
    monkeypatch.delenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', raising=False)
    s = OpportunisticInsiderShort()
    prices = _make_prices(['AAA', 'BBB'])
    regime = {'state': 'LOW_VOL'}
    signals = s.generate_signals(prices, regime, ['AAA', 'BBB'], aux_data={})
    assert signals == []


def test_generate_signals_empty_in_crisis_regime(monkeypatch):
    """CRISIS regime excluded from active_in_regimes — emit nothing."""
    monkeypatch.setenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', '1')
    s = OpportunisticInsiderShort()
    prices = _make_prices(['AAA'])
    regime = {'state': 'CRISIS'}
    signals = s.generate_signals(prices, regime, ['AAA'], aux_data={})
    assert signals == []
