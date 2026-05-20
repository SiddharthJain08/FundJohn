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
# Feature set v3 (2026-05-20):
#   • Dropped spy_realized_vol_30m — close-to-close stdev systematically
#     UNDER-represented structural crises where churn compresses but the
#     spot trends persistently (HIGH_VOL 0.222 > CRISIS 0.199 inversion).
#   • Added spy_gk_vol_daily — Garman-Klass daily volatility from SPY OHLC.
#     GK = 0.5·ln(H/L)² − (2·ln(2)−1)·ln(C/O)². Captures intraday range
#     AND directional displacement; trending crash days score correctly.
#   • Added vvix_level — CBOE vol-of-vol index. Direct measurement of the
#     convexity / options-driven-shock dimension orthogonal to spot VIX.
#     VVIX hit 207 during COVID but stayed ~130 during noisy HIGH_VOL days.
HMM_INPUT_COLS = [
    'vix_synth_30d', 'vix_synth_90d', 'vix_term_slope',
    'rr_25d', 'spy_gk_vol_daily', 'vvix_level',
]
STATE_NAMES_BY_RANK = {0: 'LOW_VOL', 1: 'TRANSITIONING',
                       2: 'HIGH_VOL', 3: 'CRISIS'}

PARQUET_PATH = ROOT / 'data' / 'master' / 'intraday_features.parquet'
MACRO_PATH   = ROOT / 'data' / 'master' / 'macro.parquet'
PRICES_30M   = ROOT / 'data' / 'master' / 'prices_30m.parquet'
PRICES_DAILY = ROOT / 'data' / 'master' / 'prices.parquet'
ENRICHED_PATH = ROOT / 'data' / 'master' / 'options_aggregates_enriched.parquet'
MODEL_DIR = ROOT / '.agents' / 'market-state'


