from __future__ import annotations
import datetime as dt
import json, logging
from pathlib import Path
import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def _inputs(tmp_path):
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    chain['date'] = pd.to_datetime(chain['date']); chain['expiry'] = pd.to_datetime(chain['expiry'])
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    px = pd.DataFrame([{'ticker': t, 'date': pd.Timestamp(d), 'close': c}
                       for t, m in meta['closes'].items() for d, c in m.items()])
    master = tmp_path / 'master'; master.mkdir()
    # 30 prior sessions of surface history so iv_rank is computable
    hist = pd.DataFrame([{'ticker': t, 'date': d.date(), 'iv30': 0.20 + 0.001 * i, 'pc_ratio': 1.0,
                          'options_features_version': 2}
                         for t in ('SPY', 'AAPL', 'XOM') for i, d in enumerate(pd.bdate_range('2026-07-20', '2026-09-02'))])
    hist.to_parquet(master / 'options_surface.parquet', index=False)
    return chain, px, master


def test_build_returns_v2_keys_with_iv_rank(tmp_path):
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    out = v2.build(chain, ['SPY', 'AAPL', 'XOM', 'ZZZT'], pd.Timestamp('2026-09-03'), master, px)
    assert set(out) == {'SPY', 'AAPL', 'XOM'}
    spy = out['SPY']
    assert spy['options_features_version'] == 3 and 0.08 < spy['iv30'] < 0.20
    assert spy['iv_rank'] is not None and 0 <= spy['iv_rank'] <= 100
    assert spy['last_price'] == pytest.approx(px[(px.ticker == 'SPY') & (px.date == '2026-09-03')]['close'].iloc[0])
    assert isinstance(spy['hv20_history'], list) and spy['rv_20'] > 0
    for k in ('gamma_atm', 'theta_atm', 'pc_ratio', 'vrp', 'expiry_date', 'earnings_dte'):
        assert k in spy


def test_master_row_precedence(tmp_path):
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    m = pd.read_parquet(master / 'options_surface.parquet')
    m = pd.concat([m, pd.DataFrame([{'ticker': 'SPY', 'date': pd.Timestamp('2026-09-03').date(), 'iv30': 0.777,
                                     'pc_ratio': 1.0, 'options_features_version': 3, 'iv30_source': 'atm_band'}])],
                  ignore_index=True)
    m.to_parquet(master / 'options_surface.parquet', index=False)
    out = v2.build(chain, ['SPY'], pd.Timestamp('2026-09-03'), master, px)
    assert out['SPY']['iv30'] == pytest.approx(0.777)
    # amendment 2026-09-06 §H (fix round 1): the master's iv30 wins, so its
    # provenance must win too — never the fresh live fit's iv30_source.
    assert out['SPY']['iv30_source'] == 'atm_band'


def test_master_row_precedence_without_iv30_source_column(tmp_path):
    """An older master vintage without the iv30_source column still loads
    (load_history schema-checks first) and precedence still picks up iv30;
    iv30_source is served None rather than left as the fresh live fit's label."""
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    m = pd.read_parquet(master / 'options_surface.parquet')
    assert 'iv30_source' not in m.columns          # the fixture's base history predates the column
    m = pd.concat([m, pd.DataFrame([{'ticker': 'SPY', 'date': pd.Timestamp('2026-09-03').date(), 'iv30': 0.777,
                                     'pc_ratio': 1.0, 'options_features_version': 2}])], ignore_index=True)
    assert 'iv30_source' not in m.columns
    m.to_parquet(master / 'options_surface.parquet', index=False)
    out = v2.build(chain, ['SPY'], pd.Timestamp('2026-09-03'), master, px)
    assert out['SPY']['iv30'] == pytest.approx(0.777)
    assert out['SPY']['iv30_source'] is None


