import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pandas as pd
from execution.parity_diff import compute_diff, format_summary

def test_compute_diff_matched_within_tolerance():
    blended = pd.DataFrame([{'ticker': 'AAPL', 'notional_usd': 10000}])
    production = pd.DataFrame([{'ticker': 'AAPL', 'notional_usd': 10050}])
    diffs = compute_diff(blended, production, tolerance_pct=0.01)
    assert len(diffs['large_diffs']) == 0
    assert diffs['matched'] == 1

def test_compute_diff_large_difference_flagged():
    blended = pd.DataFrame([{'ticker': 'AAPL', 'notional_usd': 10000}])
    production = pd.DataFrame([{'ticker': 'AAPL', 'notional_usd': 15000}])
    diffs = compute_diff(blended, production, tolerance_pct=0.01)
    assert len(diffs['large_diffs']) == 1
    assert diffs['large_diffs'][0]['ticker'] == 'AAPL'

def test_compute_diff_only_in_blended():
    blended = pd.DataFrame([{'ticker': 'AAPL', 'notional_usd': 10000}])
    production = pd.DataFrame([])
    diffs = compute_diff(blended, production, tolerance_pct=0.01)
    assert 'AAPL' in diffs['only_in_blended']

def test_compute_diff_only_in_production():
    blended = pd.DataFrame([])
    production = pd.DataFrame([{'ticker': 'AAPL', 'notional_usd': 10000}])
    diffs = compute_diff(blended, production, tolerance_pct=0.01)
    assert 'AAPL' in diffs['only_in_production']

def test_format_summary_one_liner():
    msg = format_summary({'matched': 5, 'large_diffs': [], 'only_in_blended': ['X'],
                          'only_in_production': []}, regime='LOW_VOL', run_date='2026-05-12')
    assert 'LOW_VOL' in msg
    assert '5 matched' in msg or 'matched: 5' in msg
    assert '2026-05-12' in msg
