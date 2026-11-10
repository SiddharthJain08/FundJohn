"""
crypto_historical_regimes.py — deterministic realised-vol tier classifier for
crypto backtest regimes (crypto engine Phase 3, fixes D-2).

The equity analogue (historical_regimes.py) labels history from smoothed VIX.
Crypto trades 24/7 and has no VIX of record, and the live crypto HMM
(`crypto_regime_states`, hourly since 2026-05-26) has no opinion about earlier
history. Crypto backtests therefore partition on this table instead: the same
code on the same BTC-USD closes always yields the same per-day regime.

Method
------
    close          BTC-USD daily close from the prices master (hive-aware
                   filtered read via parquet_store.prices_read_table; never a
                   whole-file read).
    rv30           annualised realised vol = std(ddof=1) of daily log returns
                   over a RV_WINDOW-row rolling window × √ANNUALISE.
                   min_periods = RV_WINDOW − 1, so the first valid value is at
                   row 29 (29 returns from 30 closes) and rows 0..28 are
                   UNKNOWN. Windows are by row, not by calendar time (the
                   master has a few missing BTC days; a gap's return simply
                   spans it).
    rv30_smoothed  SMOOTH_DAYS-row rolling median of rv30 (min_periods=1).
    regime         monotone tiers on rv30_smoothed (upper bound inclusive):
                       ≤ t1 → LOW_VOL, ≤ t2 → TRANSITIONING, ≤ t3 → HIGH_VOL,
                       else CRISIS; NaN → UNKNOWN.

The cut-points t1..t3 are calibrated ONCE by
scripts/calibrate_crypto_regime_tiers.py so the tier frequencies over the
overlap window with the live HMM match its daily-mode state frequencies, then
FROZEN below. That script rewrites ONLY the marked `CRYPTO_RV_TIERS` block of
this file (between the `>>> CRYPTO_RV_TIERS` / `<<< CRYPTO_RV_TIERS` markers)
when run with --write; do not hand-edit inside the markers without updating
the calibration comment.

Output master: data/master/crypto_historical_regimes.parquet
(date, close, rv30, rv30_smoothed, regime), written via
parquet_store.append_dedup(key_cols=['date'], mode='replace') — append-only
(NEVER-DELETE invariant, CLAUDE.md). Rebuilt daily by the collector after the
prices collect (`--rebuild`).

Public API
----------
    load_btc_closes(start=None, end=None) -> pd.Series
    classify_history(start_date=None, end_date=None, *, closes=None, tiers=None)
    calibrate_tiers(rv_smoothed, target_freq) -> (t1, t2, t3)
    rebuild_cache(start_date=None, end_date=None) -> Path
    load_table() -> pd.DataFrame          # cached table (self-heals when missing or behind the prices master)
    regime_series(dates) -> pd.Series
    regime_for_date(d) -> str
"""
from __future__ import annotations

import math
import sys
from datetime import date as _date
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

ROOT  = Path(__file__).resolve().parent.parent.parent
CACHE = ROOT / 'data' / 'master' / 'crypto_historical_regimes.parquet'

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
COLUMNS = ['date', 'close', 'rv30', 'rv30_smoothed', 'regime']

BENCH       = 'BTC-USD'
RV_WINDOW   = 30
SMOOTH_DAYS = 5
ANNUALISE   = 365

# >>> CRYPTO_RV_TIERS (rewritten by scripts/calibrate_crypto_regime_tiers.py --write)
# calibrated 2026-11-10 against crypto_regime_states 2026-05-26..2026-11-10 (daily mode)
# targets: LOW_VOL 0.414, TRANSITIONING 0.308, HIGH_VOL 0.118, CRISIS 0.160
CRYPTO_RV_TIERS: tuple[tuple[float, str], ...] = (
    (0.3597795949784727, 'LOW_VOL'),
    (0.4309998535210531, 'TRANSITIONING'),
    (0.48576732147008633, 'HIGH_VOL'),
    # > t3 → CRISIS (the catch-all)
)
# <<< CRYPTO_RV_TIERS


