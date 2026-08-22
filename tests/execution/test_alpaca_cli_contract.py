"""Alpaca CLI contract regressions (2026-08-22 audit of github.com/alpacahq/cli).

Pins the fixes from the CLI-usage audit:

1. `alpaca order get` takes `--order-id`; the positional form returns
   rc=1 {"error":"--order-id required"} — so `_poll_crypto_fill` (crypto
   protective stop sizing) and `_wait_for_fill` (direction-flip close→open
   gate) had been failing on EVERY call since 2026-05-26 (c03291a): every
   flip's matched open was skipped and no crypto stop was ever attached.
2. Every shared wrapper appends `--quiet` (stderr stays a pure JSON error
   document) and `--timeout` (CLI HTTP timeout just under the subprocess
   timeout), and surfaces `auth_error` for exit code 2.
3. `regime_liquidator._submit_extended_hours_close` passes a
   `--client-order-id` (the only unattended submit without one).
4. `afterhours_tp.reconcile_afterhours` keyset-paginates `order list`
   instead of taking the silent 50-row default.
"""
from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ae  # noqa: E402
from execution import regime_liquidator as rl  # noqa: E402
from execution import alpaca_replace_stop as ars  # noqa: E402
from execution import stop_reattach as sr  # noqa: E402
from execution import afterhours_tp as ah  # noqa: E402


def _proc(rc=0, stdout='', stderr=''):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


# ── 1. order get --order-id ─────────────────────────────────────────────────

def test_poll_crypto_fill_uses_order_id_flag(monkeypatch):
    seen = []
    def fake_cli(args, timeout=30):
        seen.append(list(args))
        return True, {'status': 'filled', 'filled_qty': '0.0002'}, None
    monkeypatch.setattr(ae, '_run_alpaca_cli', fake_cli)
    assert ae._poll_crypto_fill('oid-1', timeout=2.0, poll_interval=0.01) == 0.0002
    assert seen and seen[0][:2] == ['order', 'get']
    assert seen[0][seen[0].index('--order-id') + 1] == 'oid-1'
    assert 'oid-1' != seen[0][2], 'positional order id is rejected by the CLI'


def test_wait_for_fill_uses_order_id_flag_and_returns_true_on_fill(monkeypatch):
    seen = []
    def fake_run(argv, capture_output=True, text=True, timeout=5, check=False):
        seen.append(list(argv))
        return _proc(0, json.dumps({'status': 'filled', 'filled_qty': '10'}))
    monkeypatch.setattr(ae.subprocess, 'run', fake_run)
    assert ae._wait_for_fill('oid-9', timeout=2.0, poll_interval=0.01) is True
    argv = seen[0]
    assert argv[1:3] == ['order', 'get']
    assert argv[argv.index('--order-id') + 1] == 'oid-9'


def test_wait_for_fill_false_when_cli_rejects_positional_form(monkeypatch):
    """What production did before the fix: rc=1 '--order-id required' forever."""
    monkeypatch.setattr(ae.subprocess, 'run', lambda *a, **k: _proc(
        1, '', json.dumps({'code': 0, 'error': '--order-id required', 'status': 0})))
    assert ae._wait_for_fill('oid-9', timeout=0.05, poll_interval=0.01) is False


# ── 2. --quiet / --timeout / auth_error in every wrapper ────────────────────

@pytest.mark.parametrize('mod, fn', [
    (ae, '_run_alpaca_cli'), (rl, '_run_cli'), (ars, '_run_cli'), (sr, '_run_cli'),
])
def test_wrappers_append_quiet_and_timeout(monkeypatch, mod, fn):
    seen = {}
    def fake_run(argv, capture_output=True, text=True, timeout=None, check=False):
        seen['argv'] = list(argv); seen['timeout'] = timeout
        return _proc(0, '{"ok": true}')
    monkeypatch.setattr(mod.subprocess, 'run', fake_run)
    monkeypatch.setattr(mod, 'ALPACA_CLI', '/bin/alpaca-fake')
    ok, payload, err = getattr(mod, fn)(['clock'], timeout=10)
    assert ok and payload == {'ok': True} and err is None
    argv = seen['argv']
    assert argv[0] == '/bin/alpaca-fake' and argv[1] == 'clock'
    assert '--quiet' in argv
    assert argv[argv.index('--timeout') + 1] == '9', 'CLI --timeout = subprocess timeout - 1'
    assert seen['timeout'] == 10


