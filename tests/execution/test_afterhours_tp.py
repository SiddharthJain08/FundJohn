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
    assert stats == {'checked': 0, 'exits': 0, 'restored': 0, 'failed': 0}


def test_run_stop_monitor_gate_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_AFTERHOURS_STOP_MONITOR', raising=False)
    stats = ah.run_stop_monitor(dry_run=True)
    assert stats['exits'] == 0
