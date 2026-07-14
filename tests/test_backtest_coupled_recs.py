import pytest
from execution import backtest_coupled_recs as bc


def test_candidate_pct_uses_default_when_no_base():
    assert bc.candidate_pct(base=None, delta=0.10, default=0.07) == pytest.approx(0.077)


def test_candidate_pct_uses_existing_base():
    assert bc.candidate_pct(base=0.05, delta=0.20, default=0.07) == pytest.approx(0.06)


def test_candidate_pct_clamped():
    assert bc.candidate_pct(base=0.07, delta=5.0, default=0.07) == 0.30
    assert bc.candidate_pct(base=0.07, delta=-0.99, default=0.07) == 0.01


def test_candidate_pct_none_delta_returns_none():
    assert bc.candidate_pct(base=0.07, delta=None, default=0.07) is None


def test_candidate_pct_noise_delta_returns_none():
    assert bc.candidate_pct(base=0.07, delta=0.004, default=0.07) is None


def test_accept_rule_any_strict_improvement():
    # 2026-07-14: gate = ANY strictly-positive Sharpe improvement (was >= +0.10).
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.61, candidate_n_trades=30) is True
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.59, candidate_n_trades=30) is True
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.501, candidate_n_trades=30) is True
    # No improvement / regression → reject.
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.50, candidate_n_trades=100) is False
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.49, candidate_n_trades=100) is False
    # Trade floor unchanged.
    assert bc.qualifies(baseline_sharpe=0.50, candidate_sharpe=0.61, candidate_n_trades=29) is False


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
    def execute(self, *a, **k):
        pass
    def fetchall(self):
        return self._rows
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
    def cursor(self):
        return _FakeCursor(self._rows)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_replace_stops_anchors_to_entry_distance(monkeypatch):
    # long @ entry 200 with validated stop distance 6% → 188.0;
    # short @ entry 50 → 53.0. The OLD price-relative math would have produced
    # old_stop·(1+δ) — tightening the long for a positive "widen" δ.
    import psycopg2
    from execution import alpaca_replace_stop as ars
    rows = [
        ('coid-long',  'AAPL', 'long',  200.0, 190.0),
        ('coid-short', 'XYZ',  'short',  50.0,  52.0),
        ('coid-noent', 'BAD',  'long',   None,  10.0),   # no entry → skipped
    ]
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')
    monkeypatch.setattr(psycopg2, 'connect', lambda uri: _FakeConn(rows))
    calls = []
    monkeypatch.setattr(ars, 'replace_stop_for_coid',
                        lambda coid, ns: calls.append((coid, ns)) or
                                         {'status': 'replaced', 'coid': coid})
    out = bc._replace_stops_for_applied('S_x', 0.06, log=lambda *_: None)
    assert out == {'attempted': 2, 'replaced': 2, 'failed': 0}
    assert ('coid-long', 188.0) in calls
    assert ('coid-short', 53.0) in calls


def test_replace_stops_counts_broker_rejects(monkeypatch):
    import psycopg2
    from execution import alpaca_replace_stop as ars
    rows = [('c1', 'AAPL', 'long', 100.0, 95.0)]
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')
    monkeypatch.setattr(psycopg2, 'connect', lambda uri: _FakeConn(rows))
    monkeypatch.setattr(ars, 'replace_stop_for_coid',
                        lambda coid, ns: {'status': 'replace_failed'})
    out = bc._replace_stops_for_applied('S_x', 0.05, log=lambda *_: None)
    assert out == {'attempted': 1, 'replaced': 0, 'failed': 1}


def test_has_actionable_delta():
    assert bc.has_actionable_delta({'stop_delta_pct': None, 'target_delta_pct': None}) is False
    assert bc.has_actionable_delta({'stop_delta_pct': 0.05, 'target_delta_pct': None}) is True
    assert bc.has_actionable_delta({'stop_delta_pct': 0.004, 'target_delta_pct': None}) is False


def test_has_actionable_delta_max_hold():
    # A non-zero hold_days_delta alone makes the rec actionable (no NOISE floor).
    assert bc.has_actionable_delta(
        {'stop_delta_pct': None, 'target_delta_pct': None, 'hold_days_delta': 5}) is True
    assert bc.has_actionable_delta(
        {'stop_delta_pct': None, 'target_delta_pct': None, 'hold_days_delta': -3}) is True
    # Zero / missing hold delta + no bracket delta → not actionable.
    assert bc.has_actionable_delta(
        {'stop_delta_pct': None, 'target_delta_pct': None, 'hold_days_delta': 0}) is False
    assert bc.has_actionable_delta(
        {'stop_delta_pct': None, 'target_delta_pct': None}) is False


def test_candidate_max_hold_absolute_delta():
    # hold_days_delta is an ABSOLUTE integer day delta (not a pct): 21 + 5 = 26.
    assert bc.candidate_max_hold(base=21, delta=5) == 26
    assert bc.candidate_max_hold(base=21, delta=-7) == 14
    # default base used when base is None.
    assert bc.candidate_max_hold(base=None, delta=4) == bc.DEFAULT_MAX_HOLD + 4


