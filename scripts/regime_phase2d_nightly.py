#!/usr/bin/env python3
"""Phase 2D nightly job:
  1. Compute strategy_signal_overlap for window=90d.
  2. Backfill mastermind_proposal_outcomes for proposals decided 30+ days ago.

Run by systemd timer at 03:00 ET daily.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))


def main():
    from metrics.strategy_overlap import compute_overlap
    from metrics.mastermind_calibration import backfill_outcomes

    n_overlap = compute_overlap(window_days=90)
    print(f'[nightly] strategy_signal_overlap: inserted {n_overlap} rows')

    n_outcomes = backfill_outcomes(since_days=180, window_days=30)
    print(f'[nightly] mastermind_proposal_outcomes: backfilled {n_outcomes} rows')

    return 0


if __name__ == '__main__':
    sys.exit(main())
