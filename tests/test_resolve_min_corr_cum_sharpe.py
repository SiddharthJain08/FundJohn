from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest  # noqa: E402
from execution import regime_blended_sizer as rbs  # noqa: E402


def test_uses_per_regime_value_in_bounds():
    assert rbs._resolve_min_corr_cum_sharpe({'min_corr_cum_sharpe': 0.7}) == pytest.approx(0.7)


def test_clamps_to_bounds():
    assert rbs._resolve_min_corr_cum_sharpe({'min_corr_cum_sharpe': -5.0}) == pytest.approx(0.0)
    assert rbs._resolve_min_corr_cum_sharpe({'min_corr_cum_sharpe': 99.0}) == pytest.approx(10.0)


def test_missing_param_falls_back_to_default_when_no_db(monkeypatch):
    # No POSTGRES_URI -> DB lookup fails -> default.
    monkeypatch.delenv('POSTGRES_URI', raising=False)
    assert rbs._resolve_min_corr_cum_sharpe({}, default=1.25) == pytest.approx(1.25)
    assert rbs._resolve_min_corr_cum_sharpe(None, default=1.25) == pytest.approx(1.25)
