# tests/execution/test_stop_reattach_emergency_exit.py
"""Bounded emergency exit for past-stop positions (2026-07-23).

A position past its strategy stop used to be flag-only — naked overnight
until an operator acted (CENN/NVNO 2026-07-22). The emergency pass closes it:
RTH open -> slip-capped marketable limit now; no session -> journal into the
ext-hours monitor's pending-exit file so its next tick closes it."""
import json
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from execution import stop_reattach as sr
from execution import afterhours_tp as ahtp


def _breached_short(sym='CENN', qty=1218.0):
    return {'ticker': sym, 'side': 'short', 'current': 3.40,
            'breached_stop': 3.31, 'unrealized_pl': -231.42, 'qty': qty}


def _positions(sym='CENN', qty=-1218):
    return [{'symbol': sym, 'qty': qty, 'side': 'short',
             'current_price': 3.40, 'avg_entry_price': 3.21}]


def _wire(monkeypatch, tmp_path, *, is_open, open_orders=None,
          submit_ok=True, quote=(3.41, 3.43)):
    """Patch every effectful edge; returns the dict of captured calls."""
    calls = {'submits': [], 'cancels': [], 'alerts': []}
    journal = tmp_path / 'intents.json'
    monkeypatch.setenv('OPENCLAW_AH_INTENTS_PATH', str(journal))
    monkeypatch.delenv('OPENCLAW_PAST_STOP_EMERGENCY_EXIT', raising=False)

    def fake_cli(args, timeout=15):
        if args[0] == 'clock':
            return True, {'is_open': is_open}, None
        if args[:2] == ['order', 'list']:
            return True, (open_orders or []), None
        raise AssertionError(f'unexpected CLI call: {args}')
    monkeypatch.setattr(sr, '_run_cli', fake_cli)
    monkeypatch.setattr(sr, 'cancel_stops_for',
                        lambda sym, dry, **kw: calls['cancels'].append(sym) or 0)
    monkeypatch.setattr(sr, '_wait_qty_freed', lambda sym, need, timeout=12: True)
    monkeypatch.setattr(sr, '_post_alert',
                        lambda msg, channel='data-alerts': calls['alerts'].append(msg))
    monkeypatch.setattr(ahtp, '_latest_quote', lambda sym: quote)

    def fake_submit(**kw):
        calls['submits'].append(kw)
        return (True, {'id': 'x'}, None) if submit_ok else \
               (False, None, {'error': 'rejected'})
    monkeypatch.setattr(ahtp, '_submit_limit', fake_submit)
    return calls, journal


def _read_journal(journal):
    try:
        return json.loads(journal.read_text())
    except FileNotFoundError:
        return {}


def test_rth_open_submits_marketable_exit_and_clears_journal(monkeypatch, tmp_path):
    calls, journal = _wire(monkeypatch, tmp_path, is_open=True)
    b = _breached_short()
    sr.emergency_exit_breached([b], _positions(), dry_run=False)
    assert len(calls['submits']) == 1
    s = calls['submits'][0]
    assert s['ticker'] == 'CENN' and s['side'] == 'buy' and s['qty'] == 1218
    assert s['extended_hours'] is False and s['tif'] == 'day'
    assert s['coid'].startswith('emex_CENN_')
    # Short exit = buy priced off the ASK with slip added (stop_breach = unfloored).
    assert abs(s['limit_price'] - round(3.43 * 1.005, 2)) < 1e-9
    assert b['action'].startswith('exit_submitted')
    assert _read_journal(journal) == {}          # debt settled on submit
    assert calls['alerts']                        # trade-reports post fired


def test_closed_session_journals_for_monitor(monkeypatch, tmp_path):
    calls, journal = _wire(monkeypatch, tmp_path, is_open=False)
    b = _breached_short()
    sr.emergency_exit_breached([b], _positions(), dry_run=False)
    assert calls['submits'] == [] and calls['cancels'] == []
    assert b['action'] == 'exit_queued (monitor journal)'
    j = _read_journal(journal)
    assert j['CENN']['stop'] == 3.31 and j['CENN']['tp'] is None


def test_gate_off_restores_flag_only(monkeypatch, tmp_path):
    calls, journal = _wire(monkeypatch, tmp_path, is_open=True)
    monkeypatch.setenv('OPENCLAW_PAST_STOP_EMERGENCY_EXIT', '0')
    b = _breached_short()
    sr.emergency_exit_breached([b], _positions(), dry_run=False)
    assert calls['submits'] == [] and _read_journal(journal) == {}
    assert b['action'] == 'flag_only (gate off)'


def test_already_pending_exit_is_not_stacked(monkeypatch, tmp_path):
    resting = [{'symbol': 'CENN', 'client_order_id': 'ahsx_CENN_123',
                'status': 'new'}]
    calls, journal = _wire(monkeypatch, tmp_path, is_open=True,
                           open_orders=resting)
    b = _breached_short()
    sr.emergency_exit_breached([b], _positions(), dry_run=False)
    assert calls['submits'] == []
    assert b['action'] == 'exit_already_pending'


def test_submit_failure_keeps_journal_owed(monkeypatch, tmp_path):
    calls, journal = _wire(monkeypatch, tmp_path, is_open=True, submit_ok=False)
    b = _breached_short()
    sr.emergency_exit_breached([b], _positions(), dry_run=False)
    assert b['action'] == 'exit_failed (journaled for retry)'
    assert 'CENN' in _read_journal(journal)       # next tick still owes the exit


def test_dry_run_touches_nothing(monkeypatch, tmp_path):
    calls, journal = _wire(monkeypatch, tmp_path, is_open=True)
    b = _breached_short()
    sr.emergency_exit_breached([b], _positions(), dry_run=True)
    assert calls['submits'] == [] and calls['cancels'] == []
    assert _read_journal(journal) == {}
    assert b['action'] == 'exit_dry_run'
