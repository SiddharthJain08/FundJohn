"""Stub-cursor test of scripts/run_universe_shrink.py main() (2026-11-24
shrink Sharpe fix): a parity failure and a compute error write NOTHING for
their strategy and do not stop the pass; a zero-trade run never triggers the
whole-panel prices read (load_prices([]) == full parquet) and still gets its
normal rows."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts' / 'run_universe_shrink.py'

RUNS = {  # sid -> (run_id, run total_sharpe, trade rows)
    'S_err':  ('run-err', 0.50, [('EEE', 'LONG', date(2024, 2, 5),
                                  date(2024, 2, 9), 0.01, 4, 'LOW_VOL')]),
    'S_par':  ('run-par', 1.00, [('PPP', 'LONG', date(2024, 2, 5),
                                  date(2024, 2, 9), 0.01, 4, 'LOW_VOL')]),
    'S_zero': ('run-zero', 0.50, []),
}
RUN_TO_SID = {v[0]: k for k, v in RUNS.items()}


class _Cur:
    def __init__(self, log):
        self.log, self._rows = log, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append((sql, params))
        if 'FROM strategy_backtest_runs' in sql:
            self._rows = [(sid, rid, date(2024, 1, 1), date(2024, 4, 30), ts)
                          for sid, (rid, ts, _) in RUNS.items()]
        elif 'entry_regime' in sql and 'WHERE run_id' in sql:
            self._rows = list(RUNS[RUN_TO_SID[params[0]]][2])
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return None


class _Pg:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _Cur(self.log)

    def commit(self):
        pass

    def close(self):
        pass


def _load_script():
    spec = importlib.util.spec_from_file_location('run_universe_shrink_t', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_main_parity_fail_error_and_zero_trade(monkeypatch, tmp_path, capsys):
    mod = _load_script()
    import backtest.backtest_panel as bp
    import backtest.unified_backtest as ub
    import backtest.regime_blended_backtest as rbb

    pg = _Pg()
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'strategies': {
        sid: {'state': 'live', 'instrument_class': 'equity'} for sid in RUNS}}))
    monkeypatch.setattr(mod, 'MANIFEST', manifest)
    monkeypatch.setattr(mod, '_pg', lambda: pg)
    monkeypatch.setattr(mod, 'ensure_artifact',
                        lambda args: (tmp_path / 'a.parquet', 'shrink-test'))
    monkeypatch.setattr(mod, 'load_membership', lambda p: ([], {}))
    monkeypatch.setattr(ub, 'load_regimes', lambda: pd.Series(
        'LOW_VOL', index=pd.bdate_range('2024-01-01', '2024-04-30')))
    monkeypatch.setattr(rbb, 'regime_day_frequency', lambda p: {'LOW_VOL': 1.0})

    price_calls = []

    def fake_load_prices(tickers=None):
        price_calls.append(list(tickers or []))
        return pd.DataFrame({'ticker': [], 'date': [], 'close': []})
    monkeypatch.setattr(bp, 'load_prices', fake_load_prices)
    monkeypatch.setattr(bp, 'build_close_lookup', lambda prices: (lambda t: None))

    tiers = (*mod.LADDER_TIERS, mod.FULL_TIER)

    def fake_shrink(trades, *a, **k):
        tk = trades[0]['ticker'] if trades else None
        if tk == 'EEE':
            raise ValueError('boom')
        full = 0.2 if tk == 'PPP' else 0.5          # PPP: 0.2 vs run 1.00
        return {'metrics_by_tier': {t: {'trades_n': len(trades)} for t in tiers},
                'regime_by_tier': {t: {} for t in tiers},
                'totals_by_tier': {mod.FULL_TIER: {'sharpe': full}},
                'verdict': {'verdict': 'no_signal'}}
    monkeypatch.setattr(mod, 'shrink_and_select', fake_shrink)
    monkeypatch.setattr(mod, 'db_rows', lambda sid, run_id, result, **k:
                        [(sid, run_id, 'full', 'TOTAL')])

    inserted, recs_made, posts = [], [], []
    monkeypatch.setattr(mod, 'execute_values',
                        lambda cur, sql, rows, **k: inserted.extend(rows))
    monkeypatch.setattr(mod.recs, 'build_rationale', lambda v, window=None: 'r')
    monkeypatch.setattr(mod.recs, 'insert_recommendation',
                        lambda pg_, **k: recs_made.append(k['strategy_id']) or 7)
    monkeypatch.setattr(mod.recs, 'post_discord', lambda pg_, msg: posts.append(msg))
    monkeypatch.delenv('OPENCLAW_SHRINK_PARITY_TOL', raising=False)
    monkeypatch.setattr(sys, 'argv', ['run_universe_shrink.py', '--force',
                                      '--strategy', 'S_err,S_par,S_zero'])

    assert mod.main() == 0
    out = capsys.readouterr().out

    # zero-trade run: never the whole-panel read
    assert [] not in price_calls
    assert price_calls == [['EEE'], ['PPP']]
    # only S_zero wrote anything
    assert {r[0] for r in inserted} == {'S_zero'}
    assert recs_made == ['S_zero']
    flag_updates = [p for sql, p in pg.log
                    if 'UPDATE universe_shrink_metrics' in sql]
    # 2026-11-25 (mig 171): the parity-failed strategy's run has its stale rows
    # un-chosen (readers fall back to the run of record) BEFORE the zero-trade
    # strategy's normal chosen-flag update; both verdicts are persisted.
    assert [p[-1] for p in flag_updates] == ['run-par', 'run-zero']
    parity_rows = [p for sql, p in pg.log if 'INSERT INTO universe_shrink_parity' in sql]
    assert [(p[0], p[1], p[2]) for p in parity_rows] == [('S_par', 'run-par', False), ('S_zero', 'run-zero', True)]
    assert len([sql for sql, p in pg.log if 'SET chosen=FALSE' in sql]) == 1
    assert '[shrink] PARITY FAIL S_par full=0.2 run=1.0 — rows NOT written' in out
    assert 'ERROR S_err' in out
    assert 'parity_fail=1 errors=1' in out
    joined = '\n'.join(posts)
    assert 'PARITY FAIL' in joined and 'S_par' in joined and 'S_err' in joined


def _wire_change_pass(monkeypatch, tmp_path, mod, pg):
    """Minimal stub wiring for a one-strategy pass (S_zero: no trades, so no
    price read) whose shrink verdict is a 'change' sp500 -> tier_liquid."""
    import backtest.unified_backtest as ub
    import backtest.regime_blended_backtest as rbb
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'strategies': {
        'S_zero': {'state': 'live', 'instrument_class': 'equity'}}}))
    monkeypatch.setattr(mod, 'MANIFEST', manifest)
    monkeypatch.setattr(mod, '_pg', lambda: pg)
    monkeypatch.setattr(mod, 'ensure_artifact',
                        lambda args: (tmp_path / 'a.parquet', 'shrink-test'))
    monkeypatch.setattr(mod, 'load_membership', lambda p: ([], {}))
    monkeypatch.setattr(ub, 'load_regimes', lambda: pd.Series(
        'LOW_VOL', index=pd.bdate_range('2024-01-01', '2024-04-30')))
    monkeypatch.setattr(rbb, 'regime_day_frequency', lambda p: {'LOW_VOL': 1.0})
    tiers = (*mod.LADDER_TIERS, mod.FULL_TIER)
    monkeypatch.setattr(mod, 'shrink_and_select', lambda trades, *a, **k: {
        'metrics_by_tier': {t: {'trades_n': 0} for t in tiers},
        'regime_by_tier': {t: {} for t in tiers},
        'totals_by_tier': {mod.FULL_TIER: {'sharpe': 0.5}},
        'verdict': {'verdict': 'change', 'choice': 'tier_liquid'}})
    monkeypatch.setattr(mod, 'db_rows', lambda sid, run_id, result, **k:
                        [(sid, run_id, 'full', 'TOTAL')])
    monkeypatch.setattr(mod, 'execute_values', lambda cur, sql, rows, **k: None)
    monkeypatch.setattr(mod.recs, 'build_rationale', lambda v, window=None: 'r')
    monkeypatch.setattr(mod.recs, 'insert_recommendation', lambda pg_, **k: 7)
    monkeypatch.setattr(mod.recs, 'post_discord', lambda pg_, msg: None)
    monkeypatch.delenv('OPENCLAW_SHRINK_PARITY_TOL', raising=False)
    # the adoption import is local to the loop: stub the module it imports
    import types
    adopted = []
    stub = types.ModuleType('src.strategies.lifecycle_universe_adoption')
    stub.adopt_universe_recommendation = (
        lambda rec_id, actor='system', **k: adopted.append((rec_id, actor)))
    monkeypatch.setitem(sys.modules, 'src.strategies.lifecycle_universe_adoption', stub)
    return adopted


def test_change_verdict_is_adopted_by_default(monkeypatch, tmp_path, capsys):
    """2026-11-25 operator ruling: auto-adoption is the norm — no flag needed."""
    mod = _load_script()
    pg = _Pg()
    adopted = _wire_change_pass(monkeypatch, tmp_path, mod, pg)
    monkeypatch.setattr(sys, 'argv', ['run_universe_shrink.py', '--force',
                                      '--strategy', 'S_zero'])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert adopted == [(7, 'auto:universe-shrink')]
    assert 'adopted=1' in out
    # chosen flags follow the ADOPTED tier, not the old predicate
    flag_updates = [p for sql, p in pg.log
                    if 'UPDATE universe_shrink_metrics' in sql]
    assert flag_updates and flag_updates[-1][0] == 'tier_liquid'


def test_no_adopt_leaves_a_pending_recommendation(monkeypatch, tmp_path, capsys):
    mod = _load_script()
    pg = _Pg()
    adopted = _wire_change_pass(monkeypatch, tmp_path, mod, pg)
    monkeypatch.setattr(sys, 'argv', ['run_universe_shrink.py', '--force',
                                      '--no-adopt', '--strategy', 'S_zero'])
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert adopted == []
    assert 'changes=1' in out and 'adopted=0' in out
    flag_updates = [p for sql, p in pg.log
                    if 'UPDATE universe_shrink_metrics' in sql]
    assert flag_updates and flag_updates[-1][0] == 'sp500'   # current predicate kept
