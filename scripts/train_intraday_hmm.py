#!/usr/bin/env python3
"""train_intraday_hmm.py — fit the intraday HMM regime classifier.

Pipeline:
  1. Load `data/master/intraday_features.parquet` (Stage 2 collector
     accumulates 78 rows/day during RTH).
  2. Filter to RTH + drop quality_flag=2 rows (synthetic VIX failed).
  3. Compute training feature matrix (HMM_INPUT_COLS subset of parquet).
  4. Impute NaN → column means; cache means on the model object.
  5. Fit hmmlearn 4-state Gaussian HMM.
  6. Map raw states → regime names by ascending VIX-mean (matches the
     daily HMM convention, scripts/run_market_state.py:139–146).
  7. Save model to `.agents/market-state/hmm_intraday_latest.pkl` +
     dated copy.

Bootstrap behaviour: if the parquet has < MIN_TRAINING_ROWS (default 500)
rows, exit cleanly without training. The detector will log "no model"
and continue accumulating data; a subsequent run of this trainer once
enough rows exist will produce the model.

Refit cadence: weekly Sunday 18:00 ET (cron entry to be added in Stage 5).
The detector will pick up the new model on its next tick load.

Exit codes:
  0 — model trained or insufficient data (bootstrap mode)
  1 — partial failure (e.g., model fit didn't converge)
  2 — unrecoverable (parquet missing entirely + bootstrap fallback failed)
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

logger = logging.getLogger(__name__)

MIN_TRAINING_ROWS = 500   # Bootstrap threshold: ≈ 6 trading days of RTH ticks
N_STATES = 4
HMM_INPUT_COLS = [
    'vix_synth_30d', 'vix_synth_90d', 'vix_term_slope',
    'rr_25d', 'spy_realized_vol_30m',
]
STATE_NAMES_BY_RANK = {0: 'LOW_VOL', 1: 'TRANSITIONING',
                        2: 'HIGH_VOL', 3: 'CRISIS'}

PARQUET_PATH = ROOT / 'data' / 'master' / 'intraday_features.parquet'
MACRO_PATH   = ROOT / 'data' / 'master' / 'macro.parquet'
PRICES_30M   = ROOT / 'data' / 'master' / 'prices_30m.parquet'
MODEL_DIR = ROOT / '.agents' / 'market-state'


def _load_features() -> pd.DataFrame:
    if not PARQUET_PATH.exists():
        return pd.DataFrame()
    df = pd.read_parquet(PARQUET_PATH)
    df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)
    return df


def _load_historical_backfill() -> pd.DataFrame:
    """Synthesize daily-cadence historical rows for the 5 HMM features
    from VIX/VIX3M (macro.parquet, ~10y) + SPY 30m bars (prices_30m, ~2y).
    Returns a DataFrame with the same columns as intraday_features.parquet
    so it can be concatenated with the live RTH window.

    Why this exists: the live intraday collector only started accumulating
    in May 2026, giving the HMM ~7 trading days of VIX-18-20 ticks. That's
    not enough variance to learn what HIGH_VOL or CRISIS actually look like
    — every state centroid ends up within a 1-vol-pt band, and live ticks
    get arbitrarily mis-classified. Backfilling 10y of Cboe vol indices
    (2016-2026) pulls in COVID, 2018 Volmageddon, 2022 bear — real regime
    variance — so the rank-based labelling produces semantically correct
    centroids (LOW≈15, TRANS≈17, HIGH≈26, CRISIS≈35).

    Returns an empty DataFrame if macro.parquet is missing or doesn't
    contain VIX + VIX3M (caller falls back to live-only training).
    """
    if not MACRO_PATH.exists():
        return pd.DataFrame()
    macro = pd.read_parquet(MACRO_PATH)
    macro['date'] = pd.to_datetime(macro['date'])
    vix = macro[macro['series'] == 'VIX'][['date', 'value']].rename(columns={'value': 'vix_synth_30d'})
    vix3m = macro[macro['series'] == 'VIX3M'][['date', 'value']].rename(columns={'value': 'vix_synth_90d'})
    if vix.empty or vix3m.empty:
        return pd.DataFrame()
    bf = vix.merge(vix3m, on='date', how='inner').sort_values('date').reset_index(drop=True)
    bf['vix_term_slope'] = bf['vix_synth_90d'] / bf['vix_synth_30d']

    # Realized vol: native median of intraday 30m bars where SPY data exists,
    # VIX-implied proxy (annualized vol fraction) for older years.
    rv_native: dict = {}
    if PRICES_30M.exists():
        p30 = pd.read_parquet(PRICES_30M)
        spy30 = p30[p30['ticker'] == 'SPY'].copy()
        if not spy30.empty:
            spy30['datetime'] = pd.to_datetime(spy30['datetime'])
            spy30 = spy30.sort_values('datetime').reset_index(drop=True)
            spy30['ret'] = np.log(spy30['close'] / spy30['close'].shift(1))
            spy30['rv_30m'] = spy30['ret'].abs() * np.sqrt(252 * 13)
            spy30['date_only'] = spy30['datetime'].dt.date
            rv_native = (spy30.groupby('date_only')['rv_30m'].median()).to_dict()
    bf['spy_realized_vol_30m'] = bf.apply(
        lambda r: rv_native.get(r['date'].date(), r['vix_synth_30d'] / 100.0), axis=1,
    )
    bf['rr_25d'] = float('nan')   # backfill has no risk-reversal — let _build_X impute to mean

    # Match the live-features schema: ts_utc + source_quality_flag.
    # Use 21:00 UTC = 16:00 ET (market close) as the canonical daily tick time.
    bf['ts_utc'] = pd.to_datetime(bf['date'].dt.strftime('%Y-%m-%d') + ' 21:00:00+00:00', utc=True)
    bf['source_quality_flag'] = 1
    bf['pcr_oi'] = float('nan')
    bf['pcr_volume'] = float('nan')
    bf['zero_dte_volume_share'] = float('nan')
    return bf[['ts_utc', 'vix_synth_30d', 'vix_synth_90d', 'vix_term_slope',
               'pcr_oi', 'pcr_volume', 'rr_25d',
               'spy_realized_vol_30m', 'zero_dte_volume_share',
               'source_quality_flag']]


def _filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only rows during RTH (9:30-16:00 ET) on weekdays."""
    if df.empty:
        return df
    et = df['ts_utc'].dt.tz_convert('America/New_York')
    return df[
        (et.dt.weekday < 5) &
        (((et.dt.hour == 9) & (et.dt.minute >= 30)) |
         ((et.dt.hour > 9) & (et.dt.hour < 16)) |
         ((et.dt.hour == 16) & (et.dt.minute == 0)))
    ].copy()


