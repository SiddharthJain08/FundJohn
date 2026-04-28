"""
historical_regimes.py — deterministic VIX-tier classifier for backtest regimes.

The live engine uses a 4-state Gaussian HMM refit weekly on a 252-day window
(scripts/run_market_state.py). For historical labelling that's awkward — the
HMM has no opinion about 2018 because it was trained on the trailing year.

Backtests need *reproducibility*: the same code on the same prices should
yield the same per-day regime sequence. This module gives that via a simple,
documented mapping from a 5-day rolling-median VIX to one of four canonical
regimes.

Regime tiers (the production HMM's regime ordering is itself derived from
VIX mean, so the two classifiers are kin not strangers):

| Smoothed VIX | Regime        |
| < 15         | LOW_VOL       |
| 15 – 22      | TRANSITIONING |
| 22 – 30      | HIGH_VOL      |
| > 30         | CRISIS        |

Public API
----------
    classify_history(start_date, end_date) -> pd.DataFrame
        Columns: date, vix, vix_smoothed, regime. One row per trading day in
        the requested range.

    regime_for_date(date) -> str
        Single-day lookup. Reads cached parquet if available, else builds
        the cache. Returns one of LOW_VOL/TRANSITIONING/HIGH_VOL/CRISIS, or
        'UNKNOWN' if VIX data is missing for that date.

    find_regime_windows(regime, min_days=60) -> list[(start_iso, end_iso)]
        Contiguous spans where the smoothed regime held continuously
        (coalescing gaps ≤ 5 calendar days). Spans shorter than min_days
        are dropped.
"""
from __future__ import annotations

from datetime import date as _date, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

ROOT      = Path(__file__).resolve().parent.parent.parent
MACRO     = ROOT / 'data' / 'master' / 'macro.parquet'
CACHE     = ROOT / 'data' / 'master' / 'historical_regimes.parquet'

CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

# 5-trading-day rolling median smooths single-day VIX spikes (e.g. one-off
# event-day prints) without erasing real, sustained regime shifts. Backtest
# stability over reactivity — the live HMM stays reactive.
SMOOTH_DAYS = 5

VIX_TIERS = (
    (15.0, 'LOW_VOL'),
    (22.0, 'TRANSITIONING'),
    (30.0, 'HIGH_VOL'),
    # > 30 → CRISIS (the catch-all)
)


def _classify_smoothed(vix_smoothed: float) -> str:
    if pd.isna(vix_smoothed):
        return 'UNKNOWN'
    for upper, label in VIX_TIERS:
        if vix_smoothed < upper:
            return label
    return 'CRISIS'


def _load_vix_long() -> pd.DataFrame:
    """Read VIX series from macro.parquet — long format (date, value)."""
    if not MACRO.exists():
        raise FileNotFoundError(f'macro.parquet not found at {MACRO}')
    df = pd.read_parquet(MACRO)
    vix = df[df['series'] == 'VIX'][['date', 'value']].copy()
    if vix.empty:
        raise RuntimeError('macro.parquet has no VIX rows — run backfill_vix.py first')
    vix['date'] = pd.to_datetime(vix['date']).dt.date
    vix = vix.sort_values('date').drop_duplicates(subset=['date'], keep='last')
    return vix.reset_index(drop=True)


def classify_history(start_date: str | _date | None = None,
                     end_date:   str | _date | None = None) -> pd.DataFrame:
    """Return a DataFrame with date, vix, vix_smoothed, regime for the given range.

    Reads the full VIX series from macro.parquet, computes a 5-day rolling
    median for smoothing, then maps each smoothed VIX to a regime tier. The
    rolling window uses the *full* series so the first SMOOTH_DAYS-1 rows of
    the user-requested window still get a valid smoothed value (when prior
    history is available)."""
    df = _load_vix_long()
    df['vix_smoothed'] = df['value'].rolling(window=SMOOTH_DAYS, min_periods=1).median()
    df['regime'] = df['vix_smoothed'].apply(_classify_smoothed)
    df = df.rename(columns={'value': 'vix'})

    if start_date:
        sd = pd.to_datetime(start_date).date()
        df = df[df['date'] >= sd]
    if end_date:
        ed = pd.to_datetime(end_date).date()
        df = df[df['date'] <= ed]

    return df.reset_index(drop=True)[['date', 'vix', 'vix_smoothed', 'regime']]


def rebuild_cache(start_date: str | None = None,
                  end_date:   str | None = None) -> Path:
    """Write the full classified history to data/master/historical_regimes.parquet.

    Idempotent: overwrites the cache with the latest computation. Callers
    that want the cache should invoke this once after a VIX backfill or when
    macro.parquet has new daily rows."""
    df = classify_history(start_date, end_date)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(CACHE) + '.tmp')
    df.to_parquet(tmp, compression='snappy', index=False)
    import os as _os
    _os.replace(tmp, CACHE)
    return CACHE


_cache: pd.DataFrame | None = None