def _classify_rv(rv_smoothed: float, tiers=None) -> str:
    tiers = CRYPTO_RV_TIERS if tiers is None else tiers
    if rv_smoothed is None or pd.isna(rv_smoothed):
        return 'UNKNOWN'
    for upper, label in tiers:
        if rv_smoothed <= upper:
            return label
    return 'CRISIS'


def load_btc_closes(start=None, end=None) -> pd.Series:
    """BTC-USD daily closes (Series indexed by Timestamp, name 'close') from
    the prices master — a ticker-filtered read, never the whole file."""
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))
    from src.data import parquet_store as _ps
    tbl = _ps.prices_read_table(['date', 'ticker', 'close'],
                                filters=[('ticker', '==', BENCH)])
    df = tbl.to_pandas()
    if df.empty:
        raise RuntimeError(f'prices master has no {BENCH} rows')
    df['date'] = pd.to_datetime(df['date'])
    df = (df.dropna(subset=['close']).sort_values('date')
            .drop_duplicates(subset=['date'], keep='last'))
    s = df.set_index('date')['close'].astype(float)
    if start is not None:
        s = s[s.index >= pd.Timestamp(start)]
    if end is not None:
        s = s[s.index <= pd.Timestamp(end)]
    s.name = 'close'
    return s


def classify_history(start_date: str | _date | None = None,
                     end_date:   str | _date | None = None, *,
                     closes: pd.Series | None = None,
                     tiers=None) -> pd.DataFrame:
    """date, close, rv30, rv30_smoothed, regime for the requested range.

    rv30 is computed over the FULL close series before the window is cut, so
    a windowed call still gets warm values on its first rows (when history
    exists). `tiers` defaults to the frozen CRYPTO_RV_TIERS (read at call
    time)."""
    if closes is None:
        closes = load_btc_closes()
    s = closes.dropna().astype(float).sort_index()
    s.index = pd.to_datetime(s.index)
    s = s[~s.index.duplicated(keep='last')]
    logret = np.log(s).diff()
    rv30 = (logret.rolling(RV_WINDOW, min_periods=RV_WINDOW - 1).std(ddof=1)
            * math.sqrt(ANNUALISE))
    rv_s = rv30.rolling(SMOOTH_DAYS, min_periods=1).median()
    df = pd.DataFrame({
        'date': s.index.date,
        'close': s.to_numpy(),
        'rv30': rv30.to_numpy(),
        'rv30_smoothed': rv_s.to_numpy(),
    })
    df['regime'] = [_classify_rv(v, tiers) for v in df['rv30_smoothed']]
    if start_date is not None:
        df = df[df['date'] >= pd.to_datetime(start_date).date()]
    if end_date is not None:
        df = df[df['date'] <= pd.to_datetime(end_date).date()]
    return df.reset_index(drop=True)[COLUMNS]


def calibrate_tiers(rv_smoothed: pd.Series,
                    target_freq: Mapping[str, float]) -> tuple[float, float, float]:
    """Cut-points (t1, t2, t3) such that, over the given series (already cut
    to the calibration window), the share of values ≤ t1 / ≤ t2 / ≤ t3 is the
    nearest achievable to the cumulative target LOW_VOL / +TRANSITIONING /
    +HIGH_VOL. Exact order statistics (no interpolation), so the inclusive-
    upper tier rule reproduces the counts; tie-aware (see below)."""
    if set(target_freq) != set(CANONICAL_REGIMES):
        raise ValueError(f'target_freq keys must be {CANONICAL_REGIMES}, got {sorted(target_freq)}')
    total = float(sum(target_freq.values()))
    if abs(total - 1.0) > 0.005 or any(v < 0 for v in target_freq.values()):
        raise ValueError(f'target frequencies must be ≥ 0 and sum to 1 (got {total:.4f})')
    vals = np.sort(pd.Series(rv_smoothed).dropna().astype(float).to_numpy())
    n = len(vals)
    if n < 10:
        raise ValueError(f'need ≥ 10 non-NaN rv values to calibrate, got {n}')
    cum = np.cumsum([target_freq['LOW_VOL'], target_freq['TRANSITIONING'],
                     target_freq['HIGH_VOL']]) / total
    # The 5-day rolling median repeats values, so a cut at sorted[k-1] can
    # admit more than k rows. Choose between that value and the next-lower
    # distinct value, whichever count (#values ≤ cut) is closer to n·cum.
    out = []
    for c in cum:
        want = n * float(c)
        k = int(min(max(round(want), 1), n))
        v = float(vals[k - 1])
        cands = [v]
        lower = vals[vals < v]
        if len(lower):
            cands.append(float(lower[-1]))
        out.append(min(cands, key=lambda t: (abs(int(np.searchsorted(vals, t, 'right')) - want), -t)))
    return tuple(out)  # type: ignore[return-value]


