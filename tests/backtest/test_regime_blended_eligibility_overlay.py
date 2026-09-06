"""2026-09-06: the gate-B walk-forward takes eligibility from
strategy_regime_params (the live owner) and reads the canonical backtest
tables; the manifest field is only a fallback."""
from __future__ import annotations
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import backtest.regime_blended_backtest as mod  # noqa: E402

COLS = ['strategy_id', 'regime_state', 'sharpe', 'max_dd', 'total_return_pct',
        'trade_count', 'oos_days', 'window_count', 'note', 'declared_regimes']


def _df():
    rows = [
        ('S_LOSER', 'LOW_VOL', -2.0, 0.30, -10.0, 50, 200, 1, None, None),
        ('S_LOSER', 'HIGH_VOL', 1.0, 0.05, 3.0, 40, 100, 1, None, None),
        ('S_STAY', 'LOW_VOL', 1.5, 0.02, 5.0, 80, 200, 1, None, None),
        ('S_STAY', 'HIGH_VOL', 0.5, 0.03, 1.0, 30, 100, 1, None, None),
    ]
    for s in ('S_LOSER', 'S_STAY'):
        for r in ('TRANSITIONING', 'CRISIS'):
            rows.append((s, r, 0.0, 0.0, 0.0, 0, 0, 1, 'not_declared', None))
    return pd.DataFrame(rows, columns=COLS)


def test_params_eligibility_overrides_manifest(tmp_path, monkeypatch):
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'strategies': {'S_LOSER': {'eligible_regimes': ['LOW_VOL', 'HIGH_VOL']},
                                                   'S_STAY': {}}}))
    df = _df(); df.attrs['median_run_at'] = datetime(2026, 8, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(mod, 'load_latest_backtests', lambda uri: (df, 'canonical:2', datetime.now(timezone.utc)))
    # The live params say S_LOSER is eligible in HIGH_VOL only — the blended
    # book drops its LOW_VOL bleed and beats production.
    monkeypatch.setattr(mod, 'load_eligibility_from_params', lambda uri: {'S_LOSER': ['HIGH_VOL']})
    res = mod.run_walkforward('not-used', manifest, tmp_path / 'missing.parquet')
    assert res['eligibility_source'] == 'strategy_regime_params'
    assert res['source'].startswith('strategy_backtest_regimes')
    assert res['median_run_at'] == '2026-08-01T00:00:00+00:00'
    assert res['delta']['sharpe'] > 0 and res['gate_b']['pass'] is True
    assert res['blended']['per_regime']['LOW_VOL']['strategy_count'] == 1


def test_manifest_fallback_when_params_unavailable(tmp_path, monkeypatch):
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'strategies': {'S_LOSER': {'eligible_regimes': ['HIGH_VOL']}, 'S_STAY': {}}}))
    monkeypatch.setattr(mod, 'load_latest_backtests', lambda uri: (_df(), 'canonical:2', datetime.now(timezone.utc)))
    monkeypatch.setattr(mod, 'load_eligibility_from_params', lambda uri: {})
    res = mod.run_walkforward('not-used', manifest, tmp_path / 'missing.parquet')
    assert res['eligibility_source'] == 'manifest'
    assert res['median_run_at'] is None
    assert res['gate_b']['pass'] is True


def test_load_eligibility_fails_open_on_bad_uri():
    assert mod.load_eligibility_from_params('not-a-dsn') == {}
