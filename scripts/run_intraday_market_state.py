#!/usr/bin/env python3
"""run_intraday_market_state.py — single 5-min tick of the intraday HMM.

Pipeline:
  1. Collect 9 features via collect_intraday_features().
  2. Append features row to data/master/intraday_features.parquet.
  2b. If the tick is OUTSIDE option-market hours (9:30-16:15 ET), persist a
      carry-forward row (last state, streak extended, quality_flag=3) and
      STOP — frozen chain quotes are time-decay drift, not signal, and the
      model is trained on RTH rows only. No scoring, no transitions.
  3. If hmm_intraday_latest.pkl exists: score → state + confidence.
     If not: log "no model — accumulating data" and write a state row
     with state=UNKNOWN, confidence=0. Detector still runs every tick;
     model gets trained later on the accumulated parquet.
  4. Hysteresis: read last N=3 entries from intraday_regime_states.
     Require all 3 match the new state before declaring transition.
  5. Confidence override: max prob < 0.70 → force TRANSITIONING.
  6. Cooldown gate: redeploy:cooldown:{date} OR liquidate:cooldown:{date} —
     either key (set by a prior redeploy or a manual --force flatten)
     audit row records the transition with a _COOLDOWN suffix tag.
  7. On confirmed transition (Phase 2, 2026-05-19): spawn the pipeline
     redeploy via scripts/redeploy_pipeline.py (detached, fire-and-forget).
     Audit row uses transition_tag=INTRADAY_HMM_REDEPLOY_<from>_<to> and
     fired_liquidation=True. Cooldown-blocked transitions keep the
     existing _COOLDOWN suffix and do not spawn.
  8. Always persist a state row, even when no transition (for hysteresis lookback).

Exit codes:
  0 — normal (any of: feature collected, state appended, transition detected)
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
import subprocess
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

# SPY option market hours (ET). Outside this window chain quotes are FROZEN
# at the prior close, so the synthetic-VIX features become a deterministic
# time-decay ramp rather than market signal — scoring them produced artifact
# state flips (2026-06-02: all 6 flips after-hours; 06-03: 12 of 40). Ticks
# outside the window run the carry-forward path instead of the HMM.
# Mirrors the dashboard's CARRY-FWD QUOTES badge (server.js, 570..975 min).
OPTION_MKT_OPEN_MIN  = 9 * 60 + 30    # 9:30 ET
OPTION_MKT_CLOSE_MIN = 16 * 60 + 15   # 16:15 ET

# Tiered hysteresis: (required_ticks_to_fire, required_confidence_at_fire).
# Higher-severity upward transitions get faster confirmation (fewer ticks)
# but tighter confidence floors to counterbalance noise. Downward transitions
# always use the conservative (3, 0.70) — no urgency to re-add risk, and
# whipsaw protection matters more than speed.
HYSTERESIS_TIERS       = {
    'CRISIS':        (1, 0.90),
    'HIGH_VOL':      (2, 0.80),
    'TRANSITIONING': (3, 0.70),
    'LOW_VOL':       (3, 0.70),
}
_DOWNWARD_TIER         = (3, 0.70)
_STATE_RANK            = {'LOW_VOL': 0, 'TRANSITIONING': 1,
                          'HIGH_VOL': 2, 'CRISIS': 3}
# How many rows of state history to fetch when deciding whether a
# confirmed transition has happened. Must be > HYSTERESIS_N so that the
# previously CONFIRMED regime stays visible past any short noise tick.
# 120 ticks ≈ 10h of 5-min ticks — spans a full trading session plus
# yesterday's last confirmed state through the overnight gap.
LOOKBACK_FOR_CONFIRMED = 120

# HMM input feature ordering — FALLBACK ONLY: used when a stored pickle
# lacks `feature_names_` (every model since the 2026-05-20 v3 trainer sets
# it). Kept in sync with train_intraday_hmm.HMM_INPUT_COLS (v3: dropped
# spy_realized_vol_30m — dead since the SP-1 Polygon purge — and added the
# two daily-derived features).
HMM_INPUT_COLS = [
    'vix_synth_30d', 'vix_synth_90d', 'vix_term_slope',
    'rr_25d', 'spy_gk_vol_daily', 'vvix_level',
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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_live_intraday() -> bool:
    """Phase 1: surfaces in the Discord notification only (no broker
    action — see Phase 2 for the redeploy spawn this flag will arm)."""
    return os.environ.get('OPENCLAW_INTRADAY_HMM_LIVE') == '1'


def _is_option_market_open(ts_utc) -> bool:
    """True when SPY options are trading (9:30-16:15 ET, Mon-Fri).

    Market holidays are NOT modelled (same as the dashboard badge) — a
    holiday weekday scores frozen quotes exactly as it did before this
    guard existed; acceptable, since the failure mode is one stale day,
    not a nightly recurrence.
    """
    ts = pd.Timestamp(ts_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    et = ts.tz_convert('America/New_York')
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return OPTION_MKT_OPEN_MIN <= minutes <= OPTION_MKT_CLOSE_MIN


def _carry_forward_tick(conn, features: dict) -> dict:
    """Persist a carry-forward state row WITHOUT scoring the HMM.

    Used for every tick outside option-market hours (pre-9:30 / post-16:15
    ET): the chain quotes are frozen, so the model would classify
    deterministic time-decay drift it was never trained on (the trainer
    filters to RTH). Instead, carry the last persisted state forward with
    its streak extended so hysteresis lookback and the dashboard duration
    stay continuous across the closed window. No transition can fire and
    no market_regime row is appended, so a frozen-quote artifact can never
    overwrite the regime-of-record overnight. regime_latest.json IS still
    refreshed each tick — but with the SETTLED carried state, not the
    artifact — so the engine's staleness gate stays satisfied across the
    weekend now that the daily detector no longer writes it.
    """
    history = _last_n_states(conn, LOOKBACK_FOR_CONFIRMED)
    if history:
        # Carry the SETTLED regime (last fired row, or streak>=3 fallback),
        # NOT the raw last state: an unconfirmed 1-tick boundary flip on the
        # final scored tick must not own the night — its streak would grow
        # past the settled threshold by morning and the first real ticks
        # would fire a spurious 'transition back' + redeploy.
        carried = (_find_settled_regime(history)
                   or history[0].get('state') or 'UNKNOWN')
        carried_conf = 0.0
        for row in history:
            if row.get('state') == carried:
                try:
                    carried_conf = float(row.get('confidence') or 0.0)
                except (TypeError, ValueError):
                    carried_conf = 0.0
                break
    else:
        # Cold start while the market is closed — mirror bootstrap mode.
        carried, carried_conf = 'UNKNOWN', 0.0
    streak = _hysteresis_streak(history, carried)
    try:
        _persist_state_row(
            conn, features['ts_utc'], carried, carried, carried_conf,
            streak, False, None, features,
        )
    except Exception as e:
        logger.warning('state persist failed: %s', e)
    # Keep regime_latest.json fresh (intraday is the sole authority). The
    # carried state is a settled regime, so no artifact reaches the file.
    _refresh_regime_file(
        state=carried, confidence=carried_conf,
        vix=features.get('vix_synth_30d'),
        prior_state=carried, state_probs=None,
        ts_utc=features['ts_utc'], transition_tag=None,
    )
    return {
        'action':         'tick',
        'carry_fwd':      True,
        'ts_utc':         str(features['ts_utc']),
        'state':          carried,
        'prior':          carried,
        'confidence':     round(carried_conf, 4),
        'streak':         streak,
        'fired':          False,
        'transition_tag': None,
        'model_loaded':   False,
        'quality_flag':   features.get('source_quality_flag'),
    }


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
    """Best-effort Redis client. Returns None on any connect failure."""
    try:
        import redis
        url = os.environ.get('REDIS_URL', 'redis://localhost:6379')
        r = redis.from_url(url, socket_connect_timeout=3, decode_responses=True)
        r.ping()
        return r
    except Exception:
        return None


def _enrich_with_daily_derived(features: dict) -> dict:
    """Inject spy_gk_vol_daily and vvix_level from data/master daily sources.

    The live intraday collector emits VIX/term/skew features tick-by-tick,
    but the HMM (since 2026-05-20) also expects:
      • spy_gk_vol_daily — Garman-Klass daily SPY vol from prices.parquet
      • vvix_level       — CBOE VVIX from macro.parquet

    These are daily quantities; we use the latest available value ≤ tick
    date. Fail-soft: if either source is missing/empty the feature stays
    out of the dict and `_state_from_hmm` will impute it from
    `model.feature_means_`.
    """
    try:
        import pandas as pd  # local import — keeps cold-start of bootstrap mode cheap
        ROOT = Path(__file__).resolve().parents[1]
        prices_path = ROOT / 'data' / 'master' / 'prices.parquet'
        macro_path  = ROOT / 'data' / 'master' / 'macro.parquet'
        # Use the tick's ET date as the lookup target. Intraday ticks during
        # an open session pull yesterday's daily values — fine, they don't
        # move enough within a day to matter for regime classification.
        ts = pd.Timestamp(features.get('ts_utc')) if features.get('ts_utc') else pd.Timestamp.now(tz='UTC')
        if ts.tz is None: ts = ts.tz_localize('UTC')
        tick_date = ts.tz_convert('America/New_York').date()
        # GK daily vol
        if prices_path.exists():
            spy = pd.read_parquet(prices_path)
            spy = spy[(spy['ticker'] == 'SPY') & spy['low'].notna() & (spy['low'] > 0)]
            spy = spy.dropna(subset=['open', 'high', 'low', 'close'])
            spy['date'] = pd.to_datetime(spy['date']).dt.date
            spy = spy[spy['date'] <= tick_date].sort_values('date')
            if len(spy):
                last = spy.iloc[-1]
                ln_hl = np.log(last['high'] / last['low'])
                ln_co = np.log(last['close'] / last['open'])
                gk_var = max(0.0, 0.5 * ln_hl ** 2 - (2 * np.log(2) - 1) * ln_co ** 2)
                features['spy_gk_vol_daily'] = float(np.sqrt(gk_var) * np.sqrt(252))
        # VVIX from macro
        if macro_path.exists():
            macro = pd.read_parquet(macro_path)
            vvix = macro[macro['series'] == 'VVIX'].copy()
            vvix['date'] = pd.to_datetime(vvix['date']).dt.date
            vvix = vvix[vvix['date'] <= tick_date].sort_values('date')
            if len(vvix):
                features['vvix_level'] = float(vvix['value'].iloc[-1])
    except Exception as e:
        logger.warning('derived-feature enrichment failed (soft): %s', e)
    return features


def _refresh_regime_file(
    *,
    state: str,
    confidence: float,
    vix,
    prior_state: str | None,
    state_probs=None,
    ts_utc,
    transition_tag: str | None = None,
) -> None:
    """Merge-write regime_latest.json so engine.load_regime()'s 80h staleness
    gate stays satisfied with the intraday detector as the SOLE regime
    authority (2026-06-08: daily run_market_state.py no longer writes the
    regime-of-record — it had emitted a false CRISIS off a stale Friday VIX
    close while the intraday primary read LOW_VOL conf~1.0).

    Called on EVERY tick — scored, carry-forward, and transition — to keep
    the file's mtime fresh across the option-closed window (Fri 19:55 ET →
    Mon 09:00 ET ~= 61h < 80h). A tick whose `state` is not one of the four
    real regimes (UNKNOWN bootstrap, NaN-VIX skip) advances the freshness
    marker but PRESERVES the file's last good `state`: engine.load_regime()
    does `state = j.get('state') or 'HIGH_VOL'`, so writing UNKNOWN would
    mis-route eligibility and risk the empty-signals orphan-close blowout.
    """
    regime_file = MODEL_DIR / 'regime_latest.json'
    try:
        existing = {}
        if regime_file.exists():
            try:
                existing = json.loads(regime_file.read_text())
            except json.JSONDecodeError:
                existing = {}

        updated = dict(existing)
        if state in _STATE_RANK:                       # one of the four real regimes
            updated['state']     = state
            updated['state_raw'] = state
            updated['confidence'] = round(float(confidence), 4)
            updated['prior_state'] = prior_state
            if state_probs is not None:
                pm = {}
                try:
                    for i, p in enumerate(state_probs):
                        pm[STATE_NAMES_BY_RANK.get(i, f's{i}')] = round(float(p), 4)
                except Exception:
                    pm = {}
                if pm:
                    updated['state_probabilities'] = pm
            if vix is not None:
                try:
                    updated['vix_level'] = round(float(vix), 2)
                except (TypeError, ValueError):
                    pass

        # Freshness markers advance on EVERY call (even UNKNOWN) so the
        # engine's mtime-based stale-gate is satisfied across closed windows.
        updated['intraday_source']     = 'intraday_hmm'
        updated['intraday_updated_at'] = str(ts_utc)
        updated['intraday_transition'] = transition_tag

        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        tmp = regime_file.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(updated, indent=2))
        os.replace(tmp, regime_file)
    except Exception as e:
        logger.warning('regime file refresh failed: %s', e)


def _sync_regime_to_consumers(
    *,
    conn,
    new_state: str,
    prior_state: str | None,
    confidence: float,
    state_probs: np.ndarray | None,
    features: dict,
    ts_utc,
    transition_tag: str | None,
) -> None:
    """Propagate a confirmed intraday regime transition into the canonical
    consumer surfaces: `market_regime` Postgres table and `regime_latest.json`.

    The engine reads regime_latest.json (file-primary, with stale-gate);
    the sizer reads it via the handoff's regime block; the dashboard reads
    market_regime DB rows. Without this sync, the redeploy that fires
    after a confirmed transition will read the stale daily-HMM regime
    and effectively re-run with the OLD regime's sizer params and
    strategy eligibility.

    Both sinks are append/upsert; the next daily HMM run at 9 AM ET will
    overwrite them with the daily-cadence state.
    """
    state_probs_map = {}
    if state_probs is not None:
        try:
            for i, p in enumerate(state_probs):
                state_probs_map[STATE_NAMES_BY_RANK.get(i, f's{i}')] = round(float(p), 4)
        except Exception:
            state_probs_map = {}

    vix_intraday = features.get('vix_synth_30d')
    try:
        vix_curr = round(float(vix_intraday), 2) if vix_intraday is not None else None
    except (TypeError, ValueError):
        vix_curr = None

    intraday_meta = {
        'state_raw':           new_state,
        'state_probabilities': state_probs_map,
        'confidence':          round(float(confidence), 4),
        'prior_state':         prior_state,
        'source':              'intraday_hmm',
        'transition_tag':      transition_tag,
        'ts_utc':              str(ts_utc),
    }

    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO market_regime (state, vix_level, vix_percentile, regime_data)"
            " VALUES (%s, %s, %s, %s)",
            (new_state, vix_curr, None, json.dumps(intraday_meta)),
        )
        conn.commit()
        cur.close()
        logger.info('regime sync: market_regime row appended → %s (vix=%s)',
                    new_state, vix_curr)
    except Exception as e:
        logger.warning('regime sync: market_regime write failed: %s', e)

    # The file half of the sync is the shared per-tick refresh: it merge-writes
    # regime_latest.json from the (raw) state_probs + vix this transition saw.
    _refresh_regime_file(
        state=new_state, confidence=confidence, vix=vix_curr,
        prior_state=prior_state, state_probs=state_probs,
        ts_utc=ts_utc, transition_tag=transition_tag,
    )
    logger.info('regime sync: regime_latest.json updated → %s', new_state)


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


def _is_upward(prior_state: str | None, new_state: str) -> bool:
    """True if new_state has higher severity rank than prior_state."""
    if prior_state is None:
        return False
    return _STATE_RANK.get(new_state, -1) > _STATE_RANK.get(prior_state, -1)


def _tier_for_transition(prior_state: str | None, new_state: str) -> tuple[int, float]:
    """Return (required_ticks, required_confidence) for transition into new_state.
    With OPENCLAW_INTRADAY_15MIN_PREFETCH=1, ALL transitions require a uniform
    3-tick (45-min) confirmation at the 0.70 floor — the 15-min cadence already
    filters noise, so we drop the faster upward tiers. Flag OFF = legacy tiers."""
    import os
    if os.environ.get('OPENCLAW_INTRADAY_15MIN_PREFETCH') == '1':
        return (3, CONFIDENCE_FLOOR)
    if _is_upward(prior_state, new_state):
        return HYSTERESIS_TIERS.get(new_state, _DOWNWARD_TIER)
    return _DOWNWARD_TIER


def _find_settled_regime(history: list[dict]) -> str | None:
    """Find the regime the system is currently 'in' — the settled regime.

    Two ways a regime row counts as settled:
      1. The row's `fired_liquidation` is True — a confirmed transition
         was acted on, definitively establishing this state as the
         current regime (until the next transition fires).
      2. The row's `hysteresis_streak` >= 3 — the state has been
         continuously observed for at least 3 ticks (15 min). 3 is the
         most conservative tier minimum and serves as the "no recent
         fire" fallback for cold-starts or post-cooldown ticks.

    Walk newest→oldest and return the first matching row's state. This
    decouples "what regime are we in" from "what tier triggers a fire"
    so downward transitions (which require 3 ticks per _DOWNWARD_TIER)
    can fire even when the destination state has a shorter upward tier
    (e.g., HIGH_VOL upward = 2 ticks; CRISIS→HIGH_VOL downward = 3 ticks
    still works because we never recognize HIGH_VOL as settled at 2 ticks).
    """
    for row in history:
        state = row.get('state')
        if not state:
            continue
        if row.get('fired_liquidation'):
            return state
        try:
            row_streak = int(row.get('hysteresis_streak') or 0)
        except (TypeError, ValueError):
            row_streak = 0
        if row_streak >= 3:
            return state
    return None


def _confirmed_transition(history: list[dict], current_state: str,
                           streak: int, current_confidence: float) -> tuple[bool, str | None]:
    """Decide whether this tick is a confirmed transition.

    Find the SETTLED regime (most recent fired row, or oldest long-streak
    row as fallback). If it differs from current_state, look up the tier
    for (settled → current_state) and fire only if both `streak` and
    `current_confidence` meet the tier thresholds.

    Tier rules:
      - Upward (less severe → more severe): destination state's tier
        from HYSTERESIS_TIERS (CRISIS=1/0.90, HIGH_VOL=2/0.80,
        TRANSITIONING & LOW_VOL=3/0.70)
      - Downward or same: _DOWNWARD_TIER (3, 0.70) — no urgency to
        re-add risk on regime normalization; whipsaw protection.

    Cold-start (no settled regime in lookback) → no fire.
    """
    settled = _find_settled_regime(history)
    if settled is None or settled == current_state:
        return False, None

    n_required, conf_required = _tier_for_transition(settled, current_state)
    if streak < n_required:
        return False, None
    if current_confidence < conf_required:
        return False, None
    return True, settled


def _cooldown_active(kv: dict, date_str: str) -> bool:
    """Which cooldown keys block a confirmed-transition redeploy.
    Flag ON: only a manual liquidate cooldown blocks (the 60-min redeploy
    cooldown is dropped — 45-min 3-tick persistence is the throttle).
    Flag OFF: legacy — both redeploy and liquidate cooldowns block."""
    keys = [f'liquidate:cooldown:{date_str}']
    if os.environ.get('OPENCLAW_INTRADAY_15MIN_PREFETCH') != '1':
        keys.append(f'redeploy:cooldown:{date_str}')
    return any(kv.get(k) for k in keys)


def make_episode(ts, state: str) -> str:
    """Per-transition-episode key shared by tick-1 (candidate) and tick-3 (gate).
    `ts` MUST be the streak-start (first new-state) tick. Correctness of the
    gate depends ONLY on tick-1 and tick-3 producing the SAME string for the
    same streak-start tick; the reconstruction is best-effort."""
    return f"{ts.strftime('%Y-%m-%d')}:{state}:{ts.floor('15min').isoformat()}"


def _is_candidate_transition(settled, state, streak, confidence, market_open) -> bool:
    """First-tick signal of a (not-yet-confirmed) transition — the trigger to
    warm a prices-only refetch so data is fresh by the 3rd-tick confirmation."""
    return (
        market_open
        and settled is not None
        and state != settled
        and streak == 1
        and confidence >= CONFIDENCE_FLOOR
    )


# ── Refetch prices spawner (tick-1 candidate prefetch) ───────────────────────

def _spawn_refetch_prices(date_str: str) -> str:
    """Spawn scripts/refetch_prices.py DETACHED (fire-and-forget)."""
    log_path = ROOT / 'logs' / f'refetch_prices_{date_str}.log'
    cmd = [sys.executable, str(ROOT / 'scripts' / 'refetch_prices.py'), '--date', date_str]
    try:
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except Exception:
        fd = subprocess.DEVNULL
    try:
        subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                         stdout=fd, stderr=fd, start_new_session=True, close_fds=True)
    except Exception as e:
        logger.error('refetch spawn failed: %s', e)
        return 'spawn_error'
    finally:
        if isinstance(fd, int) and fd != subprocess.DEVNULL:
            try: os.close(fd)
            except Exception: pass
    return 'spawned'


# ── Redeploy spawner (Phase 2) ───────────────────────────────────────────────

def _spawn_redeploy(prior_state: str, new_state: str, date_str: str,
                    *, dry_run: bool, episode: str | None = None) -> str:
    """Spawn scripts/redeploy_pipeline.py DETACHED. Returns 'spawned' or
    'dry-run' for the Discord-summary tag, or 'spawn_error' on Popen failure.

    Detached pattern matches cron-schedule.js:326-332: own session, stdio
    redirected to a per-date log file, parent does not wait. The 5-min
    intraday cron tick must return in <2s; the orchestrator itself runs up
    to 30 min inside the detached child.

    `episode` (the tick-3 expected episode) episode-binds the redeploy's
    data-ready gate so it can ONLY proceed on a sentinel stamped to THIS
    transition's episode — closing the stale-`done` freshness hole. None
    (legacy/flag-OFF path) leaves the gate's episode-matching a no-op.
    """
    reason = f'INTRADAY_HMM_{prior_state}_{new_state}'
    log_dir = ROOT / 'logs'
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    log_path = log_dir / f'redeploy_pipeline_{date_str}.log'
    cmd = [
        sys.executable,
        str(ROOT / 'scripts' / 'redeploy_pipeline.py'),
        '--reason', reason,
        '--date', date_str,
    ]
    if episode is not None:
        cmd += ['--episode', episode]
    if dry_run:
        cmd.append('--dry-run')

    try:
        log_fd = os.open(
            str(log_path),
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o644,
        )
    except Exception as e:
        logger.warning('redeploy spawn: log open failed (%s) — using DEVNULL', e)
        log_fd = subprocess.DEVNULL

    try:
        subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,
            close_fds=True,
        )
    except Exception as e:
        logger.error('redeploy spawn failed: %s', e)
        if isinstance(log_fd, int) and log_fd != subprocess.DEVNULL:
            try:
                os.close(log_fd)
            except Exception:
                pass
        return 'spawn_error'

    # Parent must close its copy of the fd; the child inherits its own.
    if isinstance(log_fd, int) and log_fd != subprocess.DEVNULL:
        try:
            os.close(log_fd)
        except Exception:
            pass
    return 'dry-run' if dry_run else 'spawned'


# ── Discord notifier ─────────────────────────────────────────────────────────

def _post_webhook(channel: str, msg: str) -> bool:
    """Look up the persisted webhook for `channel` in agent_registry and
    POST `msg`. Inlined here so this module has no dependency on the
    auto-liquidator after Phase 1.
    """
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return False
    try:
        import psycopg2
        import requests
    except ImportError:
        return False
    url = None
    try:
        conn = psycopg2.connect(uri, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(
            "SELECT webhook_urls FROM agent_registry WHERE webhook_urls IS NOT NULL"
        )
        for (hooks,) in cur.fetchall():
            if hooks and channel in hooks:
                url = hooks[channel]
                break
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning('[intraday] webhook lookup failed: %s', e)
        return False
    if not url:
        logger.info('[intraday] no webhook for #%s', channel)
        return False
    try:
        r = requests.post(url, json={'content': msg[:1900]}, timeout=10)
        return bool(r.ok)
    except Exception as e:
        logger.warning('[intraday] webhook post failed: %s', e)
        return False


def _post_to_discord(channel: str, msg: str) -> None:
    """Best-effort Discord post. Falls back to `botjohn-log` if the
    requested channel has no webhook registered in agent_registry.
    """
    try:
        ok = _post_webhook(channel, msg)
        if not ok and channel != 'botjohn-log':
            _post_webhook('botjohn-log', f'[{channel}] {msg}')
    except Exception as e:
        logger.warning('discord post failed: %s', e)


# ── Main ─────────────────────────────────────────────────────────────────────

def run_one_tick(force_dry_run: bool = False) -> dict:
    """Execute a single 5-min tick. Returns the result dict (also
    persisted to intraday_regime_states)."""
    intraday = _load_intraday_features_module()

    now = pd.Timestamp.now(tz='UTC')
    features = intraday.collect_intraday_features(now_utc=now)
    market_open = _is_option_market_open(features['ts_utc'])
    if not market_open:
        # Option market closed: quotes frozen → features are carry-forward,
        # not signal. Flag 3 marks the parquet row (trainer's <2 filter
        # excludes it from training) and the DB row's features_json.
        features['source_quality_flag'] = 3
    intraday.append_features_row(features)

    if not market_open:
        conn = _connect_postgres()
        if conn is None:
            return {'action': 'error', 'reason': 'postgres_unavailable'}
        try:
            return _carry_forward_tick(conn, features)
        finally:
            conn.close()

    # Enrich with daily-derived HMM inputs (spy_gk_vol_daily, vvix_level).
    # These are NOT written to the parquet — they're score-time injections
    # so the live tick matches the trained model's 6-feature schema.
    features = _enrich_with_daily_derived(features)

    conn = _connect_postgres()
    if conn is None:
        return {'action': 'error', 'reason': 'postgres_unavailable'}

    # 2) Score against HMM if model exists
    model = None
    state_name = 'UNKNOWN'
    confidence = 0.0
    state_probs = None
    if MODEL_PATH.exists():
        try:
            with open(MODEL_PATH, 'rb') as f:
                model = pickle.load(f)
            # Skip scoring if synthetic VIX is NaN (collector failed)
            if not (isinstance(features.get('vix_synth_30d'), float)
                    and np.isnan(features.get('vix_synth_30d'))):
                state_name, confidence, state_probs = _state_from_hmm(model, features)
                # Apply confidence override
                state_name = _maybe_apply_confidence_floor(state_name, confidence)
        except Exception as e:
            logger.warning('HMM score failed: %s', e)
    else:
        logger.info('HMM model not yet trained (no %s) — accumulating data only',
                    MODEL_PATH.name)

    # 3) Hysteresis lookback. Fetch wider than HYSTERESIS_N so that
    # _confirmed_transition can see past short noise ticks to the most
    # recent CONFIRMED state. _hysteresis_streak still stops at the
    # first non-match so the extra rows are only used by the
    # confirmed-prior search.
    history = _last_n_states(conn, LOOKBACK_FOR_CONFIRMED)
    streak = _hysteresis_streak(history, state_name)
    fired, prior_state = _confirmed_transition(
        history, state_name, streak, confidence,
    )

    transition_tag = None
    fired_liquidation = False

    # 4a) Tick-1 candidate prefetch (OPENCLAW_INTRADAY_15MIN_PREFETCH)
    # On the FIRST tick of a candidate transition (streak==1, market open,
    # conf≥floor, state differs from settled), spawn a prices-only refetch
    # so data is fresh by the time the 3rd tick confirms.  Debounced per
    # episode so it fires at most once per candidate episode.
    from src.execution import intraday_prefetch as _pf
    if _pf.prefetch_enabled() and not force_dry_run:
        settled = _find_settled_regime(history)
        _cand_date = features['ts_utc'].strftime('%Y-%m-%d')
        if _is_candidate_transition(settled, state_name, streak, confidence, market_open):
            episode = make_episode(features['ts_utc'], state_name)
            rcli = _redis()
            if _pf.should_prefetch(rcli, _cand_date, episode=episode):
                _pf.set_prefetch_running(rcli, _cand_date, target_state=state_name,
                                         episode=episode,
                                         started_at=features['ts_utc'].isoformat())
                _spawn_refetch_prices(_cand_date)
                _post_to_discord('intraday-regime',
                    f':arrows_counterclockwise: candidate {settled} → {state_name} '
                    f'(tick 1/3, conf={confidence:.2f}) — prefetching prices')

    # 4) Cooldown + transition audit gates
    # Phase 2 (2026-05-19): confirmed transitions spawn scripts/redeploy_pipeline.py
    # detached (LIVE) or with --dry-run (LIVE=0). The detached child writes
    # to logs/redeploy_pipeline_<date>.log so the 5-min cron tick returns fast.
    # The `fired_liquidation` audit column is misnamed for the redeploy world
    # but kept as-is for this phase; a future cleanup will rename to
    # `fired_action` or split into `fired_redeploy`.
    if fired and prior_state and not force_dry_run:
        rcli = _redis()
        date_str = features['ts_utc'].strftime('%Y-%m-%d')
        kv = {}
        if rcli is not None:
            try:
                for k in (f'redeploy:cooldown:{date_str}',
                          f'liquidate:cooldown:{date_str}'):
                    v = rcli.get(k)
                    if v:
                        kv[k] = v
            except Exception:
                pass

        if _cooldown_active(kv, date_str):
            logger.info('confirmed transition %s→%s blocked by cooldown gate',
                        prior_state, state_name)
            transition_tag = f'INTRADAY_HMM_{prior_state}_{state_name}_COOLDOWN'
        else:
            from src.execution.intraday_prefetch import acquire_inflight, prefetch_enabled
            if prefetch_enabled() and not acquire_inflight(rcli):
                logger.info('confirmed transition %s→%s skipped — redeploy already in flight',
                            prior_state, state_name)
                transition_tag = f'INTRADAY_HMM_{prior_state}_{state_name}_INFLIGHT'
            else:
                transition_tag = f'INTRADAY_HMM_REDEPLOY_{prior_state}_{state_name}'
                # Propagate the new regime to canonical consumer surfaces BEFORE
                # spawning the redeploy — the redeploy's engine reads
                # regime_latest.json (file-primary) and the sizer falls back to
                # market_regime DB. Without this sync, the redeploy would size
                # against the stale daily-HMM regime.
                _sync_regime_to_consumers(
                    conn=conn,
                    new_state=state_name,
                    prior_state=prior_state,
                    confidence=confidence,
                    state_probs=state_probs,
                    features=features,
                    ts_utc=features['ts_utc'],
                    transition_tag=transition_tag,
                )
                is_live = _is_live_intraday()
                # Episode-bind the tick-3 data-ready gate. CORRECTNESS comes
                # from "redeploy_pipeline only proceeds on episode-match";
                # this streak-start reconstruction is a best-effort FAST-PATH
                # to reuse the tick-1 prefetch. If it's slightly off, the gate
                # simply sync-refetches fresh data (correct, slightly slower) —
                # it must NEVER let the gate proceed on a non-matching episode.
                #
                # streak-start tick = oldest consecutive history row matching state_name.
                # history is newest-first; the current tick isn't in it. With streak==N,
                # the streak-start is history[N-2] (history[0]=prev tick). Fail-safe: if
                # we can't recover it, fall back to the current ts (the gate will then
                # sync-refetch on episode mismatch — correct, just not the fast path).
                try:
                    if streak >= 2 and len(history) >= (streak - 1):
                        _start_ts = pd.Timestamp(history[streak - 2]['ts_utc'])
                        if _start_ts.tzinfo is None:
                            _start_ts = _start_ts.tz_localize('UTC')
                    else:
                        _start_ts = features['ts_utc']
                except Exception:
                    _start_ts = features['ts_utc']
                expected_episode = make_episode(_start_ts, state_name)
                spawn_kind = _spawn_redeploy(
                    prior_state, state_name, date_str, dry_run=(not is_live),
                    episode=expected_episode,
                )
                fired_liquidation = True
                logger.info(
                    'confirmed transition %s→%s — redeploy spawned (%s)',
                    prior_state, state_name, spawn_kind,
                )
                _post_to_discord(
                    'intraday-regime',
                    f':zap: **Intraday HMM** confirmed '
                    f'{prior_state} → {state_name} '
                    f'(streak={streak}, conf={confidence:.2f}, '
                    f'redeploy={spawn_kind})',
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

    # Intraday is the sole regime authority: refresh regime_latest.json every
    # tick so engine.load_regime()'s staleness gate stays satisfied (the daily
    # detector no longer writes it). On a confirmed transition _sync already
    # wrote the same content above; this idempotent re-write also guards
    # freshness if that write failed. The helper preserves the last good state
    # when state_name is UNKNOWN (bootstrap / NaN-VIX skip).
    #
    # COOLDOWN: a confirmed transition blocked by the redeploy cooldown wrote
    # NO market_regime row (the regime-of-record stays frozen). The file must
    # not advance its state either, or it would lead the db and trip the
    # doctor's state-agreement check. Pass a non-regime sentinel so the helper
    # refreshes freshness but keeps the file's last good state.
    _file_state = state_name
    if transition_tag and (transition_tag.endswith('_COOLDOWN')
                           or transition_tag.endswith('_INFLIGHT')):
        _file_state = '_COOLDOWN_HOLD'   # not in _STATE_RANK → state preserved
    _refresh_regime_file(
        state=_file_state, confidence=confidence,
        vix=features.get('vix_synth_30d'),
        prior_state=last_row_state, state_probs=state_probs,
        ts_utc=features['ts_utc'], transition_tag=transition_tag,
    )

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
