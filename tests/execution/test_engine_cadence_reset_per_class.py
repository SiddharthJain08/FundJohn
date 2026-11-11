"""Crypto engine Phase 3, Task 8 — regime-flip cadence reset is PER CLASS.

A crypto regime flip stamps cadence_reset for crypto strategies only; an
equity flip for equity strategies only. The execution_signals.regime_state
stamp is NOT changed (option 3 of the controller ruling): the crypto prior is
resolved from crypto_regime_states at the newest crypto row's created_at.
No DB / Redis: a fake cursor answers by SQL shape.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import engine  # noqa: E402

CRYPTO_IDS = frozenset({'S_cr'})
_REAL_CRYPTO_IDS = engine._crypto_strategy_ids
TS = datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc)


class _Cur:
    """equity_row answers the equity probe; crypto_row the crypto probe."""
    def __init__(self, equity_row=('LOW_VOL',), crypto_row=(TS, 'HIGH_VOL'),
                 crypto_raises=False):
        self.equity_row, self.crypto_row = equity_row, crypto_row
        self.crypto_raises = crypto_raises
        self.executed = []
        self._next = None

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if 'crypto_regime_states' in sql:
            if self.crypto_raises:
                raise RuntimeError('db down')
            self._next = self.crypto_row
        elif 'SELECT regime_state FROM execution_signals' in sql:
            self._next = self.equity_row

    def fetchone(self):
        return self._next


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv('OPENCLAW_CADENCE_RESET_ON_FLIP', raising=False)
    monkeypatch.delenv('OPENCLAW_FORCE_CADENCE_RESET', raising=False)
    monkeypatch.setattr(engine, '_crypto_strategy_ids', lambda: CRYPTO_IDS)


def _both(cur, eq_state, cr_state):
    eq, cr = {'state': eq_state}, {'state': cr_state}
    engine._stamp_cadence_reset_on_flip(cur, eq)
    engine._stamp_crypto_cadence_reset_on_flip(cur, cr)
    return eq, cr


def test_equity_flip_stamps_equity_only():
    eq, cr = _both(_Cur(('LOW_VOL',), (TS, 'HIGH_VOL')), 'TRANSITIONING', 'HIGH_VOL')
    assert eq.get('cadence_reset') is True
    assert 'cadence_reset' not in cr


def test_crypto_flip_stamps_crypto_only():
    eq, cr = _both(_Cur(('LOW_VOL',), (TS, 'HIGH_VOL')), 'LOW_VOL', 'LOW_VOL')
    assert 'cadence_reset' not in eq
    assert cr.get('cadence_reset') is True


def test_no_flip_either_class():
    eq, cr = _both(_Cur(('LOW_VOL',), (TS, 'HIGH_VOL')), 'LOW_VOL', 'HIGH_VOL')
    assert 'cadence_reset' not in eq and 'cadence_reset' not in cr


def test_equity_probe_excludes_crypto_strategies():
    cur = _Cur()
    engine._stamp_cadence_reset_on_flip(cur, {'state': 'LOW_VOL'})
    sql, params = cur.executed[0]
    assert 'NOT (strategy_id = ANY(%s))' in sql and params == (sorted(CRYPTO_IDS),)


def test_equity_probe_legacy_sql_without_crypto_strategies(monkeypatch):
    monkeypatch.setattr(engine, '_crypto_strategy_ids', lambda: frozenset())
    cur = _Cur()
    engine._stamp_cadence_reset_on_flip(cur, {'state': 'LOW_VOL'})
    sql, params = cur.executed[0]
    assert 'ANY' not in sql and params is None


def test_equity_probe_legacy_sql_when_class_lookup_fails(monkeypatch):
    def _boom():
        raise RuntimeError('manifest unreadable')
    monkeypatch.setattr(engine, '_crypto_strategy_ids', _boom)
    cur = _Cur(('LOW_VOL',))
    r = {'state': 'TRANSITIONING'}
    engine._stamp_cadence_reset_on_flip(cur, r)
    assert 'ANY' not in cur.executed[0][0] and r.get('cadence_reset') is True


def test_crypto_probe_scoped_to_crypto_rows_in_a_savepoint():
    cur = _Cur()
    engine._stamp_crypto_cadence_reset_on_flip(cur, {'state': 'HIGH_VOL'})
    sqls = [s for s, _ in cur.executed]
    assert sqls[0] == 'SAVEPOINT sp_crypto_flip_probe'
    assert 'es.strategy_id = ANY(%s)' in sqls[1] and cur.executed[1][1] == (sorted(CRYPTO_IDS),)
    assert 'crs.ts_utc <= es.created_at' in sqls[1]
    assert sqls[2] == 'RELEASE SAVEPOINT sp_crypto_flip_probe'


def test_crypto_no_prior_rows_counts_as_flip():
    cr = {'state': 'LOW_VOL'}
    engine._stamp_crypto_cadence_reset_on_flip(_Cur(crypto_row=None), cr)
    assert cr.get('cadence_reset') is True


@pytest.mark.parametrize('row', [(None, None), (TS, None)])
def test_crypto_unresolved_prior_never_stamps(row):
    cr = {'state': 'LOW_VOL'}
    engine._stamp_crypto_cadence_reset_on_flip(_Cur(crypto_row=row), cr)
    assert 'cadence_reset' not in cr


def test_crypto_db_error_rolls_back_and_fails_quiet():
    cur = _Cur(crypto_raises=True)
    cr = {'state': 'LOW_VOL'}
    engine._stamp_crypto_cadence_reset_on_flip(cur, cr)
    assert 'cadence_reset' not in cr
    sqls = [s for s, _ in cur.executed]
    assert 'ROLLBACK TO SAVEPOINT sp_crypto_flip_probe' in sqls


def test_crypto_kill_switch_and_force(monkeypatch):
    monkeypatch.setenv('OPENCLAW_CADENCE_RESET_ON_FLIP', '0')
    cur, cr = _Cur(), {'state': 'LOW_VOL'}
    engine._stamp_crypto_cadence_reset_on_flip(cur, cr)
    assert 'cadence_reset' not in cr and cur.executed == []
    monkeypatch.setenv('OPENCLAW_FORCE_CADENCE_RESET', '1')
    engine._stamp_crypto_cadence_reset_on_flip(cur, cr)
    assert cr.get('cadence_reset') is True and cur.executed == []


def test_crypto_probe_skipped_without_crypto_strategies(monkeypatch):
    monkeypatch.setattr(engine, '_crypto_strategy_ids', lambda: frozenset())
    cur, cr = _Cur(), {'state': 'LOW_VOL'}
    engine._stamp_crypto_cadence_reset_on_flip(cur, cr)
    assert 'cadence_reset' not in cr and cur.executed == []


def test_crypto_strategy_ids_from_manifest(monkeypatch):
    monkeypatch.setattr(engine, '_CRYPTO_IDS_MEMO', None)
    real = _REAL_CRYPTO_IDS()
    from strategies.instrument_class import instrument_class_for
    assert real and all(instrument_class_for(s) == 'crypto' for s in real)
    assert 'S_beta_btc' in real and 'S_beta_spy' not in real
    assert _REAL_CRYPTO_IDS() is real          # memoised


# ── run_strategies wiring: crypto strategies get the crypto dict only ───────

class _Rec:
    def __init__(self, sid):
        self.id = sid
        self.seen = None

    def generate_signals(self, prices, regime, universe, aux_data):
        self.seen = dict(regime)
        return []


def _wire(monkeypatch):
    monkeypatch.setattr(engine, 'instrument_class_for',
                        lambda sid: 'crypto' if sid in CRYPTO_IDS else 'equity')
    monkeypatch.setattr(engine, 'is_eligible', lambda sid, rs: True)
    monkeypatch.setattr(engine, '_apply_regime_overrides_to_signals', lambda *a, **k: None)
    monkeypatch.setattr(engine, 'calendar_for', lambda ic: ic)
    monkeypatch.setattr(engine, 'apply_equity_calendar', lambda p: p)
    monkeypatch.setattr(engine, 'load_crypto_regime_state', lambda: {'state': 'LOW_VOL'})


def test_run_strategies_crypto_flip_reaches_crypto_strategy_only(monkeypatch):
    _wire(monkeypatch)
    cr_s, eq_s = _Rec('S_cr'), _Rec('S_eq')
    eq_regime = {'state': 'LOW_VOL'}
    cur = _Cur(('LOW_VOL',), (TS, 'HIGH_VOL'))      # crypto HIGH_VOL -> LOW_VOL
    engine._stamp_cadence_reset_on_flip(cur, eq_regime)
    engine.run_strategies([eq_s, cr_s], None, eq_regime, [], {}, cadence_cur=cur)
    assert cr_s.seen.get('cadence_reset') is True
    assert 'cadence_reset' not in eq_s.seen


def test_run_strategies_equity_flip_never_reaches_crypto_strategy(monkeypatch):
    _wire(monkeypatch)
    cr_s, eq_s = _Rec('S_cr'), _Rec('S_eq')
    eq_regime = {'state': 'TRANSITIONING'}
    cur = _Cur(('LOW_VOL',), (TS, 'LOW_VOL'))      # equity flips, crypto steady
    engine._stamp_cadence_reset_on_flip(cur, eq_regime)
    engine.run_strategies([eq_s, cr_s], None, eq_regime, [], {}, cadence_cur=cur)
    assert eq_s.seen.get('cadence_reset') is True
    assert 'cadence_reset' not in cr_s.seen


def test_run_strategies_without_cursor_never_probes(monkeypatch):
    _wire(monkeypatch)
    called = []
    monkeypatch.setattr(engine, '_stamp_crypto_cadence_reset_on_flip',
                        lambda *a: called.append(a))
    cr_s = _Rec('S_cr')
    engine.run_strategies([cr_s], None, {'state': 'LOW_VOL'}, [], {})
    assert called == [] and 'cadence_reset' not in cr_s.seen
