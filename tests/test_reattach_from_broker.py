"""W2: read the most-recent terminal bracket's real leg prices from Alpaca
order history, preferred over the DB submission row."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from execution import stop_reattach as sr


# A nested order-list payload like `alpaca order list --status all --nested`.
_WDC_ORDERS = [
    {'symbol': 'WDC', 'side': 'buy', 'qty': '46', 'order_class': 'bracket',
     'submitted_at': '2026-06-15T14:02:52Z', 'status': 'filled', 'type': 'market',
     'legs': [
         {'symbol': 'WDC', 'side': 'sell', 'type': 'limit', 'limit_price': '717.03',
          'stop_price': None, 'status': 'expired'},
         {'symbol': 'WDC', 'side': 'sell', 'type': 'stop', 'limit_price': None,
          'stop_price': '611.89', 'status': 'canceled'},
     ]},
    {'symbol': 'WDC', 'side': 'buy', 'qty': '28', 'order_class': 'bracket',
     'submitted_at': '2026-06-12T14:02:25Z', 'status': 'filled', 'type': 'market',
     'legs': [
         {'symbol': 'WDC', 'side': 'sell', 'type': 'limit', 'limit_price': '716.52',
          'stop_price': None, 'status': 'canceled'},
         {'symbol': 'WDC', 'side': 'sell', 'type': 'stop', 'limit_price': None,
          'stop_price': '563.18', 'status': 'filled'},
     ]},
]


def test_latest_broker_bracket_picks_most_recent_long(monkeypatch):
    monkeypatch.setattr(sr, '_run_cli', lambda *a, **k: (True, _WDC_ORDERS, None))
    b = sr.latest_broker_bracket('WDC', 'long')
    assert b is not None
    assert abs(b['target'] - 717.03) < 1e-6   # TP leg of the 06-15 bracket
    assert abs(b['stop'] - 611.89) < 1e-6      # stop leg of the 06-15 bracket


def test_latest_broker_bracket_none_when_no_bracket(monkeypatch):
    monkeypatch.setattr(sr, '_run_cli', lambda *a, **k: (True, [], None))
    assert sr.latest_broker_bracket('WDC', 'long') is None