def test_candidate_max_hold_zero_or_none_returns_none():
    assert bc.candidate_max_hold(base=21, delta=0) is None
    assert bc.candidate_max_hold(base=21, delta=None) is None


def test_candidate_max_hold_clamped():
    # Floor at 1 day (can't propose instant exit), ceiling at 250 days.
    assert bc.candidate_max_hold(base=5, delta=-100) == bc.MAX_HOLD_LO
    assert bc.candidate_max_hold(base=200, delta=1000) == bc.MAX_HOLD_HI


def test_run_metrics_passes_max_hold(monkeypatch):
    # _run_metrics must forward max_hold_days to run_backtest only when set,
    # and omit it (use the backtest default) when None — so the stop/target-only
    # path stays byte-identical to pre-max-hold-coupling behaviour.
    calls = []

    class _FakeUB:
        @staticmethod
        def run_backtest(strategy_id, **kwargs):
            calls.append(kwargs)
            return ('run-id', {'sharpe': 1.0, 'total_trades': 50,
                               'median_stop_pct': 0.07, 'median_target_pct': 0.08})

    import sys, types
    fake_mod = types.ModuleType('backtest.unified_backtest')
    fake_mod.run_backtest = _FakeUB.run_backtest
    monkeypatch.setitem(sys.modules, 'backtest.unified_backtest', fake_mod)
    # also register parent package attr so `from backtest import unified_backtest` resolves
    import backtest as _bt
    monkeypatch.setattr(_bt, 'unified_backtest', fake_mod, raising=False)

    bc._run_metrics('S_x', None)                       # no max_hold → not forwarded
    bc._run_metrics('S_x', {'LOW_VOL': {}}, max_hold_days=30)  # forwarded
    bc._run_metrics('S_x', None, commit=True)          # persisting baseline

    assert 'max_hold_days' not in calls[0]
    assert calls[1]['max_hold_days'] == 30
    assert calls[0].get('commit') is False and calls[1].get('commit') is False
    assert calls[2].get('commit') is True              # fallback baseline persists
    assert all(c.get('return_metrics') is True for c in calls)


class _FakeOneCursor:
    """Cursor whose successive fetchone() calls pop from a queue (one per execute)."""
    def __init__(self, results):
        self._results = list(results)
    def execute(self, *a, **k):
        pass
    def fetchone(self):
        return self._results.pop(0)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _FakeOneConn:
    def __init__(self, results):
        self._results = results
    def cursor(self):
        return _FakeOneCursor(self._results)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def test_stored_baseline_reads_canonical_row(monkeypatch):
    import datetime
    import psycopg2
    results = [
        ('run-1', 1.23, datetime.date(2026, 7, 8), {'max_hold_days': 21}),
        (0.06, 0.09),
    ]
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')
    monkeypatch.setattr(psycopg2, 'connect', lambda uri: _FakeOneConn(results))
    base = bc._stored_baseline('S_x')
    assert base == {'sharpe': 1.23, 'median_stop_pct': 0.06,
                    'median_target_pct': 0.09,
                    'end_date': datetime.date(2026, 7, 8), 'max_hold_days': 21}


def test_stored_baseline_parses_config_json_string(monkeypatch):
    import datetime
    import psycopg2
    results = [
        ('run-1', 0.5, datetime.date(2026, 7, 8), '{"max_hold_days": 34}'),
        (None, None),
    ]
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')
    monkeypatch.setattr(psycopg2, 'connect', lambda uri: _FakeOneConn(results))
    base = bc._stored_baseline('S_x')
    assert base['max_hold_days'] == 34
    assert base['median_stop_pct'] is None


def test_stored_baseline_none_only_when_missing(monkeypatch):
    # No staleness fallback under the no-refresh regime: an old canonical row
    # just means nothing changed since — it is still the baseline.
    import psycopg2
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://stub')
    monkeypatch.setattr(psycopg2, 'connect', lambda uri: _FakeOneConn([None]))
    assert bc._stored_baseline('S_new') is None


class _CandConn:
    """Deferred-commit candidate connection stub."""
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self.executed = []
    def cursor(self):
        conn = self
        class _Cur:
            def execute(self, sql, params=None):
                conn.executed.append((sql, params))
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _Cur()
    def commit(self):
        self.committed = True
    def rollback(self):
        self.rolled_back = True
    def close(self):
        self.closed = True


