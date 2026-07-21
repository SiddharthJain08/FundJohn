"""W2: read the most-recent terminal bracket's real leg prices from Alpaca
order history, preferred over the DB submission row."""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
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


def _pos(side, avg, cur, qty=46, sym='WDC'):
    return {'symbol': sym, 'side': side, 'avg_entry_price': avg,
            'current_price': cur, 'qty': qty}


def test_reattach_uses_broker_target_not_degenerate_db(monkeypatch):
    """WDC replay: DB row target is degenerate (604.79<627.51) but the broker
    bracket's real TP (717.03) is recovered → OCO is placed, not dropped."""
    monkeypatch.setenv('OPENCLAW_REATTACH_FROM_BROKER', '1')
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda linked_only=False: {})
    monkeypatch.setattr(sr, 'latest_broker_bracket',
                        lambda t, s: {'entry': None, 'stop': 611.89,
                                      'target': 717.03, 'order_id': 'oid'})
    monkeypatch.setattr(sr, 'latest_stop_submission',
                        lambda c, t, s: {'entry_price': 627.51, 'stop_price': 516.11,
                                         'target_price': 604.79})
    monkeypatch.setattr(sr, 'cancel_stops_for', lambda s, d, **kw: 0)
    placed = {}
    def _fake_oco(*, ticker, position_side, qty, stop_price, target_price, dry_run):
        placed.update(dict(target=target_price, stop=stop_price))
        return {'ticker': ticker, 'status': 'submitted'}
    monkeypatch.setattr(sr, 'submit_protective_oco', _fake_oco)
    stats = sr.run_oco_reattach(conn=None, positions=[_pos('long', 627.80, 694.0)],
                                dry_run=False)
    assert stats['oco'] == 1
    assert placed['target'] > 694.0           # valid profit-side TP, not dropped


def test_no_silent_drop_when_target_unavailable(monkeypatch):
    """No broker legs and degenerate DB → place the stop AND record tp_missing;
    NOT a silent bare-stop-only skip."""
    monkeypatch.setenv('OPENCLAW_REATTACH_FROM_BROKER', '1')
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda linked_only=False: {})
    monkeypatch.setattr(sr, 'latest_broker_bracket', lambda t, s: None)
    monkeypatch.setattr(sr, 'latest_stop_submission',
                        lambda c, t, s: {'entry_price': 627.51, 'stop_price': 516.11,
                                         'target_price': 604.79})
    stats = sr.run_oco_reattach(conn=None, positions=[_pos('long', 627.80, 694.0)],
                                dry_run=True)
    assert stats['tp_missing'] == 1           # surfaced, not silently skipped


def test_reached_target_hands_off_to_monitor_journal(monkeypatch, tmp_path):
    """Target already passed → no OCO can rest, but the TP must not vanish:
    the levels go to the ext-hours monitor's pending-exit journal so its next
    tick captures the profit (CLYM rode 2.4% past target for 20h, 2026-07-21)."""
    import json
    monkeypatch.setenv('OPENCLAW_REATTACH_FROM_BROKER', '1')
    monkeypatch.setenv('OPENCLAW_AH_INTENTS_PATH', str(tmp_path / 'pending.json'))
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda linked_only=False: {})
    monkeypatch.setattr(sr, 'latest_broker_bracket',
                        lambda t, s: {'entry': None, 'stop': 611.89,
                                      'target': 717.03, 'order_id': 'oid'})
    monkeypatch.setattr(sr, 'latest_stop_submission',
                        lambda c, t, s: {'entry_price': 627.51, 'stop_price': 516.11,
                                         'target_price': 604.79})
    stats = sr.run_oco_reattach(conn=None, positions=[_pos('long', 627.80, 720.0)],
                                dry_run=False)
    assert stats['reached'] == 1 and stats['oco'] == 0
    j = json.loads((tmp_path / 'pending.json').read_text())
    # Journaled levels are the pass's COMPUTED bracket: tp at/below current
    # (that's what 'reached' means), stop below tp.
    assert j['WDC']['tp'] is not None and j['WDC']['tp'] <= 720.0
    assert j['WDC']['stop'] is not None and j['WDC']['stop'] < j['WDC']['tp']


def _wire_valid_bracket(monkeypatch):
    monkeypatch.setenv('OPENCLAW_REATTACH_FROM_BROKER', '1')
    monkeypatch.setattr(sr, 'fetch_tp_covered', lambda linked_only=False: {})
    monkeypatch.setattr(sr, 'latest_broker_bracket',
                        lambda t, s: {'entry': None, 'stop': 611.89,
                                      'target': 717.03, 'order_id': 'oid'})
    monkeypatch.setattr(sr, 'latest_stop_submission',
                        lambda c, t, s: {'entry_price': 627.51, 'stop_price': 516.11,
                                         'target_price': 604.79})
    monkeypatch.setattr(sr, 'cancel_stops_for', lambda s, d, **kw: 0)
    monkeypatch.setattr(sr, 'submit_protective_oco',
                        lambda **kw: {'ticker': kw['ticker'], 'status': 'rejected'})


