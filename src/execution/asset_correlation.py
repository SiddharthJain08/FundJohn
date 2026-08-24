"""Asset-level (ticker) price-return correlation for the cluster-cap filter.

Memory-safe sliced read of data/master/prices.parquet via pyarrow predicate
pushdown (NEVER loads the full panel). Pearson on daily close-to-close returns
over a trailing window. Pure correlation math is separated for unit testing.

Task P1 (shadow-first, docs/.superpowers/sdd/2026-08-24-five-repo-adoptions/
task-P1-brief.md) adds an optional Ledoit-Wolf (constant-correlation target)
shrinkage path behind OPENCLAW_ASSET_CORR_LW — see price_return_corr.
"""
from __future__ import annotations
import math
import os
import sys

from execution import shrinkage

PARQUET = "/root/openclaw/data/master/prices.parquet"
MIN_OBS = 20            # min overlapping returns to trust a pair; else 0.0
ASSET_CORR_LW_ENV = 'OPENCLAW_ASSET_CORR_LW'
# Dense-panel (LW path only) column-coverage floor. Convention lifted from
# MIN_OBS_FRAC in scripts/run_pyportfolioopt_shadow.py's own weekday-filter +
# coverage-drop panel builder — kept as a local constant here rather than an
# import so this production sizing module has no dependency on a top-level
# script. Legacy corr_from_returns / MIN_OBS above are untouched by this.
MIN_OBS_FRAC = 0.9


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
    `window`+1 trading days up to `as_of` (default: today). pyarrow predicate
    pushdown bounded to a trailing calendar window; never materializes the full
    panel or a ticker's full history. Returns {ticker: {date_str: ret}}."""
    import datetime
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    tickers = list(tickers)
    if not tickers:
        return {}
    # Trailing calendar-window floor: ~2x the trading-day window in calendar
    # days (+buffer) comfortably covers window+1 trading days across weekends/
    # holidays, so we never read a ticker's full history.
    anchor_d = (datetime.date.fromisoformat(str(as_of)) if as_of is not None
                else datetime.date.today())
    lo = (anchor_d - datetime.timedelta(days=int(window) * 2 + 10)).isoformat()
    flt = (pc.field("ticker").isin(tickers)
           & (pc.field("date") >= lo)
           & (pc.field("date") <= anchor_d.isoformat()))
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


def _asset_corr_lw_mode() -> str:
    """unset/'' -> '0' (DEFAULT: legacy path, ZERO new imports or computation
    — controller ruling 2026-08-24, see task-P1-report.md fix log). 'shadow'
    and '1' are opt-in modes only: the operator must set OPENCLAW_ASSET_CORR_LW
    deliberately (e.g. in .env) to arm either one. Any unrecognized value also
    falls back to '0' (defensive) so a typo can never accidentally arm the
    shadow fit (which imports pypfopt — a measured ~0.774s cold import — and
    is not something that may run on every live sizer cycle without explicit
    opt-in on this 2-core box)."""
    v = os.environ.get(ASSET_CORR_LW_ENV, '')
    return v if v in ('shadow', '1', '0') else '0'


def _dense_panel_from_returns(returns: dict[str, dict[str, float]]):
    """dates x tickers DENSE (no-NaN) panel from {ticker: {date: ret}}.

    Three steps, in order (mirrors the weekday-filter + MIN_OBS_FRAC
    convention in scripts/run_pyportfolioopt_shadow.py):

    (a) Weekday filter: if ANY ticker carries a weekend (Sat/Sun)
        observation — e.g. a 24/7 crypto ticker mixed in with weekday-only
        equities — every weekday-only column is NaN on those rows by
        construction, so a weekend row can never be part of a dense
        (all-tickers) panel; dropping them up front stops them from
        needlessly shrinking the trading-calendar row count the coverage
        check in (b) measures against. A panel with no weekend rows at all
        is left untouched.
    (b) Coverage filter: tickers whose non-NaN coverage over that
        (weekday-filtered) window is < MIN_OBS_FRAC (0.9) are dropped
        BEFORE intersecting dates — including one would only poison the
        N-way date intersection in (c) for every other, well-covered
        ticker, while its own pairs get forced to 0.0 post-fit regardless
        (see _lw_corr_same_shape's `fitted` check). Logs
        `[asset_corr_lw] panel: kept <k>/<n> tickers (coverage>=0.9),
        rows=<r>` — emitted ONLY when at least one ticker was actually
        dropped (k < n): the well-covered common case stays silent so it
        doesn't add a second `[asset_corr_lw]`-prefixed line alongside the
        shadow/'1' summary line callers already emit.
    (c) THEN drop rows with any remaining NaN — forms the truly dense
        (no-NaN) panel handed to shrinkage.lw_corr.

    None if fewer than 2 well-covered tickers remain after (b) or the
    row intersection in (c) is empty; shrinkage.py's own MIN_ROWS/MIN_COLS
    floor handles the remaining thinness cases (e.g. too few rows left)."""
    if len(returns) < 2:
        return None
    import pandas as pd
    df = pd.DataFrame(returns)
    if df.empty:
        return None
    dow = pd.to_datetime(df.index).dayofweek
    if (dow >= 5).any():
        df = df.loc[dow < 5]
        if df.empty:
            return None
    n_total = len(df.columns)
    min_count = MIN_OBS_FRAC * len(df)
    well_covered = [c for c in df.columns if df[c].notna().sum() >= min_count]
    if len(well_covered) < n_total:
        print(f"[asset_corr_lw] panel: kept {len(well_covered)}/{n_total} tickers "
              f"(coverage>={MIN_OBS_FRAC}), rows={len(df)}", file=sys.stderr)
    if len(well_covered) < 2:
        return None
    dense = df[well_covered].dropna(axis=0, how='any')
    if dense.empty:
        return None
    return dense


