"""W3: extended-hours take-profit placement (limit/day/extended_hours)."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from execution import afterhours_tp as ah


def test_desired_tps_long_and_short():
    positions = [
        {'symbol': 'WDC', 'side': 'long', 'qty': '46'},
        {'symbol': 'MU', 'side': 'short', 'qty': '12'},
        {'symbol': 'NOTP', 'side': 'long', 'qty': '5'},
    ]
    def lookup(sym, side):
        return {'WDC': {'target': 717.03}, 'MU': {'target': 880.0}}.get(sym)
    out = {d['ticker']: d for d in ah.desired_tps(positions, lookup)}
    assert out['WDC']['side'] == 'sell' and abs(out['WDC']['tp'] - 717.03) < 1e-6
    assert out['MU']['side'] == 'buy' and abs(out['MU']['tp'] - 880.0) < 1e-6
    assert 'NOTP' not in out            # no known TP → no order


def test_already_covered_qty_is_skipped():
    positions = [{'symbol': 'WDC', 'side': 'long', 'qty': '46'}]
    lookup = lambda s, side: {'target': 717.03}
    out = ah.desired_tps(positions, lookup, tp_covered={'WDC': 46})
    assert out == []                    # already has a resting limit for full qty


def test_place_submits_extended_hours_limit(monkeypatch):
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_TP', '1')
    calls = []
    def _fake_submit(**kw):
        calls.append(kw)
        return True, {'id': 'oid'}, None
    monkeypatch.setattr(ah, '_submit_limit', _fake_submit)
    plan = [{'ticker': 'WDC', 'side': 'sell', 'qty': 46, 'tp': 717.03}]
    n = ah._place_plan(plan, dry_run=False)
    assert n == 1
    kw = calls[0]
    assert kw['order_type'] == 'limit' and kw['extended_hours'] is True
    assert kw['tif'] == 'day' and kw['order_class'] == 'simple'
    assert abs(kw['limit_price'] - 717.03) < 1e-6


def test_gate_off_places_nothing(monkeypatch):
    monkeypatch.delenv('OPENCLAW_AFTERHOURS_TP', raising=False)
    assert ah._place_plan([{'ticker': 'WDC', 'side': 'sell', 'qty': 46,
                            'tp': 717.03}], dry_run=False) == 0


# ---------------------------------------------------------------------------
# reconcile_afterhours tests
# ---------------------------------------------------------------------------

def _setup_reconcile(monkeypatch, orders, positions, cancels):
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_TP', '1')
    import execution.stop_reattach as sr
    monkeypatch.setattr(sr, 'fetch_positions', lambda: positions)
    def _fake_cli(args, timeout=15):
        if args[:2] == ['order', 'list']:
            return True, orders, None
        if args[:2] == ['order', 'cancel']:
            cancels.append(args[-1])           # the order id
            return True, {}, None
        return True, [], None
    monkeypatch.setattr(ah, '_cli', _fake_cli)


def test_reconcile_cancels_ahtp_tp_and_oversized_stop_only(monkeypatch):
    cancels = []
    orders = [
        {'id': 'tp1', 'symbol': 'WDC', 'client_order_id': 'ahtp_WDC_1', 'type': 'limit'},
        {'id': 'st1', 'symbol': 'WDC', 'type': 'stop', 'qty': '46'},   # held 20 → oversized
        {'id': 'st2', 'symbol': 'MU',  'type': 'stop', 'qty': '12'},   # held 12 → correctly sized
    ]
    positions = [{'symbol': 'WDC', 'qty': '20'}, {'symbol': 'MU', 'qty': '12'}]
    _setup_reconcile(monkeypatch, orders, positions, cancels)
    stats = ah.reconcile_afterhours(dry_run=False)
    assert stats['tp_canceled'] == 1
    assert stats['stops_resized'] == 1
    assert 'tp1' in cancels and 'st1' in cancels
    assert 'st2' not in cancels                 # correctly-sized stop NOT canceled


def test_reconcile_skips_stop_resize_when_positions_unavailable(monkeypatch):
    """CRITICAL guard: a failed position fetch ([]) must NOT cause stops to be
    canceled."""
    cancels = []
    orders = [
        {'id': 'st1', 'symbol': 'WDC', 'type': 'stop', 'qty': '46'},
        {'id': 'st2', 'symbol': 'MU',  'type': 'stop', 'qty': '12'},
    ]
    _setup_reconcile(monkeypatch, orders, positions=[], cancels=cancels)
    stats = ah.reconcile_afterhours(dry_run=False)
    assert stats['stops_resized'] == 0
    assert cancels == []                        # NO stop canceled on empty positions


def test_reconcile_dry_run_makes_no_cancel_calls(monkeypatch):
    cancels = []
    orders = [
        {'id': 'tp1', 'symbol': 'WDC', 'client_order_id': 'ahtp_WDC_1', 'type': 'limit'},
        {'id': 'st1', 'symbol': 'WDC', 'type': 'stop', 'qty': '46'},
    ]
    positions = [{'symbol': 'WDC', 'qty': '20'}]
    _setup_reconcile(monkeypatch, orders, positions, cancels)
    stats = ah.reconcile_afterhours(dry_run=True)
    assert cancels == []                        # dry-run cancels nothing
    assert stats['tp_canceled'] == 1 and stats['stops_resized'] == 1


def test_reconcile_gate_off_returns_zero(monkeypatch):
    monkeypatch.delenv('OPENCLAW_AFTERHOURS_TP', raising=False)
    stats = ah.reconcile_afterhours(dry_run=False)
    assert stats == {'tp_canceled': 0, 'stops_resized': 0}


# ---------------------------------------------------------------------------
# main() path tests
# ---------------------------------------------------------------------------

def test_main_placement_path_reconciles_before_placing(monkeypatch):
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_TP', '1')
    order_of_calls = []
    monkeypatch.setattr(ah, 'reconcile_afterhours',
                        lambda dry_run: order_of_calls.append('reconcile') or {'tp_canceled': 0, 'stops_resized': 0})
    monkeypatch.setattr(ah, '_place_plan',
                        lambda plan, dry_run: order_of_calls.append('place') or 0)
    import execution.stop_reattach as sr
    monkeypatch.setattr(sr, 'fetch_positions', lambda: [])
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda: {})
    monkeypatch.setattr(sr, 'latest_broker_bracket', lambda s, side: None)
    rc = ah.main(['--dry-run'])
    assert rc == 0
    assert order_of_calls == ['reconcile', 'place']   # reconcile must precede placement


def test_main_reconcile_only_path_does_not_place(monkeypatch):
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_TP', '1')
    calls = []
    monkeypatch.setattr(ah, 'reconcile_afterhours',
                        lambda dry_run: calls.append('reconcile') or {'tp_canceled': 0, 'stops_resized': 0})
    monkeypatch.setattr(ah, '_place_plan', lambda plan, dry_run: calls.append('place') or 0)
    rc = ah.main(['--reconcile', '--dry-run'])
    assert rc == 0
    assert calls == ['reconcile']                      # reconcile-only: no placement


def test_module_runs_as_standalone_script():
    """systemd runs `python3 .../afterhours_tp.py` directly — the lazy
    `from execution.*` imports must resolve without a caller-provided sys.path
    (this reproduces the ExecStart invocation that crashed in the 06-16 dry-run)."""
    import subprocess
    import sys as _sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[2] / 'src' / 'execution' / 'afterhours_tp.py'
    r = subprocess.run([_sys.executable, str(script), '--help'],
                       capture_output=True, text=True)
    assert 'ModuleNotFoundError' not in r.stderr, r.stderr
    assert r.returncode == 0, (r.returncode, r.stderr)


# ── ext-hours stop/TP monitor (2026-07-20) ──────────────────────────────────

_NESTED_ORDERS = [
    # OCO parent (TP limit) with held stop leg — flat listings hide the leg.
    {'symbol': 'AKTS', 'type': 'limit', 'order_class': 'oco', 'limit_price': '9.40',
     'legs': [{'symbol': 'AKTS', 'type': 'stop', 'stop_price': '7.10'}]},
    # Bare GTC stop, no TP.
    {'symbol': 'BW', 'type': 'stop', 'order_class': 'simple', 'stop_price': '2.50'},
]


def test_protection_map_reads_oco_stop_leg_and_bare_stop():
    m = ah.protection_map(_NESTED_ORDERS)
    assert m['AKTS'] == {'stop': 7.10, 'tp': 9.40}
    assert m['BW']['stop'] == 2.50 and m['BW']['tp'] is None


def test_monitor_plan_long_stop_breach_sells_marketable():
    plan = ah.monitor_plan(
        [{'symbol': 'AKTS', 'side': 'long', 'qty': 142, 'current_price': 7.00}],
        ah.protection_map(_NESTED_ORDERS), slip_pct=0.005)
    assert len(plan) == 1
    a = plan[0]
    assert a['side'] == 'sell' and a['reason'] == 'stop_breach' and a['qty'] == 142
    assert a['limit'] == round(7.00 * 0.995, 2)        # marketable, below last
    assert a['stop'] == 7.10                            # restore level carried


def test_monitor_plan_short_stop_breach_buys_above():
    prot = {'ODD': {'stop': 40.0, 'tp': 30.0}}
    plan = ah.monitor_plan(
        [{'symbol': 'ODD', 'side': 'short', 'qty': 228, 'current_price': 41.0}],
        prot, slip_pct=0.005)
    assert len(plan) == 1 and plan[0]['side'] == 'buy'
    assert plan[0]['limit'] == round(41.0 * 1.005, 2)   # marketable, above last


def test_monitor_plan_tp_reach_and_no_breach_and_idempotency():
    prot = {'AKTS': {'stop': 7.10, 'tp': 9.40}, 'BW': {'stop': 2.50, 'tp': None}}
    pos = [
        {'symbol': 'AKTS', 'side': 'long', 'qty': 142, 'current_price': 9.55},  # TP hit
        {'symbol': 'BW',   'side': 'long', 'qty': 364, 'current_price': 3.00},  # inside band
    ]
    plan = ah.monitor_plan(pos, prot, slip_pct=0.005)
    assert [a['ticker'] for a in plan] == ['AKTS']
    assert plan[0]['reason'] == 'tp_reach'
    # Resting ahsx_ exit → skipped (no double-fire on the next tick).
    assert ah.monitor_plan(pos, prot, 0.005, resting_exit_syms={'AKTS'}) == []


def test_monitor_plan_no_protection_levels_is_not_actioned():
    # Naked positions are stop_reattach's audit problem, not the monitor's.
    plan = ah.monitor_plan(
        [{'symbol': 'XENE', 'side': 'short', 'qty': 18, 'current_price': 68.0}],
        {}, slip_pct=0.005)
    assert plan == []


def test_run_stop_monitor_skips_during_rth(monkeypatch):
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_STOP_MONITOR', '1')
    monkeypatch.setattr(ah, '_cli', lambda *a, **k: (True, {'is_open': True}, None))
    stats = ah.run_stop_monitor(dry_run=True)
    assert stats == {'checked': 0, 'exits': 0, 'restored': 0, 'failed': 0,
                     'deferred': 0}


def test_run_stop_monitor_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_AFTERHOURS_STOP_MONITOR', raising=False)
    stats = ah.run_stop_monitor(dry_run=True)
    assert stats['exits'] == 0


# ── pending-exit journal (2026-07-21) ───────────────────────────────────────
# 8 TP-passed positions were orphaned when Alpaca's OCO cancels outlived
# _wait_qty_freed AND the bare-stop restore hit insufficient-qty on the
# still-reserved shares: next tick the orders were gone, protection_map had
# no levels, and the monitor went blind. The journal makes the debt durable.

import json as _json
import time as _time


def _monitor_env(monkeypatch, tmp_path, *, orders, positions, freed,
                 submit_ok=True):
    import execution.stop_reattach as sr
    monkeypatch.setenv('OPENCLAW_AFTERHOURS_STOP_MONITOR', '1')
    monkeypatch.setenv('OPENCLAW_AH_INTENTS_PATH', str(tmp_path / 'pending.json'))
    calls = {'cancels': [], 'submits': [], 'restores': []}

    def fake_cli(args, timeout=15):
        if args == ['clock']:
            return True, {'is_open': False}, None
        return True, orders, None

    monkeypatch.setattr(ah, '_cli', fake_cli)
    monkeypatch.setattr(sr, 'fetch_positions', lambda: positions)
    monkeypatch.setattr(sr, 'cancel_stops_for',
                        lambda s, d, **k: calls['cancels'].append(s))
    monkeypatch.setattr(sr, '_wait_qty_freed', lambda s, q: freed)
    monkeypatch.setattr(sr, 'submit_protective_stop',
                        lambda **k: (calls['restores'].append(k),
                                     {'status': 'submitted'})[1])
    monkeypatch.setattr(sr, '_post_alert', lambda *a, **k: None)
    monkeypatch.setattr(
        ah, '_submit_limit',
        lambda **k: (calls['submits'].append(k),
                     (True, {}, None) if submit_ok
                     else (False, None, {'error': 'boom'}))[1])
    return calls


_AXTI_OCO = [{'symbol': 'AXTI', 'type': 'limit', 'order_class': 'oco',
              'limit_price': '50.16',
              'legs': [{'symbol': 'AXTI', 'type': 'stop',
                        'stop_price': '43.84'}]}]
_AXTI_POS = [{'symbol': 'AXTI', 'side': 'long', 'qty': 59,
              'current_price': 51.5}]


def test_monitor_journals_intent_when_cancel_outlives_wait(monkeypatch, tmp_path):
    calls = _monitor_env(monkeypatch, tmp_path, orders=_AXTI_OCO,
                         positions=_AXTI_POS, freed=False)
    stats = ah.run_stop_monitor(dry_run=False)
    assert stats['failed'] == 1 and calls['cancels'] == ['AXTI']
    assert calls['submits'] == []                      # shares never freed
    j = _json.loads((tmp_path / 'pending.json').read_text())
    assert j['AXTI']['tp'] == 50.16 and j['AXTI']['stop'] == 43.84


def test_monitor_retries_from_journal_when_orders_gone(monkeypatch, tmp_path):
    # The cancel completed AFTER the last tick: zero open orders remain, but
    # the journal remembers the levels — the exit must still happen.
    (tmp_path / 'pending.json').write_text(_json.dumps(
        {'AXTI': {'stop': 43.84, 'tp': 50.16, 'ts': _time.time()}}))
    calls = _monitor_env(monkeypatch, tmp_path, orders=[],
                         positions=_AXTI_POS, freed=True)
    stats = ah.run_stop_monitor(dry_run=False)
    assert stats['exits'] == 1
    assert calls['submits'] and calls['submits'][0]['ticker'] == 'AXTI'
    assert _json.loads((tmp_path / 'pending.json').read_text()) == {}


def test_monitor_journal_cleared_when_position_closed(monkeypatch, tmp_path):
    (tmp_path / 'pending.json').write_text(_json.dumps(
        {'GONE': {'stop': 1.0, 'tp': 2.0, 'ts': _time.time()}}))
    calls = _monitor_env(monkeypatch, tmp_path, orders=[], positions=[],
                         freed=True)
    ah.run_stop_monitor(dry_run=False)
    assert calls['submits'] == []
    assert _json.loads((tmp_path / 'pending.json').read_text()) == {}


def test_monitor_journal_cleared_when_owed_tp_rests_again(monkeypatch, tmp_path):
    # A reattach pass re-covered the symbol with a full OCO (TP resting): the
    # owed leg is back, the journal entry is settled, and an inside-band price
    # does nothing. A bare stop alone must NOT settle a TP-owing intent.
    (tmp_path / 'pending.json').write_text(_json.dumps(
        {'AXTI': {'stop': 43.84, 'tp': 50.16, 'ts': _time.time()}}))
    orders = [{'symbol': 'AXTI', 'type': 'limit', 'order_class': 'oco',
               'limit_price': '50.16',
               'legs': [{'symbol': 'AXTI', 'type': 'stop',
                         'stop_price': '43.84'}]}]
    positions = [{'symbol': 'AXTI', 'side': 'long', 'qty': 59,
                  'current_price': 47.0}]
    calls = _monitor_env(monkeypatch, tmp_path, orders=orders,
                         positions=positions, freed=True)
    ah.run_stop_monitor(dry_run=False)
    assert calls['cancels'] == [] and calls['submits'] == []
    assert _json.loads((tmp_path / 'pending.json').read_text()) == {}


def test_monitor_borrows_owed_tp_over_stop_only_protection(monkeypatch, tmp_path):
    # CLYM 2026-07-21: reached-target position carries only a bare stop, so
    # protection_map alone can never decide tp_reach. The journaled TP must be
    # borrowed onto the live protection and the capture executed.
    (tmp_path / 'pending.json').write_text(_json.dumps(
        {'CLYM': {'stop': 11.81, 'tp': 12.60, 'ts': _time.time()}}))
    orders = [{'symbol': 'CLYM', 'type': 'stop', 'order_class': 'simple',
               'stop_price': '11.81'}]
    positions = [{'symbol': 'CLYM', 'side': 'long', 'qty': 214,
                  'current_price': 12.90}]
    calls = _monitor_env(monkeypatch, tmp_path, orders=orders,
                         positions=positions, freed=True)
    stats = ah.run_stop_monitor(dry_run=False)
    assert stats['exits'] == 1
    assert calls['cancels'] == ['CLYM']
    assert calls['submits'] and calls['submits'][0]['ticker'] == 'CLYM'
    assert _json.loads((tmp_path / 'pending.json').read_text()) == {}


def test_monitor_journal_entry_expires_after_ttl(monkeypatch, tmp_path):
    (tmp_path / 'pending.json').write_text(_json.dumps(
        {'AXTI': {'stop': 43.84, 'tp': 50.16,
                  'ts': _time.time() - ah._INTENTS_TTL_S - 60}}))
    calls = _monitor_env(monkeypatch, tmp_path, orders=[],
                         positions=_AXTI_POS, freed=True)
    ah.run_stop_monitor(dry_run=False)
    assert calls['submits'] == []          # stale intent must not fire exits


# ── exit-fill reporter (2026-07-21) ─────────────────────────────────────────

_CLOSED_ORDERS = [
    # Entry bracket parent (filled) with a filled TP leg + canceled stop leg:
    # only the TP LEG is an exit.
    {'id': 'e1', 'symbol': 'MU', 'type': 'limit', 'order_class': 'bracket',
     'status': 'filled', 'side': 'buy', 'filled_qty': '3',
     'filled_avg_price': '850.0', 'limit_price': '850.0',
     'client_order_id': 'entry_MU',
     'legs': [
         {'id': 'tp1', 'symbol': 'MU', 'type': 'limit', 'status': 'filled',
          'side': 'sell', 'filled_qty': '3', 'filled_avg_price': '918.54',
          'limit_price': '886.23'},
         {'id': 'st1', 'symbol': 'MU', 'type': 'stop', 'status': 'canceled',
          'side': 'sell', 'filled_qty': '0', 'stop_price': '813.05'},
     ]},
    # Reattach OCO: parent TP canceled, stop LEG filled → stop exit.
    {'id': 'p2', 'symbol': 'BW', 'type': 'limit', 'order_class': 'oco',
     'status': 'canceled', 'side': 'sell', 'filled_qty': '0',
     'limit_price': '10.69', 'client_order_id': 'oco_BW_1',
     'legs': [{'id': 'st2', 'symbol': 'BW', 'type': 'stop',
               'status': 'filled', 'side': 'sell', 'filled_qty': '275',
               'filled_avg_price': '9.70', 'stop_price': '9.75'}]},
    # Ext-hours emulated exit (ahsx_) filled.
    {'id': 'x3', 'symbol': 'AXTI', 'type': 'limit', 'order_class': 'simple',
     'status': 'filled', 'side': 'sell', 'filled_qty': '59',
     'filled_avg_price': '51.74', 'limit_price': '51.74',
     'client_order_id': 'ahsx_AXTI_1'},
    # Plain filled entry (simple market) — must NOT be reported.
    {'id': 'e4', 'symbol': 'HPE', 'type': 'market', 'order_class': 'simple',
     'status': 'filled', 'side': 'buy', 'filled_qty': '50',
     'filled_avg_price': '45.0', 'client_order_id': 'exec_HPE'},
]


def test_classify_exit_fills_kinds_and_entry_exclusion():
    fills = {f['id']: f for f in ah.classify_exit_fills(_CLOSED_ORDERS)}
    assert set(fills) == {'tp1', 'st2', 'x3'}
    assert fills['tp1']['kind'] == 'take_profit' and fills['tp1']['level'] == 886.23
    assert fills['st2']['kind'] == 'stop' and fills['st2']['price'] == 9.70
    assert fills['x3']['kind'] == 'ah_exit'


def _reporter_env(monkeypatch, tmp_path, orders):
    import execution.stop_reattach as sr
    monkeypatch.setenv('OPENCLAW_EXIT_FILLS_STATE', str(tmp_path / 'fills.json'))
    posts = []
    monkeypatch.setattr(ah, '_cli', lambda a, timeout=15: (True, orders, None))
    monkeypatch.setattr(sr, '_post_alert',
                        lambda msg, channel='data-alerts':
                        posts.append((channel, msg)))
    return posts


def test_fill_reporter_first_run_seeds_silently(monkeypatch, tmp_path):
    posts = _reporter_env(monkeypatch, tmp_path, _CLOSED_ORDERS)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats == {'fills_seen': 3, 'reported': 0}
    assert posts == []
    st = _json.loads((tmp_path / 'fills.json').read_text())
    assert set(st['seen']) == {'tp1', 'st2', 'x3'}


def test_fill_reporter_posts_new_fills_to_trade_reports_once(monkeypatch, tmp_path):
    (tmp_path / 'fills.json').write_text(_json.dumps({'seen': ['tp1', 'st2']}))
    posts = _reporter_env(monkeypatch, tmp_path, _CLOSED_ORDERS)
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['reported'] == 1
    assert len(posts) == 1
    channel, msg = posts[0]
    assert channel == 'trade-reports'
    assert 'AXTI' in msg and '51.74' in msg
    # Second pass: nothing new.
    posts.clear()
    stats = ah.run_exit_fill_reporter(dry_run=False)
    assert stats['reported'] == 0 and posts == []


# ── quote-based exit pricing (2026-07-21) ───────────────────────────────────

def _act(side, reason, level, current, limit=None):
    return {'ticker': 'X', 'side': side, 'qty': 1, 'reason': reason,
            'level': level, 'current': current,
            'limit': limit if limit is not None else current}


def test_reprice_tp_reach_floors_at_level_when_book_is_below():
    # CLYM: last 12.90 was Monday's close; live book 12.25/12.80. The limit
    # must rest AT the target, not above the entire book.
    assert ah.reprice_exit(_act('sell', 'tp_reach', 12.60, 12.90),
                           bid=12.25, ask=12.80, slip_pct=0.005) == 12.60


def test_reprice_tp_reach_prices_off_live_bid_when_marketable():
    # Liquid gap-up: bid 918 vs level 886 → slip off the BID captures the
    # above-target value (fills at bid or better).
    got = ah.reprice_exit(_act('sell', 'tp_reach', 886.23, 911.03),
                          bid=918.0, ask=918.5, slip_pct=0.005)
    assert got == round(918.0 * 0.995, 2) and got > 886.23


def test_reprice_short_tp_reach_caps_at_level():
    # Short cover: never pay MORE than target via the tp_reach path.
    assert ah.reprice_exit(_act('buy', 'tp_reach', 52.68, 53.5),
                           bid=52.9, ask=53.0, slip_pct=0.005) == 52.68
    # Ask below level → pay the (better) ask-based price.
    got = ah.reprice_exit(_act('buy', 'tp_reach', 52.68, 50.6),
                          bid=47.6, ask=47.8, slip_pct=0.005)
    assert got == round(47.8 * 1.005, 2)


def test_reprice_stop_breach_is_unfloored():
    # Breach = get out; the limit follows the live bid down, slip-bounded.
    got = ah.reprice_exit(_act('sell', 'stop_breach', 9.75, 10.0),
                          bid=9.60, ask=9.70, slip_pct=0.005)
    assert got == round(9.60 * 0.995, 2) < 9.75


def test_reprice_falls_back_to_last_trade_without_quote():
    got = ah.reprice_exit(_act('sell', 'tp_reach', 12.60, 12.90),
                          bid=None, ask=None, slip_pct=0.005)
    assert got == round(12.90 * 0.995, 2)


def test_monitor_submits_quote_repriced_limit(monkeypatch, tmp_path):
    # Borrowed-TP CLYM scenario end-to-end: live book 12.25/12.80 → the
    # submitted ext-hours limit is the TP level, not last-trade minus slip.
    (tmp_path / 'pending.json').write_text(_json.dumps(
        {'CLYM': {'stop': 11.81, 'tp': 12.60, 'ts': _time.time()}}))
    orders = [{'symbol': 'CLYM', 'type': 'stop', 'order_class': 'simple',
               'stop_price': '11.81'}]
    positions = [{'symbol': 'CLYM', 'side': 'long', 'qty': 214,
                  'current_price': 12.90}]
    calls = _monitor_env(monkeypatch, tmp_path, orders=orders,
                         positions=positions, freed=True)
    monkeypatch.setattr(ah, '_latest_quote', lambda s: (12.25, 12.80))
    stats = ah.run_stop_monitor(dry_run=False)
    assert stats['exits'] == 1
    assert calls['submits'][0]['limit_price'] == 12.60


# ── wide-book stop-exit deferral (2026-07-24) ──────────────────────────────
# NVNO 2026-07-23 08:10Z: stop 10.61, premarket book 9.85/12.5 — the
# marketable cover priced off the ask and paid 18% over the stop. A breach
# exit that costs more than the breach must wait for a crossable book.

def test_book_too_wide_defers_only_wide_stop_breach():
    assert ah.book_too_wide('stop_breach', 9.85, 12.5)        # the NVNO book
    assert not ah.book_too_wide('stop_breach', 10.60, 10.64)  # tight book
    assert not ah.book_too_wide('tp_reach', 9.85, 12.5)       # floored at level
    assert not ah.book_too_wide('stop_breach', None, 12.5)    # one-sided book


def test_max_spread_env_override(monkeypatch):
    monkeypatch.setenv('OPENCLAW_AH_MAX_SPREAD_PCT', '30')
    assert not ah.book_too_wide('stop_breach', 9.85, 12.5)


def test_monitor_defers_stop_breach_on_wide_book(monkeypatch, tmp_path):
    pos = [{'symbol': 'AXTI', 'side': 'long', 'qty': 59, 'current_price': 42.0}]
    calls = _monitor_env(monkeypatch, tmp_path, orders=_AXTI_OCO,
                         positions=pos, freed=True)
    monkeypatch.setattr(ah, '_latest_quote', lambda s: (40.0, 47.0))
    stats = ah.run_stop_monitor(dry_run=False)
    assert stats['deferred'] == 1 and stats['exits'] == 0
    assert calls['cancels'] == [] and calls['submits'] == []
    # Deferral happens before any cancel — no protection touched, no debt owed.
    try:
        j = _json.loads((tmp_path / 'pending.json').read_text())
    except FileNotFoundError:
        j = {}
    assert 'AXTI' not in j


def test_monitor_crosses_tight_book_stop_breach(monkeypatch, tmp_path):
    pos = [{'symbol': 'AXTI', 'side': 'long', 'qty': 59, 'current_price': 42.0}]
    calls = _monitor_env(monkeypatch, tmp_path, orders=_AXTI_OCO,
                         positions=pos, freed=True)
    monkeypatch.setattr(ah, '_latest_quote', lambda s: (41.95, 42.05))
    stats = ah.run_stop_monitor(dry_run=False)
    assert stats['exits'] == 1 and stats['deferred'] == 0
    assert calls['submits'][0]['ticker'] == 'AXTI'


# ── position fetch failure must not settle journal debt (2026-07-24) ───────

def test_monitor_skips_and_keeps_journal_when_positions_unavailable(
        monkeypatch, tmp_path):
    (tmp_path / 'pending.json').write_text(_json.dumps(
        {'AXTI': {'stop': 43.84, 'tp': 50.16, 'ts': _time.time()}}))
    calls = _monitor_env(monkeypatch, tmp_path, orders=[], positions=None,
                         freed=True)
    stats = ah.run_stop_monitor(dry_run=False)
    assert stats == {'checked': 0, 'exits': 0, 'restored': 0, 'failed': 0,
                     'deferred': 0}
    assert calls['submits'] == [] and calls['cancels'] == []
    j = _json.loads((tmp_path / 'pending.json').read_text())
    assert 'AXTI' in j          # a CLI glitch is not "position closed"