def test_build_populates_oi_from_cboe_session(tmp_path, monkeypatch):
    """Task 14 note 3: options_aux_v2.build() threads oi_features_for_ticker
    through, resolving master_dir/cboe_chains when OPENCLAW_CBOE_CHAINS_ROOT
    is unset (spec 2026-09-04 Part B)."""
    from execution import options_aux_v2 as v2
    from strategies import options_oi as oi
    chain, px, master = _inputs(tmp_path)
    root = master / 'cboe_chains'; root.mkdir()
    rows = [{'date': dt.date(2026, 9, 2), 'underlying': 'SPY', 'expiry': dt.date(2026, 9, 18), 'option_type': t,
             'strike': k, 'open_interest': o, 'iv': 0.12, 'delta': d, 'gamma': 0.01, 'vega': 0.5, 'underlying_price': 640.0}
            for k, o, d, t in ((630.0, 1000.0, 0.6, 'C'), (640.0, 2000.0, 0.5, 'C'), (650.0, 500.0, 0.4, 'C'),
                               (630.0, 900.0, -0.4, 'P'), (640.0, 2200.0, -0.5, 'P'), (650.0, 300.0, -0.6, 'P'))]
    pd.DataFrame(rows).to_parquet(root / 'date=2026-09-02.parquet', index=False)
    monkeypatch.delenv('OPENCLAW_CBOE_CHAINS_ROOT', raising=False)
    oi.clear_cache()
    try:
        out = v2.build(chain, ['SPY', 'AAPL', 'XOM'], pd.Timestamp('2026-09-03'), master, px)
        spy = out['SPY']
        assert spy['oi_session'] == '2026-09-02' and spy['max_pain'] == 640.0 and spy['gex'] is not None
        assert spy['open_interest_by_strike']
        aapl = out['AAPL']                                  # no CBOE rows for AAPL in this fixture
        assert aapl['gex'] is None and aapl['open_interest_by_strike'] == {}
    finally:
        oi.clear_cache()


def test_shadow_summary_line():
    from execution import options_aux_v2 as v2
    old = {'A': {'iv30': 0.40, 'iv_rank': 50.0}, 'B': {'iv30': 0.50, 'iv_rank': 50.0}}
    new = {'A': {'iv30': 0.20, 'iv_rank': 33.0}, 'B': {'iv30': 0.25, 'iv_rank': None}}
    line = v2.shadow_summary(old, new)
    assert line.startswith('[options_surface] shadow n=2 iv30 old/new median=2.000') and 'iv_rank_nonnull=50%' in line and 'version=3' in line
    # Final fix wave 2026-09-05 (F1/F4c): the line carries the fields the
    # runbook's "clean shadow" definition reads. Called without `seconds`
    # (as here and by any legacy caller) dur is n/a, never a crash.
    assert 'rv20_nonnull=0%' in line and 'vrp_nonnull=0%' in line
    assert 'spot_stale=0%' in line and 'dur=n/a' in line
    assert 'mfiv_nonnull=0%' in line and 'rn_nonnull=0%' in line   # v3 coverage fields (spec 2026-09-06 §C.3)
    rich = {'A': {'iv30': 0.20, 'iv_rank': 33.0, 'rv_20': 0.1, 'vrp': 0.1,
                  'spot_date': '2026-09-02', 'surface_date': '2026-09-03'},
            'B': {'iv30': 0.25, 'iv_rank': None, 'rv_20': 0.1, 'vrp': None,
                  'spot_date': '2026-09-03', 'surface_date': '2026-09-03'}}
    assert 'iv30_src smile=0% band=0%' in line          # §H.5 split; no iv30_source ⇒ 0/0, never fabricated
    line2 = v2.shadow_summary(old, rich, seconds=12.4)
    assert 'rv20_nonnull=100%' in line2 and 'vrp_nonnull=50%' in line2
    assert 'spot_stale=50%' in line2 and 'dur=12s' in line2
    # Final review I2: the v3 coverage denominator is tickers with
    # n_expiries_fit >= 2 (spec §C.3), not every ticker — C has one fit and
    # must not dilute the ratio.
    cov = {'A': {'iv30': 0.20, 'n_expiries_fit': 2, 'mfiv_30d': 0.21, 'rn_skew_30d': -0.4},
           'B': {'iv30': 0.25, 'n_expiries_fit': 2},
           'C': {'iv30': 0.30, 'n_expiries_fit': 1, 'mfiv_30d': None}}
    line3 = v2.shadow_summary(old, cov)
    assert 'mfiv_nonnull=50%' in line3 and 'rn_nonnull=50%' in line3
    # Final review I3: iv30_source split over the tickers that HAVE an iv30.
    split = {'A': {'iv30': 0.20, 'iv30_source': 'smile'},
             'B': {'iv30': 0.25, 'iv30_source': 'atm_band'},
             'C': {'iv30': 0.30, 'iv30_source': 'atm_band'},
             'D': {'iv30': 0.30, 'iv30_source': None},
             'E': {'iv30': None, 'iv30_source': 'smile'}}
    line4 = v2.shadow_summary(old, split)
    assert 'iv30_src smile=25% band=50%' in line4       # 4 tickers with iv30; D's None is neither
    assert 'rn_nonnull=0%' in line4                     # existing fields keep their spacing


