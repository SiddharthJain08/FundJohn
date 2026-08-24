#!/usr/bin/env python3
"""generate_tearsheet.py — per-run advisory tearsheet (task P3+R3, 2026-08-24
five-repo-adoptions).

ADVISORY ONLY: this script never feeds a gate, sizing, or promotion decision.
It is best-effort and always exits 0 — missing data, a missing run, a DB
outage, or a rendering failure all print a reason and exit 0 so a caller
(unified_backtest.py's subprocess wrapper) can swallow it unconditionally.

Reads Postgres (dotenv .env -> POSTGRES_URI/DATABASE_URL):
  strategy_backtest_runs   -- columns used: run_id, strategy_id, start_date,
                               end_date (inspected via information_schema
                               before writing this query; see
                               task-P3R3-report.md for the full column list).
  strategy_backtest_trades -- columns used: exit_date, pnl_pct (ordered by
                               trade_seq).

Daily returns reconstruction: per-trade pnl_pct (percentage points, e.g.
1.5 = +1.5%) grouped by exit_date and summed, converted to decimal, over
every calendar day in [start_date, end_date]; days with no exits are 0.0.
This is a REALIZED-P&L APPROXIMATION, not a mark-to-market equity curve —
it ignores position sizing, doesn't track open-but-unrealized P&L between
entry and exit, and just sums same-day-exit pnl_pct. Adequate for a quick
advisory tearsheet; not a substitute for the sizer's actual portfolio
equity curve.

quantstats verdict (2026-08-24, quantstats==0.0.81, pandas==3.0.2, this box):
VERIFIED WORKING. A synthetic-series probe (`qs.reports.html(series,
output=path, title=...)`) ran clean — the only output was ~100 harmless
`findfont: Font family 'Arial' not found` warnings from matplotlib (no
DISPLAY / no Arial installed in this environment), no exception. quantstats
is used as the PRIMARY rendering path. A self-rendered single-file HTML
fallback (stats table + base64 PNG equity curve, matplotlib Agg backend) is
kept and wired via runtime try/except — if quantstats ever regresses against
a future pandas bump, generation degrades to the fallback instead of failing
the caller. See tests/scripts/test_generate_tearsheet.py for both paths
exercised directly.
"""
from __future__ import annotations

import argparse
import base64
import math
import sys
from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import psycopg2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from backtest.tail_stats import sleeve_tail_stats  # noqa: E402

DEFAULT_OUTPUT_DIR = ROOT / 'output' / 'tearsheets'


# dotenv load is deferred and called from run()/main() only — a top-level
# load_dotenv() would pollute os.environ across the whole pytest session
# (same pattern as scripts/backfill_regime_backtests.py).
def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / '.env')
    except (ImportError, PermissionError, OSError):
        pass


def _db_uri() -> Optional[str]:
    import os
    return os.environ.get('POSTGRES_URI') or os.environ.get('DATABASE_URL')


# ── DB loaders (monkeypatched in tests — never hit in the test suite) ──────

def _fetch_run(conn, *, run_id: Optional[str] = None,
                strategy_id: Optional[str] = None) -> Optional[dict]:
    cur = conn.cursor()
    try:
        if run_id:
            cur.execute("""
                SELECT run_id::text, strategy_id, start_date, end_date
                FROM strategy_backtest_runs WHERE run_id = %s
            """, (run_id,))
        else:
            cur.execute("""
                SELECT run_id::text, strategy_id, start_date, end_date
                FROM strategy_backtest_runs
                WHERE strategy_id = %s
                ORDER BY run_at DESC LIMIT 1
            """, (strategy_id,))
        row = cur.fetchone()
    finally:
        cur.close()
    if row is None:
        return None
    return {'run_id': row[0], 'strategy_id': row[1],
            'start_date': row[2], 'end_date': row[3]}


