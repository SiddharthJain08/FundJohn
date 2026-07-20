"""Tests for regime_blended_sizer_live._build_sized_payload.

Tests verify the field mapping from regime_blended_sizer's order shape into
the sized-handoff shape that alpaca_executor consumes (payload['orders']).

Key invariants checked:
  - payload uses 'orders' key (not 'signals') — alpaca_executor reads orders
  - pct_nav is set and equals abs(notional_usd) / equity
  - strategy_id is populated from contributions[0] (required for already_executed())
  - direction is lowercase string 'long'/'short'
  - entry/stop/t1 are floats mapped from bracket.entry_price/stop_loss/take_profit_1
  - qty (signed) → shares (int, absolute value)
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
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

    def test_sharpe_cadence_missing_bracket_emits_close_only(self):
        """Orders with no entry/stop/t1 are NOT dropped — they're emitted as
        close_only so the executor can act on them via `position close`.

        Contract changed in ba2d4fe (orphan-close fix). Two upstream cases
        produce a no-bracket order:
          1. Orphan close — ticker in broker, absent from current signals.
             Sizer sets strategy_id='__close_orphan__'.
          2. Partial reduce / flip close — delta opposite to all contributing
             signal directions, so `_select_bracket` returns {}. Sizer
             preserves the real (or '__flip_close__') strategy_id.

        Both must round-trip through _build_sized_payload as close_only=True
        with strategy_id intact — the executor's tier-priority depends on
        the strategy_id, and the close mechanism (`alpaca position close`
        with optional --percentage) depends on close_only=True.
        """
        from execution.regime_blended_sizer_live import _build_sized_payload
        o = _sharpe_cadence_order(entry=None, stop=None, t1=None)
        payload = _build_sized_payload([o], {'cycle_date': '2026-05-14'}, equity=100_000.0)
        assert len(payload['orders']) == 1, 'orphan-close must emit, not be dropped'
        out = payload['orders'][0]
        assert out['close_only'] is True
        assert out['entry'] is None and out['stop'] is None and out['t1'] is None
        # strategy_id passes through from sizer (here 'S_a' from the helper);
        # the orphan/flip distinction is upstream in the sizer.
        assert out['strategy_id'] == 'S_a'
        assert out['ticker'] == 'AAPL'


class TestDirectionFlipEmission:
    """A direction flip (current long → target short, or vice-versa) emits
    TWO orders from the sizer:
      • flip_close with strategy_id='__flip_close__' and no bracket
        (close_only path in payload builder, tier-1 in executor)
      • flip_open with the real joined strategy_id and a populated bracket
        in the NEW direction (tier-2/3 in executor)

    These tests pin the payload-builder routing. End-to-end sizer behavior
    is exercised by integration tests with mocked broker positions.
    """

    def test_flip_close_routes_to_close_only_branch(self):
        from execution.regime_blended_sizer_live import _build_sized_payload
        # Sizer-emitted flip_close: bracket fields are None → payload
        # builder takes the close_only branch; strategy_id stays
        # '__flip_close__' (NOT remapped to '__close_orphan__').
        o = _sharpe_cadence_order(ticker='XYZ', direction='short',
                                  notional=20_000.0,
                                  entry=None, stop=None, t1=None,
                                  strategies=('__flip_close__',))
        payload = _build_sized_payload([o], {'cycle_date': '2026-05-22'},
                                       equity=100_000.0)
        assert len(payload['orders']) == 1
        out = payload['orders'][0]
        assert out['ticker'] == 'XYZ'
        assert out['close_only'] is True
        assert out['strategy_id'] == '__flip_close__'
        assert out['direction'] == 'short'
        assert out['entry'] is None and out['stop'] is None and out['t1'] is None

    def test_flip_open_routes_to_bracket_branch_with_real_sid(self):
        from execution.regime_blended_sizer_live import _build_sized_payload
        # Sizer-emitted flip_open: full bracket in the new direction with
        # real joined strategy_id.
        o = _sharpe_cadence_order(ticker='XYZ', direction='short',
                                  notional=10_000.0,
                                  entry=50.0, stop=53.0, t1=45.0,
                                  strategies=('S_short_a', 'S_short_b'))
        payload = _build_sized_payload([o], {'cycle_date': '2026-05-22'},
                                       equity=100_000.0)
        assert len(payload['orders']) == 1
        out = payload['orders'][0]
        assert out.get('close_only') is None or out.get('close_only') is False
        assert out['strategy_id'] != '__flip_close__'
        assert out['strategy_id'] != '__close_orphan__'
        assert out['direction'] == 'short'
        assert out['entry'] == 50.0
        assert out['stop'] == 53.0
        assert out['t1'] == 45.0


class TestExecutorFlipPriorityAndPolling:
    """Pin the executor's tier-priority + flip-pair tracking. The flip_close
    must land in tier-1 (close before opens); the matched flip_open must be
    skipped if the close failed to fill."""

    def test_flip_close_tier_is_1_not_0(self):
        from execution.alpaca_executor import _exec_priority_for_test
        # If the helper hasn't been exposed, fall through to importing the
        # inner function directly via a small ad-hoc replica matching the
        # main-loop logic.
        flip_close = {'strategy_id': '__flip_close__', 'close_only': True,
                      'direction': 'short', 'notional_usd': 20_000.0}
        orphan     = {'strategy_id': '__close_orphan__', 'close_only': True,
                      'direction': 'long', 'notional_usd': 5_000.0}
        flip_open  = {'strategy_id': 'S_a|S_b', 'direction': 'short',
                      'notional_usd': 10_000.0}
        long_open  = {'strategy_id': 'S_c', 'direction': 'long',
                      'notional_usd': 8_000.0}
        assert _exec_priority_for_test(orphan)[0]     == 0
        assert _exec_priority_for_test(flip_close)[0] == 1
        assert _exec_priority_for_test(flip_open)[0]  == 2
        assert _exec_priority_for_test(long_open)[0]  == 3


# Legacy aggregate-cap tests removed 2026-05-14 — _apply_aggregate_cap
# was deleted with the cap drop. Sizer's per-ticker cap + sharpe_cadence
# normalization is the new authority; no aggregate-scale step remains.


class TestResolveAccountOrNone:
    """W3 F1: account-fetch failure must return None — caller aborts (zero orders),
    never fabricates equity.  Tests drive _resolve_account_or_none directly
    via injected session_fn / fetch_fn so no live Alpaca creds are needed.
    """

    def test_returns_none_when_session_fn_raises(self):
        """If the session factory raises, the helper must return None (not a fake dict)."""
        from execution.regime_blended_sizer_live import _resolve_account_or_none

        def _bad_session():
            raise RuntimeError('connection refused')

        result = _resolve_account_or_none(session_fn=_bad_session)
        assert result is None, (
            'fetch failure must return None, not a fabricated account dict'
        )

    def test_returns_none_when_fetch_fn_raises(self):
        """If fetch_fn raises (session ok, fetch fails), helper must return None."""
        from execution.regime_blended_sizer_live import _resolve_account_or_none

        def _ok_session():
            return object()

        def _bad_fetch(sess):
            raise ConnectionError('Alpaca API unreachable')

        result = _resolve_account_or_none(session_fn=_ok_session, fetch_fn=_bad_fetch)
        assert result is None

    def test_returns_account_dict_on_success(self):
        """Happy path: valid account dict passes through unchanged."""
        from execution.regime_blended_sizer_live import _resolve_account_or_none

        expected = {'equity': 250_000.0, 'fetched': True,
                    'regt_buying_power': 1_000_000.0}

        def _ok_session():
            return object()

        def _ok_fetch(sess):
            return expected

        result = _resolve_account_or_none(session_fn=_ok_session, fetch_fn=_ok_fetch)
        assert result is expected

    def test_returns_none_on_generic_exception(self):
        """Any exception (not just network errors) causes a None return."""
        from execution.regime_blended_sizer_live import _resolve_account_or_none

        def _bad_session():
            raise ValueError('unexpected response shape')

        result = _resolve_account_or_none(session_fn=_bad_session)
        assert result is None
