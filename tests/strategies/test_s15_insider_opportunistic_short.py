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
    assert p['min_insiders'] == 5
    assert p['min_net_sell_value'] == 20_000_000
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


def _build_aux_for_cluster(ticker, sales, history_per_seller):
    """Build aux_data with both short and long insider slices.

    The production aux loader serves a 450-day trailing window in
    `insider_history_long` that does NOT contain the same-day cluster sales
    (those live only in `insider_txns`, the 45-day current-window slice).
    Mirror that semantics in the fixture: long-history slice carries only
    the per-seller historical txns, not the current cluster.
    """
    return {
        'insider_txns':         {ticker: sales},
        'insider_history_long': {ticker: sum(history_per_seller.values(), [])},
    }


def _seller_history(name, role, n_quarters_with_sales, value_each=1_000_000):
    """Build a synthetic seller history with N distinct quarters in the t-15..t-3 window."""
    txns = []
    quarter_dates = [
        '2025-04-20',
        '2025-07-20',
        '2025-10-20',
        '2026-01-20',
    ]
    for d in quarter_dates[:n_quarters_with_sales]:
        txns.append({
            'transactionDate':  d,
            'transactionType':  'S-Sale',
            'reportingName':    name,
            'role':             role,
            'value':            value_each,
            'shares':           5_000,
            'sharesOwnedAfter': 100_000,
        })
    return txns


def test_generate_signals_fires_on_opportunistic_cluster_with_c_suite(monkeypatch):
    """Cluster: 5 insiders, $25M, 0 buys, opportunistic, CEO present → SHORT signal."""
    monkeypatch.setenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', '1')
    s = OpportunisticInsiderShort()

    idx = pd.date_range('2026-04-16', periods=30, freq='D')
    prices = pd.DataFrame({'TGT': np.linspace(100, 105, 30)}, index=idx)

    sales = [
        {'transactionDate': '2026-05-01', 'transactionType': 'S-Sale',
         'reportingName': 'CEO Person', 'role': 'officer: CEO',
         'value': 5_000_000, 'shares': 10_000, 'sharesOwnedAfter': 100_000},
        {'transactionDate': '2026-05-05', 'transactionType': 'S-Sale',
         'reportingName': 'VP Alice', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 8_000, 'sharesOwnedAfter': 80_000},
        {'transactionDate': '2026-05-08', 'transactionType': 'S-Sale',
         'reportingName': 'VP Bob', 'role': 'officer: SVP',
         'value': 5_000_000, 'shares': 6_000, 'sharesOwnedAfter': 70_000},
        {'transactionDate': '2026-05-12', 'transactionType': 'S-Sale',
         'reportingName': 'VP Carol', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 5_000, 'sharesOwnedAfter': 50_000},
        {'transactionDate': '2026-05-13', 'transactionType': 'S-Sale',
         'reportingName': 'VP Dave', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 4_000, 'sharesOwnedAfter': 40_000},
    ]
    history_per_seller = {
        'CEO Person': _seller_history('CEO Person', 'officer: CEO', n_quarters_with_sales=1),
        'VP Alice':   _seller_history('VP Alice',   'officer: VP',  n_quarters_with_sales=1),
        'VP Bob':     _seller_history('VP Bob',     'officer: SVP', n_quarters_with_sales=1),
        'VP Carol':   _seller_history('VP Carol',   'officer: VP',  n_quarters_with_sales=1),
        'VP Dave':    _seller_history('VP Dave',    'officer: VP',  n_quarters_with_sales=1),
    }
    aux = _build_aux_for_cluster('TGT', sales, history_per_seller)
    regime = {'state': 'LOW_VOL'}

    signals = s.generate_signals(prices, regime, ['TGT'], aux_data=aux)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.ticker == 'TGT'
    assert sig.direction == 'SHORT'
    assert sig.confidence == 'HIGH'
    entry = sig.entry_price
    assert sig.stop_loss >= entry * 1.15 - 0.01
    assert sig.target_1 is None
    assert sig.target_2 is None
    assert sig.target_3 is None
    assert sig.signal_params['cluster_kind'] == 'SELL_OPPORTUNISTIC'
    assert sig.signal_params['opportunistic_count'] >= 2


