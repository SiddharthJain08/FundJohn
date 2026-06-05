"""backfill_oue.py — one-shot historical OUE classification.

Walks every closed signal in execution_signals + classifies into
{over, under, expected}, then writes oue_kind/oue_sigma_delta/
oue_classified_at. Re-runnable: signals already classified are
skipped (oue_kind IS NOT NULL).

Sources, in order of preference:
  1. The structured handoff JSON for that signal_date (gives the
     authoritative ev_gbm + hv21 used at signal time).
  2. The legacy `performance_outliers` table — for ~14 cycle dates
     it has cached sigma_delta for the outliers. Closed trades
     present there at kind='over'/'under' get mapped directly;
     everything else closed on those dates → 'expected'.
  3. Fallback: mark as 'expected' so O+U+E still equals total
     closed-trade count.

Stats are printed at the end so the operator can see the split.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2

from execution.oue_classifier import _load_signal_ev, classify, _get_sigma_gate


def main(uri: str, dry_run: bool = False):
    conn = psycopg2.connect(uri)
    conn.autocommit = False
    cur = conn.cursor()

    sigma_gate = _get_sigma_gate(cur)
    print(f'[backfill_oue] sigma_gate = {sigma_gate}')

    # 1. Build perf_outliers lookup for the dates it covers.
    cur.execute("SELECT cycle_date, strategy_id, ticker, kind, sigma_delta FROM performance_outliers")
    perf_lookup: dict[tuple, tuple[str, float]] = {}
    for cycle_date, sid, tkr, kind, sigma_d in cur.fetchall():
        perf_lookup[(str(cycle_date), tkr, sid)] = (kind, float(sigma_d) if sigma_d is not None else 0.0)
    print(f'[backfill_oue] perf_lookup: {len(perf_lookup)} rows from performance_outliers')

    # 2. Pull every closed signal that hasn't been classified yet.
    cur.execute(
        """
        SELECT es.id, es.strategy_id, es.ticker, es.signal_date,
               sp.realized_pnl_pct, sp.days_held, sp.pnl_date
          FROM execution_signals es
          JOIN LATERAL (
              -- rolled_continuation rows are roll segments of an ongoing
              -- position (SP-6 D1), not trades; a 1-day GBM OUE on a roll
              -- segment is meaningless, so skip them (NULL-safe).
              SELECT realized_pnl_pct, days_held, pnl_date
                FROM signal_pnl
               WHERE signal_id = es.id
                 AND status = 'closed'
                 AND realized_pnl_pct IS NOT NULL
                 AND close_reason IS DISTINCT FROM 'rolled_continuation'
               ORDER BY pnl_date DESC
               LIMIT 1
          ) sp ON TRUE
         WHERE es.status = 'closed'
           AND es.oue_kind IS NULL
         ORDER BY es.signal_date, es.id
        """
    )
    rows = cur.fetchall()
    print(f'[backfill_oue] {len(rows)} closed signals awaiting classification')

    counts = {'over': 0, 'under': 0, 'expected': 0, 'skipped': 0}
    sources = {'handoff_json': 0, 'perf_lookup': 0, 'fallback_expected': 0}
    BATCH = 500
    n_written = 0

    for signal_id, sid, tkr, signal_date, realized, days, pnl_date in rows:
        kind, sigma_delta = None, None

        # Try handoff JSON for accurate classification.
        ev_data = _load_signal_ev(str(signal_date), tkr, sid)
        if ev_data is not None and realized is not None:
            kind, sigma_delta = classify(
                float(realized), int(days or 1),
                ev_data['ev_gbm'], ev_data['hv21'],
                sigma_gate=sigma_gate,
            )
            sources['handoff_json'] += 1
        else:
            # Try perf_outliers cache — keyed on the cycle_date the
            # outlier was logged for, which is yesterday's pnl_date.
            key_pnl = (str(pnl_date), tkr, sid)
            if key_pnl in perf_lookup:
                kind, sigma_delta = perf_lookup[key_pnl]
                sources['perf_lookup'] += 1
            else:
                # Fallback: presume within band.
                kind, sigma_delta = 'expected', 0.0
                sources['fallback_expected'] += 1

        counts[kind] += 1
        if not dry_run:
            cur.execute(
                """
                UPDATE execution_signals
                   SET oue_kind = %s,
                       oue_sigma_delta = %s,
                       oue_classified_at = NOW()
                 WHERE id = %s
                   AND oue_kind IS NULL
                """,
                (kind, round(float(sigma_delta or 0.0), 4), signal_id),
            )
            n_written += 1
            if n_written % BATCH == 0:
                conn.commit()
                print(f'[backfill_oue] committed {n_written}/{len(rows)}')

    if not dry_run:
        conn.commit()
    print(f'[backfill_oue] kinds: {counts}')
    print(f'[backfill_oue] sources: {sources}')
    print(f'[backfill_oue] total classified: {sum(counts[k] for k in ("over", "under", "expected"))}')
    if dry_run:
        print('[backfill_oue] DRY RUN — no writes performed')
    conn.close()


if __name__ == '__main__':
    uri = os.environ['POSTGRES_URI']
    dry = '--dry-run' in sys.argv
    main(uri, dry_run=dry)
