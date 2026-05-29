"""Tests for the protective-OCO reattach (take-profit + stop, REST-submitted).

The Alpaca CLI cannot emit order_class=oco with a stop leg (it coerces to
bracket), so OCO exits go via REST. These tests pin (a) the per-position
target computation and (b) the REST OCO body shape — the exact structure the
CLI could not produce — without placing any live order.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from execution.stop_reattach import _compute_new_target, _oco_order_body


def _pos(side, avg, cur):
    return {'side': side, 'avg_entry_price': avg, 'current_price': cur}


def test_target_long_ok_above_current():
    t, st = _compute_new_target(_pos('long', 100.0, 105.0),
                                {'entry_price': 100.0, 'target_price': 110.0})
    assert st == 'ok'
    assert abs(t - 110.0) < 0.01            # target above current → valid TP


def test_target_long_reached_when_current_past_target():
    t, st = _compute_new_target(_pos('long', 100.0, 112.0),
                                {'entry_price': 100.0, 'target_price': 110.0})
    assert st == 'reached'                  # already past target → don't place TP


def test_target_short_ok_below_current():
    t, st = _compute_new_target(_pos('short', 100.0, 95.0),
                                {'entry_price': 100.0, 'target_price': 90.0})
    assert st == 'ok'
    assert abs(t - 90.0) < 0.01


def test_target_missing_is_degenerate():
    _, st = _compute_new_target(_pos('long', 100.0, 105.0),
                                {'entry_price': 100.0, 'target_price': 0})
    assert st == 'degenerate'


def test_oco_body_long_sell_matches_alpaca_oco_format():
    b = _oco_order_body('AAPL', 'sell', 13, 326.45, 304.80, 'coid1')
    assert b['order_class'] == 'oco'        # the structure the CLI forced to 'bracket'
    assert b['type'] == 'limit'
    assert b['time_in_force'] == 'gtc'
    assert b['side'] == 'sell'
    assert b['qty'] == '13'
    assert b['symbol'] == 'AAPL'
    assert b['take_profit'] == {'limit_price': '326.45'}
    assert b['stop_loss'] == {'stop_price': '304.80'}
    # No conflicting top-level limit_price (that triggered the bracket coercion).
    assert 'limit_price' not in b


def test_oco_body_short_buy_exit():
    b = _oco_order_body('MU', 'buy', 12, 880.00, 960.00, 'coid2')
    assert b['side'] == 'buy'               # exit side for a short
    assert b['take_profit'] == {'limit_price': '880.00'}
    assert b['stop_loss'] == {'stop_price': '960.00'}