def test_generate_signals_does_not_fire_on_routine_cluster(monkeypatch):
    """Same cluster sizes but all 5 sellers classify as routine → no signal."""
    monkeypatch.setenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', '1')
    s = OpportunisticInsiderShort()
    idx = pd.date_range('2026-04-16', periods=30, freq='D')
    prices = pd.DataFrame({'TGT': np.linspace(100, 105, 30)}, index=idx)
    sales = [
        {'transactionDate': '2026-05-01', 'transactionType': 'S-Sale',
         'reportingName': 'Routine A', 'role': 'officer: CEO',
         'value': 5_000_000, 'shares': 10_000, 'sharesOwnedAfter': 100_000},
        {'transactionDate': '2026-05-05', 'transactionType': 'S-Sale',
         'reportingName': 'Routine B', 'role': 'officer: CFO',
         'value': 5_000_000, 'shares': 8_000, 'sharesOwnedAfter': 80_000},
        {'transactionDate': '2026-05-08', 'transactionType': 'S-Sale',
         'reportingName': 'Routine C', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 6_000, 'sharesOwnedAfter': 70_000},
        {'transactionDate': '2026-05-12', 'transactionType': 'S-Sale',
         'reportingName': 'Routine D', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 5_000, 'sharesOwnedAfter': 50_000},
        {'transactionDate': '2026-05-13', 'transactionType': 'S-Sale',
         'reportingName': 'Routine E', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 4_000, 'sharesOwnedAfter': 40_000},
    ]
    history_per_seller = {
        'Routine A': _seller_history('Routine A', 'officer: CEO', 4),
        'Routine B': _seller_history('Routine B', 'officer: CFO', 4),
        'Routine C': _seller_history('Routine C', 'officer: VP',  4),
        'Routine D': _seller_history('Routine D', 'officer: VP',  4),
        'Routine E': _seller_history('Routine E', 'officer: VP',  4),
    }
    aux = _build_aux_for_cluster('TGT', sales, history_per_seller)
    regime = {'state': 'LOW_VOL'}
    signals = s.generate_signals(prices, regime, ['TGT'], aux_data=aux)
    assert signals == []


def test_generate_signals_fails_cluster_with_offsetting_buy(monkeypatch):
    """5 sellers with $25M but 1 buy in the same window → fail Stage 1."""
    monkeypatch.setenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', '1')
    s = OpportunisticInsiderShort()
    idx = pd.date_range('2026-04-16', periods=30, freq='D')
    prices = pd.DataFrame({'TGT': np.linspace(100, 105, 30)}, index=idx)
    sales = [
        {'transactionDate': '2026-05-01', 'transactionType': 'S-Sale',
         'reportingName': 'A', 'role': 'officer: CEO',
         'value': 5_000_000, 'shares': 10_000, 'sharesOwnedAfter': 100_000},
        {'transactionDate': '2026-05-05', 'transactionType': 'S-Sale',
         'reportingName': 'B', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 8_000, 'sharesOwnedAfter': 80_000},
        {'transactionDate': '2026-05-08', 'transactionType': 'S-Sale',
         'reportingName': 'C', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 6_000, 'sharesOwnedAfter': 70_000},
        {'transactionDate': '2026-05-10', 'transactionType': 'S-Sale',
         'reportingName': 'D', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 5_000, 'sharesOwnedAfter': 50_000},
        {'transactionDate': '2026-05-11', 'transactionType': 'S-Sale',
         'reportingName': 'F', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 4_000, 'sharesOwnedAfter': 40_000},
        # offsetting buy
        {'transactionDate': '2026-05-09', 'transactionType': 'P-Purchase',
         'reportingName': 'E', 'role': 'officer: VP', 'value': 500_000,
         'shares': 1_000, 'sharesOwnedAfter': 5_000},
    ]
    history_per_seller = {
        'A': _seller_history('A', 'officer: CEO', 1),
        'B': _seller_history('B', 'officer: VP', 1),
        'C': _seller_history('C', 'officer: VP', 1),
        'D': _seller_history('D', 'officer: VP', 1),
        'F': _seller_history('F', 'officer: VP', 1),
    }
    aux = _build_aux_for_cluster('TGT', sales, history_per_seller)
    regime = {'state': 'LOW_VOL'}
    signals = s.generate_signals(prices, regime, ['TGT'], aux_data=aux)
    assert signals == []