def _load_cached() -> pd.DataFrame:
    """Load the cached classifier output, building the cache if missing."""
    global _cache
    if _cache is not None:
        return _cache
    if not CACHE.exists():
        rebuild_cache()
    _cache = pd.read_parquet(CACHE)
    _cache['date'] = pd.to_datetime(_cache['date']).dt.date
    return _cache


def regime_for_date(target: str | _date | pd.Timestamp) -> str:
    """Single-day lookup. Returns 'UNKNOWN' if VIX is missing for that date."""
    df = _load_cached()
    target_d = pd.to_datetime(target).date()
    hit = df[df['date'] == target_d]
    if hit.empty:
        # Try the latest preceding trading day (handles weekends/holidays).
        prev = df[df['date'] < target_d]
        if prev.empty:
            return 'UNKNOWN'
        return str(prev.iloc[-1]['regime'])
    return str(hit.iloc[0]['regime'])


def regime_series(dates: Iterable) -> pd.Series:
    """Vectorised lookup. Returns a Series indexed by the input dates with
    the regime label for each (carrying forward from the last available
    trading day on weekends/holidays)."""
    df = _load_cached().sort_values('date').reset_index(drop=True)
    keys = pd.to_datetime(list(dates)).date
    # Build a dense daily index spanning the cache, forward-fill for non-trading
    # days, then look up.
    dense = df.set_index('date')
    full_idx = pd.date_range(dense.index.min(), dense.index.max(), freq='D').date
    dense = dense.reindex(full_idx).ffill()
    out = []
    for k in keys:
        if k in dense.index:
            out.append(str(dense.loc[k]['regime']))
        else:
            out.append('UNKNOWN')
    return pd.Series(out, index=keys, name='regime')


def find_regime_windows(regime: str,
                        min_days: int = 60,
                        coalesce_gap_days: int = 5) -> list[tuple[str, str]]:
    """Return contiguous (start, end) ISO date ranges where the smoothed
    regime equalled `regime` continuously, with brief excursions of
    ≤coalesce_gap_days collapsed into the surrounding span. Spans shorter
    than min_days are dropped.

    Coalescing tolerates noise around tier boundaries — e.g. a one-day blip
    out of CRISIS during the COVID crash shouldn't fragment the window."""
    df = _load_cached().sort_values('date').reset_index(drop=True)
    if regime not in set(df['regime'].unique()) and regime != 'UNKNOWN':
        # Caller asked for a regime that never appears in history.
        return []

    # Build a list of (start, end) raw spans where regime == requested.
    raw: list[list[_date]] = []
    in_span = False
    cur_start: _date | None = None
    last_d:    _date | None = None
    for _, row in df.iterrows():
        d = row['date']
        is_match = row['regime'] == regime
        if is_match and not in_span:
            cur_start = d
            in_span = True
        elif not is_match and in_span:
            raw.append([cur_start, last_d])
            in_span = False
        last_d = d
    if in_span and cur_start is not None and last_d is not None:
        raw.append([cur_start, last_d])

    if not raw:
        return []

    # Coalesce adjacent spans separated by ≤ coalesce_gap_days calendar days.
    coalesced: list[list[_date]] = []
    for span in raw:
        if not coalesced:
            coalesced.append(span)
            continue
        prev = coalesced[-1]
        gap = (span[0] - prev[1]).days
        if gap <= coalesce_gap_days:
            prev[1] = span[1]
        else:
            coalesced.append(span)

    # Filter by min_days (calendar-day span; close enough for window planning).
    out: list[tuple[str, str]] = []
    for span in coalesced:
        span_days = (span[1] - span[0]).days + 1
        if span_days >= min_days:
            out.append((span[0].isoformat(), span[1].isoformat()))
    return out


# CLI for ad-hoc inspection.
if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--rebuild', action='store_true', help='rebuild cache parquet')
    ap.add_argument('--summary', action='store_true', help='print per-regime stats')
    ap.add_argument('--min-days', type=int, default=60)
    args = ap.parse_args()

    if args.rebuild:
        out = rebuild_cache()
        print(f'wrote {out}')

    if args.summary or not args.rebuild:
        df = classify_history()
        print(f'classified rows: {len(df)}')
        print(f'date range: {df["date"].min()} → {df["date"].max()}')
        print('per-regime trading-day counts:')
        for r, n in df['regime'].value_counts().items():
            print(f'  {r:14s} {n:5d}')
        print(f'\nwindows ≥ {args.min_days} days, by regime:')
        for r in CANONICAL_REGIMES:
            wins = find_regime_windows(r, min_days=args.min_days)
            total = sum((pd.to_datetime(e) - pd.to_datetime(s)).days + 1 for s, e in wins)
            longest = max((pd.to_datetime(e) - pd.to_datetime(s)).days + 1 for s, e in wins) if wins else 0
            print(f'  {r:14s} {len(wins)} windows, {total} days total (longest {longest})')
            for s, e in wins:
                print(f'                  {s} → {e}')