@pytest.mark.parametrize('mod, fn', [
    (ae, '_run_alpaca_cli'), (rl, '_run_cli'), (ars, '_run_cli'), (sr, '_run_cli'),
])
def test_wrappers_do_not_duplicate_caller_flags(monkeypatch, mod, fn):
    seen = {}
    monkeypatch.setattr(mod.subprocess, 'run',
                        lambda argv, **k: seen.setdefault('argv', list(argv)) and _proc(0, '[]'))
    getattr(mod, fn)(['order', 'list', '-q', '--timeout', '3'], timeout=10)
    argv = seen['argv']
    assert argv.count('--quiet') == 0 and argv.count('-q') == 1
    assert argv.count('--timeout') == 1 and argv[argv.index('--timeout') + 1] == '3'


@pytest.mark.parametrize('mod, fn', [
    (ae, '_run_alpaca_cli'), (rl, '_run_cli'), (ars, '_run_cli'), (sr, '_run_cli'),
])
def test_wrappers_flag_auth_error_and_parse_multiline_stderr(monkeypatch, mod, fn):
    # v0.0.10+ pretty-prints the stderr document across lines
    body = json.dumps({'error': 'unauthorized', 'code': 40110000, 'status': 401, 'hint': ''}, indent=2)
    monkeypatch.setattr(mod.subprocess, 'run', lambda argv, **k: _proc(2, '', body))
    if mod is sr:
        monkeypatch.setattr(sr, '_RATE_LIMIT_BACKOFF', ())
    ok, payload, err = getattr(mod, fn)(['account', 'get'], timeout=5)
    assert ok is False and payload is None
    assert err['exit_code'] == 2 and err['auth_error'] is True
    assert err['status'] == 401 and err['error'] == 'unauthorized'


def test_wrapper_api_error_is_not_auth_error(monkeypatch):
    body = json.dumps({'error': 'order not found', 'code': 40410000, 'status': 404})
    monkeypatch.setattr(ae.subprocess, 'run', lambda argv, **k: _proc(1, '', body))
    ok, _, err = ae._run_alpaca_cli(['order', 'get', '--order-id', 'x'], timeout=5)
    assert ok is False and err['auth_error'] is False and err['status'] == 404


# ── 3. liquidator ext-hours close carries a client-order-id ─────────────────

def test_ext_hours_close_passes_client_order_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(rl, '_run_cli', lambda args, timeout=30: (
        seen.setdefault('args', list(args)) and (True, {'id': 'o1', 'status': 'accepted'}, None)))
    monkeypatch.setattr('src.execution.alpaca_executor._pick_limit_price', lambda s, side: 101.25)
    res = rl._submit_extended_hours_close({'symbol': 'AAPL', 'side': 'sell', 'qty': 10})
    args = seen['args']
    assert args[:2] == ['order', 'submit'] and '--extended-hours' in args
    coid = args[args.index('--client-order-id') + 1]
    assert coid.startswith('rl_ext_AAPL_sell_') and res['client_order_id'] == coid
    assert res['order_id'] == 'o1'


def test_ext_hours_close_honours_supplied_client_order_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(rl, '_run_cli', lambda args, timeout=30: (
        seen.setdefault('args', list(args)) and (True, {'id': 'o1'}, None)))
    monkeypatch.setattr('src.execution.alpaca_executor._pick_limit_price', lambda s, side: 5.0)
    rl._submit_extended_hours_close({'symbol': 'X', 'side': 'buy', 'qty': 1, 'client_order_id': 'fixed-1'})
    assert seen['args'][seen['args'].index('--client-order-id') + 1] == 'fixed-1'


# ── 4. afterhours reconcile paginates order list ────────────────────────────

def test_afterhours_reconcile_pages_past_500_rows(monkeypatch):
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_TP', '1')
    monkeypatch.setattr(sr, 'fetch_positions', lambda: [{'symbol': 'AAPL', 'qty': '10'}])
    calls, cancels = [], []
    page1 = [{'id': f'o{i}', 'symbol': 'AAPL', 'type': 'limit', 'client_order_id': 'x'} for i in range(500)]
    page2 = [{'id': 'o500', 'symbol': 'AAPL', 'type': 'limit', 'client_order_id': 'ahtp_AAPL_1'}]
    def fake_cli(args, timeout=15):
        calls.append(list(args))
        if args[:2] == ['order', 'list']:
            return True, (page2 if '--after-order-id' in args else page1), None
        if args[:2] == ['order', 'cancel']:
            cancels.append(args[args.index('--order-id') + 1]); return True, {}, None
        return True, {}, None
    monkeypatch.setattr(ah, '_cli', fake_cli)
    stats = ah.reconcile_afterhours(dry_run=False)
    lists = [c for c in calls if c[:2] == ['order', 'list']]
    assert len(lists) == 2
    assert lists[0][lists[0].index('--limit') + 1] == '500'
    assert lists[0][lists[0].index('--direction') + 1] == 'asc'
    assert lists[1][lists[1].index('--after-order-id') + 1] == 'o499'
    assert cancels == ['o500'], 'the ahtp_ TP on page 2 must be found and cancelled'
    assert stats['tp_canceled'] == 1