def test_generate_signals_respects_cooldown(monkeypatch):
    """If ticker has a recent stop-out within cooldown window, skip."""
    monkeypatch.setenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', '1')
    s = OpportunisticInsiderShort()
    idx = pd.date_range('2026-04-16', periods=30, freq='D')
    prices = pd.DataFrame({'TGT': np.linspace(100, 105, 30)}, index=idx)
    sales = [
        {'transactionDate': '2026-05-01', 'transactionType': 'S-Sale',
         'reportingName': 'A', 'role': 'officer: CEO',
         'value': 5_000_000, 'shares': 10_000, 'sharesOwnedAfter': 100_000},
        {'transactionDate': '2026-05-05', 'transactionType': 'S-Sale',
         'reportingName': 'B', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 8_000, 'sharesOwnedAfter': 80_000},
        {'transactionDate': '2026-05-08', 'transactionType': 'S-Sale',
         'reportingName': 'C', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 6_000, 'sharesOwnedAfter': 70_000},
        {'transactionDate': '2026-05-12', 'transactionType': 'S-Sale',
         'reportingName': 'D', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 5_000, 'sharesOwnedAfter': 50_000},
        {'transactionDate': '2026-05-13', 'transactionType': 'S-Sale',
         'reportingName': 'E', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 4_000, 'sharesOwnedAfter': 40_000},
    ]
    history_per_seller = {
        'A': _seller_history('A', 'officer: CEO', 1),
        'B': _seller_history('B', 'officer: VP', 1),
        'C': _seller_history('C', 'officer: VP', 1),
        'D': _seller_history('D', 'officer: VP', 1),
        'E': _seller_history('E', 'officer: VP', 1),
    }
    aux = _build_aux_for_cluster('TGT', sales, history_per_seller)
    # Recent stop-out 10 days ago — within 30-day cooldown
    aux['recent_stop_outs'] = {'TGT': pd.Timestamp('2026-05-05')}
    regime = {'state': 'LOW_VOL'}
    signals = s.generate_signals(prices, regime, ['TGT'], aux_data=aux)
    assert signals == []


def test_generate_signals_caps_at_max_concurrent(monkeypatch):
    """25 qualifying tickers → only top 20 by score are emitted."""
    monkeypatch.setenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', '1')
    s = OpportunisticInsiderShort()
    tickers = [f'T{i:02d}' for i in range(25)]
    idx = pd.date_range('2026-04-16', periods=30, freq='D')
    prices = pd.DataFrame(
        {t: np.linspace(100, 105, 30) for t in tickers}, index=idx,
    )
    sales_per_ticker = {}
    history_per_ticker = {}
    for i, t in enumerate(tickers):
        v = (i + 1) * 4_000_000
        sales_per_ticker[t] = [
            {'transactionDate': '2026-05-01', 'transactionType': 'S-Sale',
             'reportingName': f'A{i}', 'role': 'officer: CEO',
             'value': v, 'shares': 10_000, 'sharesOwnedAfter': 100_000},
            {'transactionDate': '2026-05-05', 'transactionType': 'S-Sale',
             'reportingName': f'B{i}', 'role': 'officer: VP',
             'value': v, 'shares': 8_000, 'sharesOwnedAfter': 80_000},
            {'transactionDate': '2026-05-08', 'transactionType': 'S-Sale',
             'reportingName': f'C{i}', 'role': 'officer: VP',
             'value': v, 'shares': 6_000, 'sharesOwnedAfter': 70_000},
            {'transactionDate': '2026-05-12', 'transactionType': 'S-Sale',
             'reportingName': f'D{i}', 'role': 'officer: VP',
             'value': v, 'shares': 5_000, 'sharesOwnedAfter': 50_000},
            {'transactionDate': '2026-05-13', 'transactionType': 'S-Sale',
             'reportingName': f'E{i}', 'role': 'officer: VP',
             'value': v, 'shares': 4_000, 'sharesOwnedAfter': 40_000},
        ]
        history_per_ticker[t] = [
            *_seller_history(f'A{i}', 'officer: CEO', 1, value_each=v),
            *_seller_history(f'B{i}', 'officer: VP',  1, value_each=v),
            *_seller_history(f'C{i}', 'officer: VP',  1, value_each=v),
            *_seller_history(f'D{i}', 'officer: VP',  1, value_each=v),
            *_seller_history(f'E{i}', 'officer: VP',  1, value_each=v),
        ]
    aux = {
        'insider_txns':         sales_per_ticker,
        'insider_history_long': history_per_ticker,
    }
    regime = {'state': 'LOW_VOL'}
    signals = s.generate_signals(prices, regime, tickers, aux_data=aux)
    assert len(signals) == 20
    top_tickers = {sig.ticker for sig in signals[:5]}
    assert 'T24' in top_tickers
    assert 'T23' in top_tickers


