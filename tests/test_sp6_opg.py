"""tests/test_sp6_opg.py — SP-6 Task 10: alpaca_executor OPG dual-path for drops.

RE-INTRODUCES OPG (market-on-open), removed after the 2026-05-18 paper-fill
incident (214/224 OPG closes ack'd-but-EXPIRED unfilled while the DB wrongly said
"closed"). These tests pin the ack≠fill discipline: the result returned for an
is_dropped close reflects the POLLED terminal outcome — status='filled' (with the
real fill price) ONLY on a confirmed fill; expired/canceled/etc. otherwise.

No DB. The Alpaca CLI (`_run_alpaca_cli`), the position-qty fetch
(`_fetch_position_qty`), the session classifier (`_alpaca_session_kind`), and the
poll-to-terminal loop (`_poll_to_terminal`) are all mocked — NO real orders.

Branching on OPENCLAW_OPEN_CLOSE_MODE ∈ {rth_market, opg_then_day, opg_live}:
  • rth_market (default, safe) — existing RTH `position close`; for an is_dropped
    close it ADDITIONALLY polls-to-terminal so it reports a confirmed `filled`.
  • opg_then_day (paper) — market tif=opg pre-open → poll → if EXPIRED, fire the
    tif=day RTH `position close` day-sweep → report the sweep's polled outcome.
  • opg_live — OPG + day-sweep belt-and-suspenders (same code path in Phase A).

Run:
    source ./worktree-env.sh
    python3 -m pytest tests/test_sp6_opg.py -v
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ae  # noqa: E402
from execution.open_reconcile import _resolve_fill_price  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _dropped_order(ticker='SPY', side='sell', current_usd=5000.0):
    """A run_reconcile-shaped is_dropped close_only order (positional call)."""
    return {
        'ticker': ticker,
        'side': side,
        'direction': 'short' if side == 'sell' else 'long',
        'close_only': True,
        'is_dropped': True,
        'current_usd': current_usd,
        'notional_usd': abs(current_usd),
        'strategy_id': '__close_orphan__',
    }


def _exec(order, run_date='2026-05-18', equity=5000.0):
    """Invoke execute_single with the REAL positional signature
    (sess, equity, order, run_date) — matches run_reconcile's call."""
    return ae.execute_single(None, equity, order, run_date)


# ──────────────────────────────────────────────────────────────────────────────
# (1) 2026-05-18 REPLAY (core): OPG acks then EXPIRES → day-sweep fires
# ──────────────────────────────────────────────────────────────────────────────

def test_opg_then_day_opg_expires_fires_day_sweep():
    """OPG submitted pre-open, ACKS, then EXPIRES unfilled (the 2026-05-18 case).
    opg_then_day must: (a) poll the OPG to terminal, (b) detect the non-fill,
    (c) fire the tif=day RTH `position close` day-sweep, (d) report the SWEEP's
    confirmed terminal status — NOT report `filled` for the expired OPG."""

    # session: premarket at OPG submit → rth at the day-sweep check.
    sessions = iter(['premarket', 'rth'])

    # _run_alpaca_cli sequence: OPG submit (ok), then the day-sweep position close
    # (ok). (No cancel: 'expired' is already terminal.)
    cli_calls = []

    def fake_cli(args, timeout=30):
        cli_calls.append(list(args))
        if args[:2] == ['order', 'submit']:
            return True, {'id': 'opg_oid', 'status': 'accepted'}, None
        if args[:2] == ['position', 'close']:
            return True, {'id': 'sweep_oid', 'qty': 50, 'notional': 5000.0}, None
        raise AssertionError(f'unexpected CLI call: {args}')

    # poll: first call (OPG) → expired; second call (sweep) → filled.
    polls = iter([
        {'id': 'opg_oid', 'status': 'expired', 'filled_qty': 0},
        {'id': 'sweep_oid', 'status': 'filled', 'filled_qty': 50,
         'filled_avg_price': 101.25},
    ])

    with mock.patch.dict('os.environ', {'OPENCLAW_OPEN_CLOSE_MODE': 'opg_then_day'}), \
            mock.patch.object(ae, '_alpaca_session_kind', side_effect=lambda: next(sessions)), \
            mock.patch.object(ae, '_fetch_position_qty', return_value=50.0), \
            mock.patch.object(ae, '_run_alpaca_cli', side_effect=fake_cli), \
            mock.patch.object(ae, '_poll_to_terminal', side_effect=lambda *a, **k: next(polls)):
        res = _exec(_dropped_order())

    # (d) NOT `filled` for the expired OPG — the OPG branch fell through.
    # The FINAL result reflects the SWEEP's confirmed fill.
    assert res['status'] == 'filled', res
    assert res['order_id'] == 'sweep_oid'
    assert abs(res['entry'] - 101.25) < 1e-6
    assert res['tif'] == 'day'                      # day-sweep, not opg
    assert res['client_order_id'].endswith('_C')    # reason-tagged close coid kept

    # (b)/(c): OPG submit (tif=opg) then a position close were both issued.
    submit_calls = [c for c in cli_calls if c[:2] == ['order', 'submit']]
    assert len(submit_calls) == 1
    assert '--time-in-force' in submit_calls[0]
    assert submit_calls[0][submit_calls[0].index('--time-in-force') + 1] == 'opg'
    assert any(c[:2] == ['position', 'close'] for c in cli_calls)

    # The CONFIRMED-fill result DOES drive a ledger-close.
    assert _resolve_fill_price(res) == 101.25