def rebuild_cache(start_date: str | None = None,
                  end_date:   str | None = None) -> Path:
    """Upsert the classified history into CACHE via append_dedup(key=['date'],
    mode='replace'): rows for recomputed dates are replaced, no date is ever
    dropped (NEVER-DELETE). Idempotent."""
    df = classify_history(start_date, end_date)
    df['date'] = pd.to_datetime(df['date']).dt.date
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))
    from src.data import parquet_store as _ps
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    _ps.append_dedup(CACHE, df, key_cols=['date'], mode='replace')
    global _cache
    _cache = None
    return CACHE


_cache: pd.DataFrame | None = None


def _source_max_date() -> _date | None:
    """Newest `date` in the prices master from footer stats (no data read)."""
    if str(ROOT) not in sys.path:
        sys.path.append(str(ROOT))
    from src.data import parquet_store as _ps
    mx = _ps._max_date_from_footers([_ps.PRICES_PATH])
    return None if mx is None else pd.to_datetime(mx).date()


def _cache_is_stale() -> bool:
    """True only when the prices master has advanced past the cache — the
    equity module's source-relative rule. A calendar-age rule would rebuild
    in every consumer after a weekend whose BTC bars never landed (the cache
    cannot advance past the source), each doing a prices read + master merge."""
    try:
        cached = pd.to_datetime(pd.read_parquet(CACHE, columns=['date'])['date']).max()
        src = _source_max_date()
    except Exception:  # noqa: BLE001 — an unreadable probe must not break consumers
        return False
    if src is None:
        return False
    return pd.isna(cached) or cached.date() < src


def load_table() -> pd.DataFrame:
    """The cached table sorted by date, (re)building it when missing or stale.
    A failed rebuild falls back to the existing (stale) cache."""
    global _cache
    if _cache is not None:
        return _cache
    if not CACHE.exists() or _cache_is_stale():
        try:
            rebuild_cache()
        except Exception:  # noqa: BLE001
            if not CACHE.exists():
                raise
    df = pd.read_parquet(CACHE)
    df['date'] = pd.to_datetime(df['date']).dt.date
    _cache = df.sort_values('date').reset_index(drop=True)
    return _cache


def regime_series(dates: Iterable) -> pd.Series:
    """Vectorised lookup indexed by the input dates (as datetime.date). Missing
    days inside the table's span carry the previous day's label; dates outside
    the span are 'UNKNOWN'."""
    df = load_table()
    keys = pd.to_datetime(list(dates)).date
    dense = df.set_index('date')['regime']
    full_idx = pd.date_range(dense.index.min(), dense.index.max(), freq='D').date
    dense = dense.reindex(full_idx).ffill()
    out = [str(dense[k]) if k in dense.index else 'UNKNOWN' for k in keys]
    return pd.Series(out, index=keys, name='regime')


def regime_for_date(target) -> str:
    return str(regime_series([target]).iloc[0])


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true', help='upsert the cache parquet')
    ap.add_argument('--summary', action='store_true', help='print per-regime stats')
    args = ap.parse_args()
    if args.rebuild:
        print(f'wrote {rebuild_cache()}')
    if args.summary or not args.rebuild:
        df = classify_history()
        print(f'classified rows: {len(df)}  ({df["date"].min()} → {df["date"].max()})')
        print(f'tiers: {CRYPTO_RV_TIERS}')
        for r, n in df['regime'].value_counts().items():
            print(f'  {r:14s} {n:5d}')
