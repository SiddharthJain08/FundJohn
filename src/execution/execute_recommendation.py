#!/usr/bin/env python3
"""execute_recommendation.py — CLI entry point for bot-button recommendation execution.

Usage: python3 src/execution/execute_recommendation.py <rec_id>
Prints JSON result to stdout. Exit 0 on success, exit 1 on error.
"""
import os, sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.alpaca_trader import execute_recommendation


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'ok': False, 'error': 'Usage: execute_recommendation.py <rec_id>'}))
        sys.exit(1)

    rec_id       = sys.argv[1].strip()
    postgres_uri = os.environ.get('POSTGRES_URI', '')

    if not postgres_uri:
        print(json.dumps({'ok': False, 'error': 'POSTGRES_URI not set'}))
        sys.exit(1)

    result = execute_recommendation(rec_id, postgres_uri)
    print(json.dumps(result))
    sys.exit(0 if result.get('ok') else 1)


if __name__ == '__main__':
    main()
