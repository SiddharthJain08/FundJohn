"""2026-11-25 operator ruling: a regime-flip redeploy that lands outside RTH is
deferred to the open and fired by the intraday detector's first live tick in
[09:35, 15:30) ET — never executed as ext-hours limit orders."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _load(monkeypatch, *, prefetch_flag=False):
    if prefetch_flag:
        monkeypatch.setenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', '1')
    else:
        monkeypatch.delenv('OPENCLAW_INTRADAY_15MIN_PREFETCH', raising=False)
    spec = importlib.util.spec_from_file_location('rims_deferred', ROOT / 'scripts' / 'run_intraday_market_state.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


def _et(s):
    return pd.Timestamp(s, tz='America/New_York').tz_convert('UTC')


@pytest.mark.parametrize('ts,expected', [
    ('2026-11-24 09:34', False),   # before the window (opening auction)
    ('2026-11-24 09:35', True),
    ('2026-11-24 12:00', True),
    ('2026-11-24 15:29', True),
    ('2026-11-24 15:30', False),   # clear of the 15:00 compute / 15:50 execute lane
    ('2026-11-24 16:05', False),
    ('2026-11-22 12:00', False),   # Sunday
    ('2026-11-26 12:00', False),   # Thanksgiving (trading_calendar master)
])
def test_deferred_window(monkeypatch, ts, expected):
    m = _load(monkeypatch)
    assert m._deferred_window(_et(ts)) is expected


def _fake_redis(marker):
    r = MagicMock()
    r.get.return_value = json.dumps(marker) if marker is not None else None
    return r


def test_fires_pending_marker_in_window_and_consumes_it(monkeypatch):
    m = _load(monkeypatch)
    marker = {'reason': 'INTRADAY_HMM_LOW_VOL_HIGH_VOL', 'episode': 'ep-1',
              'date': '2026-11-23', 'deferred_at': '2026-11-23T16:02:10-05:00'}
    r = _fake_redis(marker)
    spawns, posts = [], []
    monkeypatch.setattr(m, '_redis', lambda: r)
    monkeypatch.setattr(m, '_spawn_redeploy_cmd',
                        lambda reason, date_str, *, dry_run, episode=None:
                        spawns.append((reason, date_str, dry_run, episode)) or 'spawned')
    monkeypatch.setattr(m, '_post_to_discord', lambda ch, msg: posts.append((ch, msg)))
    tag = m._maybe_fire_deferred_redeploy(_et('2026-11-24 09:45'), dry_run=False)
    assert tag == 'DEFERRED_REDEPLOY_SPAWNED'
    assert spawns == [('INTRADAY_HMM_LOW_VOL_HIGH_VOL_DEFERRED', '2026-11-24', False, None)]
    r.delete.assert_called_once_with(m.DEFERRED_KEY)      # one fire per marker
    assert posts and 'deferred redeploy' in posts[0][1]


def test_nothing_pending_is_a_noop(monkeypatch):
    m = _load(monkeypatch)
    r = _fake_redis(None)
    monkeypatch.setattr(m, '_redis', lambda: r)
    monkeypatch.setattr(m, '_spawn_redeploy_cmd', lambda *a, **k: pytest.fail('must not spawn'))
    assert m._maybe_fire_deferred_redeploy(_et('2026-11-24 10:00'), dry_run=False) is None
    r.delete.assert_not_called()


def test_marker_outside_window_is_left_alone(monkeypatch):
    m = _load(monkeypatch)
    r = _fake_redis({'reason': 'INTRADAY_HMM_LOW_VOL_HIGH_VOL'})
    monkeypatch.setattr(m, '_redis', lambda: r)
    monkeypatch.setattr(m, '_spawn_redeploy_cmd', lambda *a, **k: pytest.fail('must not spawn'))
    assert m._maybe_fire_deferred_redeploy(_et('2026-11-24 16:05'), dry_run=False) is None
    r.get.assert_not_called(); r.delete.assert_not_called()


def test_inflight_redeploy_puts_marker_back(monkeypatch):
    m = _load(monkeypatch, prefetch_flag=True)
    marker = {'reason': 'INTRADAY_HMM_LOW_VOL_HIGH_VOL'}
    r = _fake_redis(marker)
    monkeypatch.setattr(m, '_redis', lambda: r)
    import importlib
    ip = importlib.import_module('src.execution.intraday_prefetch')
    monkeypatch.setattr(ip, 'prefetch_enabled', lambda: True)
    monkeypatch.setattr(ip, 'acquire_inflight', lambda rcli: False)
    monkeypatch.setattr(m, '_spawn_redeploy_cmd', lambda *a, **k: pytest.fail('must not spawn'))
    assert m._maybe_fire_deferred_redeploy(_et('2026-11-24 10:00'), dry_run=False) is None
    r.delete.assert_called_once_with(m.DEFERRED_KEY)
    restored = [c for c in r.set.call_args_list if c.args and c.args[0] == m.DEFERRED_KEY]
    assert len(restored) == 1 and json.loads(restored[0].args[1])['reason'] == marker['reason']


def test_confirmed_transition_spawn_still_builds_reason(monkeypatch):
    m = _load(monkeypatch)
    seen = []
    monkeypatch.setattr(m, '_spawn_redeploy_cmd',
                        lambda reason, date_str, *, dry_run, episode=None:
                        seen.append((reason, date_str, dry_run, episode)) or 'spawned')
    assert m._spawn_redeploy('LOW_VOL', 'HIGH_VOL', '2026-11-24', dry_run=True, episode='ep') == 'spawned'
    assert seen == [('INTRADAY_HMM_LOW_VOL_HIGH_VOL', '2026-11-24', True, 'ep')]
