import importlib.util
import os
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


def test_size_wiring(monkeypatch):
    """DB-free: verifies _size() wires the live-lane-mirroring signals/state,
    stubs the Redis force-fire flag, and toggles the bench-sizing env — without
    touching Postgres (via a fake _regime_params) or the real sizer body (via
    a spy replacing size_positions)."""
    monkeypatch.setattr(mod, '_regime_params', lambda regime: {})
    import execution.regime_blended_sizer as _sizer

    calls = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return [{'ticker': 'SPY', 'action': 'open_long', 'target_usd': 1.0, 'strategy_id': 'S_beta_spy'}]

    monkeypatch.setattr(_sizer, 'size_positions', _spy)
    monkeypatch.setenv('OPENCLAW_SAMEDAY_SIGNAL_TARGET', '1')

    result = mod._size(100_000.0, 'LOW_VOL', False)

    assert result == {'SPY': 1.0}
    assert calls[0]['signals'] and calls[0]['strategy_state'] == {}
    assert _sizer._check_force_fire_flag() is False
    assert os.environ.get('OPENCLAW_BENCH_RELATIVE_SIZING') is None

    mod._size(100_000.0, 'LOW_VOL', True)
    assert os.environ.get('OPENCLAW_BENCH_RELATIVE_SIZING') == '1'
