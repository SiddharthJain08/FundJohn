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
MODEL_DIR = ROOT / '.agents' / 'market-state'


def _load_features() -> pd.DataFrame:
    if not PARQUET_PATH.exists():
        return pd.DataFrame()
    df = pd.read_parquet(PARQUET_PATH)
    df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)
    return df


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
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [TRAIN] %(message)s')

    df = _load_features()
    if df.empty:
        logger.warning('no intraday_features.parquet — bootstrap mode '
                       '(detector accumulates data; rerun trainer later)')
        print(json.dumps({'action': 'bootstrap', 'reason': 'no_parquet'}))
        return 0
    df = _filter_rth(df)
    if 'source_quality_flag' in df.columns:
        df = df[df['source_quality_flag'] < 2]
    if len(df) < args.min_rows:
        logger.warning('only %d RTH rows; need ≥%d to train (bootstrap mode)',
                       len(df), args.min_rows)
        print(json.dumps({
            'action': 'bootstrap',
            'rows_available': int(len(df)),
            'rows_required': int(args.min_rows),
        }))
        return 0

    feature_names = [c for c in HMM_INPUT_COLS if c in df.columns]
    X, means = _build_X(df)
    logger.info('training on %d samples × %d features (cols=%s)',
                X.shape[0], X.shape[1], feature_names)
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
        'features':          feature_names,
        'regime_name_by_state': model.regime_name_by_state_,
        'feature_means':     {k: round(v, 4) for k, v in means.items()},
        'out':               str(out_latest),
    }
    print(json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
