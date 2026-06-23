"""Asset-level (ticker) price-return correlation for the cluster-cap filter.

Memory-safe sliced read of data/master/prices.parquet via pyarrow predicate
pushdown (NEVER loads the full panel). Pearson on daily close-to-close returns
over a trailing window. Pure correlation math is separated for unit testing.
"""
from __future__ import annotations
import math

PARQUET = "/root/openclaw/data/master/prices.parquet"
MIN_OBS = 20            # min overlapping returns to trust a pair; else 0.0


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def corr_from_returns(returns: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    """Pairwise Pearson on {ticker: {date: ret}}. Diagonal 1.0; symmetric.
    Pairs with < MIN_OBS overlapping dates -> 0.0 (never cluster on thin evidence)."""
    tickers = sorted(returns)
    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for t in tickers:
        out[t][t] = 1.0
    for i, a in enumerate(tickers):
        da = returns[a]
        for b in tickers[i + 1:]:
            db = returns[b]
            common = sorted(set(da) & set(db))
            if len(common) < MIN_OBS:
                rho = 0.0
            else:
                r = _pearson([da[d] for d in common], [db[d] for d in common])
                rho = 0.0 if r is None else max(-1.0, min(1.0, r))
            out[a][b] = out[b][a] = rho
    return out


def _load_returns(tickers, window, as_of=None):
    """Sliced read: daily close-to-close returns for `tickers` over the last
    `window`+1 trading days up to `as_of`. pyarrow predicate pushdown; never
    materializes the full panel. Returns {ticker: {date_str: ret}}."""
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    tickers = list(tickers)
    if not tickers:
        return {}
    flt = pc.field("ticker").isin(tickers)
    if as_of is not None:
        flt = flt & (pc.field("date") <= str(as_of))
    tbl = pq.read_table(PARQUET, columns=["ticker", "date", "close"], filters=flt)
    df = tbl.to_pandas()
    df["date"] = df["date"].astype(str)
    out: dict[str, dict[str, float]] = {}
    need = window + 1
    for tk, g in df.groupby("ticker"):
        g = g.sort_values("date").tail(need)
        closes = g["close"].astype(float).tolist()
        dates = g["date"].tolist()
        rets: dict[str, float] = {}
        for k in range(1, len(closes)):
            p = closes[k - 1]
            if p and p == p and closes[k] == closes[k]:   # nonzero + non-NaN
                rets[dates[k]] = closes[k] / p - 1.0
        out[str(tk)] = rets
    return out


def price_return_corr(tickers, window=63, as_of=None):
    """Ticker x ticker Pearson correlation of daily returns over the trailing
    window. Fail-open: any read/compute error -> {} (caller applies no capping)."""
    try:
        return corr_from_returns(_load_returns(tickers, window, as_of))
    except Exception:
        return {}
