#!/usr/bin/env python3
"""SP-6 fill-model counterfactual study driver.

Compares open[t+1] vs close[t+1] entry fills across all manifest state='live'
strategies. Results are written to analysis/fill_model_study/results.jsonl
(one JSON line per strategy, resumable). The --summarize flag reads results.jsonl
and writes report.md + prints the VERDICT.

Usage:
  # Run the full sweep (operator-invoked; each strategy in its own subprocess):
  python3 scripts/backtest_fill_model_study.py

  # Run a single strategy (in-process, both variants; stdout = one JSON line):
  python3 scripts/backtest_fill_model_study.py --single S_xxx

  # Summarize after the sweep completes (or partially):
  python3 scripts/backtest_fill_model_study.py --summarize

The driver is COUNTERFACTUAL-ONLY: run_backtest is called with commit=False
everywhere; zero writes to strategy_backtest_* tables.
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = str(ROOT / 'src')
sys.path.insert(0, SRC_DIR)
sys.path.insert(0, str(ROOT))

RESULTS_DIR = ROOT / 'analysis' / 'fill_model_study'
RESULTS_FILE = RESULTS_DIR / 'results.jsonl'
REPORT_FILE = RESULTS_DIR / 'report.md'

# Spec §3 thresholds.
PARITY_THRESHOLD_PCT = 0.02       # 2 %
MAX_SUSPECT_BEFORE_INVALID = 5    # >5 SIM-SUSPECT → INVALID
CONSIDERATION_MEDIAN_DSHARPE = 0.10
CONSIDERATION_PCT_POSITIVE = 0.60  # 60 %


# ── Manifest helpers ─────────────────────────────────────────────────────────

def _live_strategies() -> list[str]:
    """Return strategy IDs with state='live' only (not candidate/staging)."""
    manifest_path = ROOT / 'src' / 'strategies' / 'manifest.json'
    m = json.loads(manifest_path.read_text())
    return [sid for sid, entry in (m.get('strategies') or {}).items()
            if entry.get('state') == 'live']


def _already_done() -> set[str]:
    """Return sids already present in results.jsonl."""
    if not RESULTS_FILE.exists():
        return set()
    done: set[str] = set()
    for line in RESULTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            if 'sid' in row:
                done.add(row['sid'])
        except Exception:
            pass
    return done


# ── Single-strategy in-process runner ────────────────────────────────────────

def _run_single(sid: str) -> dict:
    """Run both fill variants for one strategy (commit=False) and return the
    merged JSON-line dict. Prints one JSON line to stdout (for parent loop
    to capture)."""
    from backtest.unified_backtest import run_backtest, _resolve_instrument_class

    ic = _resolve_instrument_class(sid)
    results: dict = {'sid': sid}
    mock_conn = _make_mock_conn()

    for fm in ('close', 'open'):
        try:
            _, metrics = run_backtest(
                sid,
                fill_model=fm,
                commit=False,
                return_metrics=True,
                instrument_class=ic,
                conn=mock_conn,
            )
        except Exception as exc:
            metrics = {'error': str(exc)}
        results[fm] = metrics

    # delta_sharpe: open - close (None if either is missing)
    sh_close = (results.get('close') or {}).get('sharpe')
    sh_open  = (results.get('open')  or {}).get('sharpe')
    results['delta_sharpe'] = (
        round(sh_open - sh_close, 6)
        if (sh_close is not None and sh_open is not None)
        else None
    )

    # trades_parity_pct: |n_open - n_close| / n_close
    n_close = (results.get('close') or {}).get('total_trades', 0) or 0
    n_open  = (results.get('open')  or {}).get('total_trades', 0) or 0
    if n_close == 0 and n_open == 0:
        results['trades_parity_pct'] = 0.0
        results['sim_suspect'] = False
    elif n_close == 0:
        results['trades_parity_pct'] = None   # can't compute ratio
        results['sim_suspect'] = True
    else:
        parity = abs(n_open - n_close) / n_close
        results['trades_parity_pct'] = round(parity, 6)
        results['sim_suspect'] = parity > PARITY_THRESHOLD_PCT

    results['ts'] = datetime.datetime.utcnow().isoformat()
    return results


def _make_mock_conn():
    """Return a no-op DB connection for commit=False runs."""
    from unittest.mock import MagicMock
    mock_conn = MagicMock()
    mock_cur = MagicMock()
    mock_conn.cursor.return_value = mock_cur
    mock_conn.__enter__ = lambda s: s
    mock_conn.__exit__ = MagicMock(return_value=False)
    return mock_conn


# ── Summarize ────────────────────────────────────────────────────────────────

def _summarize() -> str:
    """Read results.jsonl and produce report.md + return VERDICT string."""
    if not RESULTS_FILE.exists():
        print('[fill-study] No results file found; run the sweep first.')
        return 'NO_RESULTS'

    rows: list[dict] = []
    for line in RESULTS_FILE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            pass

    if not rows:
        print('[fill-study] results.jsonl is empty.')
        return 'NO_RESULTS'

    n_total = len(rows)
    suspects = [r for r in rows if r.get('sim_suspect')]
    n_suspect = len(suspects)

    if n_suspect > MAX_SUSPECT_BEFORE_INVALID:
        verdict = 'INVALID-SIM'
        _write_report(rows, suspects, verdict)
        print(f'[fill-study] VERDICT: {verdict}')
        return verdict

    # Headline set: SIM-SUSPECT excluded.
    headline = [r for r in rows if not r.get('sim_suspect') and r.get('delta_sharpe') is not None]
    n_headline = len(headline)

    if n_headline == 0:
        verdict = 'CLOSE-FILL-STANDS'
        _write_report(rows, suspects, verdict)
        print(f'[fill-study] VERDICT: {verdict}')
        return verdict

    delta_sharpes = [r['delta_sharpe'] for r in headline]
    median_ds = _median(delta_sharpes)
    n_positive = sum(1 for d in delta_sharpes if d >= CONSIDERATION_MEDIAN_DSHARPE)
    n_negative = sum(1 for d in delta_sharpes if d <= -CONSIDERATION_MEDIAN_DSHARPE)
    pct_positive_ds = sum(1 for d in delta_sharpes if d > 0) / n_headline

    # Consideration bar (spec §3): median ΔSharpe ≥ +0.10 AND ≥60% strategies positive.
    if (median_ds is not None and median_ds >= CONSIDERATION_MEDIAN_DSHARPE
            and pct_positive_ds >= CONSIDERATION_PCT_POSITIVE):
        verdict = 'CONSIDERATION-BAR-MET'
    else:
        verdict = 'CLOSE-FILL-STANDS'

    _write_report(rows, suspects, verdict,
                  n_headline=n_headline, n_total=n_total,
                  n_suspect=n_suspect, median_ds=median_ds,
                  n_positive=n_positive, n_negative=n_negative,
                  pct_positive_ds=pct_positive_ds)
    print(f'[fill-study] VERDICT: {verdict}')
    return verdict


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


def _write_report(rows: list[dict], suspects: list[dict], verdict: str, *,
                  n_headline: int = 0, n_total: int = 0, n_suspect: int = 0,
                  median_ds: float | None = None,
                  n_positive: int = 0, n_negative: int = 0,
                  pct_positive_ds: float = 0.0) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')
    lines: list[str] = [
        f'# SP-6 Fill-Model Counterfactual Study Report',
        f'',
        f'Generated: {ts}',
        f'',
        f'## Verdict',
        f'',
        f'**`{verdict}`**',
        f'',
        '> Consideration bar: median ΔSharpe ≥ +0.10 AND ≥60% strategies positive AND '
        'trades-parity clean. Anything less → close-fill stands.',
        f'',
        f'## Summary',
        f'',
        f'| Metric | Value |',
        f'|--------|-------|',
        f'| Strategies evaluated | {n_total} |',
        f'| Headline set (excl. SIM-SUSPECT) | {n_headline} |',
        f'| SIM-SUSPECT (parity breach >2%) | {n_suspect} |',
        f'| Median ΔSharpe (open − close) | {median_ds if median_ds is not None else "N/A"} |',
        f'| ΔSharpe ≥ +0.10 count | {n_positive} |',
        f'| ΔSharpe ≤ −0.10 count | {n_negative} |',
        f'| % strategies with positive ΔSharpe | {round(pct_positive_ds * 100, 1) if n_headline else "N/A"}% |',
        f'',
        f'## Per-Strategy Results',
        f'',
        f'| SID | Sharpe (close) | Sharpe (open) | ΔSharpe | n_trades (close) | n_trades (open) | Parity% | SIM-SUSPECT |',
        f'|-----|----------------|---------------|---------|-----------------|-----------------|---------|-------------|',
    ]
    for r in sorted(rows, key=lambda x: (x.get('sim_suspect', False), -(x.get('delta_sharpe') or -999))):
        sid = r.get('sid', '?')
        cl = r.get('close') or {}
        op = r.get('open') or {}
        sh_cl = cl.get('sharpe')
        sh_op = op.get('sharpe')
        ds = r.get('delta_sharpe')
        n_cl = cl.get('total_trades', '?')
        n_op = op.get('total_trades', '?')
        par = r.get('trades_parity_pct')
        susp = '⚠️' if r.get('sim_suspect') else ''
        lines.append(
            f'| {sid} | {_fmt(sh_cl)} | {_fmt(sh_op)} | {_fmt(ds)} | {n_cl} | {n_op} | {_fmt(par, pct=True)} | {susp} |'
        )
    if suspects:
        lines.extend([
            f'',
            f'## SIM-SUSPECT Strategies',
            f'',
            'These strategies have |n_trades_open − n_trades_close| / n_trades_close > 2% '
            'and are excluded from the headline. Review sim logic if count > 5.',
            f'',
        ])
        for r in suspects:
            sid = r.get('sid', '?')
            n_cl = (r.get('close') or {}).get('total_trades', '?')
            n_op = (r.get('open') or {}).get('total_trades', '?')
            par = r.get('trades_parity_pct')
            lines.append(f'- `{sid}`: n_close={n_cl}, n_open={n_op}, parity={_fmt(par, pct=True)}')
    REPORT_FILE.write_text('\n'.join(lines) + '\n')
    print(f'[fill-study] Report written to {REPORT_FILE}')


def _fmt(v, pct: bool = False) -> str:
    if v is None:
        return 'N/A'
    if pct:
        return f'{round(v * 100, 2)}%'
    if isinstance(v, float):
        return f'{v:.4f}'
    return str(v)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description='SP-6 fill-model counterfactual study (open[t+1] vs close[t+1])')
    ap.add_argument('--single', metavar='SID',
                    help='Run one strategy in-process and print JSON to stdout')
    ap.add_argument('--summarize', action='store_true',
                    help='Read results.jsonl and write report.md + print VERDICT')
    args = ap.parse_args()

    if args.single:
        row = _run_single(args.single)
        print(json.dumps(row))
        return 0

    if args.summarize:
        verdict = _summarize()
        return 0 if verdict != 'NO_RESULTS' else 1

    # Full sweep: one subprocess per strategy, resumable.
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    sids = _live_strategies()
    done = _already_done()
    pending = [s for s in sids if s not in done]
    print(f'[fill-study] {len(sids)} live strategies, {len(done)} already done, '
          f'{len(pending)} pending')

    import subprocess
    ok = 0
    fail = 0
    for sid in pending:
        print(f'[fill-study] running {sid}...', flush=True)
        try:
            result = subprocess.run(
                ['nice', '-n', '19',
                 sys.executable, str(Path(__file__).resolve()), '--single', sid],
                capture_output=True, text=True, timeout=600,
            )
            stdout = result.stdout.strip()
            if result.returncode != 0 or not stdout:
                print(f'[fill-study] FAIL {sid}: exit={result.returncode} '
                      f'stderr={result.stderr[:200]}')
                fail += 1
                continue
            # Take the last non-empty line (logging might precede the JSON).
            json_line = None
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith('{'):
                    json_line = line
                    break
            if json_line is None:
                print(f'[fill-study] FAIL {sid}: no JSON line in stdout')
                fail += 1
                continue
            # Validate it parses.
            json.loads(json_line)
            with RESULTS_FILE.open('a') as fh:
                fh.write(json_line + '\n')
            ok += 1
            print(f'[fill-study] OK {sid}')
        except subprocess.TimeoutExpired:
            print(f'[fill-study] TIMEOUT {sid}')
            fail += 1
        except Exception as exc:
            print(f'[fill-study] ERROR {sid}: {exc}')
            fail += 1

    print(f'[fill-study] sweep done: ok={ok} fail={fail}')
    return 0 if fail == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
