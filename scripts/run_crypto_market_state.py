"""SP-3.1 Phase B: 24/7 crypto regime detector driver. Gated on
OPENCLAW_CRYPTO_REGIME=1. Fetches recent hourly bars, computes features, fits
or loads the HMM (weekly refit), scores the latest regime, applies hysteresis,
and PERSISTS (crypto_regime_latest.json + crypto_regime_states row). It NEVER
spawns a redeploy — fired_redeploy stays FALSE (Phase C owns that)."""
from __future__ import annotations
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from ingestion import crypto_bars as cb       # noqa: E402
from regime import crypto_regime as cr         # noqa: E402

MODEL_DIR = ROOT / '.agents' / 'crypto-market-state'
MODEL_PATH = MODEL_DIR / 'hmm_crypto_latest.pkl'
LATEST_JSON = MODEL_DIR / 'crypto_regime_latest.json'
LOOKBACK_ROWS = 200  # hysteresis lookback (~8 days of hourly states)


def _last_n_states(conn, n: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, prior_state, confidence, hysteresis_streak, fired_redeploy "
            "FROM crypto_regime_states ORDER BY ts_utc DESC LIMIT %s", (n,))
        cols = ['state', 'prior_state', 'confidence', 'hysteresis_streak', 'fired_redeploy']
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def _insert_state(conn, ts_utc, state, prior_state, confidence, streak, tag, features):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crypto_regime_states "
            "(ts_utc, state, prior_state, confidence, hysteresis_streak, "
            " fired_redeploy, transition_tag, features_json) "
            "VALUES (%s,%s,%s,%s,%s, FALSE, %s, %s) ON CONFLICT (ts_utc) DO NOTHING",
            (ts_utc, state, prior_state, float(confidence), int(streak), tag,
             json.dumps(features)))
    conn.commit()


def main() -> int:
    if os.environ.get('OPENCLAW_CRYPTO_REGIME') != '1':
        print('[crypto-regime] gate OFF (OPENCLAW_CRYPTO_REGIME != 1) — no-op')
        return 0
    MODEL_DIR.mkdir(parents=True, exist_ok=True)

    cb.update_recent(hours_back=48)
    bars = pd.read_parquet(cb.DEFAULT_PARQUET)
    feats = cr.compute_features(bars)
    if len(feats) < 200:
        print(f'[crypto-regime] insufficient features ({len(feats)}) — run backfill first')
        return 1

    is_refit = date.today().weekday() == 0 or not MODEL_PATH.exists()
    if is_refit:
        model = cr.fit_hmm(feats)
        cr.save_model(model, MODEL_DIR / f'hmm_crypto_{date.today().isoformat()}.pkl')
        cr.save_model(model, MODEL_PATH)
    else:
        model = cr.load_model(MODEL_PATH)

    state, confidence, probs = cr.score_latest(model, feats)
    ts_utc = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:00:00Z')
    latest_features = {k: round(float(feats.iloc[-1][k]), 4) for k in cr.CRYPTO_FEATURE_COLS}

    uri = os.environ.get('POSTGRES_URI', '')
    conn = psycopg2.connect(uri)
    try:
        history = _last_n_states(conn, LOOKBACK_ROWS)
        streak = cr.hysteresis_streak(history, state)
        fired, prior = cr.confirmed_transition(history, state, streak, confidence)
        tag = f'CRYPTO_HMM_{prior}_{state}' if fired else None
        prior_state = prior if fired else (history[0]['state'] if history else None)
        _insert_state(conn, ts_utc, state, prior_state, confidence, streak, tag, latest_features)
    finally:
        conn.close()

    j = cr.build_latest_json(ts_utc=ts_utc, state=state, prior_state=prior_state,
                             confidence=confidence, streak=streak, probs=probs,
                             features=latest_features, transition_tag=tag,
                             refit_performed=is_refit)
    LATEST_JSON.write_text(json.dumps(j, indent=2))
    print(f'[crypto-regime] {state} conf={confidence:.3f} streak={streak} '
          f'transition={tag or "none"}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
