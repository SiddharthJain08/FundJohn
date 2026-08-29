import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import benchmark_sleeve as bs  # noqa: E402


class _Cur:
    def __init__(self, rows, fail=False): self.rows, self.fail = rows, fail
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        if self.fail: raise RuntimeError('db down')
        assert "parameters ->> %s" in sql and params == (bs.PARAM_KEY,)
    def fetchall(self): return self.rows


class _Conn:
    def __init__(self, rows, fail=False): self._c = _Cur(rows, fail)
    def cursor(self): return self._c
    def close(self): pass


def test_loads_ids_from_registry_parameters():
    assert bs.load_benchmark_sleeve_ids(conn=_Conn([('S_beta_spy',)])) == {'S_beta_spy'}


def test_db_failure_is_empty_set_not_raise():
    assert bs.load_benchmark_sleeve_ids(conn=_Conn([], fail=True)) == set()


def test_benchmark_tickers_any_direction():
    meta = {'SPY': {'strategies': ['S_x', 'S_beta_spy'], 'directions': [1, 1]},
            'ZZTA': {'strategies': ['S_x'], 'directions': [1]},
            'QQQ': {'strategies': ['S_beta_spy'], 'directions': [-1]}}
    assert bs.benchmark_tickers(meta, {'S_beta_spy'}) == {'SPY', 'QQQ'}
    assert bs.benchmark_tickers(meta, set()) == set()