def test_oco_rejected_position_closed_no_restore(monkeypatch):
    """KLAC 2026-07-21 replay: TP filled mid-pass so the OCO submit bounced
    ("oco orders must be exit orders"); restoring the bare stop then OPENS a
    position on a flat symbol. With the position gone, restore must be
    skipped."""
    _wire_valid_bracket(monkeypatch)
    monkeypatch.setattr(sr, '_position_still_open', lambda s: False)
    restored = []
    monkeypatch.setattr(sr, 'submit_protective_stop',
                        lambda **kw: restored.append(kw) or {'status': 'submitted'})
    stats = sr.run_oco_reattach(conn=None, positions=[_pos('long', 627.80, 694.0)],
                                dry_run=False)
    assert stats['closed_mid_pass'] == 1 and stats['restored'] == 0
    assert restored == []                     # no order touched the flat symbol


def test_oco_rejected_position_open_still_restores(monkeypatch):
    """Guard the existing behavior: a rejection with the position still open
    (e.g. transient broker error) must keep restoring the bare stop."""
    _wire_valid_bracket(monkeypatch)
    monkeypatch.setattr(sr, '_position_still_open', lambda s: True)
    restored = []
    monkeypatch.setattr(sr, 'submit_protective_stop',
                        lambda **kw: restored.append(kw) or {'status': 'submitted'})
    stats = sr.run_oco_reattach(conn=None, positions=[_pos('long', 627.80, 694.0)],
                                dry_run=False)
    assert stats['restored'] == 1 and stats['closed_mid_pass'] == 0
    assert len(restored) == 1


def _sweep_cli(positions, orders, cancels):
    """_run_cli stub covering position list / order list / order cancel."""
    def _cli(args, timeout=15):
        if args[:2] == ['position', 'list']:
            return True, positions, None
        if args[:2] == ['order', 'list']:
            return True, orders, None
        if args[:2] == ['order', 'cancel']:
            cancels.append(args[args.index('--order-id') + 1])
            return True, None, None
        raise AssertionError(f'unexpected CLI call: {args}')
    return _cli


def test_sweep_cancels_orphan_exits_only(monkeypatch):
    """Flat-symbol exit orders (bare stop, OCO parent, ahtp limit) are
    cancelled; entry orders and covered symbols are untouched."""
    monkeypatch.setattr(sr, '_post_alert', lambda *a, **k: None)
    orders = [
        # KLAC replay: bare stop, no position → cancel.
        {'symbol': 'KLAC', 'type': 'stop', 'qty': '4', 'id': 'o1',
         'status': 'new', 'client_order_id': 'x1'},
        # OCO parent (pure exit pair) on a flat symbol → cancel.
        {'symbol': 'ODD', 'type': 'limit', 'order_class': 'oco', 'qty': '170',
         'id': 'o2', 'status': 'new', 'client_order_id': 'x2'},
        # Afterhours monitor exit limit on a flat symbol → cancel.
        {'symbol': 'CLYM', 'type': 'limit', 'qty': '214', 'id': 'o3',
         'status': 'new', 'client_order_id': 'ahtp_abc'},
        # Sizer ENTRY limit — legitimately positionless → keep.
        {'symbol': 'MU', 'type': 'limit', 'qty': '10', 'id': 'o4',
         'status': 'new', 'client_order_id': 'x4'},
        # Bracket parent = entry + attached exits → keep.
        {'symbol': 'HPE', 'type': 'limit', 'order_class': 'bracket', 'qty': '5',
         'id': 'o5', 'status': 'new', 'client_order_id': 'x5'},
        # Stop on a symbol that HAS a position → keep.
        {'symbol': 'SNDK', 'type': 'stop', 'qty': '1', 'id': 'o6',
         'status': 'new', 'client_order_id': 'x6'},
        # Stuck pending_cancel (paper defect) → leave to die.
        {'symbol': 'XENE', 'type': 'stop', 'qty': '2', 'id': 'o7',
         'status': 'pending_cancel', 'client_order_id': 'x7'},
    ]
    cancels = []
    monkeypatch.setattr(sr, '_run_cli',
                        _sweep_cli([{'symbol': 'SNDK', 'qty': '3'}], orders, cancels))
    labels = sr.sweep_orphan_exits(dry_run=False)
    assert cancels == ['o1', 'o2', 'o3']
    assert len(labels) == 3


def test_sweep_noop_when_broker_unreadable(monkeypatch):
    """Never cancel on unverifiable flatness: position list failure → no-op."""
    def _cli(args, timeout=15):
        if args[:2] == ['position', 'list']:
            return False, None, {'error': 'down'}
        raise AssertionError('must not list or cancel orders')
    monkeypatch.setattr(sr, '_run_cli', _cli)
    assert sr.sweep_orphan_exits(dry_run=False) == []