def test_engine_flag_selects_dict(monkeypatch, caplog, tmp_path):
    from execution import engine, options_aux_v2 as v2
    old = {'SPY': {'iv30': 0.4, 'iv_rank': 50.0}}
    new = {'SPY': {'iv30': 0.12, 'iv_rank': 40.0, 'options_features_version': 2}}
    monkeypatch.setattr(v2, 'build', lambda *a, **k: new)
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE', raising=False)
    monkeypatch.setenv('OPENCLAW_SHADOW_LOG_DIR', str(tmp_path))
    with caplog.at_level(logging.INFO):
        assert engine._apply_options_surface(old, None, [], None, None, None) is old
    assert any('[options_surface] shadow' in r.message for r in caplog.records)
    # The daily-cycle step log keeps only the last 4,000 chars, which drops
    # this line before it reaches disk (brief 2026-09-06). lib.shadow_log
    # gives it a durable, dedicated sink the flip gate reads instead.
    shadow_log_path = tmp_path / 'options_surface_shadow.log'
    assert shadow_log_path.exists()
    lines = shadow_log_path.read_text().splitlines()
    assert len(lines) == 1
    assert '[options_surface] shadow n=1' in lines[0]
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE', '1')
    assert engine._apply_options_surface(old, None, [], None, None, None) is new


