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
