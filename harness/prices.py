"""Memory-safe sliced daily-OHLC loader + ATR. Never loads the full panel.

Schema adaptation (confirmed at build time against
/root/openclaw/data/master/prices.parquet):
  - columns are ticker,date,open,high,low,close,volume,vwap,transactions,source
  - `date` is a STRING (arrow large_string) in ISO 'YYYY-MM-DD' form, NOT a
    timestamp. ISO dates sort lexicographically == chronologically, so the
    pyarrow predicate compares `date` against plain string bounds (NOT
    pd.Timestamp, which would mismatch/raise on a string column), and the
    frame is kept with a string index throughout (no datetime conversion).
"""
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pyarrow.compute as pc

PARQUET = "/root/openclaw/data/master/prices.parquet"
COLS = ["ticker", "date", "open", "high", "low", "close"]


def load_daily(tickers, start, end):
    """Per-ticker date-indexed OHLC frame, filtered on read via pyarrow
    predicate pushdown (never materializes the full panel). Sorted ascending
    by date. `start`/`end` are inclusive ISO 'YYYY-MM-DD' string bounds.
    """
    tickers = list(tickers)
    flt = (pc.field("ticker").isin(tickers) &
           (pc.field("date") >= str(start)) &
           (pc.field("date") <= str(end)))
    tbl = pq.read_table(PARQUET, columns=COLS, filters=flt)  # predicate pushdown
    df = tbl.to_pandas()
    df["date"] = df["date"].astype(str)
    out = {}
    for tk, g in df.groupby("ticker"):
        out[str(tk)] = g.sort_values("date").set_index("date")
    return out


def atr(df, n=20, as_of=None):
    """Wilder/simple ATR over the last `n` daily bars up to and including
    `as_of` (default last row). `as_of` is an ISO string compared against the
    string date index. Returns nan if fewer than `n` bars are available.
    """
    if df is None or len(df) == 0:
        return float("nan")
    d = df if as_of is None else df.loc[:str(as_of)]
    if len(d) < n:
        return float("nan")
    h, l, c = d["high"].astype(float), d["low"].astype(float), d["close"].astype(float)
    pc_ = c.shift(1)
    tr = pd.concat([(h - l), (h - pc_).abs(), (l - pc_).abs()], axis=1).max(axis=1)
    return float(tr.tail(n).mean())
