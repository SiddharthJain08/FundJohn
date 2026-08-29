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

    # A3 (final fix wave, 2026-08-29): _size() rebinds these six sizer attrs
    # and sets os.environ directly (not via monkeypatch), and this test calls
    # _size() twice — so without priming monkeypatch here, the stubs/env
    # written by the FIRST call leak into the rest of the pytest process.
    # `monkeypatch.setattr(_sizer, name, getattr(_sizer, name))` records the
    # CURRENT attribute (the real function, since _size hasn't run yet) so
    # monkeypatch restores it at teardown regardless of what _size() later
    # assigns directly; same trick for the env vars _size() writes with
    # `os.environ[...] = ...`.
    for _name in ('_load_broker_positions_usd', '_post_corr_cumsharpe_log', '_post_flatten_alert',
                  '_post_ops_alert', '_maybe_flatten_zero_conviction', '_check_force_fire_flag'):
        monkeypatch.setattr(_sizer, _name, getattr(_sizer, _name))
    for _env in ('OPENCLAW_BENCH_RELATIVE_SIZING', 'OPENCLAW_INTRADAY_REDEPLOY', 'OPENCLAW_CLOSE_PROXY_SNAPSHOT'):
        if _env in os.environ:
            monkeypatch.setenv(_env, os.environ[_env])
        else:
            monkeypatch.delenv(_env, raising=False)

    result = mod._size(100_000.0, 'LOW_VOL', False)

    assert result == {'SPY': 1.0}
    assert calls[0]['signals'] and calls[0]['strategy_state'] == {}
    assert _sizer._check_force_fire_flag() is False
    assert os.environ.get('OPENCLAW_BENCH_RELATIVE_SIZING') is None

    mod._size(100_000.0, 'LOW_VOL', True)
    assert os.environ.get('OPENCLAW_BENCH_RELATIVE_SIZING') == '1'