def _fetch_trades(conn, run_id: str) -> list[dict]:
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT exit_date, pnl_pct
            FROM strategy_backtest_trades
            WHERE run_id = %s
            ORDER BY trade_seq
        """, (run_id,))
        rows = cur.fetchall()
    finally:
        cur.close()
    return [{'exit_date': r[0], 'pnl_pct': r[1]} for r in rows]


# ── Pure functions (fully unit-testable, no DB) ─────────────────────────────

def build_daily_returns(trades: list[dict], start_date, end_date) -> pd.Series:
    """Daily decimal-return series over [start_date, end_date] (inclusive,
    calendar days). Days with no exits are 0.0. See module docstring for the
    realized-P&L-approximation caveat."""
    idx = pd.date_range(pd.Timestamp(start_date), pd.Timestamp(end_date), freq='D')
    daily = pd.Series(0.0, index=idx)
    for t in trades:
        exit_date = t.get('exit_date')
        pnl = t.get('pnl_pct')
        if exit_date is None or pnl is None:
            continue
        ts = pd.Timestamp(exit_date)
        if ts in daily.index:
            daily.loc[ts] += float(pnl) / 100.0
    return daily


def _render_quantstats(returns: pd.Series, *, title: str, output_path: Path) -> None:
    import matplotlib
    matplotlib.use('Agg')
    import quantstats as qs
    qs.reports.html(returns, output=str(output_path), title=title)


def _fmt(x: Optional[float], pct: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return 'n/a'
    return f'{x * 100:.2f}%' if pct else f'{x:.4f}'


def _render_fallback(returns: pd.Series, trades: list[dict], *, strategy_id: str,
                      run_id: str, output_path: Path,
                      quantstats_error: Optional[str] = None) -> None:
    """Self-rendered single-file HTML: stats table + base64 PNG equity curve.
    Live only when quantstats raises (see module docstring — verified NOT
    the normal case on this box, but kept as a defensive path)."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    equity = (1.0 + returns).cumprod()
    n = len(returns)
    years = max(n / 365.25, 1e-9)
    last = float(equity.iloc[-1]) if n else 1.0
    total_return = last - 1.0 if n else 0.0
    cagr = (last ** (1.0 / years) - 1.0) if n and last > 0 else None
    std = float(returns.std(ddof=0)) if n > 1 else 0.0
    vol = std * math.sqrt(252) if n > 1 else None
    mean_r = float(returns.mean()) if n else 0.0
    sharpe = (mean_r / std * math.sqrt(252)) if n > 1 and std > 0 else None
    running_max = equity.cummax()
    drawdown = (equity / running_max) - 1.0
    max_dd = float(drawdown.min()) if n else None

    pnl_list = [t['pnl_pct'] for t in trades if t.get('pnl_pct') is not None]
    trade_count = len(pnl_list)
    win_rate = (sum(1 for p in pnl_list if p > 0) / trade_count) if trade_count else None
    tail = sleeve_tail_stats(pnl_list) if pnl_list else {
        'sortino': None, 'cvar_5': None, 'downside_dev': None}

    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(equity.index, equity.values)
    ax.set_title(f'{strategy_id} — equity curve (realized-P&L approximation)')
    ax.set_ylabel('Growth of $1')
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=110, bbox_inches='tight')
    plt.close(fig)
    img_b64 = base64.b64encode(buf.getvalue()).decode('ascii')

    rows = [
        ('CAGR (approx)', _fmt(cagr, pct=True)),
        ('Total return', _fmt(total_return, pct=True)),
        ('Annualized vol', _fmt(vol, pct=True)),
        ('Sharpe (approx)', _fmt(sharpe)),
        ('Sortino (tail_stats)', _fmt(tail['sortino'])),
        ('CVaR 5% (tail_stats)', _fmt(tail['cvar_5'], pct=True)),
        ('Max drawdown', _fmt(max_dd, pct=True)),
        ('Win rate', _fmt(win_rate, pct=True)),
        ('Trade count', str(trade_count)),
    ]
    rows_html = '\n'.join(f'<tr><td>{k}</td><td>{v}</td></tr>' for k, v in rows)
    note = ''
    if quantstats_error:
        note = (f'<p><em>quantstats fallback engaged '
                f'({quantstats_error}).</em></p>')

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{strategy_id} tearsheet — {run_id}</title></head>
<body>
<h1>{strategy_id} — {run_id}</h1>
<p>Realized-P&amp;L approximation: daily series built from per-trade
exit-date pnl_pct, 0.0 on days with no exits. Not a mark-to-market equity
curve. Advisory only — never a gate/sizing/promotion input.</p>
{note}
<table border="1" cellpadding="4">
<tbody>
{rows_html}
</tbody>
</table>
<h2>Equity curve</h2>
<img src="data:image/png;base64,{img_b64}" alt="equity curve" />
</body></html>
"""
    Path(output_path).write_text(html)


def generate_html_tearsheet(returns: pd.Series, trades: list[dict], *,
                             strategy_id: str, run_id: str,
                             output_path) -> Path:
    """Write the tearsheet HTML to output_path (parents created). Tries
    quantstats first (verified live on this box, see module docstring);
    falls back to the self-rendered path on any exception."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    title = f'{strategy_id} — {run_id}'
    try:
        _render_quantstats(returns, title=title, output_path=output_path)
    except Exception as e:
        _render_fallback(returns, trades, strategy_id=strategy_id, run_id=run_id,
                          output_path=output_path,
                          quantstats_error=f'{type(e).__name__}: {e}')
    return output_path


# ── Orchestration ────────────────────────────────────────────────────────

def run(*, run_id: Optional[str] = None, strategy_id: Optional[str] = None,
        output_dir=None) -> int:
    """Full flow: connect, load run + trades, render, print path, return 0.
    Every failure mode (DB unavailable, no run, no trades, render error)
    prints a one-line reason and returns 0 — advisory tool, never breaks
    the caller."""
    _load_env()
    uri = _db_uri()
    if not uri:
        print('[generate_tearsheet] no POSTGRES_URI/DATABASE_URL set — skipping')
        return 0
    try:
        conn = psycopg2.connect(uri)
    except Exception as e:
        print(f'[generate_tearsheet] DB unavailable: {type(e).__name__}: {e}')
        return 0
    try:
        run_row = _fetch_run(conn, run_id=run_id, strategy_id=strategy_id)
        if run_row is None:
            print(f'[generate_tearsheet] no run found '
                  f'(run_id={run_id!r} strategy={strategy_id!r})')
            return 0
        trades = _fetch_trades(conn, run_row['run_id'])
    finally:
        conn.close()

    if not trades:
        print(f'[generate_tearsheet] no trades for run_id={run_row["run_id"]} '
              f'— skipping (advisory, non-fatal)')
        return 0

    daily = build_daily_returns(trades, run_row['start_date'], run_row['end_date'])
    out_dir = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'{run_row["strategy_id"]}_{run_row["run_id"]}.html'
    try:
        generate_html_tearsheet(
            daily, trades, strategy_id=run_row['strategy_id'],
            run_id=run_row['run_id'], output_path=out_path,
        )
    except Exception as e:
        print(f'[generate_tearsheet] generation failed: {type(e).__name__}: {e}')
        return 0
    print(str(out_path))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--run-id', help='Run ID (UUID) to render')
    g.add_argument('--strategy', help='Strategy ID: render its latest run')
    ap.add_argument('--output-dir', default=None,
                     help=f'Output directory (default {DEFAULT_OUTPUT_DIR})')
    args = ap.parse_args()
    return run(run_id=args.run_id, strategy_id=args.strategy,
               output_dir=args.output_dir)


if __name__ == '__main__':
    sys.exit(main())
