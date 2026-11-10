"""scripts/register_s_beta_btc.py — pure statement build + DB-free dry run."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location('register_s_beta_btc', ROOT / 'scripts' / 'register_s_beta_btc.py')
reg = importlib.util.module_from_spec(_SPEC)
sys.modules['register_s_beta_btc'] = reg
_SPEC.loader.exec_module(reg)


def test_statement_parameters_match_sizer_contract():
    sql, params = reg.build_statement()
    assert params[0] == 'S_beta_btc'
    assert json.loads(params[5]) == {'benchmark_sleeve': True, 'instrument_class': 'crypto',
                                     'benchmark_ticker': 'BTC-USD'}
    assert params[6] == ['BTC-USD'] and params[7] == 'daily' and params[3] == 2
    assert params[4] == 'src/strategies/implementations/S_beta_btc.py'
    # sizer-side reader agrees
    sys.path.insert(0, str(ROOT / 'src'))
    from execution.benchmark_sleeve import sleeve_benchmark
    assert sleeve_benchmark(json.loads(params[5])) == ('BTC-USD', 'crypto')


def test_upsert_is_idempotent_and_never_resets_status():
    sql, _ = reg.build_statement()
    assert "'pending_approval'" in sql
    assert 'ON CONFLICT (id) DO UPDATE SET' in sql
    assert "COALESCE(strategy_registry.parameters, '{}'::jsonb) || EXCLUDED.parameters" in sql
    update_clause = sql.split('DO UPDATE SET', 1)[1]
    assert 'status' not in update_clause


def test_dry_run_never_connects(monkeypatch, capsys):
    def _boom(*a, **k):
        raise AssertionError('dry run must not apply')
    monkeypatch.setattr(reg, '_apply', _boom)
    monkeypatch.setitem(sys.modules, 'psycopg2', None)     # import would raise
    assert reg.main(['--dry-run']) == 0
    out = capsys.readouterr().out
    assert 'DRY RUN' in out and 'INSERT INTO strategy_registry' in out and 'BTC-USD' in out