def test_ablation_classifier_disabled_lets_routine_clusters_through(monkeypatch):
    """When OPENCLAW_S15_DISABLE_OPPORTUNISTIC_CLASSIFIER=1, routine clusters fire."""
    monkeypatch.setenv('OPENCLAW_S15_INSIDER_OPPORTUNISTIC', '1')
    monkeypatch.setenv('OPENCLAW_S15_DISABLE_OPPORTUNISTIC_CLASSIFIER', '1')
    s = OpportunisticInsiderShort()
    idx = pd.date_range('2026-04-16', periods=30, freq='D')
    prices = pd.DataFrame({'TGT': np.linspace(100, 105, 30)}, index=idx)
    sales = [
        {'transactionDate': '2026-05-01', 'transactionType': 'S-Sale',
         'reportingName': 'Routine A', 'role': 'officer: CEO',
         'value': 5_000_000, 'shares': 10_000, 'sharesOwnedAfter': 100_000},
        {'transactionDate': '2026-05-05', 'transactionType': 'S-Sale',
         'reportingName': 'Routine B', 'role': 'officer: CFO',
         'value': 5_000_000, 'shares': 8_000, 'sharesOwnedAfter': 80_000},
        {'transactionDate': '2026-05-08', 'transactionType': 'S-Sale',
         'reportingName': 'Routine C', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 6_000, 'sharesOwnedAfter': 70_000},
        {'transactionDate': '2026-05-12', 'transactionType': 'S-Sale',
         'reportingName': 'Routine D', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 5_000, 'sharesOwnedAfter': 50_000},
        {'transactionDate': '2026-05-13', 'transactionType': 'S-Sale',
         'reportingName': 'Routine E', 'role': 'officer: VP',
         'value': 5_000_000, 'shares': 4_000, 'sharesOwnedAfter': 40_000},
    ]
    history_per_seller = {
        'Routine A': _seller_history('Routine A', 'officer: CEO', 4),
        'Routine B': _seller_history('Routine B', 'officer: CFO', 4),
        'Routine C': _seller_history('Routine C', 'officer: VP',  4),
        'Routine D': _seller_history('Routine D', 'officer: VP',  4),
        'Routine E': _seller_history('Routine E', 'officer: VP',  4),
    }
    aux = _build_aux_for_cluster('TGT', sales, history_per_seller)
    regime = {'state': 'LOW_VOL'}
    signals = s.generate_signals(prices, regime, ['TGT'], aux_data=aux)
    # With classifier disabled, this routine cluster passes Stages 1, 2 (ablated), 3
    assert len(signals) == 1
    assert signals[0].signal_params['opportunistic_count'] == 0
    assert signals[0].signal_params['routine_count'] == 5
