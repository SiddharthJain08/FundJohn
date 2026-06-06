"""SP-7 Phase B Task 3 — B0 repair: gate, month enumeration, diff logic."""
from __future__ import annotations
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from scripts import sp7_b0_repair_metadata as b0


def test_gate_refuses_without_env():
    with patch.dict('os.environ', {}, clear=False):
        import os
        os.environ.pop('OPENCLAW_BACKFILL_ALLOW_OVERWRITE', None)
        with pytest.raises(SystemExit) as e:
            b0.check_overwrite_gate()
        assert e.value.code == 2


def test_gate_passes_with_env():
    with patch.dict('os.environ', {'OPENCLAW_BACKFILL_ALLOW_OVERWRITE': '1'}):
        b0.check_overwrite_gate()  # no raise


def test_month_ends_span():
    ends = b0.month_ends(date(2021, 1, 1), date(2021, 3, 15))
    assert ends == [date(2021, 1, 31), date(2021, 2, 28), date(2021, 3, 15)]
    # final partial month capped at end date


def test_diff_updates_only_changed_rows():
    existing = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': False, 'in_r3000': False, 'market_cap': None},
        {'symbol': 'OK',   'in_sp500': False, 'in_r1000': True, 'in_r3000': True, 'market_cap': 5e9},
    ])
    rebuilt = pd.DataFrame([
        {'symbol': 'AAPL', 'in_sp500': True, 'in_r1000': True, 'in_r3000': True, 'market_cap': 2.9e12},
        {'symbol': 'OK',   'in_sp500': False, 'in_r1000': True, 'in_r3000': True, 'market_cap': 5e9},
    ])
    updates = b0.diff_derived(existing, rebuilt)
    assert [u['symbol'] for u in updates] == ['AAPL']
    assert updates[0]['in_r1000'] is True and updates[0]['market_cap'] == 2.9e12
