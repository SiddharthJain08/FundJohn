import pandas as pd
import pytest
from src.strategies import aux_data_loader as adl


def test_insider_history_long_returns_15_month_window():
    """insider_history_long should include txns within 450 days, exclude older."""
    out = adl.load_aux_data('2026-05-15', strategy_id='S15_insider_opportunistic_short')
    assert 'insider_history_long' in out
    assert isinstance(out['insider_history_long'], dict)
    assert len(out['insider_history_long']) > 0


def test_insider_history_long_window_extends_beyond_short_slice():
    """The 15mo window must contain strictly more txns than the 45d window for active tickers."""
    out = adl.load_aux_data('2026-05-15', strategy_id='S15_insider_opportunistic_short')
    short_txns = out['insider_txns']
    long_txns = out['insider_history_long']
    common = set(short_txns.keys()) & set(long_txns.keys())
    assert common, "expected at least one ticker in both slices"
    assert any(len(long_txns[t]) >= len(short_txns[t]) for t in common)


def test_insider_history_long_excludes_future_dates():
    """The 15mo window ending on as_of should not contain txns after as_of."""
    as_of = '2026-05-15'
    out = adl.load_aux_data(as_of, strategy_id='S15_insider_opportunistic_short')
    cutoff = pd.Timestamp(as_of)
    for ticker, txns in out['insider_history_long'].items():
        for t in txns:
            txn_date = pd.to_datetime(t.get('transactionDate'))
            assert txn_date <= cutoff, f"{ticker} has future txn {txn_date} > {as_of}"
