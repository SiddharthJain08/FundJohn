"""SP-7 A3 — total-return close from split-adjusted prices + cash dividends.

DOCUMENTED HELPER ONLY in Phase A (spec §3 A3): backtest engines adopt it in
their own projects. Not wired into any engine here.

tr_factor convention
--------------------
On an ex-date, a holder of the stock receives a cash dividend. The
split-adjusted close on that day already reflects the price drop (market
convention: price opens ex-dividend). The total-return factor on ex-date d is:

    tr_factor[d] = (close[d] + dividend[d]) / close[d]

For non-dividend days tr_factor = 1.0 (no dividend → factor is neutral).

Cumulative reinvestment shifts the factor forward: tr_close[d] = close[d] *
cumprod(tr_factor)[d-1]. This means the ex-date's dividend is credited to
every *subsequent* close (you can't reinvest a dividend until after you
receive it), giving a monotonically increasing total-return index relative
to the price index as long as dividends are positive.

Example (see tests/backtest/test_total_return.py):
    close[2026-01-02] = 100, close[2026-01-05] = 99, div = 1
    tr_factor[2026-01-05] = (99+1)/99 = 100/99 ≈ 1.0101
    tr_close[2026-01-06] = 100.0 * (100/99) ≈ 101.01
"""
from __future__ import annotations

import pandas as pd


def total_return_close(prices: pd.DataFrame, actions: pd.DataFrame,
                       ticker: str) -> pd.DataFrame:
    """Returns DataFrame[date, close, tr_factor, tr_close] for one ticker.

    Parameters
    ----------
    prices : DataFrame with columns [ticker, date, close] (all tickers OK;
        filtered to *ticker* internally).
    actions : DataFrame with columns [symbol, action_type, ex_date, cash_amount].
        Rows with action_type != 'cash_dividend' are ignored.
    ticker : str — the ticker to compute total-return for.

    Returns
    -------
    DataFrame with columns:
        date       — str or original dtype, sorted ascending
        close      — split-adjusted close (input, unchanged)
        tr_factor  — (close + dividend) / close on ex-date; 1.0 otherwise
        tr_close   — cumulative-reinvestment total-return close
    """
    px = (prices[prices.ticker == ticker][["date", "close"]]
          .sort_values("date").reset_index(drop=True))
    div = actions[(actions.symbol == ticker)
                  & (actions.action_type == "cash_dividend")]
    div_map = dict(zip(div.ex_date.astype(str), div.cash_amount.astype(float)))
    px["dividend"] = px.date.astype(str).map(div_map).fillna(0.0)
    px["tr_factor"] = (px.close + px.dividend) / px.close
    # Reinvest: each ex-date's factor lifts every SUBSEQUENT close.
    # cumprod().shift(1) means: "what was the compounded adjustment BEFORE
    # this bar?" — so day-1 gets factor 1.0 (no prior adjustments).
    cum = px.tr_factor.cumprod().shift(1).fillna(1.0)
    px["tr_close"] = px.close * cum
    return px[["date", "close", "tr_factor", "tr_close"]]
