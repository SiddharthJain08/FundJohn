"""src/ingestion/intraday_features.py — 5-min intraday feature collector.

Pulls a snapshot of every input the intraday HMM regime detector needs
and returns a single 9-feature dict. Optionally appends to the master
parquet at `data/master/intraday_features.parquet` using the
append+dedup+atomic-write pattern from
`src/ingestion/ingest_prices_30m.py:200-231`.

Sources (Stage 0 verified 2026-05-08):
  * SPY option chain (Alpaca CLI: ships bid/ask + delta + IV per contract)
  * SPY/HYG/LQD 1-min OHLCV (Polygon equity feed)

Features (column names of the master parquet):
  ts_utc                     — UTC timestamp aligned to 5-min boundary
  vix_synth_30d              — Cboe variance-swap 30-day VIX
  vix_synth_90d              — same formula at 90-day horizon
  vix_term_slope             — vix_synth_90d / vix_synth_30d
  pcr_oi                     — sum(put_OI) / sum(call_OI) across chain
                               (NaN under Alpaca-only path; populated
                                  via Polygon snapshot enrichment)
  pcr_volume                 — sum(put_vol) / sum(call_vol)
  rr_25d                     — 25-Δ put IV − 25-Δ call IV (skew)
  spy_realized_vol_30m       — ann. stdev of last 30 min of 1-min returns
  zero_dte_volume_share      — frac of total chain volume in 0DTE expiries
                                 (NaN under Alpaca-only path)
  source_quality_flag        — 0=full, 1=partial (missing sub-feature),
                                  2=fallback (synthetic VIX NaN)

NOTE on Polygon enrichment: the live OI/volume fields require a
per-tick Polygon snapshot pull (`fetch_polygon_chain`). When that
fails or the env is misconfigured, those features fall back to NaN
and `source_quality_flag=1`. The HMM impute-then-train approach in
Stage 3 handles this cleanly.

Public API:
    collect_intraday_features(now_utc=None) -> dict
    append_features_row(row, parquet_path=DEFAULT_PARQUET) -> Path
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

# Avoid the broken src/ingestion/__init__.py — import synthetic_vix as
# a sibling module via importlib so test/runtime parity is preserved.
import importlib.util as _ilu
_HERE = Path(__file__).resolve().parent
_spec = _ilu.spec_from_file_location('synthetic_vix', _HERE / 'synthetic_vix.py')
_synth = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_synth)
compute_synthetic_vix = _synth.compute_synthetic_vix

ALPACA_CLI = os.environ.get('ALPACA_CLI_BIN', '/root/go/bin/alpaca')

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PARQUET = ROOT / 'data' / 'master' / 'intraday_features.parquet'

POLYGON_BASE = 'https://api.polygon.io'

FEATURE_COLUMNS = [
    'ts_utc',
    'vix_synth_30d', 'vix_synth_90d', 'vix_term_slope',
    'pcr_oi', 'pcr_volume', 'rr_25d',
    'spy_realized_vol_30m', 'zero_dte_volume_share',
    'source_quality_flag',
]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _utc_now() -> pd.Timestamp:
    return pd.Timestamp.now(tz='UTC')


def _floor_to_5min(ts: pd.Timestamp) -> pd.Timestamp:
    """Snap to nearest 5-min boundary (truncating)."""
    return ts.floor('5min')


def _alpaca_chain_pull(symbol: str, expiry_lo: str, expiry_hi: str,
                        timeout: int = 30) -> dict:
    """Pull the Alpaca chain JSON for a date window. Paginates if the
    underlying chain is larger than `--limit`. Returns the combined
    `snapshots` dict (key=contract symbol, value=snapshot)."""
    snapshots: dict = {}
    page_token: Optional[str] = None
    pages_pulled = 0
    while True:
        args = [
            ALPACA_CLI, 'data', 'option', 'chain',
            '--underlying-symbol', symbol,
            '--expiration-date-gte', expiry_lo,
            '--expiration-date-lte', expiry_hi,
            '--limit', '1000',
        ]
        if page_token:
            args += ['--page-token', page_token]
        proc = subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f'alpaca chain CLI failed (rc={proc.returncode}): {proc.stderr[:200]}'
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f'chain CLI emitted non-JSON: {e}') from e
        snapshots.update(payload.get('snapshots') or {})
        page_token = payload.get('next_page_token')
        pages_pulled += 1
        if not page_token or pages_pulled > 20:
            break
    return snapshots


def _parse_occ_symbol(occ: str) -> dict | None:
    """OCC: 'SPY260612C00580000' → {underlying, expiry, opt_type, strike}.
    Returns None on malformed input."""
    if not occ or len(occ) < 15:
        return None
    # Last 8 chars are strike × 1000 (e.g., '00580000' → 580.000)
    try:
        strike = float(occ[-8:]) / 1000.0
    except ValueError:
        return None
    opt_type = occ[-9]
    if opt_type not in ('C', 'P'):
        return None
    expiry_chars = occ[-15:-9]   # YYMMDD
    try:
        expiry = datetime.strptime(expiry_chars, '%y%m%d').date()
    except ValueError:
        return None
    underlying = occ[:-15]
    return {
        'underlying': underlying,
        'expiration_date': pd.Timestamp(expiry).tz_localize('UTC'),
        'option_type': opt_type,
        'strike': strike,
    }


def _alpaca_chain_to_df(snapshots: dict) -> pd.DataFrame:
    """Flatten Alpaca CLI chain response into the DataFrame shape
    `compute_synthetic_vix` expects."""
    rows = []
    for sym, snap in snapshots.items():
        meta = _parse_occ_symbol(sym)
        if not meta:
            continue
        q = snap.get('latestQuote') or {}
        g = snap.get('greeks') or {}
        bid = float(q.get('bp') or 0.0)
        ask = float(q.get('ap') or 0.0)
        delta = g.get('delta')
        iv = snap.get('impliedVolatility')
        rows.append({
            'symbol':          sym,
            'expiration_date': meta['expiration_date'],
            'strike':          meta['strike'],
            'option_type':     meta['option_type'],
            'bid':             bid,
            'ask':             ask,
            'iv':              float(iv) if iv is not None else float('nan'),
            'delta':           float(delta) if delta is not None else float('nan'),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _polygon_minute_bars(ticker: str, start: str, end: str,
                          api_key: str, limit_bars: int = 50_000,
                          timeout: int = 15) -> pd.DataFrame:
    """Pull 1-min Polygon aggregates for `ticker` over [start, end].
    Mirrors `src/ingestion/ingest_prices_30m.py:_polygon_30m_bars` but
    at 1-min cadence. Returns OHLCV DataFrame indexed by tz-aware UTC
    datetimes."""
    import requests
    url = (f'{POLYGON_BASE}/v2/aggs/ticker/{ticker}/range/1/minute/'
           f'{start}/{end}')
    params = {'adjusted': 'true', 'sort': 'asc', 'limit': limit_bars,
              'apiKey': api_key}
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code == 404:
        return pd.DataFrame()
    if r.status_code == 429:
        # 5-min cron — don't retry; next tick will fetch fresh data.
        raise RuntimeError(f'Polygon 429 for {ticker}')
    r.raise_for_status()
    js = r.json()
    rows = js.get('results') or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df['datetime'] = pd.to_datetime(df['t'], unit='ms', utc=True)
    return df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low',
                               'c': 'close', 'v': 'volume'})[
        ['datetime', 'open', 'high', 'low', 'close', 'volume']
    ].sort_values('datetime').reset_index(drop=True)


# ── Feature computations ─────────────────────────────────────────────────────

def _compute_pcr(chain_df: pd.DataFrame) -> tuple[float, float]:
    """Put-Call ratio from chain DataFrame. Returns (pcr_oi, pcr_volume).
    Both NaN when the relevant Polygon-only columns are absent."""
    if 'open_interest' in chain_df.columns and not chain_df['open_interest'].isna().all():
        oi_call = chain_df.loc[chain_df['option_type'].str.upper().str.startswith('C'),
                                'open_interest'].fillna(0).sum()
        oi_put = chain_df.loc[chain_df['option_type'].str.upper().str.startswith('P'),
                               'open_interest'].fillna(0).sum()
        pcr_oi = (oi_put / oi_call) if oi_call > 0 else float('nan')
    else:
        pcr_oi = float('nan')

    if 'volume' in chain_df.columns and not chain_df['volume'].isna().all():
        v_call = chain_df.loc[chain_df['option_type'].str.upper().str.startswith('C'),
                               'volume'].fillna(0).sum()
        v_put = chain_df.loc[chain_df['option_type'].str.upper().str.startswith('P'),
                              'volume'].fillna(0).sum()
        pcr_vol = (v_put / v_call) if v_call > 0 else float('nan')
    else:
        pcr_vol = float('nan')
    return float(pcr_oi), float(pcr_vol)


def _compute_rr_25d(chain_df: pd.DataFrame) -> float:
    """25-Δ risk reversal = IV(put @ Δ≈-0.25) − IV(call @ Δ≈+0.25).
    Picks the contract closest to the target delta in EACH bucket
    among contracts within ±15-90 DTE. Returns NaN on insufficient
    delta/IV data."""
    if 'delta' not in chain_df.columns or 'iv' not in chain_df.columns:
        return float('nan')
    df = chain_df[(~chain_df['delta'].isna()) & (~chain_df['iv'].isna()) &
                  (chain_df['iv'] > 0)]
    if df.empty:
        return float('nan')
    # Restrict to ~30-DTE contracts (15-90 DTE window).
    if 't_days' in df.columns:
        df = df[(df['t_days'] >= 15) & (df['t_days'] <= 90)]
    if df.empty:
        return float('nan')
    calls = df[df['option_type'].str.upper().str.startswith('C')]
    puts  = df[df['option_type'].str.upper().str.startswith('P')]
    if calls.empty or puts.empty:
        return float('nan')
    target_call = 0.25
    target_put = -0.25
    cal_pick = calls.iloc[(calls['delta'] - target_call).abs().argmin()]
    put_pick = puts.iloc[(puts['delta'] - target_put).abs().argmin()]
    return float(put_pick['iv'] - cal_pick['iv'])


def _compute_realized_vol_30m(spy_bars: pd.DataFrame, now_utc: pd.Timestamp,
                               window_min: int = 30) -> float:
    """Annualised realised vol from the last `window_min` minutes of
    SPY 1-min returns. Returns NaN when fewer than 5 bars are
    available (insufficient sample)."""
    if spy_bars is None or spy_bars.empty:
        return float('nan')
    recent = spy_bars[spy_bars['datetime'] >= (now_utc - pd.Timedelta(minutes=window_min))]
    if len(recent) < 5:
        return float('nan')
    rets = recent['close'].pct_change().dropna()
    if len(rets) < 4 or rets.std() == 0:
        return float('nan')
    # Annualise: 1-min sample → ×√(252 × 390) (252 trading days, 390 min/day)
    return float(rets.std() * math.sqrt(252.0 * 390.0)) * 100.0


def _compute_zero_dte_vol_share(chain_df: pd.DataFrame) -> float:
    """Fraction of total chain volume sitting in ≤1-DTE expiries.
    Requires Polygon volume enrichment; NaN otherwise."""
    if 'volume' not in chain_df.columns or chain_df['volume'].isna().all():
        return float('nan')
    if 't_days' not in chain_df.columns:
        return float('nan')
    total = chain_df['volume'].fillna(0).sum()
    if total <= 0:
        return float('nan')
    zd = chain_df.loc[chain_df['t_days'] <= 1, 'volume'].fillna(0).sum()
    return float(zd / total)


# ── Public entry ─────────────────────────────────────────────────────────────

def collect_intraday_features(now_utc: Optional[pd.Timestamp] = None,
                                spy_chain_df: Optional[pd.DataFrame] = None,
                                spy_bars: Optional[pd.DataFrame] = None,
                                spot: Optional[float] = None) -> dict:
    """Single-tick feature dict.

    Args:
        now_utc: tz-aware UTC timestamp; defaults to now.
        spy_chain_df: pre-fetched SPY chain (test harness path).
                      When None, pulled live via Alpaca CLI.
        spy_bars: pre-fetched SPY 1-min bars (test harness path).
                  When None, pulled live via Polygon.
        spot: SPY spot price; when None, derived from spy_bars latest
              close or the chain's at-the-money strike.

    Returns: 10-key dict matching FEATURE_COLUMNS schema.
    """
    if now_utc is None:
        now_utc = _utc_now()
    if not isinstance(now_utc, pd.Timestamp):
        now_utc = pd.Timestamp(now_utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.tz_localize('UTC')
    ts_floor = _floor_to_5min(now_utc)

    quality_flag = 0

    # 1) SPY chain
    if spy_chain_df is None:
        # Pull live: a window covering 0-120 DTE is enough for both 30d and
        # 90d brackets plus the 25-Δ RR window.
        try:
            today = now_utc.strftime('%Y-%m-%d')
            cutoff = (now_utc + pd.Timedelta(days=120)).strftime('%Y-%m-%d')
            snaps = _alpaca_chain_pull('SPY', today, cutoff)
            spy_chain_df = _alpaca_chain_to_df(snaps)
        except Exception:
            spy_chain_df = pd.DataFrame()
            quality_flag = max(quality_flag, 2)

    # 2) SPY 1-min bars (last 60 min for realised vol)
    if spy_bars is None:
        try:
            api_key = os.environ.get('POLYGON_API_KEY', '')
            if api_key:
                start = (now_utc - pd.Timedelta(minutes=90)).strftime('%Y-%m-%d')
                end = now_utc.strftime('%Y-%m-%d')
                spy_bars = _polygon_minute_bars('SPY', start, end, api_key)
            else:
                spy_bars = pd.DataFrame()
        except Exception:
            spy_bars = pd.DataFrame()

    # 3) Spot derivation
    if spot is None or spot <= 0:
        if spy_bars is not None and not spy_bars.empty:
            spot = float(spy_bars['close'].iloc[-1])
        elif spy_chain_df is not None and not spy_chain_df.empty:
            spot = float(spy_chain_df['strike'].median())
        else:
            spot = float('nan')
            quality_flag = max(quality_flag, 2)

    # 4) Tag t_days for downstream feature helpers
    if spy_chain_df is not None and not spy_chain_df.empty:
        spy_chain_df['t_days'] = (
            (spy_chain_df['expiration_date'] - now_utc).dt.total_seconds() / 86400.0
        )

    # 5) Synthetic VIX
    if spy_chain_df is not None and not spy_chain_df.empty and not math.isnan(spot):
        vix = compute_synthetic_vix(spy_chain_df, spot=spot, t_now=now_utc)
        if vix.get('fallback_flag'):
            quality_flag = max(quality_flag, 1)
    else:
        vix = {'vix_synth_30d': float('nan'), 'vix_synth_90d': float('nan'),
               'term_slope': float('nan')}
        quality_flag = max(quality_flag, 2)

    # 6) PCR + 0DTE share (NaN under Alpaca-only path)
    if spy_chain_df is not None and not spy_chain_df.empty:
        pcr_oi, pcr_vol = _compute_pcr(spy_chain_df)
        zd_share = _compute_zero_dte_vol_share(spy_chain_df)
        rr_25 = _compute_rr_25d(spy_chain_df)
    else:
        pcr_oi = pcr_vol = zd_share = rr_25 = float('nan')
        quality_flag = max(quality_flag, 1)

    rv30 = _compute_realized_vol_30m(spy_bars, now_utc=now_utc) \
        if spy_bars is not None else float('nan')

    # Quality bumps for missing sub-features (don't overwrite a higher flag)
    for v in (pcr_oi, pcr_vol, zd_share, rr_25, rv30):
        if isinstance(v, float) and math.isnan(v):
            quality_flag = max(quality_flag, 1)

    return {
        'ts_utc':                ts_floor,
        'vix_synth_30d':         vix.get('vix_synth_30d', float('nan')),
        'vix_synth_90d':         vix.get('vix_synth_90d', float('nan')),
        'vix_term_slope':        vix.get('term_slope',    float('nan')),
        'pcr_oi':                pcr_oi,
        'pcr_volume':            pcr_vol,
        'rr_25d':                rr_25,
        'spy_realized_vol_30m':  rv30,
        'zero_dte_volume_share': zd_share,
        'source_quality_flag':   int(quality_flag),
    }


def append_features_row(row: dict,
                         parquet_path: Path | str = DEFAULT_PARQUET) -> Path:
    """Append a single feature row to the master parquet.

    Append+dedup+atomic-write — never deletes prior rows. If the
    parquet doesn't exist yet, creates it.

    Mirrors `src/ingestion/ingest_prices_30m.py:200-231` discipline:
        1. Read existing (if present)
        2. Concat new row(s)
        3. drop_duplicates(subset=['ts_utc'], keep='last')
        4. Sort by ts_utc
        5. Atomic to_parquet (write to .tmp, os.replace)
    """
    parquet_path = Path(parquet_path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    new_df = pd.DataFrame([row])
    new_df['ts_utc'] = pd.to_datetime(new_df['ts_utc'], utc=True)

    if parquet_path.exists():
        existing = pd.read_parquet(parquet_path)
        existing['ts_utc'] = pd.to_datetime(existing['ts_utc'], utc=True)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['ts_utc'], keep='last')
    else:
        combined = new_df

    combined = combined.sort_values('ts_utc').reset_index(drop=True)
    # Reorder columns for stable schema
    cols = [c for c in FEATURE_COLUMNS if c in combined.columns]
    extra = [c for c in combined.columns if c not in cols]
    combined = combined[cols + extra]

    tmp = parquet_path.with_suffix(parquet_path.suffix + '.tmp')
    combined.to_parquet(tmp, index=False)
    os.replace(tmp, parquet_path)
    return parquet_path


def append_features_rows(rows: list[dict],
                          parquet_path: Path | str = DEFAULT_PARQUET) -> Path:
    """Bulk version of append_features_row for backfill paths."""
    if not rows:
        return Path(parquet_path)
    parquet_path = Path(parquet_path)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    new_df = pd.DataFrame(rows)
    new_df['ts_utc'] = pd.to_datetime(new_df['ts_utc'], utc=True)

    if parquet_path.exists():
        existing = pd.read_parquet(parquet_path)
        existing['ts_utc'] = pd.to_datetime(existing['ts_utc'], utc=True)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined = combined.drop_duplicates(subset=['ts_utc'], keep='last')
    else:
        combined = new_df.drop_duplicates(subset=['ts_utc'], keep='last')

    combined = combined.sort_values('ts_utc').reset_index(drop=True)
    cols = [c for c in FEATURE_COLUMNS if c in combined.columns]
    extra = [c for c in combined.columns if c not in cols]
    combined = combined[cols + extra]

    tmp = parquet_path.with_suffix(parquet_path.suffix + '.tmp')
    combined.to_parquet(tmp, index=False)
    os.replace(tmp, parquet_path)
    return parquet_path