def test_opg_expired_result_pre_sweep_blocks_ledger_close():
    """The actual 8b landmine: the EXPIRED OPG terminal dict (ack-but-expire) must
    NEVER resolve to a fill price. _resolve_fill_price returns None → no ledger
    write → CLOSED_AT_OPEN is NOT written on a still-held position."""
    expired_opg = {'ticker': 'SPY', 'status': 'expired', 'order_id': 'opg_oid',
                   'tif': 'opg', 'client_order_id': 'AX..._C'}
    assert _resolve_fill_price(expired_opg) is None

    # And a submit-ack (the OLD pre-Task-10 contract) is ALSO blocked now.
    bare_ack = {'status': 'submitted', 'entry': 101.0, 'order_id': 'x'}
    assert _resolve_fill_price(bare_ack) is None

    # 'recovered' can't sneak a ledger-close either.
    recovered = {'status': 'recovered', 'entry': 101.0}
    assert _resolve_fill_price(recovered) is None

    # Only a confirmed fill resolves.
    filled = {'status': 'filled', 'entry': 101.0}
    assert _resolve_fill_price(filled) == 101.0


# ──────────────────────────────────────────────────────────────────────────────
# (2) opg_then_day happy path: OPG FILLS at the open → filled + fill price
# ──────────────────────────────────────────────────────────────────────────────

def test_opg_then_day_opg_fills_at_open():
    """OPG fills at the open cross → execute_single reports status='filled' with
    the real fill price and does NOT day-sweep. _resolve_fill_price → price."""

    def fake_cli(args, timeout=30):
        if args[:2] == ['order', 'submit']:
            return True, {'id': 'opg_oid', 'status': 'accepted'}, None
        raise AssertionError(f'no further CLI expected; got {args}')

    with mock.patch.dict('os.environ', {'OPENCLAW_OPEN_CLOSE_MODE': 'opg_then_day'}), \
            mock.patch.object(ae, '_alpaca_session_kind', return_value='premarket'), \
            mock.patch.object(ae, '_fetch_position_qty', return_value=50.0), \
            mock.patch.object(ae, '_run_alpaca_cli', side_effect=fake_cli), \
            mock.patch.object(ae, '_poll_to_terminal', return_value={
                'id': 'opg_oid', 'status': 'filled', 'filled_qty': 50,
                'filled_avg_price': 99.80}):
        res = _exec(_dropped_order())

    assert res['status'] == 'filled', res
    assert res['order_id'] == 'opg_oid'
    assert res['tif'] == 'opg'                       # filled on the OPG, no sweep
    assert abs(res['entry'] - 99.80) < 1e-6
    assert res['qty'] == 50
    assert res['client_order_id'].endswith('_C')
    # Confirmed fill → ledger-close fires.
    assert _resolve_fill_price(res) == 99.80


