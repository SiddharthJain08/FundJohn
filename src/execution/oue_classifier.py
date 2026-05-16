"""OUE (Over / Under / Expected) classifier for closed trades.

Replaces the older OUR (Over / Under / Rejected-signals) summary as of
2026-05-16. Each closed trade is classified exactly once based on its
realized return vs the GBM expectation captured at signal time:

    sigma_holding = hv21 × sqrt(days_held / 252)         # GBM σ
    sigma_delta   = (realized_pct - ev_gbm) / sigma_holding

    |sigma_delta| ≥ SIGMA_GATE → 'over' (positive) or 'under' (negative)
                              else → 'expected'

The result is persisted on `execution_signals.oue_kind` so future queries
don't need the handoff JSON. SIGMA_GATE matches the existing
`pipeline_config.sigma_gate` used by performance_outliers (default 2.0).

The classifier is called:
  1. From engine.py's signal-close path (engine.update_pnl) — populates
     OUE for newly-closed trades each cycle.
  2. From src/maintenance/backfill_oue.py — one-shot historical pass.
"""
from __future__ import annotations
import json
import math
import os
from pathlib import Path
from typing import Optional

import psycopg2

TRADING_DAYS_PER_YEAR = 252
SIGMA_FLOOR = 0.005     # protects against near-zero hv → infinite sigma_delta
HANDOFF_DIR = Path(__file__).resolve().parents[2] / 'output' / 'handoffs'


def _get_sigma_gate(cur) -> float:
    """Read sigma_gate from pipeline_config, default 2.0. Matches the
    threshold used by load_yesterdays_performance_outliers so the OUE
    classifier and the legacy outlier-digest agree."""
    try:
        cur.execute("SELECT value FROM pipeline_config WHERE key='sigma_gate'")
        row = cur.fetchone()
        if row:
            return float(row[0])
    except Exception:
        pass
    return 2.0


def _load_signal_ev(signal_date: str, ticker: str, strategy_id: str) -> Optional[dict]:
    """Find the {ev_gbm, hv21} pair for a signal from its structured handoff.
    Returns None if the JSON file is missing or the signal isn't in it
    (handoffs older than ~30 days may have been rotated)."""
    path = HANDOFF_DIR / f'{signal_date}_structured.json'
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    # Signals may live under 'signals' (green) or 'prefiltered' (vetoed)
    for bucket in ('signals', 'prefiltered'):
        for s in data.get(bucket) or []:
            if s.get('ticker') == ticker and s.get('strategy_id') == strategy_id:
                ev = s.get('ev_gbm')
                hv = s.get('hv21')
                if ev is not None and hv is not None:
                    return {'ev_gbm': float(ev), 'hv21': float(hv)}
    return None


def classify(realized_pct: float, days_held: int,
             ev_gbm: float, hv21: float,
             sigma_gate: float = 2.0) -> tuple[str, float]:
    """Pure classifier — returns (kind, sigma_delta). Caller is responsible
    for handling missing inputs (in which case classification should be
    'expected' with sigma_delta=0.0).
    """
    days_held = max(int(days_held or 1), 1)
    sigma_holding = max(hv21 * math.sqrt(days_held / TRADING_DAYS_PER_YEAR), SIGMA_FLOOR)
    delta = float(realized_pct) - float(ev_gbm)
    sigma_delta = delta / sigma_holding if sigma_holding > 0 else 0.0
    if sigma_delta >= sigma_gate:
        return 'over', sigma_delta
    if sigma_delta <= -sigma_gate:
        return 'under', sigma_delta
    return 'expected', sigma_delta


def classify_and_persist(uri: str, signal_id: int) -> Optional[tuple[str, float]]:
    """Classify a single closed signal and write back to execution_signals.

    Idempotent — if oue_kind is already set, returns the existing value
    without recomputing. Returns None on any failure (caller should treat
    as 'expected' for accounting purposes).
    """
    try:
        with psycopg2.connect(uri) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT es.id, es.strategy_id, es.ticker, es.signal_date,
                           es.oue_kind,
                           sp.realized_pnl_pct, sp.days_held
                      FROM execution_signals es
                      LEFT JOIN signal_pnl sp ON sp.signal_id = es.id
                                              AND sp.status = 'closed'
                                              AND sp.realized_pnl_pct IS NOT NULL
                     WHERE es.id = %s
                     ORDER BY sp.pnl_date DESC NULLS LAST
                     LIMIT 1
                    """,
                    (signal_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                _, strategy_id, ticker, signal_date, existing_kind, realized, days = row
                if existing_kind is not None:
                    return existing_kind, None
                if realized is None:
                    return None
                ev_data = _load_signal_ev(str(signal_date), ticker, strategy_id)
                gate = _get_sigma_gate(cur)
                if ev_data is None:
                    # Can't compute — treat as expected so O+U+E sums to
                    # closed-count without an "unknown" leak.
                    kind, sigma_delta = 'expected', 0.0
                else:
                    kind, sigma_delta = classify(
                        float(realized), int(days or 1),
                        ev_data['ev_gbm'], ev_data['hv21'],
                        sigma_gate=gate,
                    )
                cur.execute(
                    """
                    UPDATE execution_signals
                       SET oue_kind = %s,
                           oue_sigma_delta = %s,
                           oue_classified_at = NOW()
                     WHERE id = %s
                       AND oue_kind IS NULL
                    """,
                    (kind, round(sigma_delta, 4), signal_id),
                )
                return kind, sigma_delta
    except Exception as e:
        print(f'[oue_classifier] persist failed for signal_id={signal_id}: {e}')
        return None


def classify_batch(uri: str, signal_ids: list[int]) -> dict[str, int]:
    """Classify many signals in one shot — used by the daily cycle to
    pick up everything that closed in this cycle. Returns counts by kind."""
    counts = {'over': 0, 'under': 0, 'expected': 0, 'skipped': 0}
    for sid in signal_ids:
        result = classify_and_persist(uri, sid)
        if result is None:
            counts['skipped'] += 1
        else:
            counts[result[0]] += 1
    return counts


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: oue_classifier.py <signal_id> [signal_id ...]')
        sys.exit(1)
    uri = os.environ['POSTGRES_URI']
    ids = [int(x) for x in sys.argv[1:]]
    print(classify_batch(uri, ids))