def _build_X(df: pd.DataFrame) -> tuple[np.ndarray, dict[str, float]]:
    """Build the (n_samples, n_features) training matrix; impute NaN
    column-wise with means; return matrix + means dict."""
    cols = [c for c in HMM_INPUT_COLS if c in df.columns]
    if not cols:
        raise ValueError('parquet has none of the HMM input columns')
    raw = df[cols].astype(float)
    means = {c: float(raw[c].mean()) for c in cols if not raw[c].isna().all()}
    # Some columns may be all-NaN (e.g., rr_25d before chain enrichment).
    # In that case use 0 as a placeholder; the model effectively ignores
    # that dimension because variance is zero across the sample.
    for c in cols:
        if c not in means:
            means[c] = 0.0
        raw[c] = raw[c].fillna(means[c])
    return raw.values, means


def _fit_hmm(X: np.ndarray):
    from hmmlearn import hmm
    model = hmm.GaussianHMM(
        n_components=N_STATES,
        covariance_type='full',
        n_iter=200,
        random_state=42,
        tol=1e-4,
    )
    model.fit(X)
    return model


def _attach_metadata(model, X: np.ndarray, feature_names: list[str],
                     means: dict[str, float]):
    """Decorate the trained model with the metadata the detector reads.

    feature_means_       : dict[str, float] — for NaN imputation at score time
    feature_names_       : list[str] — exact column ordering at training
    regime_name_by_state_: dict[int, str] — ascending-VIX state→regime map
    trained_rows_        : int — sample count for observability
    trained_at_          : str — ISO timestamp
    """
    # State→regime map: rank by mean of the first feature (vix_synth_30d).
    means_per_state = model.means_[:, 0]
    rank_order = np.argsort(means_per_state)
    name_map = {int(rank_order[i]): STATE_NAMES_BY_RANK[i]
                for i in range(min(N_STATES, len(rank_order)))}
    model.feature_means_       = means
    model.feature_names_       = list(feature_names)
    model.regime_name_by_state_ = name_map
    model.trained_rows_        = int(X.shape[0])
    model.trained_at_          = datetime.now(timezone.utc).isoformat()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--min-rows', type=int, default=MIN_TRAINING_ROWS)
    ap.add_argument('--out-name', default='hmm_intraday_latest.pkl')
    ap.add_argument('--no-backfill', action='store_true',
                    help='Skip the 10y VIX/VIX3M historical backfill (live-features only).')
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [TRAIN] %(message)s')

    live = _load_features()
    if live.empty:
        logger.warning('no intraday_features.parquet — bootstrap mode '
                       '(detector accumulates data; rerun trainer later)')
        print(json.dumps({'action': 'bootstrap', 'reason': 'no_parquet'}))
        return 0
    live = _filter_rth(live)
    if 'source_quality_flag' in live.columns:
        live = live[live['source_quality_flag'] < 2]

    # Concatenate historical backfill ahead of live ticks so the HMM learns
    # state emissions from real regime breadth (COVID, 2022 bear, 2018 spike).
    # Multi-cadence (daily backfill + 5-min live) is OK for emission learning;
    # transmat is slightly biased at the boundary but classification accuracy
    # dominates that bias by orders of magnitude.
    backfill = pd.DataFrame() if args.no_backfill else _load_historical_backfill()
    if backfill.empty:
        df = live
        backfill_rows = 0
    else:
        df = pd.concat([backfill, live], ignore_index=True).sort_values('ts_utc').reset_index(drop=True)
        backfill_rows = len(backfill)
        logger.info('appended %d backfilled daily rows (%s → %s) to %d live ticks',
                    backfill_rows, backfill['ts_utc'].min(), backfill['ts_utc'].max(), len(live))

    if len(df) < args.min_rows:
        logger.warning('only %d total rows; need ≥%d to train (bootstrap mode)',
                       len(df), args.min_rows)
        print(json.dumps({
            'action': 'bootstrap',
            'rows_available': int(len(df)),
            'rows_required': int(args.min_rows),
        }))
        return 0

    feature_names = [c for c in HMM_INPUT_COLS if c in df.columns]
    X, means = _build_X(df)
    logger.info('training on %d samples × %d features (%d backfilled + %d live, cols=%s)',
                X.shape[0], X.shape[1], backfill_rows, len(live), feature_names)
    try:
        model = _fit_hmm(X)
    except Exception as e:
        logger.error('hmm fit failed: %s', e)
        return 1
    _attach_metadata(model, X, feature_names, means)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    out_latest = MODEL_DIR / args.out_name
    out_dated  = MODEL_DIR / f'hmm_intraday_{date.today().isoformat()}.pkl'
    for path in (out_latest, out_dated):
        with open(path, 'wb') as f:
            pickle.dump(model, f)
    logger.info('saved → %s and %s', out_latest, out_dated)

    summary = {
        'action':            'trained',
        'rows':              int(X.shape[0]),
        'backfill_rows':     int(backfill_rows),
        'live_rows':         int(len(live)),
        'features':          feature_names,
        'regime_name_by_state': model.regime_name_by_state_,
        'feature_means':     {k: round(v, 4) for k, v in means.items()},
        'state_centroids':   [
            {
                'state': int(s),
                'label': model.regime_name_by_state_.get(int(s), '?'),
                'vix_30d':    round(float(model.means_[s][0]), 3),
                'vix_90d':    round(float(model.means_[s][1]), 3),
                'term_slope': round(float(model.means_[s][2]), 3),
                'rr_25d':     round(float(model.means_[s][3]), 4),
                'rv_30m':     round(float(model.means_[s][4]), 4),
            }
            for s in range(N_STATES)
        ],
        'out':               str(out_latest),
    }
    print(json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