def test_opg_live_also_fills_at_open():
    """opg_live shares the opg_then_day code path in Phase A: OPG fill → filled."""
    with mock.patch.dict('os.environ', {'OPENCLAW_OPEN_CLOSE_MODE': 'opg_live'}), \
            mock.patch.object(ae, '_alpaca_session_kind', return_value='premarket'), \
            mock.patch.object(ae, '_fetch_position_qty', return_value=10.0), \
            mock.patch.object(ae, '_run_alpaca_cli',
                              return_value=(True, {'id': 'opg_oid'}, None)), \
            mock.patch.object(ae, '_poll_to_terminal', return_value={
                'status': 'filled', 'filled_qty': 10, 'filled_avg_price': 50.0}):
        res = _exec(_dropped_order(current_usd=500.0))
    assert res['status'] == 'filled'
    assert res['tif'] == 'opg'


# ──────────────────────────────────────────────────────────────────────────────
# (3) rth_market (default): is_dropped close in RTH → polled position close
# ──────────────────────────────────────────────────────────────────────────────

def test_rth_market_dropped_close_polls_to_filled():
    """rth_market (default) for an is_dropped close: ONE `position close` submit
    (no OPG), then poll-to-terminal → reports a confirmed `filled`. ack≠fill: the
    submit-ack alone is never reported as filled."""
    cli_calls = []

    def fake_cli(args, timeout=30):
        cli_calls.append(list(args))
        assert args[:2] == ['position', 'close'], args
        return True, {'id': 'close_oid', 'qty': 50, 'notional': 5000.0}, None

    # No OPENCLAW_OPEN_CLOSE_MODE set → defaults to rth_market.
    with mock.patch.dict('os.environ', {}, clear=False), \
            mock.patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
            mock.patch.object(ae, '_run_alpaca_cli', side_effect=fake_cli), \
            mock.patch.object(ae, '_poll_to_terminal', return_value={
                'id': 'close_oid', 'status': 'filled', 'filled_qty': 50,
                'filled_avg_price': 100.5}) as m_poll:
        import os as _os
        _os.environ.pop('OPENCLAW_OPEN_CLOSE_MODE', None)
        res = _exec(_dropped_order())

    assert res['status'] == 'filled', res
    assert res['order_id'] == 'close_oid'
    assert abs(res['entry'] - 100.5) < 1e-6
    assert res['tif'] == 'day'
    # Exactly ONE CLI submit (the position close) — no OPG order submit.
    assert len(cli_calls) == 1
    assert not any(c[:2] == ['order', 'submit'] for c in cli_calls)
    m_poll.assert_called_once()
    assert _resolve_fill_price(res) == 100.5


def test_rth_market_dropped_close_not_filled_blocks_ledger():
    """rth_market is_dropped close that the broker does NOT fill (e.g. canceled):
    execute_single reports the polled terminal status with NO price, so
    _resolve_fill_price → None and the ledger-close does NOT fire."""
    with mock.patch.dict('os.environ', {'OPENCLAW_OPEN_CLOSE_MODE': 'rth_market'}), \
            mock.patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
            mock.patch.object(ae, '_run_alpaca_cli',
                              return_value=(True, {'id': 'close_oid', 'qty': 50}, None)), \
            mock.patch.object(ae, '_poll_to_terminal', return_value={
                'id': 'close_oid', 'status': 'canceled', 'filled_qty': 0}):
        res = _exec(_dropped_order())

    assert res['status'] == 'canceled', res
    assert res.get('entry') in (None, 0, 0.0)
    assert _resolve_fill_price(res) is None


def test_rth_market_dropped_premarket_defers_pending():
    """rth_market is_dropped close seen in PREMARKET (no OPG armed) → reports
    pending so no ledger-close fires until the 9:31 day-sweep runs."""
    with mock.patch.dict('os.environ', {'OPENCLAW_OPEN_CLOSE_MODE': 'rth_market'}), \
            mock.patch.object(ae, '_alpaca_session_kind', return_value='premarket'), \
            mock.patch.object(ae, '_run_alpaca_cli') as m_cli, \
            mock.patch.object(ae, '_poll_to_terminal') as m_poll:
        res = _exec(_dropped_order())

    assert res['status'] == 'pending', res
    m_cli.assert_not_called()        # nothing submitted pre-open in rth_market
    m_poll.assert_not_called()
    assert _resolve_fill_price(res) is None


