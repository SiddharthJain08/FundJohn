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
