"""Tests for regime_blended_sizer_live._build_sized_payload.

Tests verify the field mapping from regime_blended_sizer's order shape into
the sized-handoff shape that alpaca_executor consumes (payload['orders']).

Key invariants checked:
  - payload uses 'orders' key (not 'signals') — alpaca_executor reads orders
  - pct_nav is set and equals abs(notional_usd) / equity
  - strategy_id is populated from contributions[0] (required for already_executed())
  - direction is lowercase string 'long'/'short' (matches deterministic_sizer convention)
  - entry/stop/t1 are floats mapped from bracket.entry_price/stop_loss/take_profit_1
  - qty (signed) → shares (int, absolute value)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest


def _make_order(ticker='AAPL', direction=1, qty=100.5, notional=10050.0,
                entry=100.0, stop=95.0, t1=110.0,
                strategy_id='S1', source_mode='consolidate',
                extra_contributions=None):
    contributions = [{'strategy_id': strategy_id, 'attribution_weight': 1.0}]
    if extra_contributions:
        contributions += extra_contributions
    return {
        'ticker': ticker,
        'direction': direction,
        'qty': qty,
        'notional_usd': notional,
        'bracket': {
            'entry_price': entry,
            'stop_loss': stop,
            'take_profit_1': t1,
        },
        'contributions': contributions,
        'source_mode': source_mode,
    }


class TestBuildSizedPayload:
    def test_payload_uses_orders_key_not_signals(self):
        """Critical: alpaca_executor reads handoff.get('orders'), not 'signals'."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        payload = _build_sized_payload([_make_order()], {'cycle_date': '2026-05-12'})
        assert 'orders' in payload
        assert 'signals' not in payload

    def test_basic_long_field_mapping(self):
        from execution.regime_blended_sizer_live import _build_sized_payload
        handoff = {'cycle_date': '2026-05-12', 'regime': {'state': 'LOW_VOL'}, 'prefiltered': []}
        orders = [_make_order()]
        payload = _build_sized_payload(orders, handoff, equity=100_000.0)

        assert payload['cycle_date'] == '2026-05-12'
        assert len(payload['orders']) == 1
        sig = payload['orders'][0]

        assert sig['ticker'] == 'AAPL'
        assert sig['direction'] == 'long'           # lowercase
        assert sig['strategy_id'] == 'S1'           # from contributions[0]
        assert sig['entry'] == pytest.approx(100.0)
        assert sig['stop'] == pytest.approx(95.0)
        assert sig['t1'] == pytest.approx(110.0)
        assert sig['shares'] == 100                 # int, absolute value of qty
        assert sig['notional_usd'] == pytest.approx(10050.0)
        assert sig['source_mode'] == 'consolidate'
        assert sig['contributing_strategies'] == ['S1']

    def test_pct_nav_computed_from_equity(self):
        """pct_nav = abs(notional_usd) / equity — required by alpaca_executor daily-cap."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        orders = [_make_order(notional=20_000.0)]
        payload = _build_sized_payload(orders, {'cycle_date': '2026-05-12'}, equity=100_000.0)
        sig = payload['orders'][0]
        assert sig['pct_nav'] == pytest.approx(0.2, rel=1e-5)

    def test_short_direction(self):
        from execution.regime_blended_sizer_live import _build_sized_payload
        orders = [_make_order(direction=-1, qty=-50, notional=5000.0,
                               entry=100.0, stop=105.0, t1=90.0)]
        payload = _build_sized_payload(orders, {'cycle_date': '2026-05-12'})
        sig = payload['orders'][0]
        assert sig['direction'] == 'short'
        assert sig['shares'] == 50                  # absolute value

    def test_empty_orders_returns_empty_list(self):
        from execution.regime_blended_sizer_live import _build_sized_payload
        payload = _build_sized_payload([], {'cycle_date': '2026-05-12'})
        assert payload['orders'] == []

    def test_multiple_contributing_strategies(self):
        """Consolidate-mode: strategy_id from first contribution; full list in contributing_strategies."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        orders = [_make_order(strategy_id='S1',
                               extra_contributions=[{'strategy_id': 'S2', 'attribution_weight': 0.5}])]
        payload = _build_sized_payload(orders, {'cycle_date': '2026-05-12'})
        sig = payload['orders'][0]
        assert sig['strategy_id'] == 'S1'           # first contributing strategy
        assert sig['contributing_strategies'] == ['S1', 'S2']

    def test_no_contributions_strategy_id_unknown(self):
        """Orders with empty contributions get strategy_id='unknown' (safe fallback)."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        order = _make_order()
        order['contributions'] = []
        payload = _build_sized_payload([order], {'cycle_date': '2026-05-12'})
        assert payload['orders'][0]['strategy_id'] == 'unknown'

    def test_vetoed_carries_prefiltered(self):
        """Prefiltered signals from handoff are carried into vetoed list."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        handoff = {
            'cycle_date': '2026-05-12',
            'prefiltered': [{'ticker': 'XYZ', 'strategy_id': 'S_OLD', 'reason': 'low_ev'}],
        }
        payload = _build_sized_payload([], handoff)
        assert len(payload['vetoed']) == 1
        assert payload['vetoed'][0]['ticker'] == 'XYZ'

    def test_tradejohn_decision_carried_through(self):
        """tradejohn_decision metadata from confirmer is forwarded to the order."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        order = _make_order()
        order['tradejohn_decision'] = {'action': 'approve', 'multiplier': 0.8, 'rationale': 'test'}
        payload = _build_sized_payload([order], {'cycle_date': '2026-05-12'})
        assert payload['orders'][0]['tradejohn_decision']['multiplier'] == 0.8

    def test_kelly_final_equals_pct_nav(self):
        """kelly_final is set to pct_nav as a proxy (no raw Kelly from consolidator path)."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        orders = [_make_order(notional=10_000.0)]
        payload = _build_sized_payload(orders, {'cycle_date': '2026-05-12'}, equity=100_000.0)
        sig = payload['orders'][0]
        assert sig['kelly_final'] == pytest.approx(sig['pct_nav'])

    def test_t2_is_none(self):
        """regime_blended_sizer does not produce t2; it must be None to avoid executor errors."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        payload = _build_sized_payload([_make_order()], {'cycle_date': '2026-05-12'})
        assert payload['orders'][0]['t2'] is None


def _sharpe_cadence_order(ticker='AAPL', direction='long', notional=10050.0,
                          entry=100.0, stop=95.0, t1=110.0,
                          strategies=('S_a',)):
    """Sharpe-cadence shape: string direction, top-level bracket fields,
    shares=0, contributing_strategies list, no `qty` key.
    """
    return {
        'ticker':                  ticker,
        'strategy_id':             '|'.join(strategies),
        'direction':               direction,
        'notional_usd':            notional,
        'pct_nav':                 notional / 100_000,
        'shares':                  0,
        'entry':                   entry,
        'stop':                    stop,
        't1':                      t1,
        't2':                      None,
        'kelly_final':             notional / 100_000,
        'ev':                      0.0,
        'p_t1':                    0.5,
        'source_mode':             'sharpe_cadence',
        'target_usd':              notional if direction == 'long' else -notional,
        'current_usd':             0.0,
        'contributing_strategies': list(strategies),
    }


class TestSharpeCadenceShape:
    """Regression: _build_sized_payload must accept the sharpe_cadence
    order shape (string direction, top-level entry/stop/t1, no `qty`,
    no `bracket` dict). Before fix it crashed with KeyError 'bracket'
    and TypeError comparing 'long' > 0."""

    def test_sharpe_cadence_long_order(self):
        from execution.regime_blended_sizer_live import _build_sized_payload
        payload = _build_sized_payload([_sharpe_cadence_order()],
                                       {'cycle_date': '2026-05-14'}, equity=100_000.0)
        assert len(payload['orders']) == 1
        sig = payload['orders'][0]
        assert sig['direction'] == 'long'
        assert sig['entry'] == 100.0
        assert sig['stop'] == 95.0
        assert sig['t1'] == 110.0
        # shares computed from notional/entry when `qty` absent
        assert sig['shares'] == 100
        assert sig['pct_nav'] > 0
        assert sig['strategy_id'] == 'S_a'   # top-level pass-through (joined)
        assert sig['source_mode'] == 'sharpe_cadence'

    def test_sharpe_cadence_short_order(self):
        from execution.regime_blended_sizer_live import _build_sized_payload
        o = _sharpe_cadence_order(direction='short', entry=100.0, stop=105.0, t1=90.0)
        payload = _build_sized_payload([o], {'cycle_date': '2026-05-14'}, equity=100_000.0)
        sig = payload['orders'][0]
        assert sig['direction'] == 'short'
        assert sig['shares'] >= 1

    def test_sharpe_cadence_missing_bracket_dropped(self):
        """Orders with no entry/stop/t1 (orphan-close case) are skipped, not crashed."""
        from execution.regime_blended_sizer_live import _build_sized_payload
        o = _sharpe_cadence_order(entry=None, stop=None, t1=None)
        payload = _build_sized_payload([o], {'cycle_date': '2026-05-14'}, equity=100_000.0)
        assert payload['orders'] == []  # silently dropped (logged to stderr)


# Legacy aggregate-cap tests removed 2026-05-14 — _apply_aggregate_cap
# was deleted with the cap drop. Sizer's per-ticker cap + sharpe_cadence
# normalization is the new authority; no aggregate-scale step remains.
