import json
from src.execution import regime_liquidator as rl


class _Cur:
    def __init__(s): s.sql = []
    def execute(s, q, p=None): s.sql.append((q, p))
    def fetchone(s): return ('uuid-1',)
    def __enter__(s): return s
    def __exit__(s, *a): pass


class _Conn:
    def __init__(s): s.cur = _Cur(); s.committed = 0
    def cursor(s): return s.cur
    def commit(s): s.committed += 1
    def __enter__(s): return s
    def __exit__(s, *a): pass


def _row(**kw):
    r = dict(run_date='2026-11-10', regime_from='A', regime_to='B', ticker='BTC-USD',
             direction='long', qty=0.1, result_status='filled')
    r.update(kw)
    return r


def _run(monkeypatch, row):
    c = _Conn()
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://x')
    import psycopg2
    monkeypatch.setattr(psycopg2, 'connect', lambda dsn: c)
    assert rl._write_liquidation_audit(row) == 'uuid-1'
    return c.cur.sql[0]


def test_write_liquidation_audit_includes_asset_class_when_given(monkeypatch):
    sql, params = _run(monkeypatch, _row(asset_class='crypto', source='crypto_breaker'))
    assert 'asset_class' in sql
    assert 'crypto' in params
    cr = [p for p in params if isinstance(p, str) and p.startswith('{')][0]
    assert json.loads(cr)['source'] == 'crypto_breaker'


def test_write_liquidation_audit_default_source_unchanged(monkeypatch):
    sql, params = _run(monkeypatch, _row())
    assert 'asset_class' not in sql
    assert len(params) == 8
    assert json.loads(params[6])['source'] == 'close_subset'


def test_write_liquidation_audit_missing_key_returns_none(monkeypatch):
    import psycopg2
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://x')
    monkeypatch.setattr(psycopg2, 'connect', lambda dsn: _Conn())
    row = _row()
    del row['run_date']
    assert rl._write_liquidation_audit(row) is None


def test_write_liquidation_audit_error_msg_column_and_payload(monkeypatch):
    sql, params = _run(monkeypatch, _row(asset_class='crypto', error_msg='insufficient'))
    assert 'error_msg' in sql and sql.count('%s') == len(params) == 10
    assert params[-1] == 'insufficient' and json.loads(params[6])['error'] == 'insufficient'
    sql2, params2 = _run(monkeypatch, _row(error_msg='x'))
    assert sql2.count('%s') == len(params2) == 9
