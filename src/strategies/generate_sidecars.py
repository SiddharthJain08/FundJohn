#!/usr/bin/env python3
"""
One-shot script: scan strategy implementations and emit .requirements.json sidecars.
After this initial run, StrategyCoder writes sidecars as Artifact 4.

Usage:
    cd /root/openclaw && python3 src/strategies/generate_sidecars.py
"""
import ast
import json
import os
import re
import sys
from pathlib import Path

IMPL_DIR = Path(__file__).parent / 'implementations'

# Maps access-pattern tokens → canonical dataset/column names
DATASET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"aux_data.*'options'|aux_data.*\"options\""), 'options_eod'),
    (re.compile(r"options_eod|opts\.get\(|iv_spread|iv_rank|max_pain|gamma|theta|gex"), 'options_eod'),
    (re.compile(r"financials|quality_score|roe|pe_ratio|book_value|roic|gross_margin"), 'financials'),
    (re.compile(r"insider|form_4|cluster_buy|insider_trans"), 'insider'),
    (re.compile(r"macro|cpi|gdp|rate|yield|vix"), 'macro'),
    (re.compile(r"earnings|eps|dte|earnings_date"), 'earnings'),
    (re.compile(r"prices|close|returns|momentum|high_52|sma|ema|atr"), 'prices'),
]

OPTIONAL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"iv_skew|skew_25delta|put_call_skew"), 'implied_volatility_skew_25delta'),
    (re.compile(r"unusual_flow|flow_score"), 'unusual_options_flow'),
    (re.compile(r"short_interest|short_float"), 'short_interest'),
    (re.compile(r"beta\b"), 'market_beta'),
]

# Map file stem → strategy_id (use manifest.json naming convention)
def stem_to_id(stem: str) -> str:
    return stem  # keep as-is; matches manifest canonical_file stem


def detect_datasets(source: str) -> tuple[list[str], list[str]]:
    required: set[str] = set()
    optional: set[str] = set()

    # Prices are required by virtually every strategy
    required.add('prices')

    for pattern, dataset in DATASET_PATTERNS:
        if pattern.search(source):
            required.add(dataset)

    for pattern, col in OPTIONAL_PATTERNS:
        if pattern.search(source):
            optional.add(col)

    return sorted(required), sorted(optional)


def main() -> None:
    py_files = sorted(IMPL_DIR.glob('*.py'))
    py_files = [f for f in py_files if f.name != '__init__.py']

    written = 0
    for path in py_files:
        source = path.read_text(errors='replace')
        strategy_id = path.stem

        required, optional = detect_datasets(source)

        sidecar = {
            'strategy_id': strategy_id,
            'required': required,
            'optional': optional,
        }

        out_path = IMPL_DIR / f'{strategy_id}.requirements.json'
        out_path.write_text(json.dumps(sidecar, indent=2) + '\n')
        print(f'  {strategy_id}: required={required}, optional={optional}')
        written += 1

    print(f'\nWrote {written} sidecar files to {IMPL_DIR}')


if __name__ == '__main__':
    main()
