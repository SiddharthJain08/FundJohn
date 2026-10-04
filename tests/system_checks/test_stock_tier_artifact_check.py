import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from system_checks.checks.universe_tier_coherence import stock_tier_problems


def _df(rows):
    return pd.DataFrame([{'run_id': 'r', 'tier': t, 'snapshot_date': '2025-12-31', 'symbols': s}
                         for t, s in rows])


def test_clean_artifact_and_pre_phase1_artifact_pass():
    ok = _df([('sp500', ['A', 'B']), ('stocks_sp500', ['A'])])
    assert stock_tier_problems(ok, {'B': 'etf'}) == []
    assert stock_tier_problems(_df([('sp500', ['A', 'B'])]), {'B': 'etf'}) == []


def test_flags_non_subset_and_fund_members():
    bad = _df([('sp500', ['A']), ('stocks_sp500', ['A', 'Z', 'SPY'])])
    probs = stock_tier_problems(bad, {'SPY': 'etf'})
    assert any('not in sp500' in p for p in probs)
    assert any('etf/fund members' in p for p in probs)
