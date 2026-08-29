import importlib.util
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
spec = importlib.util.spec_from_file_location('bench_replay', ROOT / 'scripts' / 'bench_relative_sizing_replay.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)


def test_diff_books():
    off = {'SPY': 60_000.0, 'ZZTA': 80_000.0, 'ZZTB': 60_000.0}
    on = {'SPY': 150_000.0, 'ZZTA': 50_000.0}
    d = mod.diff_books(off, on, {'SPY'})
    assert d['dropped'] == ['ZZTB'] and d['added'] == []
    assert d['moves'][0] == ('SPY', 60_000.0, 150_000.0, 90_000.0)
    assert d['gross_off'] == 200_000.0 and d['gross_on'] == 200_000.0
    assert abs(d['beta_off'] - 0.3) < 1e-9 and abs(d['beta_on'] - 0.75) < 1e-9
