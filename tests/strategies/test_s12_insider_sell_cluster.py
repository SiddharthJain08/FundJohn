"""Unit tests for S12_insider sell-cluster extension.

The existing strategy emits LONG signals on insider-buy clusters.
This file tests the NEW sell-cluster path gated by OPENCLAW_S12_SELL_CLUSTER.

Test #1 pins existing behavior (gate-OFF regression).
Tests #2-#8 cover the new SELL branch with the gate ON.

--- FIXTURE ADAPTATIONS from plan template ---

The real s12_insider.py contract differs from the plan template in three ways:

1. `prices` is a WIDE DataFrame indexed by date (DatetimeIndex) with ticker
   symbols as columns, not a flat table with 'ticker'/'date'/'close' columns.
   The strategy accesses `prices[ticker]` to get a Series of closes, and reads
   `prices.index[-1]` for the reference date.

2. `aux_data['insider_txns']` is a DICT of {ticker: [list of dicts]}, NOT a
   flat pandas DataFrame.  The strategy calls `insider_data.get(ticker, [])`.

3. Each transaction dict uses camelCase keys matching the FMP/SEC filing format:
   - `transactionDate`  (not `transaction_date`)
   - `transactionType`  (not `transaction_type`)
   - `value`            (not `net_value`)
   - `reportingName`    (not `insider_name`)

4. `regime` is a dict (e.g. {'state': 'LOW_VOL'}), not a bare string.

The SELL branch (Task 2) is expected to mirror the BUY branch, detecting
'SALE' or 'SELL' in the uppercased transactionType.  'S-Sale'.upper() →
'S-SALE', which contains 'SALE', so the existing plan fixtures translate
directly once column names are corrected.
"""
from __future__ import annotations

import os
from unittest.mock import patch

import pandas as pd
import pytest

from src.strategies.implementations.s12_insider import InsiderClusterBuy


# ---------------------------------------------------------------------------
# Fixture builders — adapted to the real strategy contract
# ---------------------------------------------------------------------------

def _make_txn(ticker: str, transaction_date: str, insider_name: str,
              transaction_type: str, net_value: float) -> dict:
    """Build a single insider transaction dict matching the real camelCase schema."""
    return {
        'transactionDate': transaction_date,
        'transactionType': transaction_type,
        'value':           net_value,
        'reportingName':   insider_name,
        # Optional fields that the strategy ignores but real data may carry:
        'ticker': ticker,
    }


def _make_aux(ticker_txns: dict[str, list[dict]]) -> dict:
    """Build aux_data with insider_txns as the dict-of-lists the strategy expects."""
    return {'insider_txns': ticker_txns}


def _make_prices(ticker: str, current_price: float = 100.0,
                 n_bars: int = 30) -> pd.DataFrame:
    """Build a wide prices DataFrame: DatetimeIndex × ticker-columns.

    The strategy uses:
      - prices.index[-1]  as the reference date
      - prices[ticker].dropna()  as the close series
      - prices.empty  as an early-exit guard
    """
    dates = pd.bdate_range('2026-04-14', periods=n_bars)
    closes = [current_price] * n_bars
    return pd.DataFrame({ticker: closes}, index=dates)


def _make_regime(state: str = 'LOW_VOL') -> dict:
    """Build a regime dict matching the real contract."""
    return {'state': state}


def _make_universe(tickers: list[str]) -> list[str]:
    return list(tickers)


def _five_sellers_3m(ticker: str = 'GLW') -> list[dict]:
    """5 distinct insiders selling $600K each = $3M total, 0 buys."""
    return [
        _make_txn(ticker, '2026-05-12', f'Officer_{i}', 'S-Sale', 600_000.0)
        for i in range(5)
    ]


