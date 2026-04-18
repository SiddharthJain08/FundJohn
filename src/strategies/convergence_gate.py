"""
convergence_gate.py — Re-exports run_backtest from auto_backtest for standalone use.

Usage:
    python3 src/strategies/convergence_gate.py src/strategies/implementations/S_xx.py

Runs the same 3-window convergence check as auto_backtest.py.
Exit 0 = passed (≥2 of 3 windows met Sharpe/DD/trade thresholds).
Exit 1 = failed.
"""

import sys
import json
from auto_backtest import run_backtest  # noqa: F401 — same module, just CLI alias


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 convergence_gate.py <path/to/strategy.py>', file=sys.stderr)
        sys.exit(1)

    result = run_backtest(sys.argv[1])
    print(json.dumps(result, indent=2))
    sys.exit(0 if result['passed'] else 1)
