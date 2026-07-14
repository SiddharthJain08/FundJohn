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

    assert 'max_hold_days' not in calls[0]
    assert calls[1]['max_hold_days'] == 30
    # both always pass commit=False + return_metrics=True
    assert all(c.get('commit') is False and c.get('return_metrics') is True for c in calls)
