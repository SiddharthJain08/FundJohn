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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_live_intraday() -> bool:
    """Phase 1: surfaces in the Discord notification only (no broker
    action — see Phase 2 for the redeploy spawn this flag will arm)."""
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


# ── Redeploy spawner (Phase 2) ───────────────────────────────────────────────

def _spawn_redeploy(prior_state: str, new_state: str, date_str: str,
                    *, dry_run: bool) -> str:
    """Spawn scripts/redeploy_pipeline.py DETACHED. Returns 'spawned' or
    'dry-run' for the Discord-summary tag, or 'spawn_error' on Popen failure.

    Detached pattern matches cron-schedule.js:326-332: own session, stdio
    redirected to a per-date log file, parent does not wait. The 5-min
    intraday cron tick must return in <2s; the orchestrator itself runs up
    to 30 min inside the detached child.
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
    intraday.append_features_row(features)
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

    # 4) Cooldown + transition audit gates
    # Phase 2 (2026-05-19): confirmed transitions spawn scripts/redeploy_pipeline.py
    # detached (LIVE) or with --dry-run (LIVE=0). The detached child writes
    # to logs/redeploy_pipeline_<date>.log so the 5-min cron tick returns fast.
    # The `fired_liquidation` audit column is misnamed for the redeploy world
    # but kept as-is for this phase; a future cleanup will rename to
    # `fired_action` or split into `fired_redeploy`.
    if fired and prior_state and not force_dry_run:
        rcli = _redis()
        cooldown_active = False
        date_str = features['ts_utc'].strftime('%Y-%m-%d')
        # Cooldown gate honors BOTH keys:
        #   redeploy:cooldown:{date}  — written by scripts/redeploy_pipeline.py
        #     after a successful intraday redeploy; suppresses thrash on
        #     whipsaw transitions.
        #   liquidate:cooldown:{date} — written by regime_liquidator.py on a
        #     successful manual --force flatten. Suppressing intraday
        #     redeploys during this window keeps the operator's flatten
        #     intent from being immediately undone by a re-detection.
        if rcli is not None:
            try:
                for k in (f'redeploy:cooldown:{date_str}',
                          f'liquidate:cooldown:{date_str}'):
                    if rcli.get(k):
                        cooldown_active = True
                        break
            except Exception:
                pass

        if cooldown_active:
            logger.info('confirmed transition %s→%s blocked by cooldown gate',
                        prior_state, state_name)
            transition_tag = f'INTRADAY_HMM_{prior_state}_{state_name}_COOLDOWN'
        else:
            is_live = _is_live_intraday()
            spawn_kind = _spawn_redeploy(
                prior_state, state_name, date_str, dry_run=(not is_live),
            )
            transition_tag = (
                f'INTRADAY_HMM_REDEPLOY_{prior_state}_{state_name}'
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
