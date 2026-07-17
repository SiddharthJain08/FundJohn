"""Config resolution for the asset-correlation cluster cap.

The cap's enable + threshold are operator-tunable from the dashboard, so they
must come from `pipeline_config` (read every sizer cycle → a slider change or
kill takes effect next cycle with no johnbot restart), mirroring how
`_load_lambda` resolves `position_sizing_lambda`. Precedence:
  pipeline_config (operator slider)  >  env (legacy OPENCLAW_ASSET_CORR_*)  >  default
The gate FAILS OFF: any DB error or absent config → (enabled-from-env-or-False,
default thr), and the read can never throw out of the sizer hot path. Threshold
is clamped to a sane band so an operator-pasted value can't break clustering.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT))

from execution.regime_blended_sizer import (        # noqa: E402
    _load_asset_corr_cfg,
    _ASSET_CORR_THR_MIN,
    _ASSET_CORR_THR_MAX,
    _ASSET_CORR_THR_DEFAULT,
    _ASSET_CORR_CAP_PCT_MIN,
    _ASSET_CORR_CAP_PCT_MAX,
    _ASSET_CORR_CAP_PCT_DEFAULT,
)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a, **k):
        pass
    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def cursor(self):
        return _FakeCursor(self._rows)


def _patch_db(monkeypatch, rows=None, raises=False):
    """Patch psycopg2.connect to yield `rows` (list of (key, value)) or raise."""
    monkeypatch.setenv('POSTGRES_URI', 'postgresql://fake/db')
    import psycopg2
    if raises:
        def _boom(*a, **k):
            raise RuntimeError('db down')
        monkeypatch.setattr(psycopg2, 'connect', _boom)
    else:
        monkeypatch.setattr(psycopg2, 'connect', lambda *a, **k: _FakeConn(rows or []))


def _clear_env(monkeypatch):
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_CAP', raising=False)
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_THR', raising=False)
    monkeypatch.delenv('OPENCLAW_ASSET_CORR_CAP_PCT', raising=False)


def test_pipeline_config_enables_and_sets_threshold(monkeypatch):
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, rows=[('asset_corr_cap_enabled', '1'),
                                 ('asset_corr_cap_thr', '0.6')])
    enabled, thr, _ = _load_asset_corr_cfg()
    assert enabled is True
    assert thr == pytest.approx(0.6)


def test_threshold_clamped_below_band(monkeypatch):
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, rows=[('asset_corr_cap_enabled', '1'),
                                 ('asset_corr_cap_thr', '0.05')])
    enabled, thr, _ = _load_asset_corr_cfg()
    assert enabled is True
    assert thr == pytest.approx(_ASSET_CORR_THR_MIN)


def test_threshold_clamped_above_band(monkeypatch):
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, rows=[('asset_corr_cap_thr', '2.0')])
    _, thr, _ = _load_asset_corr_cfg()
    assert thr == pytest.approx(_ASSET_CORR_THR_MAX)


def test_env_fallback_when_config_absent(monkeypatch):
    """No pipeline_config rows → legacy env gates apply."""
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP', '1')
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_THR', '0.65')
    _patch_db(monkeypatch, rows=[])
    enabled, thr, _ = _load_asset_corr_cfg()
    assert enabled is True
    assert thr == pytest.approx(0.65)


def test_defaults_fail_off_when_config_and_env_absent(monkeypatch):
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, rows=[])
    enabled, thr, _ = _load_asset_corr_cfg()
    assert enabled is False                      # fail OFF
    assert thr == pytest.approx(_ASSET_CORR_THR_DEFAULT)


def test_db_error_never_throws_and_fails_off(monkeypatch):
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, raises=True)
    enabled, thr, _ = _load_asset_corr_cfg()        # must not raise
    assert enabled is False
    assert thr == pytest.approx(_ASSET_CORR_THR_DEFAULT)


def test_config_disabled_overrides_env_enabled_kill_switch(monkeypatch):
    """Operator kill switch: pipeline_config enabled=0 wins over a stale env gate."""
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP', '1')
    _patch_db(monkeypatch, rows=[('asset_corr_cap_enabled', '0'),
                                 ('asset_corr_cap_thr', '0.6')])
    enabled, _, _ = _load_asset_corr_cfg()
    assert enabled is False


def test_cap_pct_from_pipeline_config(monkeypatch):
    """Per-cluster gross cap (fraction of NAV) is config-driven, 3rd return value."""
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, rows=[('asset_corr_cap_enabled', '1'),
                                 ('asset_corr_cap_pct', '0.20')])
    _, _, cap_pct = _load_asset_corr_cfg()
    assert cap_pct == pytest.approx(0.20)


def test_cap_pct_clamped_above_band(monkeypatch):
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, rows=[('asset_corr_cap_pct', '5.0')])
    _, _, cap_pct = _load_asset_corr_cfg()
    assert cap_pct == pytest.approx(_ASSET_CORR_CAP_PCT_MAX)


def test_cap_pct_clamped_below_band(monkeypatch):
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, rows=[('asset_corr_cap_pct', '0.001')])
    _, _, cap_pct = _load_asset_corr_cfg()
    assert cap_pct == pytest.approx(_ASSET_CORR_CAP_PCT_MIN)


def test_cap_pct_default_when_absent(monkeypatch):
    _clear_env(monkeypatch)
    _patch_db(monkeypatch, rows=[])
    _, _, cap_pct = _load_asset_corr_cfg()
    assert cap_pct == pytest.approx(_ASSET_CORR_CAP_PCT_DEFAULT)


def test_cap_pct_env_fallback_when_config_absent(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv('OPENCLAW_ASSET_CORR_CAP_PCT', '0.18')
    _patch_db(monkeypatch, rows=[])
    _, _, cap_pct = _load_asset_corr_cfg()
    assert cap_pct == pytest.approx(0.18)
