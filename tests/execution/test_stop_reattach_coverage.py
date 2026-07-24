"""Regression: a bare TP-only limit must NOT count as downside (stop) coverage.

Root cause 2026-06-16→06-18: afterhours_tp.py places bare day-TIF, order_class
'simple' take-profit limits (client_order_id 'ahtp_*'). stop_reattach's coverage
detection counted ANY resting limit as protection, so it treated such positions
as "already covered" and skipped placing the GTC OCO/stop. When the day-TIF TPs
expired the positions were left fully naked (no stop, no TP).

The durable invariant: only a LINKED limit (the limit leg of an OCO/bracket,
which carries a linked stop) counts as downside coverage. A standalone 'simple'
TP limit does not protect the downside and must not mask a missing stop.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from execution import stop_reattach as sr


def _orders():
    # WDC: real OCO — its top-level row is a limit with order_class='oco'.
    # CIEN: bare afterhours TP — order_class 'simple', coid 'ahtp_*', no stop.
    return [
        {'symbol': 'WDC',  'type': 'limit', 'qty': '35', 'order_class': 'oco',
         'limit_price': '807.82', 'client_order_id': 'oco_WDC_1'},
        {'symbol': 'CIEN', 'type': 'limit', 'qty': '42', 'order_class': 'simple',
         'limit_price': '493.40', 'client_order_id': 'ahtp_CIEN_1'},
    ]


def test_linked_only_excludes_bare_tp(monkeypatch):
    monkeypatch.setattr(sr, '_run_cli', lambda *a, **k: (True, _orders(), None))
    cov = sr.fetch_tp_covered(linked_only=True)
    assert cov.get('WDC') == 35.0          # OCO limit leg = real coverage
    assert 'CIEN' not in cov               # bare 'simple' TP is NOT coverage


def test_default_counts_all_tps_for_afterhours_idempotency(monkeypatch):
    # afterhours_tp relies on the permissive default to avoid double-placing
    # its own ahtp_ TPs, so the default behaviour must be unchanged.
    monkeypatch.setattr(sr, '_run_cli', lambda *a, **k: (True, _orders(), None))
    cov = sr.fetch_tp_covered()
    assert cov.get('WDC') == 35.0
    assert cov.get('CIEN') == 42.0


# ── CLI timeout resilience (2026-07-24) ─────────────────────────────────────
# 00:05Z: one slow `order list` raised TimeoutExpired straight out of the
# 20:05 ET reattach pass — the pass died before the emergency-exit check ran.
# A hung broker call must degrade to (False, None, err), never propagate.

def test_run_cli_timeout_degrades_to_failure(monkeypatch):
    import subprocess as sp
    monkeypatch.setattr('time.sleep', lambda s: None)
    n = {'calls': 0}
    def fake_run(cmd, **kw):
        n['calls'] += 1
        raise sp.TimeoutExpired(cmd, kw.get('timeout') or 0)
    monkeypatch.setattr(sr.subprocess, 'run', fake_run)
    ok, payload, err = sr._run_cli(['order', 'list'])
    assert ok is False and payload is None
    assert 'timeout' in err['error']
    assert n['calls'] == 4              # initial try + one per backoff step


def test_run_cli_timeout_then_success_recovers(monkeypatch):
    import subprocess as sp, types
    monkeypatch.setattr('time.sleep', lambda s: None)
    n = {'calls': 0}
    def fake_run(cmd, **kw):
        n['calls'] += 1
        if n['calls'] == 1:
            raise sp.TimeoutExpired(cmd, 15)
        return types.SimpleNamespace(returncode=0, stdout='[]', stderr='')
    monkeypatch.setattr(sr.subprocess, 'run', fake_run)
    ok, payload, err = sr._run_cli(['position', 'list'])
    assert ok is True and payload == [] and err is None
    assert n['calls'] == 2


def test_fetch_positions_distinguishes_failure_from_flat(monkeypatch):
    monkeypatch.setattr(sr, '_run_cli', lambda *a, **k: (False, None, {'error': 'x'}))
    assert sr.fetch_positions() is None          # couldn't ask
    monkeypatch.setattr(sr, '_run_cli', lambda *a, **k: (True, [], None))
    assert sr.fetch_positions() == []            # asked, genuinely flat
