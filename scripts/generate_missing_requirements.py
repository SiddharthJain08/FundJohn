#!/usr/bin/env python3
"""
generate_missing_requirements.py — backfill <strategy>.requirements.json.

18 existing strategies never shipped a requirements.json, so the dashboard
approval flow and the lifecycle unstack hook (src/strategies/lifecycle.py
::_enqueue_orphan_columns) can't compute their data dependencies. This
helper scans each .py file for references to known data-types and writes
a best-effort JSON. Every emitted file is printed to stdout for human
review before committing.

Usage:
    python3 scripts/generate_missing_requirements.py            # write
    python3 scripts/generate_missing_requirements.py --dry-run  # preview only

Canonical data types (match schema_registry.json + existing JSONs):
    prices, financials, options_eod, insider, macro, earnings,
    unusual_options_flow, news
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

IMPL_DIR = Path(__file__).resolve().parent.parent / 'src' / 'strategies' / 'implementations'

# Pattern → canonical data-type. Ordered by specificity so longer keys win.
DETECTION_RULES = [
    (re.compile(r"aux_data(\[|\.get\()['\"]unusual[_\- ]flow['\"]"),    'unusual_options_flow'),
    (re.compile(r"aux_data(\[|\.get\()['\"]options['\"]"),               'options_eod'),
    (re.compile(r"aux_data(\[|\.get\()['\"]options_eod['\"]"),           'options_eod'),
    (re.compile(r"aux_data(\[|\.get\()['\"]iv['\"]|implied_volatility"), 'options_eod'),
    (re.compile(r"aux_data(\[|\.get\()['\"]insider['\"]|cluster_buy|form_4"), 'insider'),
    (re.compile(r"aux_data(\[|\.get\()['\"]financials?['\"]|eps|pe_ratio|gross_margin|free_cash_flow"), 'financials'),
    (re.compile(r"aux_data(\[|\.get\()['\"]earnings['\"]|earnings_dte|earnings_date"), 'earnings'),
    (re.compile(r"aux_data(\[|\.get\()['\"]macro['\"]|VIX|VVIX|VIX3M|regime\[|stress_score"), 'macro'),
    (re.compile(r"aux_data(\[|\.get\()['\"]news['\"]|market_news|sentiment"), 'news'),
]

# Prices are implicit: every strategy receives prices as a direct argument.
# We always declare it as required unless the strategy signature omits it
# (which would be an error anyway).
ALWAYS_REQUIRED = {'prices'}


def detect_columns(py_path: Path) -> set[str]:
    text = py_path.read_text(encoding='utf-8', errors='ignore')
    found: set[str] = set(ALWAYS_REQUIRED)
    for pat, col in DETECTION_RULES:
        if pat.search(text):
            found.add(col)
    return found


def load_existing(json_path: Path) -> dict | None:
    if not json_path.exists():
        return None
    try:
        return json.loads(json_path.read_text())
    except Exception:
        return None


def process(dry_run: bool) -> int:
    impls = sorted(p for p in IMPL_DIR.glob('*.py') if p.stem != '__init__')
    missing = [p for p in impls if not (p.with_suffix('.requirements.json')).exists()]
    if not missing:
        print(f'[gen_reqs] no missing requirements.json — {len(impls)} strategies are already covered.')
        return 0

    print(f'[gen_reqs] {len(missing)} strategies missing requirements.json:')
    for p in missing:
        print(f'  • {p.stem}')
    print()

    for p in missing:
        cols = sorted(detect_columns(p))
        strategy_id = p.stem  # implementations/FOO.py → FOO; canonical_file lives in manifest
        required = cols
        optional: list[str] = []
        payload = {
            'strategy_id': strategy_id,
            'required':    required,
            'optional':    optional,
        }
        json_text = json.dumps(payload, indent=2) + '\n'
        target = p.with_suffix('.requirements.json')
        print(f'── {target.name} ──')
        print(json_text.rstrip())
        if not dry_run:
            target.write_text(json_text, encoding='utf-8')
        print()

    if dry_run:
        print('[gen_reqs] DRY RUN — no files written. Rerun without --dry-run to apply.')
    else:
        print(f'[gen_reqs] wrote {len(missing)} file(s). Review each diff + human-check the detected columns before committing.')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='Print what would be written; make no changes.')
    args = ap.parse_args()
    sys.exit(process(args.dry_run))
