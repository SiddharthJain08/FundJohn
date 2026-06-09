import importlib.util
from pathlib import Path

def _load(monkeypatch, flag):
    if flag: monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    else: monkeypatch.delenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', raising=False)
    spec = importlib.util.spec_from_file_location(
        'rims', Path('/root/openclaw/scripts/run_intraday_market_state.py'))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def test_redeploy_cooldown_ignored_when_flag_on(monkeypatch):
    m = _load(monkeypatch, flag=True)
    assert m._cooldown_active({'redeploy:cooldown:2026-06-09': '1'}, '2026-06-09') is False
    assert m._cooldown_active({'liquidate:cooldown:2026-06-09': '1'}, '2026-06-09') is True

def test_both_cooldowns_block_when_flag_off(monkeypatch):
    m = _load(monkeypatch, flag=False)
    assert m._cooldown_active({'redeploy:cooldown:2026-06-09': '1'}, '2026-06-09') is True
    assert m._cooldown_active({'liquidate:cooldown:2026-06-09': '1'}, '2026-06-09') is True
