"""Tests for SP-7 A3 total-return helper.

tr_factor convention: (close + dividend) / close on the ex-date row.
This means a $1 dividend on a $99 close gives tr_factor = 100/99 ≈ 1.0101,
NOT 1.0. The factor is then cumprod'd and shifted so each ex-date's dividend
is reinvested into every *subsequent* close (tr_close = close * cumprod(shift)).
"""
import pandas as pd
import pytest

from backtest.total_return import total_return_close


def test_dividend_adds_back_into_return_series():
    prices = pd.DataFrame({
        "ticker": ["X"] * 3,
        "date": ["2026-01-02", "2026-01-05", "2026-01-06"],
        "close": [100.0, 99.0, 100.0],   # split-adjusted closes
    })
    actions = pd.DataFrame({
        "symbol": ["X"], "action_type": ["cash_dividend"],
        "ex_date": ["2026-01-05"], "cash_amount": [1.0],
    })
    tr = total_return_close(prices, actions, "X")
    # tr_factor on ex-date = (99 + 1) / 99 = 100/99 ≈ 1.01010101
    assert tr.loc[tr.date == "2026-01-05", "tr_factor"].iloc[0] == pytest.approx(100 / 99)
    # tr_close on day-3: price return = +1% from 99→100, plus prior div factor
    # tr_close = 100.0 * (100/99) ≈ 101.01 > 100.0
    assert tr.loc[tr.date == "2026-01-06", "tr_close"].iloc[0] == pytest.approx(100.0 * (100 / 99))


def test_no_dividends_identity():
    prices = pd.DataFrame({
        "ticker": ["X"] * 2, "date": ["2026-01-02", "2026-01-05"],
        "close": [100.0, 101.0],
    })
    actions = pd.DataFrame(columns=["symbol", "action_type", "ex_date", "cash_amount"])
    tr = total_return_close(prices, actions, "X")
    assert tr.tr_close.tolist() == [100.0, 101.0]