def _lw_corr_same_shape(returns: dict[str, dict[str, float]]):
    """LW-shrunk correlations reshaped into the SAME nested-dict shape as
    corr_from_returns (sorted tickers, diagonal 1.0, symmetric, clipped to
    [-1, 1]). Pairs with < MIN_OBS overlapping observations are forced to 0.0
    AFTER shrinkage (preserves the legacy thin-evidence rule) — as are pairs
    touching a ticker the dense panel had to drop (`a not in fitted or b not
    in fitted` below), whatever the drop reason — thin MIN_OBS overlap,
    sub-MIN_OBS_FRAC coverage, or an all-NaN column shrinkage._clean_panel
    strips. None if the panel is too thin for shrinkage.lw_corr to fit at
    all (caller falls back to legacy)."""
    panel = _dense_panel_from_returns(returns)
    if panel is None:
        return None
    corr, _gamma = shrinkage.lw_corr(panel)
    if corr is None:
        return None
    tickers = sorted(returns)
    fitted = set(corr.index)
    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for t in tickers:
        out[t][t] = 1.0
    for i, a in enumerate(tickers):
        da = returns[a]
        for b in tickers[i + 1:]:
            db = returns[b]
            common = set(da) & set(db)
            if len(common) < MIN_OBS or a not in fitted or b not in fitted:
                rho = 0.0
            else:
                rho = max(-1.0, min(1.0, float(corr.loc[a, b])))
            out[a][b] = out[b][a] = rho
    return out


def _log_asset_corr_lw_shadow(returns: dict[str, dict[str, float]],
                              legacy: dict[str, dict[str, float]]) -> None:
    """Best-effort: fit LW on the dense panel and log ONE comparison line to
    stderr. Never raises past this function; never touches `legacy`.

    NOTE on mean_abs_delta_rho: it compares two DIFFERENT samples, not just
    two estimators on the same data — legacy uses each pair's own
    pairwise-max overlap (up to `window` obs each), while the LW side uses
    the N-way dense intersection across every well-covered ticker (which can
    be a strict subset of any one pair's overlap). Read the number as
    "shrinkage + sample-narrowing combined", not shrinkage alone.

    When the dense panel can't be formed or shrinkage.lw_corr can't fit it
    (too few well-covered tickers / rows), this logs a distinct
    '[asset_corr_lw] shadow: skipped ...' line instead of silently emitting
    nothing — so an operator can tell "shadow ran, nothing to compare" apart
    from "shadow never ran" or "shadow raised"."""
    panel = _dense_panel_from_returns(returns)
    if panel is None:
        print(f"[asset_corr_lw] shadow: skipped (dense panel too thin, "
              f"n_requested={len(returns)})", file=sys.stderr)
        return
    corr, gamma = shrinkage.lw_corr(panel)
    if corr is None:
        print(f"[asset_corr_lw] shadow: skipped (lw_corr could not fit, "
              f"n_dense={panel.shape[1]} rows={panel.shape[0]})", file=sys.stderr)
        return
    cols = list(corr.columns)
    deltas = []
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            leg_rho = legacy.get(a, {}).get(b)
            if leg_rho is None:
                continue
            deltas.append(abs(float(corr.loc[a, b]) - leg_rho))
    mean_abs_delta = (sum(deltas) / len(deltas)) if deltas else 0.0
    print(f"[asset_corr_lw] shadow: n={len(cols)} "
          f"mean_abs_delta_rho={mean_abs_delta:.4f} gamma={gamma:.3f}",
          file=sys.stderr)


def price_return_corr(tickers, window=63, as_of=None):
    """Ticker x ticker Pearson correlation of daily returns over the trailing
    window. Fail-open: any read/compute error -> {} (caller applies no capping).

    OPENCLAW_ASSET_CORR_LW (task P1, shadow-first) gates an optional
    Ledoit-Wolf (constant-correlation target) shrinkage path:
      unset/'' (DEFAULT) or '0' — legacy path ONLY: zero LW computation, zero
        pypfopt import, zero logging. This is the default per controller
        ruling 2026-08-24 — shadow mode must be an operator opt-in, never the
        out-of-the-box behavior, on a 2-core box with an OOM history.
      'shadow' (opt-in) — returns the legacy result UNCHANGED; best-effort,
        in a try/except that can never affect the result, also fits LW on the
        dense panel built from the same slices and logs one
        `[asset_corr_lw] shadow: ...` comparison line to stderr (or
        `[asset_corr_lw] shadow failed: <err>` on any exception).
      '1' — returns the LW-shrunk correlations in the legacy output shape
        (thin pairs still forced to 0.0 post-shrinkage); falls back to the
        legacy result if LW can't be fit (e.g. the dense panel is too thin).
    """
    try:
        returns = _load_returns(tickers, window, as_of)
        legacy = corr_from_returns(returns)
    except Exception:
        return {}

    mode = _asset_corr_lw_mode()
    if mode == '0':
        return legacy

    if mode == '1':
        try:
            lw = _lw_corr_same_shape(returns)
        except Exception:
            lw = None
        return lw if lw is not None else legacy

    # 'shadow' (opt-in only, never the default): legacy result is
    # authoritative; LW runs best-effort purely for the stderr comparison
    # line and can never affect the result.
    try:
        _log_asset_corr_lw_shadow(returns, legacy)
    except Exception as e:
        print(f"[asset_corr_lw] shadow failed: {e}", file=sys.stderr)
    return legacy