def _load_features() -> pd.DataFrame:
    if not PARQUET_PATH.exists():
        return pd.DataFrame()
    df = pd.read_parquet(PARQUET_PATH)
    df['ts_utc'] = pd.to_datetime(df['ts_utc'], utc=True)
    # Enrich with the two derived features that aren't in the live collector
    # output yet: spy_gk_vol_daily (from daily SPY OHLC) and vvix_level (from
    # macro). For an intraday tick on date D, the "yesterday's close" daily
    # value is what the trainer used at backfill time, so use latest available
    # ≤ tick date (an asof-merge in spirit).
    if df.empty:
        return df
    df['_tick_date'] = pd.to_datetime(
        df['ts_utc'].dt.tz_convert('America/New_York').dt.date,
    ).astype('datetime64[ns]')
    # GK from prices.parquet
    if PRICES_DAILY.exists():
        spy_d = pd.read_parquet(PRICES_DAILY)
        spy_d = spy_d[spy_d['ticker'] == 'SPY'].copy()
        spy_d = spy_d.dropna(subset=['open', 'high', 'low', 'close'])
        spy_d = spy_d[spy_d['low'] > 0]
        if not spy_d.empty:
            spy_d['_tick_date'] = pd.to_datetime(spy_d['date']).astype('datetime64[ns]')
            ln_hl = np.log(spy_d['high'] / spy_d['low'])
            ln_co = np.log(spy_d['close'] / spy_d['open'])
            gk_var = 0.5 * ln_hl ** 2 - (2 * np.log(2) - 1) * ln_co ** 2
            spy_d['spy_gk_vol_daily'] = np.sqrt(gk_var.clip(lower=0)) * np.sqrt(252)
            spy_d = spy_d.sort_values('_tick_date')[['_tick_date', 'spy_gk_vol_daily']]
            df = pd.merge_asof(
                df.sort_values('_tick_date'),
                spy_d, on='_tick_date', direction='backward',
            )
    # VVIX from macro.parquet
    if MACRO_PATH.exists():
        macro = pd.read_parquet(MACRO_PATH)
        vvix_df = macro[macro['series'] == 'VVIX'][['date', 'value']].copy()
        if not vvix_df.empty:
            vvix_df['_tick_date'] = pd.to_datetime(vvix_df['date']).astype('datetime64[ns]')
            vvix_df = vvix_df.sort_values('_tick_date').rename(columns={'value': 'vvix_level'})
            df = pd.merge_asof(
                df.sort_values('_tick_date'),
                vvix_df[['_tick_date', 'vvix_level']],
                on='_tick_date', direction='backward',
            )
    df = df.drop(columns=['_tick_date'])
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

    # Garman-Klass daily vol from SPY OHLC. GK is range+drift aware so trending
    # crash days (low intraday churn, persistent drift) get the high RV they
    # deserve — fixes the rv_30m HIGH_VOL>CRISIS inversion in the old model.
    # σ²_GK = 0.5·ln(H/L)² − (2·ln(2)−1)·ln(C/O)², annualised by √252.
    gk_by_date: dict = {}
    if PRICES_DAILY.exists():
        pdaily = pd.read_parquet(PRICES_DAILY)
        spy_d = pdaily[pdaily['ticker'] == 'SPY'].copy()
        spy_d = spy_d.dropna(subset=['open', 'high', 'low', 'close'])
        spy_d = spy_d[spy_d['low'] > 0]
        if not spy_d.empty:
            spy_d['date'] = pd.to_datetime(spy_d['date'])
            ln_hl = np.log(spy_d['high'] / spy_d['low'])
            ln_co = np.log(spy_d['close'] / spy_d['open'])
            gk_var = 0.5 * ln_hl ** 2 - (2 * np.log(2) - 1) * ln_co ** 2
            spy_d['gk_ann'] = np.sqrt(gk_var.clip(lower=0)) * np.sqrt(252)
            gk_by_date = dict(zip(spy_d['date'].dt.date, spy_d['gk_ann']))
            logger.info(
                'spy_gk_vol_daily: %d daily-OHLC rows (μ=%.4f σ=%.4f range %.4f..%.4f)',
                len(gk_by_date), spy_d['gk_ann'].mean(), spy_d['gk_ann'].std(),
                spy_d['gk_ann'].min(), spy_d['gk_ann'].max(),
            )
    bf['spy_gk_vol_daily'] = bf['date'].dt.date.map(gk_by_date)

    # VVIX as a direct feature — CBOE vol-of-vol index. Captures
    # options-driven structural shocks orthogonal to spot VIX.
    vvix_by_date = {
        d.date(): v for d, v in zip(
            pd.to_datetime(macro[macro['series'] == 'VVIX']['date']),
            macro[macro['series'] == 'VVIX']['value'],
        )
    }
    bf['vvix_level'] = bf['date'].dt.date.map(vvix_by_date)
    logger.info(
        'vvix_level: %d daily VVIX rows mapped',
        bf['vvix_level'].notna().sum(),
    )

    # rr_25d backfill — direct sources only, no OLS smoothing:
    #   Stage A (~461 days, 2024-04 → 2026-04): rescaled SPY skew from
    #     options_aggregates_enriched (otm_put_iv − otm_call_iv). Vendor OTM
    #     strikes ≠ exact ±0.25-delta so raw scale differs from the live
    #     collector (live μ=0.052 σ=0.010 vs enriched μ=0.067 σ=0.059) —
    #     z-score-match to live moments.
    #   Stage B (~2080 days pre-2024 + post-Apr-2026): VVIX z-score, rescaled
    #     to live rr_25d moments. VVIX is the CBOE-published vol-of-vol
    #     index — a directly-observed market measurement of the same
    #     convexity premium that drives put-call skew. Correlation with
    #     anchor rr_25d ≈ 0.29 (same as VIX itself). Preserves the natural
    #     variance of the rr_25d signal across regimes — no conditional-mean
    #     smoothing that OLS would impose.
    rr_by_date: dict = {}
    vvix_rescale: tuple | None = None
    if ENRICHED_PATH.exists():
        en = pd.read_parquet(ENRICHED_PATH)
        spy_en = en[en['ticker'] == 'SPY'].copy()
        if not spy_en.empty and 'otm_put_iv' in spy_en.columns:
            spy_en['date_only'] = pd.to_datetime(spy_en['date']).dt.date
            spy_en['skew_raw'] = spy_en['otm_put_iv'] - spy_en['otm_call_iv']
            valid = spy_en['skew_raw'].dropna()
            if len(valid) >= 50:
                live_intra = pd.read_parquet(PARQUET_PATH)
                live_rr = live_intra['rr_25d'].dropna()
                if len(live_rr) >= 30:
                    skew_m, skew_s = float(valid.mean()), float(valid.std())
                    live_m, live_s = float(live_rr.mean()), float(live_rr.std())
                    if skew_s > 0 and live_s > 0:
                        spy_en['rr_rescaled'] = (
                            (spy_en['skew_raw'] - skew_m) / skew_s * live_s + live_m
                        )
                        rr_by_date = dict(zip(spy_en['date_only'], spy_en['rr_rescaled']))
                        logger.info(
                            'rr_25d Stage A: %d enriched-skew days '
                            'rescaled (skew μ=%.4f σ=%.4f → live μ=%.4f σ=%.4f)',
                            len(rr_by_date), skew_m, skew_s, live_m, live_s,
                        )
                        # Stage B prep: load VVIX, compute z-score-rescale params
                        vvix_df = macro[macro['series'] == 'VVIX'][['date', 'value']].rename(columns={'value': 'vvix'})
                        vvix_df['date'] = pd.to_datetime(vvix_df['date'])
                        if len(vvix_df) >= 100:
                            v_m, v_s = float(vvix_df['vvix'].mean()), float(vvix_df['vvix'].std())
                            if v_s > 0:
                                vvix_rescale = (v_m, v_s, live_m, live_s,
                                                dict(zip(vvix_df['date'].dt.date, vvix_df['vvix'])))
                                logger.info(
                                    'rr_25d Stage B: VVIX-rescaled (vvix μ=%.1f σ=%.1f → '
                                    'rr μ=%.4f σ=%.4f) covering %d days',
                                    v_m, v_s, live_m, live_s, len(vvix_df),
                                )
    # Apply rr_25d: Stage A direct lookup, else Stage B VVIX-rescale
    def _rr_for_row(r):
        d = r['date'].date()
        if d in rr_by_date:
            return rr_by_date[d]
        if vvix_rescale:
            v_m, v_s, l_m, l_s, vvix_map = vvix_rescale
            vvix_val = vvix_map.get(d)
            if vvix_val is not None:
                return (float(vvix_val) - v_m) / v_s * l_s + l_m
        return float('nan')   # _build_X falls back to column mean
    bf['rr_25d'] = bf.apply(_rr_for_row, axis=1)

    # Match the live-features schema: ts_utc + source_quality_flag.
    # Use 21:00 UTC = 16:00 ET (market close) as the canonical daily tick time.
    bf['ts_utc'] = pd.to_datetime(bf['date'].dt.strftime('%Y-%m-%d') + ' 21:00:00+00:00', utc=True)
    bf['source_quality_flag'] = 1
    bf['pcr_oi'] = float('nan')
    bf['pcr_volume'] = float('nan')
    bf['zero_dte_volume_share'] = float('nan')
    return bf[['ts_utc', 'vix_synth_30d', 'vix_synth_90d', 'vix_term_slope',
               'pcr_oi', 'pcr_volume', 'rr_25d',
               'spy_gk_vol_daily', 'vvix_level', 'zero_dte_volume_share',
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
                **{feat: round(float(model.means_[s][k]), 4)
                   for k, feat in enumerate(feature_names)},
            }
            for s in range(N_STATES)
        ],
        'out':               str(out_latest),
    }
    print(json.dumps(summary, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
