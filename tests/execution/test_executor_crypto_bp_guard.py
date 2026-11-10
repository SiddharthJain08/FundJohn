"""Crypto Phase 3 Task 6: non-marginable buying power in the account state +
the executor's crypto BUY guard (operator rule: the only funding constraint
for crypto is non-marginable buying power)."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest  # noqa: E402

from execution import alpaca_executor as ae  # noqa: E402
from execution import alpaca_trader as at  # noqa: E402


# ── account parse ──────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._p


class _Sess:
    _base = 'http://fake'

    def __init__(self, payload=None, exc=None):
        self._payload, self._exc = payload, exc

    def get(self, url, timeout=10):
        if self._exc:
            raise self._exc
        return _Resp(self._payload)


def test_account_state_parses_nmbp():
    st = at._fetch_account_state(_Sess({'equity': '120000', 'cash': '5000.5',
                                        'buying_power': '240000',
                                        'non_marginable_buying_power': '4321.25'}))
    assert st['fetched'] is True
    assert st['non_marginable_buying_power'] == 4321.25
    assert st['buying_power'] == 240000.0
    assert st['cash'] == 5000.5
    assert st['equity'] == 120000.0


def test_account_state_nmbp_none_when_absent():
    st = at._fetch_account_state(_Sess({'equity': '120000'}))
    assert st['fetched'] is True
    assert st['non_marginable_buying_power'] is None


def test_account_state_nmbp_none_when_unparseable():
    st = at._fetch_account_state(_Sess({'equity': '1', 'non_marginable_buying_power': 'n/a'}))
    assert st['non_marginable_buying_power'] is None


def test_account_state_nmbp_none_on_fetch_failure():
    st = at._fetch_account_state(_Sess(exc=RuntimeError('boom')))
    assert st['fetched'] is False
    assert st['non_marginable_buying_power'] is None


# ── executor guard ─────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    monkeypatch.setenv('OPENCLAW_CRYPTO_HOLD_DISABLE', '1')
    ae._reset_crypto_account_memo()
    yield
    ae._reset_crypto_account_memo()


def _stub_account(monkeypatch, nmbp=None, exc=None):
    calls = []

    def _fetch(sess):
        calls.append(sess)
        if exc:
            raise exc
        return {'equity': 100000.0, 'fetched': True,
                'non_marginable_buying_power': nmbp}
    monkeypatch.setattr(ae, '_alpaca_session', lambda: object())
    monkeypatch.setattr(ae, '_fetch_account_state', _fetch)
    return calls


def _cli():
    seen = []

    def _call(args, timeout=30):
        joined = ' '.join(args)
        seen.append(joined)
        if 'data crypto latest-trades' in joined:
            return True, {'trades': {'BTC/USD': {'p': 50000.0}}}, None
        if 'order submit' in joined:
            return True, {'id': 'ord-1'}, None
        raise AssertionError(f'unexpected CLI call: {joined}')
    return _call, seen


BUY_5K = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.05}   # 100k * 5% = $5k


def test_guard_refuses_buy_over_nmbp(monkeypatch):
    _stub_account(monkeypatch, nmbp=1000.0)
    call, seen = _cli()
    with patch.object(ae, '_run_alpaca_cli', side_effect=call):
        res = ae._route_crypto_order(dict(BUY_5K), 100000.0, 'C1')
    assert res['status'] == 'refused'
    assert res['reason'] == 'crypto_buying_power'
    assert res['ticker'] == 'BTC/USD'
    assert res['client_order_id'] == 'C1'
    assert res['order_class'] == 'simple'
    assert not any('order submit' in s for s in seen)
    assert not seen  # refused before even the price call


def test_guard_allows_buy_under_nmbp(monkeypatch):
    _stub_account(monkeypatch, nmbp=10000.0)
    call, seen = _cli()
    with patch.object(ae, '_run_alpaca_cli', side_effect=call):
        res = ae._route_crypto_order(dict(BUY_5K), 100000.0, 'C2')
    assert res['status'] == 'submitted'


def test_guard_fetches_once_per_process(monkeypatch):
    calls = _stub_account(monkeypatch, nmbp=10000.0)
    call, _ = _cli()
    with patch.object(ae, '_run_alpaca_cli', side_effect=call):
        ae._route_crypto_order(dict(BUY_5K), 100000.0, 'C3')
        ae._route_crypto_order(dict(BUY_5K), 100000.0, 'C4')
    assert len(calls) == 1


def test_guard_exempts_closes(monkeypatch):
    calls = _stub_account(monkeypatch, nmbp=0.0)
    sentinel = {'status': 'submitted', 'ticker': 'BTC/USD', 'close': True}
    monkeypatch.setattr(ae, '_route_crypto_close', lambda *a, **k: sentinel)
    res = ae._route_crypto_order({'ticker': 'BTC-USD', 'direction': 'long', 'close_only': True,
                                  'pct_nav': 0.05}, 100000.0, 'C5')
    assert res is sentinel
    assert calls == []


def test_guard_exempts_sell_direction(monkeypatch):
    calls = _stub_account(monkeypatch, nmbp=0.0)
    call, seen = _cli()
    with patch.object(ae, '_run_alpaca_cli', side_effect=call):
        res = ae._route_crypto_order({'ticker': 'BTC-USD', 'direction': 'short', 'pct_nav': 0.05},
                                     100000.0, 'C6')
    assert res['status'] == 'submitted'
    assert calls == []


def test_guard_fails_open_on_none_field(monkeypatch):
    calls = _stub_account(monkeypatch, nmbp=None)
    call, _ = _cli()
    with patch.object(ae, '_run_alpaca_cli', side_effect=call):
        res = ae._route_crypto_order(dict(BUY_5K), 100000.0, 'C7')
        ae._route_crypto_order(dict(BUY_5K), 100000.0, 'C7b')
    assert res['status'] == 'submitted'
    assert len(calls) == 1   # unknown is memoised too — no refetch per order


def test_guard_fails_open_on_fetch_exception(monkeypatch):
    _stub_account(monkeypatch, exc=RuntimeError('network down'))
    call, _ = _cli()
    with patch.object(ae, '_run_alpaca_cli', side_effect=call):
        res = ae._route_crypto_order(dict(BUY_5K), 100000.0, 'C8')
    assert res['status'] == 'submitted'


def test_guard_fails_open_on_unfetched_state(monkeypatch):
    # _fetch_account_state swallows errors and returns fetched=False; its nmbp
    # is then None by construction — the guard must not refuse.
    monkeypatch.setattr(ae, '_alpaca_session', lambda: object())
    monkeypatch.setattr(ae, '_fetch_account_state',
                        lambda s: {'fetched': False, 'non_marginable_buying_power': 0.0})
    call, _ = _cli()
    with patch.object(ae, '_run_alpaca_cli', side_effect=call):
        res = ae._route_crypto_order(dict(BUY_5K), 100000.0, 'C9')
    assert res['status'] == 'submitted'
