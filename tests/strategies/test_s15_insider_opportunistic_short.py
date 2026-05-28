import pandas as pd
import pytest
from src.strategies.implementations.s15_insider_opportunistic_short import (
    classify_insider,
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