def test_shadow_summary_failure_does_not_drop_options(monkeypatch, caplog):
    from execution import engine, options_aux_v2 as v2
    old = {'SPY': {'iv30': 0.4, 'iv_rank': 50.0}}
    monkeypatch.setattr(v2, 'build', lambda *a, **k: {'SPY': {'iv30': 0.12}})
    # *a/**k: the engine now passes seconds=, and a TypeError here would make
    # this test pass for the wrong reason.
    monkeypatch.setattr(v2, 'shadow_summary', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE', raising=False)
    with caplog.at_level(logging.WARNING):
        assert engine._apply_options_surface(old, None, [], None, None, None) is old
    assert any('shadow summary failed' in r.message for r in caplog.records)


# ── Final fix wave 2026-09-05, F2: the v2 IMPORT is inside the guard ──
def test_import_failure_serves_legacy_dict(monkeypatch, caplog):
    """A module-level failure in options_aux_v2 (bad dependency, half-applied
    deploy) must degrade to the legacy dict exactly like a build failure.

    Both the sys.modules sentinel AND the parent-package attribute have to go:
    `from execution import options_aux_v2` finds the already-imported submodule
    as an attribute of `execution` before it ever consults sys.modules."""
    import sys
    from execution import engine
    old = {'SPY': {'iv30': 0.4, 'iv_rank': 50.0}}
    monkeypatch.delattr(sys.modules['execution'], 'options_aux_v2', raising=False)
    monkeypatch.setitem(sys.modules, 'execution.options_aux_v2', None)
    with caplog.at_level(logging.WARNING):
        assert engine._apply_options_surface(old, None, [], None, None, None) is old
    assert any('v2 build failed' in r.message for r in caplog.records)


def test_import_is_restored_after_the_guarded_failure():
    """The guard must not leave the module unimportable for the next cycle."""
    from execution import engine, options_aux_v2 as v2
    old = {'SPY': {'iv30': 0.4, 'iv_rank': 50.0}}
    assert callable(v2.build)
    # opts=None -> build() returns {} -> the helper still serves `old` (flag off)
    assert engine._apply_options_surface(old, None, [], '2026-09-03', None, None) is old


# ── Final fix wave 2026-09-05, F4c: shadow-mode wall-clock budget ──
def test_budget_stops_the_shadow_build_and_warns(tmp_path, monkeypatch, caplog):
    """Budget 0 with the flag OFF: the dict is diagnostic only, so the loop
    stops after the first ticker and returns a partial dict. groupby yields
    sorted keys, so the survivor is deterministic."""
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    chain = chain[chain['ticker'].isin(['AAPL', 'SPY'])]
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE', raising=False)
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_BUDGET_S', '0')
    with caplog.at_level(logging.WARNING):
        out = v2.build(chain, ['SPY', 'AAPL'], pd.Timestamp('2026-09-03'), master, px)
    assert set(out) == {'AAPL'}                       # 1/2, the first sorted ticker
    assert any('budget 0s exceeded after 1/2 tickers — partial shadow' in r.message
               for r in caplog.records)


def test_budget_with_flag_on_runs_to_completion_and_warns_once(tmp_path, monkeypatch, caplog):
    """Flag ON: the dict is load-bearing, so the budget only warns — once."""
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    chain = chain[chain['ticker'].isin(['AAPL', 'SPY', 'XOM'])]
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE', '1')
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_BUDGET_S', '0')
    with caplog.at_level(logging.WARNING):
        out = v2.build(chain, ['SPY', 'AAPL', 'XOM'], pd.Timestamp('2026-09-03'), master, px)
    assert set(out) == {'SPY', 'AAPL', 'XOM'}
    warns = [r for r in caplog.records if 'budget' in r.message and 'running to completion' in r.message]
    assert len(warns) == 1


def test_budget_seconds_defaults_and_survives_garbage(monkeypatch):
    from execution import options_aux_v2 as v2
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE_BUDGET_S', raising=False)
    assert v2.budget_seconds() == v2.DEFAULT_BUDGET_S
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_BUDGET_S', 'not-a-number')
    assert v2.budget_seconds() == v2.DEFAULT_BUDGET_S
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_BUDGET_S', '90')
    assert v2.budget_seconds() == 90.0


def test_build_records_spot_date_per_ticker(tmp_path, monkeypatch):
    """F1: the live spot is the LAST KNOWN close, and its date is reported so
    the shadow line can count the stale ones."""
    from execution import options_aux_v2 as v2
    chain, px, master = _inputs(tmp_path)
    monkeypatch.delenv('OPENCLAW_OPTIONS_SURFACE_BUDGET_S', raising=False)
    out = v2.build(chain, ['SPY'], pd.Timestamp('2026-09-03'), master, px)
    assert out['SPY']['spot_date'] == '2026-09-03' and out['SPY']['surface_date'] == '2026-09-03'
    # Same chain, but prices end at T-1 (the 15:00 ET intraday-overlay shape).
    stale_px = px[px['date'] < pd.Timestamp('2026-09-03')]
    out2 = v2.build(chain, ['SPY'], pd.Timestamp('2026-09-03'), master, stale_px)
    spy = out2['SPY']
    assert spy['spot_date'] < spy['surface_date']
    assert spy['rv_20'] is not None and spy['vrp'] is not None    # F1: not dropped
    assert 'spot_stale=100%' in v2.shadow_summary({}, out2, seconds=1.0)