# ──────────────────────────────────────────────────────────────────────────────
# (4) Regression: the NON-dropped close path is byte-identical (no poll, no OPG)
# ──────────────────────────────────────────────────────────────────────────────

def test_non_dropped_close_path_unchanged_rth():
    """A regular (non-is_dropped) close_only order in RTH: single `position close`,
    status='submitted' (the legacy ack-shaped result), NO poll, NO OPG — proving
    the existing pipeline close path is untouched even with OPG modes set."""
    order = {
        'ticker': 'SPY', 'direction': 'short', 'close_only': True,
        'notional_usd': 5000.0, 'current_usd': 5000.0, 'strategy_id': 's1',
    }
    with mock.patch.dict('os.environ', {'OPENCLAW_OPEN_CLOSE_MODE': 'opg_then_day'}), \
            mock.patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
            mock.patch.object(ae, '_run_alpaca_cli',
                              return_value=(True, {'id': 'oid', 'qty': 50,
                                                   'notional': 5000.0}, None)), \
            mock.patch.object(ae, '_poll_to_terminal') as m_poll:
        res = _exec(order)

    # Legacy ack-shaped result preserved (status='submitted', under 'entry').
    assert res['status'] == 'submitted', res
    assert res['order_id'] == 'oid'
    assert res['tif'] == 'day'
    m_poll.assert_not_called()       # non-dropped path never polls


# ──────────────────────────────────────────────────────────────────────────────
# (5) Mode resolution: unknown / unset value falls back to the safe default
# ──────────────────────────────────────────────────────────────────────────────

def test_open_close_mode_defaults_and_rejects_typos():
    import os as _os
    _os.environ.pop('OPENCLAW_OPEN_CLOSE_MODE', None)
    assert ae._open_close_mode() == 'rth_market'
    with mock.patch.dict('os.environ', {'OPENCLAW_OPEN_CLOSE_MODE': 'opg_then_day'}):
        assert ae._open_close_mode() == 'opg_then_day'
    with mock.patch.dict('os.environ', {'OPENCLAW_OPEN_CLOSE_MODE': 'OPG_LIVE_typo'}):
        assert ae._open_close_mode() == 'rth_market'   # typo never arms OPG


# ──────────────────────────────────────────────────────────────────────────────
# (6) Fix 3: filled-without-price guard — _resolve_fill_price loud + safe
# ──────────────────────────────────────────────────────────────────────────────

def test_resolve_fill_price_filled_zero_returns_none():
    """status='filled' with a zero/None fill price returns None (safe: no ledger
    write) AND emits a WARNING so the corner is not silent."""
    from execution.open_reconcile import _resolve_fill_price

    # None and 0.0 both result in None.
    assert _resolve_fill_price({'status': 'filled', 'entry': 0.0}) is None
    assert _resolve_fill_price({'status': 'filled', 'entry': None}) is None
    assert _resolve_fill_price({'status': 'filled', 'fill_price': 0.0}) is None


def test_resolve_fill_price_filled_zero_warns():
    """The warning path is reachable: status='filled' + zero price triggers the
    logger.warning in _resolve_fill_price so operators notice the discrepancy."""
    from execution.open_reconcile import _resolve_fill_price

    with mock.patch('execution.open_reconcile.logger') as mock_logger:
        result = _resolve_fill_price({'status': 'filled', 'entry': 0.0})

    assert result is None                          # safe: no ledger write
    mock_logger.warning.assert_called_once()       # loud: warning was emitted
    # The warning message must reference the filled-without-price situation.
    warn_msg = mock_logger.warning.call_args[0][0]
    assert 'filled' in warn_msg.lower()


def test_resolve_fill_price_filled_positive_no_warning():
    """A genuine confirmed fill (positive price) returns the price without warning."""
    from execution.open_reconcile import _resolve_fill_price

    with mock.patch('execution.open_reconcile.logger') as mock_logger:
        result = _resolve_fill_price({'status': 'filled', 'entry': 101.50})

    assert result == 101.50
    mock_logger.warning.assert_not_called()        # no spurious warnings on a good fill


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-v']))
