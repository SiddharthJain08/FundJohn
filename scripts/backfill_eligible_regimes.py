#!/usr/bin/env python3
"""Phase 1 backfill: populate manifest.json `eligible_regimes` for every live strategy.

Usage:
  python scripts/backfill_eligible_regimes.py --output output/regime_eligibility_$(date +%Y-%m-%d).json
  # operator reviews; optionally edits the JSON
  python scripts/backfill_eligible_regimes.py --apply --input <reviewed.json>
"""
import argparse, json, os, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from backtest.regime_performance_analyzer import (
    analyze_dataframe, load_thresholds_from_db, load_signal_pnl,
)

MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'

def propose():
    uri = os.environ['POSTGRES_URI']
    thresholds = load_thresholds_from_db(uri)
    df = load_signal_pnl(uri, days=730)
    return analyze_dataframe(df, thresholds)

def apply(reviewed: dict):
    manifest = json.loads(MANIFEST.read_text())
    applied = 0
    for sid, body in reviewed.items():
        if sid in manifest.get('strategies', {}):
            manifest['strategies'][sid]['eligible_regimes'] = body['eligible_regimes']
            applied += 1
    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f'Applied eligible_regimes to {applied} strategies in {MANIFEST}')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', help='Write proposal JSON here')
    ap.add_argument('--apply', action='store_true', help='Write reviewed JSON into manifest')
    ap.add_argument('--input', help='Reviewed JSON to apply')
    args = ap.parse_args()
    if args.apply:
        if not args.input:
            sys.exit('--apply requires --input')
        apply(json.loads(Path(args.input).read_text()))
    else:
        result = propose()
        out = json.dumps(result, indent=2, default=str)
        if args.output:
            Path(args.output).parent.mkdir(parents=True, exist_ok=True)
            Path(args.output).write_text(out)
            print(f'Proposal written to {args.output} ({len(result)} strategies)')
        else:
            print(out)

if __name__ == '__main__':
    main()
