"""tests/test_corporate_actions.py

Unit tests for src/backtest/adjust_for_corporate_actions.py and
src/pipeline/alpaca_corporate_actions.py (Phase 2.3 of alpaca-cli
integration). Both helpers operate against a synthetic
corporate_actions.parquet seeded into a tempdir, so no live broker call
and no master-parquet contact.

Run:
    pytest tests/test_corporate_actions.py -v
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))


# ── adjust_for_corporate_actions ───────────────────────────────────────────

@pytest.fixture
def synthetic_corp_parquet(tmp_path, monkeypatch):
    """Seed a temp corporate_actions.parquet with NVDA 10:1 + a 2:1+3:1 ticker."""
    parquet = tmp_path / 'corporate_actions.parquet'
    rows = [
        # NVDA 10:1 forward split on 2024-06-10
        {'id': 'nvda-1', 'symbol': 'NVDA', 'action_type': 'forward_split',
         'ex_date': '2024-06-10', 'ratio': 10.0, 'new_rate': 10, 'old_rate': 1,
         'cash_amount': None, 'cusip': None, 'payable_date': None,
         'record_date': None, 'raw': '{}'},
        # SYNTH ticker: 2:1 then 3:1 (cumulative 6:1)
        {'id': 'synth-1', 'symbol': 'SYNTH', 'action_type': 'forward_split',
         'ex_date': '2024-01-15', 'ratio': 2.0, 'new_rate': 2, 'old_rate': 1,
         'cash_amount': None, 'cusip': None, 'payable_date': None,
         'record_date': None, 'raw': '{}'},
        {'id': 'synth-2', 'symbol': 'SYNTH', 'action_type': 'forward_split',
         'ex_date': '2024-08-20', 'ratio': 3.0, 'new_rate': 3, 'old_rate': 1,
         'cash_amount': None, 'cusip': None, 'payable_date': None,
         'record_date': None, 'raw': '{}'},
        # SYNTH cash dividend — should NOT alter prices in current pass
        {'id': 'synth-div', 'symbol': 'SYNTH', 'action_type': 'cash_dividend',
         'ex_date': '2024-04-01', 'ratio': None, 'new_rate': None, 'old_rate': None,
         'cash_amount': 0.50, 'cusip': None, 'payable_date': None,
         'record_date': None, 'raw': '{}'},
    ]
    pd.DataFrame(rows).to_parquet(parquet, index=False)
    # Patch the module's parquet path
    from backtest import adjust_for_corporate_actions as adj
    monkeypatch.setattr(adj, 'CORPORATE_ACTIONS_PARQUET', parquet)
    # Also need to clear the (no) cache — _load_split_actions_for doesn't
    # cache, so this patch is sufficient
    return parquet


def test_forward_split_2_for_1_halves_pre_split_prices(synthetic_corp_parquet):
    """SYNTH had a 2:1 split on 2024-01-15. Prices BEFORE 2024-01-15
    should be halved on read (and the second 3:1 also applies, so total ÷6
    for pre-2024-01-15 dates."""
    from backtest.adjust_for_corporate_actions import adjusted_close
    dates = ['2024-01-01', '2024-02-01', '2024-08-21', '2024-12-31']
    raw = [600.0, 300.0, 100.0, 100.0]
    adjusted = adjusted_close('SYNTH', dates, raw)
    # 2024-01-01: before BOTH splits → divided by 2*3 = 6
    assert adjusted[0] == pytest.approx(100.0)
    # 2024-02-01: after 2:1 but before 3:1 → divided by 3
    assert adjusted[1] == pytest.approx(100.0)
    # 2024-08-21: after both → unchanged
    assert adjusted[2] == pytest.approx(100.0)
    # 2024-12-31: after both → unchanged
    assert adjusted[3] == pytest.approx(100.0)


def test_multiple_splits_compose(synthetic_corp_parquet):
    """Already exercised in the 2-for-1 test above — keep an explicit named
    test to make the composition guarantee discoverable."""
    from backtest.adjust_for_corporate_actions import adjusted_close
    dates = ['2024-01-01']
    raw = [600.0]
    adj = adjusted_close('SYNTH', dates, raw)
    # 2:1 then 3:1 → cum_ratio = 6 → 600 / 6 = 100
    assert adj[0] == pytest.approx(100.0)


def test_dividend_does_not_alter_prices(synthetic_corp_parquet):
    """Cash-dividend rows are recorded but NOT applied — split-only adjustment."""
    from backtest.adjust_for_corporate_actions import adjusted_close
    # Dates around the cash dividend on 2024-04-01 (ratio is None for it)
    dates = ['2024-03-30', '2024-04-02']
    raw = [100.0, 100.0]
    adjusted = adjusted_close('SYNTH', dates, raw)
    # Only the 2:1 (2024-01-15) and 3:1 (2024-08-20) splits affect 2024-03-30
    # and 2024-04-02 — both fall AFTER the 2:1 split and BEFORE the 3:1.
    # So both prices are divided by 3 only.
    assert adjusted[0] == pytest.approx(100.0 / 3.0)
    assert adjusted[1] == pytest.approx(100.0 / 3.0)


def test_no_actions_returns_raw(synthetic_corp_parquet):
    """A ticker with no corporate actions returns prices unchanged."""
    from backtest.adjust_for_corporate_actions import adjusted_close
    dates = ['2024-01-01', '2024-12-31']
    raw = [100.0, 200.0]
    adjusted = adjusted_close('UNKNOWN', dates, raw)
    assert adjusted == raw


def test_adjust_dataframe_handles_volume(synthetic_corp_parquet):
    """adjust_dataframe should also scale volume by cum_ratio (inverse to price)."""
    from backtest.adjust_for_corporate_actions import adjust_dataframe
    df = pd.DataFrame([
        {'ticker': 'SYNTH', 'date': '2024-01-01', 'open': 600, 'high': 600,
         'low':    600,    'close': 600, 'volume': 1_000_000},
        {'ticker': 'SYNTH', 'date': '2024-12-31', 'open': 100, 'high': 100,
         'low':    100,    'close': 100, 'volume': 6_000_000},
    ])
    out = adjust_dataframe(df, ticker_col='ticker', date_col='date',
                           close_cols=('open', 'high', 'low', 'close'))
    # 2024-01-01 row: close 600 / 6 = 100, volume 1M * 6 = 6M
    row0 = out[out['date'] == '2024-01-01'].iloc[0]
    assert row0['close']  == pytest.approx(100.0)
    assert row0['volume'] == pytest.approx(6_000_000)
    # 2024-12-31 row: unchanged
    row1 = out[out['date'] == '2024-12-31'].iloc[0]
    assert row1['close']  == pytest.approx(100.0)


# ── alpaca_corporate_actions fetcher ───────────────────────────────────────

def _mock_proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_fetch_corporate_actions_parses_response():
    """Mock the CLI; verify forward_split rows produce ratio = new_rate/old_rate."""
    from pipeline import alpaca_corporate_actions as aca
    cli_response = json.dumps({
        'corporate_actions': {
            'forward_splits': [{
                'id': 'split-1', 'symbol': 'NVDA',
                'ex_date': '2024-06-10', 'new_rate': 10, 'old_rate': 1,
                'payable_date': '2024-06-10', 'record_date': '2024-06-07',
                'cusip': '67066G104',
            }],
            'cash_dividends': [{
                'id': 'div-1', 'symbol': 'AAPL',
                'ex_date': '2025-02-10', 'rate': 0.24,
                'payable_date': '2025-02-13',
            }],
        },
        'next_page_token': '',
    })
    with patch('pipeline.alpaca_corporate_actions.subprocess.run',
               return_value=_mock_proc(0, cli_response, '')):
        rows = aca.fetch_corporate_actions(['NVDA', 'AAPL'],
                                            '2024-01-01', '2025-12-31')
    assert len(rows) == 2
    fwd = next(r for r in rows if r['action_type'] == 'forward_split')
    assert fwd['symbol'] == 'NVDA'
    assert fwd['ratio']  == 10.0
    assert fwd['ex_date'] == '2024-06-10'
    div = next(r for r in rows if r['action_type'] == 'cash_dividend')
    assert div['symbol']      == 'AAPL'
    assert div['cash_amount'] == 0.24
    assert div['ratio']       is None
