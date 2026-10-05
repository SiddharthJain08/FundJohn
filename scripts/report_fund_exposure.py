#!/usr/bin/env python3
"""Fund-exposure report (universe security type, Phase 1; spec 2026-10-04 §1 "Report").

READ-ONLY. For every manifest strategy in state live or candidate, the share of
its LATEST primary-window backtest run's trades whose ticker is an ETF/fund per
the vendor profile cache (data/.cache/fmp_profile.json), alongside the live
``universe_filter_ref``, the ``backtest_universe_cap`` and whether any regime is
currently eligible (strategy_regime_params.eligible). This is the evidence for
Phase 2 (which strategies to opt into the stocks_* tiers).

Needs Postgres at RUN time (POSTGRES_URI from the environment); the session is
set read-only and only SELECTs are issued. Tickers with no usable profile are
counted as ``unknown`` (never as funds).

Usage:
  POSTGRES_URI=... python3 scripts/report_fund_exposure.py [--json] [--states live,candidate]
                                                           [--min-share 0.05] [--profile-cache PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from collections import Counter
from typing import Iterable, Mapping, Optional, Union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.strategies.universe_meta import security_types_from_profiles  # noqa: E402

MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
PROFILE_CACHE = ROOT / 'data' / '.cache' / 'fmp_profile.json'
# 'secondary' (preferred/note/warrant lines under a parent's name) is non-common
# and counted here with the funds.
FUND_TYPES = ('etf', 'fund', 'secondary')


# ── pure parts ───────────────────────────────────────────────────────────────

def ticker_security_type(ticker: str, profiles: dict, types: Optional[dict] = None) -> Optional[str]:
    """Type for a trade ticker: broker symbol first, then the vendor hyphen
    form for dotted symbols (BRK.B -> BRK-B); None when unknown. `types` is the
    cache-wide result of security_types_from_profiles (computed here when
    omitted; pass it to avoid recomputing per ticker)."""
    if types is None:
        types = security_types_from_profiles(profiles)
    t = types.get(ticker)
    if t is None and '.' in ticker:
        t = types.get(ticker.replace('.', '-'))
    return t


def fund_share(trades: Union[Iterable[str], Mapping[str, int]], profiles: dict,
               types: Optional[dict] = None) -> dict:
    """{'total', 'fund', 'unknown', 'share'}. `trades` is either {ticker: trade
    count} (what the DB path supplies, via GROUP BY) or a list with one ticker
    per trade. share = fund / total (None when total == 0)."""
    counts = trades if isinstance(trades, Mapping) else Counter(trades)
    if types is None:
        types = security_types_from_profiles(profiles)
    total = fund = unknown = 0
    for tk, n in counts.items():
        total += n
        t = ticker_security_type(tk, profiles, types)
        if t is None:
            unknown += n
        elif t in FUND_TYPES:
            fund += n
    return {'total': total, 'fund': fund, 'unknown': unknown,
            'share': (fund / total) if total else None}


def _ref_name(ref) -> Optional[str]:
    return ref.rsplit(':', 1)[-1] if ref and ':' in ref else ref


def build_rows(manifest: dict, states: set, trades_by_strategy: dict,
               profiles: dict, active_by_strategy: dict) -> list[dict]:
    """One row per manifest strategy in `states`, sorted by fund share desc."""
    rows = []
    types = security_types_from_profiles(profiles)  # once per report
    for sid, entry in sorted(manifest.get('strategies', {}).items()):
        if entry.get('state') not in states:
            continue
        meta = entry.get('metadata') or {}
        s = fund_share(trades_by_strategy.get(sid, []), profiles, types)
        rows.append({
            'strategy_id': sid,
            'state': entry.get('state'),
            'trades': s['total'], 'fund_trades': s['fund'],
            'unknown_trades': s['unknown'], 'fund_share': s['share'],
            'universe_filter_ref': _ref_name(meta.get('universe_filter_ref')),
            'backtest_universe_cap': meta.get('backtest_universe_cap'),
            'any_regime_active': bool(active_by_strategy.get(sid, False)),
        })
    rows.sort(key=lambda r: (-1 if r['fund_share'] is None else r['fund_share']),
              reverse=True)
    return rows


def format_table(rows: list[dict], min_share: float = 0.0) -> str:
    hdr = (f"{'strategy':<46}{'state':<10}{'trades':>7}{'fund%':>7}{'unk':>5}  "
           f"{'filter_ref':<14}{'bt_cap':<13}active")
    out = [hdr, '-' * len(hdr)]
    for r in rows:
        if r['fund_share'] is not None and r['fund_share'] < min_share:
            continue
        pct = '   n/a' if r['fund_share'] is None else f"{100 * r['fund_share']:6.1f}"
        out.append(f"{r['strategy_id']:<46}{(r['state'] or ''):<10}{r['trades']:>7}{pct:>7}"
                   f"{r['unknown_trades']:>5}  {(r['universe_filter_ref'] or '-'):<14}"
                   f"{(r['backtest_universe_cap'] or '-'):<13}{'yes' if r['any_regime_active'] else 'no'}")
    return '\n'.join(out)


# ── DB (run time only) ───────────────────────────────────────────────────────

def fetch_inputs(dsn: str, strategy_ids: list[str]):
    """SELECT-only reads on a read-only session. Returns
    (trades_by_strategy {sid: {ticker: trade count}}, active_by_strategy {sid: bool})."""
    import psycopg2
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT ON (strategy_id) strategy_id, run_id::text
                  FROM strategy_backtest_runs WHERE primary_window
                 ORDER BY strategy_id, run_at DESC""")
            runs = {sid: rid for sid, rid in cur.fetchall() if sid in set(strategy_ids)}
            trades: dict[str, dict[str, int]] = {sid: {} for sid in runs}
            by_run = {rid: sid for sid, rid in runs.items()}
            if by_run:
                # run_id compared as uuid (index-friendly, no cast on the key
                # column) and aggregated in SQL: one row per (run, ticker).
                cur.execute("SELECT run_id::text, ticker, count(*) "
                            "FROM strategy_backtest_trades "
                            "WHERE run_id = ANY(%s::uuid[]) GROUP BY 1, 2",
                            (list(by_run),))
                for rid, tk, n in cur.fetchall():
                    trades[by_run[rid]][tk] = int(n)
            cur.execute("SELECT strategy_id, bool_or(eligible) FROM strategy_regime_params "
                        "GROUP BY strategy_id")
            active = {sid: bool(a) for sid, a in cur.fetchall()}
        return trades, active
    finally:
        conn.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog='report_fund_exposure')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--states', default='live,candidate')
    ap.add_argument('--min-share', type=float, default=0.0,
                    help='table only: hide strategies below this fund share')
    ap.add_argument('--profile-cache', default=str(PROFILE_CACHE))
    args = ap.parse_args(argv)

    dsn = os.environ.get('POSTGRES_URI', '')
    if not dsn:
        raise SystemExit('POSTGRES_URI not set')
    manifest = json.loads(MANIFEST.read_text())
    states = {s.strip() for s in args.states.split(',') if s.strip()}
    sids = [sid for sid, e in manifest.get('strategies', {}).items() if e.get('state') in states]
    try:
        profiles = json.loads(Path(args.profile_cache).read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f'cannot read profile cache {args.profile_cache}: {e}')
    trades, active = fetch_inputs(dsn, sids)
    rows = build_rows(manifest, states, trades, profiles, active)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(format_table(rows, args.min_share))
    return 0


if __name__ == '__main__':
    sys.exit(main())
