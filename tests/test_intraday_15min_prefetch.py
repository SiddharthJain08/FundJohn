import importlib, os
import pandas as pd

def _reload_features():
    import src.ingestion.intraday_features as f
    return importlib.reload(f)

def test_feature_floor_is_15min_when_flag_on(monkeypatch):
    monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    f = _reload_features()
    ts = pd.Timestamp('2026-06-09 14:07:00', tz='UTC')
    assert f._floor_ts(ts) == pd.Timestamp('2026-06-09 14:00:00', tz='UTC')
    ts2 = pd.Timestamp('2026-06-09 14:22:00', tz='UTC')
    assert f._floor_ts(ts2) == pd.Timestamp('2026-06-09 14:15:00', tz='UTC')

def test_feature_floor_is_5min_when_flag_off(monkeypatch):
    monkeypatch.delenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', raising=False)
    f = _reload_features()
    ts = pd.Timestamp('2026-06-09 14:07:00', tz='UTC')
    assert f._floor_ts(ts) == pd.Timestamp('2026-06-09 14:05:00', tz='UTC')

import importlib.util

def _reload_detector(monkeypatch, flag):
    if flag: monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    else: monkeypatch.delenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', raising=False)
    from pathlib import Path
    path = Path('/root/openclaw/scripts/run_intraday_market_state.py')
    spec = importlib.util.spec_from_file_location('rims', path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def test_uniform_3tick_when_flag_on(monkeypatch):
    m = _reload_detector(monkeypatch, flag=True)
    assert m._tier_for_transition('LOW_VOL', 'HIGH_VOL') == (3, 0.70)
    assert m._tier_for_transition('TRANSITIONING', 'CRISIS') == (3, 0.70)
    assert m._tier_for_transition('CRISIS', 'LOW_VOL') == (3, 0.70)

def test_tiered_when_flag_off(monkeypatch):
    m = _reload_detector(monkeypatch, flag=False)
    assert m._tier_for_transition('LOW_VOL', 'HIGH_VOL') == (2, 0.80)
    assert m._tier_for_transition('TRANSITIONING', 'CRISIS') == (1, 0.90)