# ---------------------------------------------------------------------------
# Test 1: gate-OFF regression (MUST PASS before SELL branch exists)
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '0'}, clear=False)
def test_gate_off_yields_no_short_signals_even_with_perfect_cluster():
    """With the gate OFF, no SHORT signals fire even on a GLW-like sell cluster.

    This test pins existing behavior — it MUST PASS before the SELL branch is
    implemented.  If it fails, the fixture does not match the real contract.
    """
    strat = InsiderClusterBuy()
    sells = _five_sellers_3m('GLW')
    sells.append(
        _make_txn('GLW', '2026-05-22', 'CTO_Jaymin_Amin', 'S-Sale', 5_260_000.0)
    )
    signals = strat.generate_signals(
        prices=_make_prices('GLW', current_price=192.0),
        regime=_make_regime('LOW_VOL'),
        universe=_make_universe(['GLW']),
        aux_data=_make_aux({'GLW': sells}),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == [], f'expected zero SHORTs with gate OFF, got {shorts}'


# ---------------------------------------------------------------------------
# Tests 2-8: gate ON — all MUST FAIL until the SELL branch is implemented
# ---------------------------------------------------------------------------

@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_five_sellers_3m_zero_buys_emits_one_short():
    """5 distinct sellers, $3M total, 0 buys → one SHORT signal on GLW."""
    strat = InsiderClusterBuy()
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime=_make_regime('LOW_VOL'),
        universe=_make_universe(['GLW']),
        aux_data=_make_aux({'GLW': _five_sellers_3m('GLW')}),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert len(shorts) == 1
    short = shorts[0]
    assert short.ticker == 'GLW'
    assert short.direction == 'SHORT'


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_four_sellers_blocks_breadth_gate():
    """Only 4 distinct sellers — fails the min_insiders=5 breadth gate."""
    strat = InsiderClusterBuy()
    sells = _five_sellers_3m('GLW')[:4]  # 4 sellers, not 5
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime=_make_regime('LOW_VOL'),
        universe=_make_universe(['GLW']),
        aux_data=_make_aux({'GLW': sells}),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == []


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_five_sellers_plus_one_buy_blocks_zero_buys_gate():
    """5 sellers + 1 buyer → zero-buys gate must block the SHORT signal."""
    strat = InsiderClusterBuy()
    sells = _five_sellers_3m('GLW')
    sells.append(
        _make_txn('GLW', '2026-05-15', 'Lone_Buyer', 'P-Purchase', 60_000.0)
    )
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime=_make_regime('LOW_VOL'),
        universe=_make_universe(['GLW']),
        aux_data=_make_aux({'GLW': sells}),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == [], 'zero-buys gate must block when any P-Purchase is in window'


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_five_sellers_below_dollar_threshold_blocks():
    """5 sellers × $250K = $1.25M — below the expected $2M+ cluster threshold."""
    strat = InsiderClusterBuy()
    sells = [
        _make_txn('GLW', '2026-05-12', f'Officer_{i}', 'S-Sale', 250_000.0)
        for i in range(5)
    ]
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime=_make_regime('LOW_VOL'),
        universe=_make_universe(['GLW']),
        aux_data=_make_aux({'GLW': sells}),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == []


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_one_seller_many_tranches_blocks_breadth_gate():
    """A single insider selling in 5 separate transactions must NOT pass breadth gate."""
    strat = InsiderClusterBuy()
    sells = [
        _make_txn('GLW', f'2026-05-{10 + i:02d}', 'Solo_CFO', 'S-Sale', 600_000.0)
        for i in range(5)
    ]
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime=_make_regime('LOW_VOL'),
        universe=_make_universe(['GLW']),
        aux_data=_make_aux({'GLW': sells}),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == [], 'distinct_insiders must use set; 1 person != 5 sellers'


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_glw_historical_fixture():
    """The actual GLW 2026-05 pattern: 10 distinct officers, ~$33M, 0 buys."""
    strat = InsiderClusterBuy()
    glw_rows = [
        ('Zhang John Z',           '2026-05-06', 2_770_000.0),
        ('Schlesinger Edward A',   '2026-05-07', 4_200_000.0),
        ('STEVERSON LEWIS A',      '2026-05-08', 5_440_000.0),
        ('Gullo Michelle L',       '2026-05-08', 1_000_000.0),
        ('Becker Stefan',          '2026-05-08', 3_950_000.0),
        ('TILLMAN MICHAUNE D',     '2026-05-12',   674_000.0),
        ('Seetharam Soumya',       '2026-05-12', 4_120_000.0),
        ('Verkleeren Ronald L',    '2026-05-14', 2_080_000.0),
        ('Nelson Avery H III',     '2026-05-18', 3_920_000.0),
        ('Amin Jaymin',            '2026-05-22', 5_260_000.0),
    ]
    sells = [
        _make_txn('GLW', date, name, 'S-Sale', value)
        for (name, date, value) in glw_rows
    ]
    signals = strat.generate_signals(
        prices=_make_prices('GLW', current_price=192.0),
        regime=_make_regime('LOW_VOL'),
        universe=_make_universe(['GLW']),
        aux_data=_make_aux({'GLW': sells}),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert len(shorts) == 1
    s = shorts[0]
    assert s.ticker == 'GLW'
    if getattr(s, 'stop_loss', None) is not None:
        assert s.stop_loss > 192.0, 'SHORT stop_loss must be above current price'
    if getattr(s, 'target_1', None) is not None:
        assert s.target_1 < 192.0, 'SHORT target_1 must be below current price'


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_buy_cluster_and_sell_cluster_simultaneously():
    """A ticker with BOTH 5 sellers AND 3 buyers — zero-buys gate blocks SHORT."""
    strat = InsiderClusterBuy()
    sells = _five_sellers_3m('ZZZ')
    buys = [
        _make_txn('ZZZ', '2026-05-13', f'Buyer_{i}', 'P-Purchase', 200_000.0)
        for i in range(3)
    ]
    all_txns = sells + buys
    signals = strat.generate_signals(
        prices=_make_prices('ZZZ'),
        regime=_make_regime('LOW_VOL'),
        universe=_make_universe(['ZZZ']),
        aux_data=_make_aux({'ZZZ': all_txns}),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == [], 'zero-buys gate must block SHORT when buys are present'
