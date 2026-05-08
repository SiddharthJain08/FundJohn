#!/usr/bin/env python3
"""run_intraday_market_state.py — single 5-min tick of the intraday HMM.

Pipeline:
  1. Collect 9 features via collect_intraday_features().
  2. Append features row to data/master/intraday_features.parquet.
  3. If hmm_intraday_latest.pkl exists: score → state + confidence.
     If not: log "no model — accumulating data" and write a state row
     with state=UNKNOWN, confidence=0. Detector still runs every tick;
     model gets trained later on the accumulated parquet.
  4. Hysteresis: read last N=3 entries from intraday_regime_states.
     Require all 3 match the new state before declaring transition.
  5. Confidence override: max prob < 0.70 → force TRANSITIONING.
  6. Cooldown gate: liquidate:cooldown:{date} Redis key — if set, skip
     liquidation regardless of transition.
  7. On confirmed transition (and OPENCLAW_INTRADAY_HMM_LIVE=1):
     - Persist state row with fired_liquidation=True
     - Call regime_liquidator.liquidate_on_regime_change(
         force_transition_tag='INTRADAY_HMM_<from>_<to>')
     - Post to #intraday-regime Discord
  8. Always persist a state row, even when no transition (for hysteresis lookback).

Exit codes:
  0 — normal (any of: feature collected, state appended, transition fired)
  1 — partial failure (e.g., features collected but model load failed)
  2 — unrecoverable (POSTGRES_URI missing, parquet write failed)
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import pickle
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

logger = logging.getLogger(__name__)

# Constants — keep in sync with the daily HMM (run_market_state.py)
HYSTERESIS_N           = 3
CONFIDENCE_FLOOR       = 0.70
TRANSITIONING_FALLBACK = 'TRANSITIONING'
STATE_NAMES_BY_RANK    = {0: 'LOW_VOL', 1: 'TRANSITIONING',
                          2: 'HIGH_VOL', 3: 'CRISIS'}

# HMM input feature ordering — fixed for compatibility with stored pickles.
# When new features are added later (e.g., PCR/0DTE once we wire Polygon
# enrichment), append at the END so prior pickles still work. Order matters.
HMM_INPUT_COLS = [
    'vix_synth_30d', 'vix_synth_90d', 'vix_term_slope',
    'rr_25d', 'spy_realized_vol_30m',
    # NaN-safe ordering: leave OI/volume features at the back; their
    # NaN-imputation strategy may diverge once added to training.
]

MODEL_DIR = ROOT / '.agents' / 'market-state'
MODEL_PATH = MODEL_DIR / 'hmm_intraday_latest.pkl'


# ── Module loaders (bypass src/ingestion/__init__.py) ────────────────────────

def _load_intraday_features_module():
    spec = importlib.util.spec_from_file_location(
        'intraday_features', ROOT / 'src' / 'ingestion' / 'intraday_features.py',
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_regime_liquidator_module():
    # liquidator imports execution.alpaca_trader internally; needs sys.path.
    from execution import regime_liquidator as rl  # noqa
    return rl


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_live_intraday() -> bool:
    """Gate independent of OPENCLAW_ALPACA_LIVE_LIQUIDATE. Both must be 1
    to actually fire broker action."""
    return os.environ.get('OPENCLAW_INTRADAY_HMM_LIVE') == '1'


def _connect_postgres():
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return None
    try:
        import psycopg2
    except ImportError:
        return None
    try:
        return psycopg2.connect(uri, connect_timeout=10)
    except Exception as e:
        logger.warning('postgres connect failed: %s', e)
        return None


def _last_n_states(conn, n: int) -> list[dict]:
    """Pull the last N intraday_regime_states rows ordered DESC by ts_utc.
    Returns list of dicts (newest first)."""
    cur = conn.cursor()
    cur.execute(
        """SELECT ts_utc, state, prior_state, confidence, hysteresis_streak,
                  fired_liquidation, transition_tag
             FROM intraday_regime_states
             ORDER BY ts_utc DESC
             LIMIT %s""",
        (n,),
    )
    cols = ('ts_utc', 'state', 'prior_state', 'confidence',
            'hysteresis_streak', 'fired_liquidation', 'transition_tag')
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    return rows


def _persist_state_row(conn, ts_utc, state, prior_state, confidence,
                        hysteresis_streak, fired_liquidation, transition_tag,
                        features_dict):
    """Insert one row. Idempotent on PRIMARY KEY conflict."""
    cur = conn.cursor()
    safe_features = {k: (None if (isinstance(v, float) and np.isnan(v)) else v)
                     for k, v in features_dict.items()
                     if k != 'ts_utc'}
    cur.execute(
        """INSERT INTO intraday_regime_states
             (ts_utc, state, prior_state, confidence, hysteresis_streak,
              fired_liquidation, transition_tag, features_json)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (ts_utc) DO NOTHING""",
        (ts_utc, state, prior_state, float(confidence),
         int(hysteresis_streak), bool(fired_liquidation),
         transition_tag, json.dumps(safe_features, default=str)),
    )
    conn.commit()
    cur.close()


def _redis():
    """Best-effort Redis. Mirrors regime_liquidator._redis."""
    try:
        import redis
        url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
        r = redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _state_from_hmm(model, features: dict) -> tuple[str, float, np.ndarray]:
    """Score one feature dict against the trained HMM.

    Returns (state_name, confidence, state_probs).
    NaN feature values are imputed with column means stored on the
    model object (set at training time as `.feature_means_`).
    """
    means = getattr(model, 'feature_means_', None)
    feature_names = getattr(model, 'feature_names_', HMM_INPUT_COLS)
    x = np.array([[
        (features.get(c) if not (
            isinstance(features.get(c), float) and np.isnan(features.get(c))
        ) else (means.get(c) if means else 0.0))
        for c in feature_names
    ]], dtype=float)

    # Re-impute remaining NaN with 0 (training-time fallback)
    x = np.nan_to_num(x, nan=0.0)

    state_probs = model.predict_proba(x)[0]
    state_raw = int(np.argmax(state_probs))
    confidence = float(np.max(state_probs))

    # State→regime map: rely on `model.regime_name_by_state_` if the
    # trainer set it, else fall back to ascending VIX-mean ordering.
    name_map = getattr(model, 'regime_name_by_state_', None)
    if name_map is None:
        # Sort raw states by mean VIX of their first feature col.
        means_per_state = model.means_[:, 0]
        rank_order = np.argsort(means_per_state)
        name_map = {int(rank_order[i]): STATE_NAMES_BY_RANK[i]
                    for i in range(len(rank_order))}
    state_name = name_map.get(state_raw, 'UNKNOWN')
    return state_name, confidence, state_probs


def _maybe_apply_confidence_floor(state_name: str, confidence: float) -> str:
    if confidence < CONFIDENCE_FLOOR:
        return TRANSITIONING_FALLBACK
    return state_name


def _hysteresis_streak(history: list[dict], current_state: str) -> int:
    """Count consecutive most-recent rows whose state matches current_state.
    history is newest-first."""
    n = 0
    for row in history:
        if row.get('state') == current_state:
            n += 1
        else:
            break
    return n + 1   # +1 because the new tick itself counts


def _confirmed_transition(history: list[dict], current_state: str,
                           streak: int, n_required: int) -> tuple[bool, str | None]:
    """Decide whether this tick is a confirmed transition.

    A "confirmed transition" requires:
      - The current state has been observed for ≥ N_required ticks in a row
        (this tick included; that's `streak >= N_required`).
      - The state IMMEDIATELY before the streak started was different.

    Returns (fired, prior_state). prior_state is the state we're
    transitioning FROM, derived from the first row whose state differs
    from current_state in the newest-first history.
    """
    if streak < n_required:
        return False, None
    prior = None
    for row in history:
        if row.get('state') != current_state:
            prior = row.get('state')
            break
    if prior is None:
        # First-time observation — no prior state recorded; not a transition.
        return False, None
    return True, prior


# ── Discord notifier ─────────────────────────────────────────────────────────

def _post_to_discord(channel: str, msg: str) -> None:
    """Reuse regime_liquidator's webhook lookup pattern. Best-effort."""
    try:
        rl = _load_regime_liquidator_module()
        rl._post_to_discord(channel, msg)
    except Exception as e:
        logger.warning('discord post failed: %s', e)


# ── Main ─────────────────────────────────────────────────────────────────────

def run_one_tick(force_dry_run: bool = False) -> dict:
    """Execute a single 5-min tick. Returns the result dict (also
    persisted to intraday_regime_states)."""
    intraday = _load_intraday_features_module()

    now = pd.Timestamp.now(tz='UTC')
    features = intraday.collect_intraday_features(now_utc=now)
    intraday.append_features_row(features)

    conn = _connect_postgres()
    if conn is None:
        return {'action': 'error', 'reason': 'postgres_unavailable'}

    # 2) Score against HMM if model exists
    model = None
    state_name = 'UNKNOWN'
    confidence = 0.0
    if MODEL_PATH.exists():
        try:
            with open(MODEL_PATH, 'rb') as f:
                model = pickle.load(f)
            # Skip scoring if synthetic VIX is NaN (collector failed)
            if not (isinstance(features.get('vix_synth_30d'), float)
                    and np.isnan(features.get('vix_synth_30d'))):
                state_name, confidence, _probs = _state_from_hmm(model, features)
                # Apply confidence override
                state_name = _maybe_apply_confidence_floor(state_name, confidence)
        except Exception as e:
            logger.warning('HMM score failed: %s', e)
    else:
        logger.info('HMM model not yet trained (no %s) — accumulating data only',
                    MODEL_PATH.name)

    # 3) Hysteresis lookback
    history = _last_n_states(conn, HYSTERESIS_N)
    streak = _hysteresis_streak(history, state_name)
    fired, prior_state = _confirmed_transition(
        history, state_name, streak, HYSTERESIS_N,
    )

    transition_tag = None
    fired_liquidation = False

    # 4) Cooldown + live gates
    if fired and prior_state and not force_dry_run:
        rcli = _redis()
        cooldown_active = False
        if rcli is not None:
            try:
                date_str = features['ts_utc'].strftime('%Y-%m-%d')
                if rcli.get(f'liquidate:cooldown:{date_str}'):
                    cooldown_active = True
            except Exception:
                pass

        if cooldown_active:
            logger.info('confirmed transition %s→%s blocked by cooldown gate',
                        prior_state, state_name)
            transition_tag = f'INTRADAY_HMM_{prior_state}_{state_name}_COOLDOWN'
        elif _is_live_intraday():
            transition_tag = f'INTRADAY_HMM_{prior_state}_{state_name}'
            try:
                rl = _load_regime_liquidator_module()
                liq_result = rl.liquidate_on_regime_change(
                    force_transition_tag=transition_tag,
                )
                if liq_result.get('action') in ('liquidated', 'dry_run'):
                    fired_liquidation = True
                    _post_to_discord(
                        'intraday-regime',
                        f':zap: **Intraday HMM** confirmed {prior_state} → {state_name} '
                        f'(streak={streak}, conf={confidence:.2f}, '
                        f'fired={liq_result.get("action")})',
                    )
                else:
                    logger.info('liquidator returned %s', liq_result.get('action'))
            except Exception as e:
                logger.warning('liquidator call failed: %s', e)
        else:
            transition_tag = f'INTRADAY_HMM_{prior_state}_{state_name}_DRYRUN'
            logger.info('confirmed transition %s→%s (DRY-RUN, OPENCLAW_INTRADAY_HMM_LIVE=0)',
                        prior_state, state_name)
            _post_to_discord(
                'intraday-regime',
                f':warning: **Intraday HMM DRY-RUN** would fire {prior_state} → {state_name} '
                f'(streak={streak}, conf={confidence:.2f})',
            )

    # 5) Persist state row
    last_row_state = history[0]['state'] if history else None
    try:
        _persist_state_row(
            conn, features['ts_utc'], state_name, last_row_state,
            confidence, streak, fired_liquidation, transition_tag, features,
        )
    except Exception as e:
        logger.warning('state persist failed: %s', e)

    conn.close()

    return {
        'action': 'tick',
        'ts_utc':           str(features['ts_utc']),
        'state':            state_name,
        'prior':            last_row_state,
        'confidence':       round(confidence, 4),
        'streak':           streak,
        'fired':            fired_liquidation,
        'transition_tag':   transition_tag,
        'model_loaded':     model is not None,
        'quality_flag':     features.get('source_quality_flag'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='Skip liquidation broker call regardless of '
                         'OPENCLAW_INTRADAY_HMM_LIVE.')
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [INTRADAY] %(message)s')
    try:
        result = run_one_tick(force_dry_run=args.dry_run)
        print(json.dumps(result, default=str))
        if result.get('action') == 'error':
            return 2
        return 0
    except Exception as e:
        logger.exception('intraday tick failed')
        return 1


if __name__ == '__main__':
    sys.exit(main())
