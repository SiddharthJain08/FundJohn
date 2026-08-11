"""Guard behaviour of finalize_sized_payload (sized_handoff.py).

Regression for ERR-20260612-001: when an intraday regime-redeploy has already
submitted today's orders, the EOD into-close fill's `trade` step must NOT abort
(old behaviour: `return False` -> sizer rc=3 -> the whole eod-into-close-fill
cycle aborted before reconcile/report/health on every redeploy day). It must
instead leave the existing sized handoff intact (send_report + d-1 context read
it) and return True so the cycle proceeds; the executor's per-order
already_executed() idempotency prevents any double-submit.
"""
import importlib
import pytest

sized_handoff = importlib.import_module('execution.sized_handoff')
import execution.handoff as handoff_mod
import psycopg2


class _FakeCursor:
    def __init__(self, count):
        self._count = count
    def execute(self, *a, **k):
        return None
    def fetchone(self):
        return (self._count,)
    def close(self):
        return None


class _FakeConn:
    def __init__(self, count):
        self._count = count
    def cursor(self):
        return _FakeCursor(self._count)
    def close(self):
        return None


@pytest.fixture
def patched(monkeypatch):
    """Patch the DB + handoff IO; return a dict tracking write_handoff calls."""
    state = {'writes': [], 'count': 0}
    monkeypatch.setenv('POSTGRES_URI', 'postgres://fake/db')
    monkeypatch.delenv('OPENCLAW_FORCE_RESIZE', raising=False)
    monkeypatch.delenv('OPENCLAW_TRADE_AGENT_DRY_RUN', raising=False)
    # Lane flags default-clear so each test picks its lane explicitly (the
    # production box exports OPENCLAW_EOD_RECONCILE=1 globally in .env).
    monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    monkeypatch.delenv('OPENCLAW_INTRADAY_REDEPLOY', raising=False)
    monkeypatch.delenv('OPENCLAW_SAMEDAY_TOPUP', raising=False)
    monkeypatch.setattr(psycopg2, 'connect', lambda uri: _FakeConn(state['count']))
    monkeypatch.setattr(handoff_mod, 'read_handoff', lambda rd, kind: {})
    monkeypatch.setattr(handoff_mod, 'write_handoff',
                        lambda rd, kind, payload: state['writes'].append((rd, kind)))
    return state


def _payload():
    return {'orders': [{'ticker': 'XYZ', 'strategy_id': 'S_x', 'pct_nav': 0.01}],
            'vetoed': []}


def test_guard_trip_proceeds_without_overwrite(patched):
    """Submissions already exist -> return True (cycle proceeds), and the
    existing sized handoff is left intact (write_handoff NOT called)."""
    patched['count'] = 77  # a redeploy already submitted today
    ok = sized_handoff.finalize_sized_payload('2026-06-11', _payload(), source='test')
    assert ok is True, 'guard-trip must not abort the cycle (rc=0, not rc=3)'
    assert patched['writes'] == [], 'must not overwrite the existing sized handoff'


def test_no_existing_submissions_writes_and_returns_true(patched):
    """No submissions yet -> normal write + return True."""
    patched['count'] = 0
    ok = sized_handoff.finalize_sized_payload('2026-06-11', _payload(), source='test')
    assert ok is True
    assert patched['writes'] == [('2026-06-11', 'sized')]


def test_force_resize_bypasses_guard(patched, monkeypatch):
    """OPENCLAW_FORCE_RESIZE=1 -> deliberately re-size even if submissions exist."""
    patched['count'] = 77
    monkeypatch.setenv('OPENCLAW_FORCE_RESIZE', '1')
    ok = sized_handoff.finalize_sized_payload('2026-06-11', _payload(), source='test')
    assert ok is True
    assert patched['writes'] == [('2026-06-11', 'sized')]


def _flatten_payload(n=3):
    return {'orders': [{'ticker': 'T%d' % i, 'strategy_id': '__close_orphan__',
                        'target_usd': 0.0, 'pct_nav': 0.01} for i in range(n)],
            'vetoed': []}


