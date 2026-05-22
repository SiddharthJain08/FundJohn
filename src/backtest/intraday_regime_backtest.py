"""src/backtest/intraday_regime_backtest.py — walk-forward backtest harness.

Validates the intraday HMM regime detector by replaying historical
`intraday_features.parquet` rows through the trained model, identifying
detected transitions, and scoring liquidate-then-re-enter vs
buy-and-hold P&L over a forward window.

This module is read-mostly: it never modifies the parquet, never calls
the broker, and never touches the live HMM model file. It loads
whatever model path the operator supplies (or trains a fresh one on
the train-window of each split) and produces an `output/intraday_hmm_walkforward.json`
report.

Walk-forward strategy:
  - Sort all rows by ts_utc.
  - Split into K expanding-window pairs:
      train: rows[:i*split_size]
      test:  rows[i*split_size : (i+1)*split_size]
  - Fit a fresh HMM on each train window; score the test window.
  - For every confirmed transition T (hysteresis=3, confidence>=0.7):
      A. liquidate → re-enter at T+5min: equity multiplied by
         (close[T+24h] / close[T+5min])
      B. buy-and-hold: equity multiplied by (close[T+24h] / close[T])
  - Aggregate Sharpe delta + max-dd delta across all transitions.

Required SPY price data: provided as parameter (DataFrame ts→close).
The harness does not pull it; the caller (e.g., a notebook) supplies it.

Output:
    {
        'splits': [...],
        'aggregate': {
            'n_transitions':  int,
            'sharpe_delta':   float (positive = liquidate-protected better),
            'max_dd_delta':   float,
            'avg_protect_pct': float,
        },
    }

NOT in scope here:
  - Live SPY price fetching (caller's job)
  - Multi-strategy backtest comparing daily vs intraday HMM
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

HYSTERESIS_N      = 3
CONFIDENCE_FLOOR  = 0.70
DEFAULT_HORIZON_HOURS = 24
HMM_INPUT_COLS = [
    'vix_synth_30d', 'vix_synth_90d', 'vix_term_slope',
    'rr_25d', 'spy_realized_vol_30m',
]
STATE_NAMES_BY_RANK = {0: 'LOW_VOL', 1: 'TRANSITIONING',
                        2: 'HIGH_VOL', 3: 'CRISIS'}


@dataclass
class TransitionEvent:
    ts_utc: pd.Timestamp
    from_state: str
    to_state: str
    confidence: float


def _train_hmm(X_train: np.ndarray, n_iter: int = 200, random_state: int = 42):
    from hmmlearn import hmm
    model = hmm.GaussianHMM(
        n_components=4,
        covariance_type='full',
        n_iter=n_iter,
        random_state=random_state,
        tol=1e-4,
    )
    model.fit(X_train)
    return model


def _state_name_map(model) -> dict[int, str]:
    means_per_state = model.means_[:, 0]
    rank_order = np.argsort(means_per_state)
    return {int(rank_order[i]): STATE_NAMES_BY_RANK[i]
            for i in range(len(rank_order))}


def _detect_transitions(model, X_test: np.ndarray, ts_test: np.ndarray,
                         hysteresis_n: int = HYSTERESIS_N,
                         confidence_floor: float = CONFIDENCE_FLOOR
                         ) -> list[TransitionEvent]:
    """Replay test rows through the model. Returns confirmed transitions.

    Mirror of the live detector's hysteresis + confidence-floor logic.
    """
    name_map = _state_name_map(model)
    probs = model.predict_proba(X_test)
    raw_states = np.argmax(probs, axis=1)
    confidences = np.max(probs, axis=1)

    states = []
    for raw, conf in zip(raw_states, confidences):
        nm = name_map.get(int(raw), 'UNKNOWN')
        if conf < confidence_floor:
            states.append('TRANSITIONING')
        else:
            states.append(nm)

    transitions: list[TransitionEvent] = []
    streak = 0
    last_confirmed = None
    for i, (s, ts, conf) in enumerate(zip(states, ts_test, confidences)):
        if i == 0:
            streak = 1
            last_confirmed = s
            continue
        if s == states[i - 1]:
            streak += 1
        else:
            streak = 1
        if streak == hysteresis_n and s != last_confirmed and last_confirmed is not None:
            ts_ev = pd.Timestamp(ts)
            if ts_ev.tzinfo is None:
                ts_ev = ts_ev.tz_localize('UTC')
            transitions.append(TransitionEvent(
                ts_utc=ts_ev, from_state=last_confirmed,
                to_state=s, confidence=float(conf),
            ))
            last_confirmed = s
        elif streak >= hysteresis_n:
            last_confirmed = s
    return transitions


def _score_transition(event: TransitionEvent, prices: pd.DataFrame,
                       horizon_hours: int) -> dict | None:
    """For one transition, compute (a) liquidate-then-reenter return,
    (b) buy-and-hold return over `horizon_hours` forward window."""
    if prices is None or prices.empty:
        return None
    # Normalise tz on both sides so pandas datetime64[us, UTC] columns
    # compare cleanly against tz-aware Timestamps.
    event_ts = pd.Timestamp(event.ts_utc)
    if event_ts.tzinfo is None:
        event_ts = event_ts.tz_localize('UTC')
    end_ts = event_ts + pd.Timedelta(hours=horizon_hours)
    fwd = prices[(prices['ts_utc'] >= event_ts) & (prices['ts_utc'] <= end_ts)]
    if len(fwd) < 2:
        return None
    p_t = float(fwd['close'].iloc[0])
    p_end = float(fwd['close'].iloc[-1])
    bh_return = (p_end / p_t) - 1.0
    # Liquidate-then-reenter: assume re-entry one tick after T (5 min)
    fwd_after = fwd.iloc[1:]
    if fwd_after.empty:
        return None
    p_reentry = float(fwd_after['close'].iloc[0])
    liq_return = (p_end / p_reentry) - 1.0
    return {
        'ts_utc':       str(event.ts_utc),
        'from':         event.from_state,
        'to':           event.to_state,
        'confidence':   round(event.confidence, 4),
        'p_t':          p_t,
        'p_reentry':    p_reentry,
        'p_end':        p_end,
        'bh_return':    round(bh_return, 6),
        'liq_return':   round(liq_return, 6),
        'protect_pct':  round((liq_return - bh_return), 6),
    }


def walk_forward(features_df: pd.DataFrame, prices_df: Optional[pd.DataFrame],
                  n_splits: int = 4,
                  hysteresis_n: int = HYSTERESIS_N,
                  confidence_floor: float = CONFIDENCE_FLOOR,
                  horizon_hours: int = DEFAULT_HORIZON_HOURS,
                  ) -> dict:
    """Run walk-forward backtest. Returns the report dict.

    `features_df` columns: ts_utc + HMM_INPUT_COLS (NaN imputed before
    matrix build).
    `prices_df` columns: ts_utc, close. If None, the report records
    transitions but no P&L scoring (used when SPY price history is
    unavailable in the env)."""
    if features_df.empty:
        return {'error': 'no features data'}

    df = features_df.copy()
    df = df.sort_values('ts_utc').reset_index(drop=True)
    feature_names = [c for c in HMM_INPUT_COLS if c in df.columns]
    if not feature_names:
        return {'error': 'no HMM input cols in features_df'}

    n = len(df)
    split_size = max(1, n // n_splits)
    splits = []
    for i in range(1, n_splits):
        train = df.iloc[: i * split_size]
        test = df.iloc[i * split_size : (i + 1) * split_size]
        if len(train) < 50 or len(test) < 10:
            continue
        # Build matrices
        means = {c: float(train[c].mean()) if not train[c].isna().all() else 0.0
                 for c in feature_names}
        X_train = train[feature_names].fillna(value=means).values
        X_test  = test[feature_names].fillna(value=means).values
        ts_test = test['ts_utc'].values
        try:
            model = _train_hmm(X_train)
        except Exception as e:
            splits.append({'split': i, 'error': f'train failed: {e}'})
            continue
        transitions = _detect_transitions(
            model, X_test, ts_test,
            hysteresis_n=hysteresis_n, confidence_floor=confidence_floor,
        )
        scored = [_score_transition(ev, prices_df, horizon_hours)
                  for ev in transitions]
        scored = [s for s in scored if s is not None]
        splits.append({
            'split':            i,
            'train_rows':       len(train),
            'test_rows':        len(test),
            'transitions':      len(transitions),
            'scored_events':    len(scored),
            'events':           scored[:50],
        })

    # Aggregate across splits
    all_events = [e for s in splits for e in s.get('events', [])]
    if all_events:
        protect_pcts = [e['protect_pct'] for e in all_events]
        avg_protect = float(np.mean(protect_pcts))
        # Sharpe-like: mean / std of protect_pct, annualised by transition cadence
        std_protect = float(np.std(protect_pcts)) if len(protect_pcts) > 1 else 1.0
        sharpe_delta = avg_protect / max(std_protect, 1e-9)
        max_dd = float(np.min(protect_pcts))
    else:
        avg_protect = 0.0
        sharpe_delta = 0.0
        max_dd = 0.0

    return {
        'splits':    splits,
        'aggregate': {
            'n_transitions': len(all_events),
            'avg_protect_pct': round(avg_protect, 6),
            'sharpe_delta':    round(sharpe_delta, 4),
            'max_dd_delta':    round(max_dd, 6),
            'hysteresis_n':    hysteresis_n,
            'confidence_floor': confidence_floor,
            'horizon_hours':   horizon_hours,
        },
    }


def _main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--features',
                    default='data/master/intraday_features.parquet')
    ap.add_argument('--prices', default=None,
                    help='Optional parquet with [ts_utc, close] for SPY '
                         'P&L scoring. If omitted, transitions are '
                         'detected but unscored.')
    ap.add_argument('--n-splits', type=int, default=4)
    ap.add_argument('--out', default='output/intraday_hmm_walkforward.json')
    args = ap.parse_args()

    features = pd.read_parquet(args.features)
    features['ts_utc'] = pd.to_datetime(features['ts_utc'], utc=True)
    prices = None
    if args.prices:
        prices = pd.read_parquet(args.prices)
        prices['ts_utc'] = pd.to_datetime(prices['ts_utc'], utc=True)
    report = walk_forward(features, prices, n_splits=args.n_splits)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    print(json.dumps(report['aggregate'], indent=2))
    return 0


def run_with_resolver(strategy, start, end, resolver, **kwargs):
    """SP-2 Phase A: per-bar resolver opt-in stub.

    Phase A acceptance is "the option exists". This iterates trading days
    and calls resolver.resolve(strategy.id, as_of=bar_date) so callers
    can wire per-bar universes through this engine. Phase B/C will join
    this universe up to the engine's existing logic (which is shaped
    differently across engines — manifest-driven, features-driven, etc.).
    """
    from src.backtest._trading_calendar import trading_days
    results = []
    for bar_date in trading_days(start, end):
        universe = resolver.resolve(strategy.id, as_of=bar_date)
        results.append({"date": bar_date, "universe": universe})
    return results


if __name__ == '__main__':
    raise SystemExit(_main())
