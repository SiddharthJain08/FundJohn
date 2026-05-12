#!/usr/bin/env python3
"""One-shot CLI to manually add/remove a regime from a strategy's eligible_regimes.

Usage:
  python scripts/update_eligible_regimes.py --strategy S21 --add HIGH_VOL
  python scripts/update_eligible_regimes.py --strategy S21 --remove CRISIS
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'src' / 'strategies' / 'manifest.json'
ALL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strategy', required=True)
    ap.add_argument('--add')
    ap.add_argument('--remove')
    args = ap.parse_args()

    if not args.add and not args.remove:
        sys.exit('Specify --add or --remove')

    target = args.add or args.remove
    if target not in ALL_REGIMES:
        sys.exit(f'Invalid regime {target!r}; must be one of {ALL_REGIMES}')

    manifest = json.loads(MANIFEST.read_text())
    record = manifest.get('strategies', {}).get(args.strategy)
    if record is None:
        sys.exit(f'Strategy {args.strategy} not in manifest')

    eligible = list(record.get('eligible_regimes') or list(ALL_REGIMES))
    if args.add and args.add not in eligible:
        eligible.append(args.add)
    if args.remove and args.remove in eligible:
        eligible.remove(args.remove)
    record['eligible_regimes'] = eligible

    MANIFEST.write_text(json.dumps(manifest, indent=2))
    print(f'{args.strategy} eligible_regimes = {eligible}')

if __name__ == '__main__':
    main()