def _wire_run(monkeypatch, *, baseline, cand_metrics, conn):
    monkeypatch.setenv('OPENCLAW_BACKTEST_COUPLED_RECS', '1')
    monkeypatch.setattr(bc, '_load_recs', lambda rec_date=None: [
        {'id': 1, 'strategy_id': 'S_x', 'stop_delta_pct': 0.10,
         'target_delta_pct': None, 'hold_days_delta': None}])
    monkeypatch.setattr(bc, '_eligible_regimes', lambda sid: ['LOW_VOL'])
    monkeypatch.setattr(bc, '_stored_baseline', lambda sid: baseline)
    cand_calls = []
    def _fake_candidate(sid, override, max_hold_days=None):
        cand_calls.append({'override': override, 'max_hold_days': max_hold_days})
        return conn, 'cand-run-id', cand_metrics
    monkeypatch.setattr(bc, '_run_candidate', _fake_candidate)
    return cand_calls


_BASE = {'sharpe': 1.0, 'median_stop_pct': 0.05, 'median_target_pct': 0.08,
         'end_date': None, 'max_hold_days': 21}


def test_run_single_backtest_per_rec_dry_run_rolls_back(monkeypatch):
    # ONE backtest per rec (the candidate); dry-run never persists it.
    conn = _CandConn()
    calls = _wire_run(monkeypatch, baseline=_BASE,
                      cand_metrics={'sharpe': 1.2, 'total_trades': 100}, conn=conn)
    out = bc.run(dry_run=True, log=lambda *_: None)
    assert out == {'applied': 1, 'rejected': 0, 'scanned': 1}
    assert len(calls) == 1                                    # candidate only
    assert calls[0]['override'] == {'LOW_VOL': {'stop_pct': pytest.approx(0.055)}}
    assert conn.rolled_back and not conn.committed and conn.closed


def test_run_reject_rolls_back_candidate(monkeypatch):
    conn = _CandConn()
    _wire_run(monkeypatch, baseline=_BASE,
              cand_metrics={'sharpe': 0.9, 'total_trades': 100}, conn=conn)
    marks = []
    monkeypatch.setattr(bc, '_mark_outcome', lambda rid, o, n: marks.append(o))
    out = bc.run(dry_run=False, log=lambda *_: None)
    assert out == {'applied': 0, 'rejected': 1, 'scanned': 1}
    assert conn.rolled_back and not conn.committed and conn.closed
    assert marks == ['rejected']


def test_run_apply_commits_candidate_as_canonical(monkeypatch):
    # APPLY: set_params first, then the candidate run is tagged + COMMITTED —
    # it becomes the canonical primary_window row (no weekly refresh needed).
    import sys, types
    conn = _CandConn()
    _wire_run(monkeypatch, baseline=_BASE,
              cand_metrics={'sharpe': 1.2, 'total_trades': 100}, conn=conn)
    set_calls = []
    fake_em = types.ModuleType('strategies.eligibility_manager')
    fake_em.set_params = lambda **kw: set_calls.append(kw)
    monkeypatch.setitem(sys.modules, 'strategies.eligibility_manager', fake_em)
    import strategies as _s
    monkeypatch.setattr(_s, 'eligibility_manager', fake_em, raising=False)
    fake_panel = types.ModuleType('backtest.backtest_panel')
    fake_panel.rebuild = lambda sid: None
    monkeypatch.setitem(sys.modules, 'backtest.backtest_panel', fake_panel)
    import backtest as _b
    monkeypatch.setattr(_b, 'backtest_panel', fake_panel, raising=False)
    marks = []
    monkeypatch.setattr(bc, '_mark_outcome', lambda rid, o, n: marks.append(o))
    monkeypatch.setattr(bc, '_replace_stops_for_applied',
                        lambda sid, sp, log: {'attempted': 0, 'replaced': 0, 'failed': 0})
    out = bc.run(dry_run=False, log=lambda *_: None)
    assert out == {'applied': 1, 'rejected': 0, 'scanned': 1}
    assert conn.committed and not conn.rolled_back and conn.closed
    assert len(set_calls) == 1 and set_calls[0]['stop_pct'] == pytest.approx(0.055)
    # the run row is tagged as a coupling apply before commit
    assert any('notes' in sql and params[1] == 'cand-run-id'
               for sql, params in conn.executed)
    assert marks == ['applied']


def test_run_falls_back_to_persisting_baseline_when_no_stored(monkeypatch):
    # Never-backtested strategy → baseline runs WITH commit=True (self-heals
    # the canonical store), then the single candidate as usual.
    conn = _CandConn()
    _wire_run(monkeypatch, baseline=None,
              cand_metrics={'sharpe': 1.2, 'total_trades': 100}, conn=conn)
    base_calls = []
    def _fake_metrics(sid, override, max_hold_days=None, commit=False):
        base_calls.append({'override': override, 'commit': commit})
        return {'sharpe': 1.0, 'total_trades': 80,
                'median_stop_pct': 0.05, 'median_target_pct': 0.08}
    monkeypatch.setattr(bc, '_run_metrics', _fake_metrics)
    out = bc.run(dry_run=True, log=lambda *_: None)
    assert out == {'applied': 1, 'rejected': 0, 'scanned': 1}
    assert base_calls == [{'override': None, 'commit': True}]
