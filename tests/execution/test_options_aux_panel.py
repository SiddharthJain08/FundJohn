"""options_aux_panel — the live options aux served from the surface master
(spec docs/specs/2026-10-27-options-aux-panel-source-spec.md).

Parity contract: for the same ticker/day the panel build equals
options_aux_v2.build on the raw chain, because the master row was written by
the same features_for_day call the raw path makes."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'
TODAY = pd.Timestamp('2026-09-03')
TICKERS = ['SPY', 'AAPL', 'XOM']


def _chain():
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    chain['date'] = pd.to_datetime(chain['date'])
    chain['expiry'] = pd.to_datetime(chain['expiry'])
    return chain


def _px():
    with open(FIX / 'options_chain_2026-09-03_spots.json') as fh:
        meta = json.load(fh)
    return pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c}
                         for t, m in meta['closes'].items() for d, c in m.items()])


def _hist_rows():
    return [{'ticker': t, 'date': d.date(), 'iv30': 0.20 + 0.001 * i, 'pc_ratio': 1.0,
             'options_features_version': 3, 'iv30_source': 'smile'}
            for t in TICKERS for i, d in enumerate(pd.bdate_range('2026-07-20', '2026-09-02'))]


def _master_with_today(tmp_path, chain, px):
    """History + a row for TODAY built by features_for_day on the raw chain —
    exactly what scripts/build_options_surface.py writes."""
    from strategies.options_surface import features_for_day, prepare_chain
    rows = _hist_rows()
    for t in TICKERS:
        g = chain[chain.ticker == t]
        spot = float(px[(px.ticker == t) & (px.date <= TODAY)].sort_values('date')['close'].iloc[-1])
        r = features_for_day(prepare_chain(g, TODAY), spot, TODAY)
        r.update({'ticker': t, 'date': TODAY.date()})
        rows.append(r)
    master = tmp_path / 'master'
    master.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_parquet(master / 'options_surface.parquet', index=False)
    return master


def _same(a, b):
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return a == pytest.approx(b, rel=1e-9, abs=1e-12)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k]) for k in a)
    try:
        if a is None or b is None:
            return a is b or (isinstance(a, float) and math.isnan(a)) or (isinstance(b, float) and math.isnan(b))
        return bool(a == b) or (float(a) == pytest.approx(float(b), rel=1e-9))
    except Exception:  # noqa: BLE001
        return a == b


def test_source_flag_reads_env(monkeypatch):
    from execution import options_aux_panel as panel
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE_SOURCE', raising=False)
    assert panel.panel_source_enabled() is False
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_SOURCE', 'raw')
    assert panel.panel_source_enabled() is False
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_SOURCE', 'panel')
    assert panel.panel_source_enabled() is True


def test_master_row_path_matches_v2_on_the_raw_chain(tmp_path, monkeypatch):
    """No overlay: the panel serves TODAY's master row; v2 recomputes it from
    the raw chain. Every served key must agree."""
    from execution import options_aux_panel as panel, options_aux_v2 as v2
    monkeypatch.delenv('OPENCLAW_INTRADAY_AUX', raising=False)
    chain, px = _chain(), _px()
    master = _master_with_today(tmp_path, chain, px)
    ref = v2.build(chain, TICKERS + ['ZZZT'], TODAY, master, px)
    out = panel.build(TICKERS + ['ZZZT'], TODAY, master, px, None, overlay=None)
    assert set(out) == set(ref) == set(TICKERS)
    for t in TICKERS:
        a, b = out[t], ref[t]
        assert a['surface_date'] == b['surface_date'] == '2026-09-03'
        assert a['spot_date'] == b['spot_date']
        missing = set(b) - set(a)
        assert not missing, f'{t}: panel lacks {sorted(missing)}'
        bad = [k for k in b if not _same(a[k], b[k])]
        assert not bad, f'{t}: mismatch on {bad}: ' + ', '.join(f'{k} panel={a[k]!r} v2={b[k]!r}' for k in bad[:5])
        assert a['iv_rank'] is not None and 0 <= a['iv_rank'] <= 100


def test_overlay_path_matches_v2_on_the_spliced_overlay(tmp_path, monkeypatch):
    """Overlay day (15:00 ET): the panel computes today's row from the overlay
    rows exactly as v2 does on the spliced chain; history comes from the master."""
    from execution import options_aux_panel as panel, options_aux_v2 as v2
    chain, px = _chain(), _px()
    master = tmp_path / 'master'
    master.mkdir()
    pd.DataFrame(_hist_rows()).to_parquet(master / 'options_surface.parquet', index=False)
    ref = v2.build(chain, TICKERS, TODAY, master, px)
    out = panel.build(TICKERS, TODAY, master, px, None, overlay=chain)
    assert set(out) == set(ref)
    for t in TICKERS:
        a, b = out[t], ref[t]
        assert a['surface_date'] == '2026-09-03'
        bad = [k for k in b if not _same(a[k], b[k])]
        assert not bad, f'{t}: mismatch on {bad}'


def test_without_overlay_and_without_master_rows_ticker_is_absent(tmp_path):
    from execution import options_aux_panel as panel
    master = tmp_path / 'master'
    master.mkdir()
    pd.DataFrame(_hist_rows()).to_parquet(master / 'options_surface.parquet', index=False)
    out = panel.build(['SPY', 'NOPE'], TODAY, master, _px(), None, overlay=None)
    assert 'NOPE' not in out
    # SPY has history but no row for TODAY → the latest prior row is served
    assert out['SPY']['surface_date'] == '2026-09-02'
    assert out['SPY']['near_iv'] == out['SPY']['iv30']


def test_missing_master_serves_nothing(tmp_path, caplog):
    from execution import options_aux_panel as panel
    (tmp_path / 'master').mkdir()
    with caplog.at_level(logging.WARNING):
        out = panel.build(TICKERS, TODAY, tmp_path / 'master', _px(), None, overlay=None)
    assert out == {}
    assert any('surface master' in r.message and 'missing' in r.message for r in caplog.records)


# ── engine wiring ─────────────────────────────────────────────────────────────
def test_engine_serves_from_panel_only_when_both_flags_say_so(monkeypatch):
    from execution import engine
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE', raising=False)
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE_SOURCE', raising=False)
    assert engine._options_served_from_panel() is False
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_SOURCE', 'panel')
    assert engine._options_served_from_panel() is False        # shadow only
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE', '1')
    assert engine._options_served_from_panel() is True
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_SOURCE', 'raw')
    assert engine._options_served_from_panel() is False        # v2 live, raw source


def test_engine_shadow_uses_panel_build_and_ignores_the_sameday_skip(monkeypatch, caplog, tmp_path):
    """source=panel: the shadow line compares legacy against the panel build,
    even on the 15:00 ET compute where the raw v2 shadow is skipped."""
    from execution import engine, options_aux_panel as panel, options_aux_v2 as v2
    old = {'SPY': {'iv30': 0.4, 'iv_rank': 50.0}}
    new = {'SPY': {'iv30': 0.12, 'iv_rank': 40.0, 'options_features_version': 3}}
    calls = {'panel': 0, 'v2': 0}
    monkeypatch.setattr(panel, 'build', lambda *a, **k: (calls.__setitem__('panel', calls['panel'] + 1), new)[1])
    monkeypatch.setattr(panel, 'load_today_overlay', lambda *a, **k: None)
    monkeypatch.setattr(v2, 'build', lambda *a, **k: (calls.__setitem__('v2', calls['v2'] + 1), new)[1])
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_SOURCE', 'panel')
    monkeypatch.setenv('OPENCLAW_SIGNALS_SKIP_SHADOW_OPTIONS', '1')
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE', raising=False)
    monkeypatch.setenv('OPENCLAW_SHADOW_LOG_DIR', str(tmp_path))
    with caplog.at_level(logging.INFO):
        served = engine._apply_options_surface(old, None, ['SPY'], TODAY, tmp_path, None)
    assert served is old
    assert calls == {'panel': 1, 'v2': 0}
    assert any('[options_surface] shadow n=1' in r.message for r in caplog.records)
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE', '1')
    assert engine._apply_options_surface(old, None, ['SPY'], TODAY, tmp_path, None) is new


def test_engine_live_panel_path_never_opens_the_raw_window(monkeypatch, tmp_path, caplog):
    from execution import engine, options_aux_panel as panel
    served = {'SPY': {'iv30': 0.1}}
    monkeypatch.setattr(panel, 'build', lambda *a, **k: served)
    monkeypatch.setattr(panel, 'load_today_overlay', lambda *a, **k: None)
    monkeypatch.setattr(engine, '_load_options_window',
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError('raw window opened')))
    monkeypatch.setenv('OPENCLAW_SHADOW_LOG_DIR', str(tmp_path))
    out = engine._serve_options_from_panel(['SPY'], TODAY, tmp_path)
    assert out is served
    src = (ROOT / 'src' / 'execution' / 'engine.py').read_text()
    assert "if _need('options') and _options_served_from_panel():" in src
    assert "elif _need('options') and opts_path.exists():" in src
