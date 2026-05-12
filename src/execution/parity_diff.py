"""Daily 21:00 UTC — diff parity_orders by source for the run_date.

Compares regime_blended (DRY-RUN, formula-only) vs production
(mirrored from alpaca_submissions by Task 17) at ticker level.

Posts a one-line summary to #botjohn-log:
  Parity 2026-05-12 (LOW_VOL): 12 matched, 1 large diff, 0 only_blended, 0 only_prod

If matched-rate ratio drifts > 1% per ticker, large_diffs lists them.
After 30 trading days of clean parity in HIGH_VOL/CRISIS, operator can
flip OPENCLAW_REGIME_BLENDED_LIVE=1.

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-revision.md §"Correction 5"
"""
from __future__ import annotations
import json, os, sys
from datetime import date
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

DEFAULT_TOLERANCE_PCT = 0.01

def compute_diff(blended: pd.DataFrame, production: pd.DataFrame, tolerance_pct: float) -> dict:
    """Diff two ticker→notional DataFrames. Returns counts + large_diffs list + only-in-X sets."""
    b_idx = {row['ticker']: row for _, row in blended.iterrows()} if len(blended) else {}
    p_idx = {row['ticker']: row for _, row in production.iterrows()} if len(production) else {}
    matched, large = 0, []
    for ticker in set(b_idx) & set(p_idx):
        b_n = float(b_idx[ticker]['notional_usd'])
        p_n = float(p_idx[ticker]['notional_usd'])
        if p_n == 0:
            if abs(b_n) > 1:
                large.append({'ticker': ticker, 'blended': b_n, 'production': p_n})
            else:
                matched += 1
            continue
        rel = abs(b_n - p_n) / abs(p_n)
        if rel > tolerance_pct:
            large.append({'ticker': ticker, 'blended': b_n, 'production': p_n,
                           'rel_diff_pct': rel})
        else:
            matched += 1
    return {
        'matched': matched,
        'large_diffs': large,
        'only_in_blended': sorted(set(b_idx) - set(p_idx)),
        'only_in_production': sorted(set(p_idx) - set(b_idx)),
    }

def format_summary(diff: dict, regime: str, run_date) -> str:
    return (f'**Parity {run_date} ({regime})**: '
            f'{diff["matched"]} matched, '
            f'{len(diff["large_diffs"])} large diffs, '
            f'{len(diff["only_in_blended"])} only_blended, '
            f'{len(diff["only_in_production"])} only_prod')

def main():
    """Read parity_orders for today (or latest), diff sources, post + log."""
    import psycopg2
    uri = os.environ['POSTGRES_URI']
    run_date = date.today()
    with psycopg2.connect(uri) as conn:
        blended = pd.read_sql(
            "SELECT ticker, notional_usd FROM parity_orders "
            "WHERE signal_date=%s AND source='regime_blended'",
            conn, params=(run_date,))
        # Try 'production' first; fall back to 'deterministic' for backward compat.
        production = pd.read_sql(
            "SELECT ticker, notional_usd FROM parity_orders "
            "WHERE signal_date=%s AND source='production'",
            conn, params=(run_date,))
        if production.empty:
            production = pd.read_sql(
                "SELECT ticker, notional_usd FROM parity_orders "
                "WHERE signal_date=%s AND source='deterministic'",
                conn, params=(run_date,))
        regime_df = pd.read_sql(
            "SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1", conn)
        regime = regime_df.iloc[0]['state'] if len(regime_df) else 'UNKNOWN'

    diff = compute_diff(blended, production, DEFAULT_TOLERANCE_PCT)

    out_path = ROOT / 'output' / f'sizer_parity_{run_date.isoformat()}.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(
        {'regime': regime, 'date': run_date.isoformat(),
         'blended_count': len(blended), 'production_count': len(production),
         **diff}, indent=2, default=str))

    msg = format_summary(diff, regime, run_date.isoformat())
    if diff['large_diffs']:
        msg += '\n```\n' + '\n'.join(
            f"  {d['ticker']}: blended=${d['blended']:.0f} prod=${d['production']:.0f}"
            for d in diff['large_diffs'][:10]) + '\n```'

    try:
        from execution.regime_liquidator import _post_to_discord
        _post_to_discord('botjohn-log', msg)
    except Exception as e:
        print(f'[parity_diff] Discord post failed ({e}); printing to stdout')
    print(msg)

if __name__ == '__main__':
    main()