def test_pure_flatten_payload_bypasses_guard(patched):
    """2026-08-07 regression: the same-day zero-conviction flatten (all orders
    __close_orphan__) was silently discarded by the guard because the 10:00
    base cycle had already submitted — 47 closes logged as liquidated, $0
    reached the broker. A pure-flatten payload must be WRITTEN even when
    submissions already exist for the run_date."""
    patched['count'] = 53  # the morning wave already submitted
    ok = sized_handoff.finalize_sized_payload('2026-08-07', _flatten_payload(),
                                              source='test')
    assert ok is True
    assert patched['writes'] == [('2026-08-07', 'sized')], \
        'a pure flatten must overwrite the stale morning handoff'


def test_mixed_payload_still_refused_when_submissions_exist(patched):
    """One open order in the payload, NO lane flags -> the ERR-20260612-001
    refusal stands (the top-up lane below requires OPENCLAW_EOD_RECONCILE)."""
    patched['count'] = 53
    payload = _flatten_payload()
    payload['orders'].append({'ticker': 'NEW', 'strategy_id': 'S_x', 'pct_nav': 0.02})
    ok = sized_handoff.finalize_sized_payload('2026-08-07', payload, source='test')
    assert ok is True
    assert patched['writes'] == [], 'mixed payloads must not overwrite'


def test_empty_orders_payload_still_refused_when_submissions_exist(patched):
    """No orders at all is NOT a flatten — an empty payload must never clobber
    the morning handoff (send_report + d-1 context read it)."""
    patched['count'] = 53
    ok = sized_handoff.finalize_sized_payload('2026-08-07',
                                              {'orders': [], 'vetoed': []},
                                              source='test')
    assert ok is True
    assert patched['writes'] == []


# ── Same-day top-up lane (operator directive 2026-08-11) ─────────────────────
# 2026-08-10 incident: the 15:00 ET compute targeted $43.2k gross vs the
# morning redeploy wave's $23.7k, and the whole re-size was discarded by the
# keep-intact branch — the authoritative same-day lane could flatten but never
# ADD. The EOD lane (OPENCLAW_EOD_RECONCILE=1, not the intraday-redeploy lane)
# now RE-TRUES: it writes the fresh handoff; the executor's wave-scoped dedup
# (sized_at → _w<HHMMSS> coid tag) lets its delta orders through.

def test_eod_lane_topup_overwrites_when_submissions_exist(patched, monkeypatch):
    """EOD lane + existing submissions + non-empty payload -> RE-TRUE write."""
    patched['count'] = 40  # the morning redeploy wave already submitted
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    ok = sized_handoff.finalize_sized_payload('2026-08-10', _payload(), source='test')
    assert ok is True
    assert patched['writes'] == [('2026-08-10', 'sized')], \
        'the authoritative same-day compute must overwrite (afternoon top-up)'


def test_redeploy_lane_never_topups(patched, monkeypatch):
    """The intraday-redeploy lane keeps first-writer-wins semantics even with
    OPENCLAW_EOD_RECONCILE=1 in the environment (it is global in .env)."""
    patched['count'] = 40
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_INTRADAY_REDEPLOY', '1')
    ok = sized_handoff.finalize_sized_payload('2026-08-10', _payload(), source='test')
    assert ok is True
    assert patched['writes'] == [], 'redeploy lane must not re-true'


def test_topup_kill_switch(patched, monkeypatch):
    """OPENCLAW_SAMEDAY_TOPUP=0 restores the keep-intact behaviour."""
    patched['count'] = 40
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    monkeypatch.setenv('OPENCLAW_SAMEDAY_TOPUP', '0')
    ok = sized_handoff.finalize_sized_payload('2026-08-10', _payload(), source='test')
    assert ok is True
    assert patched['writes'] == []


def test_eod_lane_empty_payload_still_refused(patched, monkeypatch):
    """Even in the top-up lane an empty-orders payload keeps the earlier
    handoff — there is nothing to execute and send_report reads it."""
    patched['count'] = 40
    monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')
    ok = sized_handoff.finalize_sized_payload('2026-08-10',
                                              {'orders': [], 'vetoed': []},
                                              source='test')
    assert ok is True
    assert patched['writes'] == []


def test_sized_at_wave_stamp_always_present(patched):
    """finalize stamps sized_at (UTC, second resolution) — the executor derives
    the _w<HHMMSS> wave tag from it, so it must exist on every write path."""
    patched['count'] = 0
    payload = _payload()
    sized_handoff.finalize_sized_payload('2026-06-11', payload, source='test')
    import re
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', payload['sized_at'])
