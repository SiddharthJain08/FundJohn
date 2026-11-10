#!/usr/bin/env python3
"""Calibrate the crypto realised-vol regime tiers against the live crypto HMM.

Picks the three cut-points of src/strategies/crypto_historical_regimes.py's
CRYPTO_RV_TIERS so that, over the overlap window [--start, --end], the share
of days in each tier matches the live HMM's state frequencies (daily mode of
the hourly `crypto_regime_states` rows).

    --frequencies '<json>'  use these targets (no DB)
    --db                    read targets from crypto_regime_states via POSTGRES_URI
    --write                 rewrite the marked CRYPTO_RV_TIERS block of the
                            module source IN PLACE (between the
                            '# >>> CRYPTO_RV_TIERS' / '# <<< CRYPTO_RV_TIERS'
                            markers) with the frozen cut-points and a
                            '# calibrated <today> against crypto_regime_states
                            <start>..<end> (daily mode)' comment.

Reads the prices master (BTC-USD only, filtered read). NEVER writes the
crypto_historical_regimes.parquet cache — run the module's --rebuild for that.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
from strategies import crypto_historical_regimes as chr_mod  # noqa: E402

MODULE_PATH = ROOT / 'src' / 'strategies' / 'crypto_historical_regimes.py'
BEGIN = '# >>> CRYPTO_RV_TIERS'
END = '# <<< CRYPTO_RV_TIERS'


def db_frequencies(start: str, end: str) -> dict[str, float]:
    import psycopg2
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        raise SystemExit('POSTGRES_URI not set')
    sql = """
        WITH d AS (
          SELECT (ts_utc AT TIME ZONE 'UTC')::date AS day, state, count(*) AS n
          FROM crypto_regime_states
          WHERE ts_utc >= %s::date AND ts_utc < (%s::date + 1)
          GROUP BY 1, 2),
        m AS (
          SELECT DISTINCT ON (day) day, state FROM d ORDER BY day, n DESC, state)
        SELECT state, count(*) FROM m GROUP BY state"""
    with psycopg2.connect(uri) as conn, conn.cursor() as cur:
        cur.execute(sql, (start, end))
        rows = cur.fetchall()
    total = sum(n for _, n in rows)
    if not total:
        raise SystemExit('crypto_regime_states has no rows in the window')
    out = {r: 0.0 for r in chr_mod.CANONICAL_REGIMES}
    for state, n in rows:
        if state in out:
            out[state] = n / total
    s = sum(out.values())
    return {k: v / s for k, v in out.items()}


def render_block(tiers: tuple[float, float, float], start: str, end: str, today: str,
                 target: dict[str, float], n_days: int | None = None,
                 provenance: str | None = None) -> str:
    """provenance=None means the targets were read by --db; otherwise it is
    the caller's statement of where --frequencies came from."""
    t1, t2, t3 = tiers
    tgt = ', '.join(f'{k} {target[k]:.3f}' for k in chr_mod.CANONICAL_REGIMES)
    days = f' ({n_days} days in the prices master)' if n_days is not None else ''
    src = (f'# targets read from crypto_regime_states {start}..{end} via --db (daily mode of hourly states)\n'
           if provenance is None else f'# targets {provenance}\n')
    return (
        f'{BEGIN} (rewritten by scripts/calibrate_crypto_regime_tiers.py --write)\n'
        f'# calibrated {today} on BTC-USD rv30_smoothed {start}..{end}{days}\n'
        + src +
        f'# targets: {tgt}\n'
        'CRYPTO_RV_TIERS: tuple[tuple[float, str], ...] = (\n'
        f"    ({t1!r}, 'LOW_VOL'),\n"
        f"    ({t2!r}, 'TRANSITIONING'),\n"
        f"    ({t3!r}, 'HIGH_VOL'),\n"
        '    # > t3 → CRISIS (the catch-all)\n'
        ')\n'
        f'{END}\n'
    )


def rewrite_module(src: str, block: str) -> str:
    if src.count(BEGIN) != 1 or src.count(END) != 1:
        raise SystemExit(f'expected exactly one {BEGIN!r} and one {END!r} marker in {MODULE_PATH}')
    pat = re.compile(re.escape(BEGIN) + r'.*?' + re.escape(END) + r'\n', re.S)
    new, n = pat.subn(lambda _m: block, src)
    if n != 1:
        raise SystemExit('marker block not found in order')
    return new


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--start', required=True)
    ap.add_argument('--end', required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--frequencies')
    g.add_argument('--db', action='store_true')
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--provenance', help='with --frequencies: where the targets came from (written into the comment)')
    a = ap.parse_args(argv)

    target = json.loads(a.frequencies) if a.frequencies else db_frequencies(a.start, a.end)
    closes = chr_mod.load_btc_closes()
    full = chr_mod.classify_history(closes=closes)
    win = full[(full['date'] >= pd.Timestamp(a.start).date())
               & (full['date'] <= pd.Timestamp(a.end).date())]
    rv = win['rv30_smoothed']
    tiers = chr_mod.calibrate_tiers(rv, target)
    print(f'window {a.start}..{a.end}: {len(win)} BTC-USD days ({rv.notna().sum()} with rv30)')
    print(f'cut-points: t1={tiers[0]:.6f} t2={tiers[1]:.6f} t3={tiers[2]:.6f}')
    new_tiers = ((tiers[0], 'LOW_VOL'), (tiers[1], 'TRANSITIONING'), (tiers[2], 'HIGH_VOL'))
    check = chr_mod.classify_history(a.start, a.end, closes=closes,
                                     tiers=new_tiers)
    got = check['regime'].value_counts(normalize=True)
    for k in chr_mod.CANONICAL_REGIMES:
        print(f'  {k:14s} target {target[k]*100:5.1f} %  matched {got.get(k, 0.0)*100:5.1f} %')
    hist = chr_mod.classify_history(closes=closes, tiers=new_tiers)
    hf = hist['regime'].value_counts(normalize=True)
    print('full-history shares: ' + ', '.join(f'{k} {hf.get(k, 0.0)*100:.1f} %'
                                              for k in chr_mod.CANONICAL_REGIMES + ('UNKNOWN',)))
    if a.write:
        if a.frequencies and not a.provenance:
            raise SystemExit('--write with --frequencies requires --provenance (say where the targets came from)')
        block = render_block(tiers, a.start, a.end, date.today().isoformat(), target,
                             n_days=len(win), provenance=None if a.db else a.provenance)
        MODULE_PATH.write_text(rewrite_module(MODULE_PATH.read_text(), block))
        print(f'rewrote CRYPTO_RV_TIERS in {MODULE_PATH}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
