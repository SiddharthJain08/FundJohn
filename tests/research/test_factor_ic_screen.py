"""D1 — rank-IC / quantile / turnover screen (spec 2026-09-12 §4 D1).

Synthetic frames only. compute_ic_screen is pure — it takes a factor panel and
a close panel and never touches parquet. run_ic_screen's price read is
monkeypatched at factor_prescreen.load_price_window in the one test that
exercises it.

Run only this file:
  cd /root/openclaw/.claude/worktrees/qd-adoptions && \
    nice -n 19 python3 -m pytest tests/research/test_factor_ic_screen.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from research import factor_ic_screen as fis  # noqa: E402

# Pinned so the pure-noise assertion is deterministic. mean-IC over N
# rebalances has SE ~ 1/sqrt((K-1)*N); at K=300, N~100 that is ~0.0058, so
# |IC| < 0.01 is ~1.7 sigma. Verified empirically in Step 4 — if a pandas/numpy
# upgrade moves the draw, sweep SEED over 1..20 and re-pin, never loosen the
# threshold (it is the spec's).
SEED     = 7
N_DAYS   = 520
N_TICKER = 300


def _panel():
    rng = np.random.default_rng(SEED)
    idx = pd.bdate_range(end='2026-08-21', periods=N_DAYS)
    cols = [f'ZZT{i:03d}' for i in range(N_TICKER)]
    rets = rng.normal(0.0004, 0.014, size=(N_DAYS, N_TICKER))
    closes = pd.DataFrame(100.0 * np.exp(np.cumsum(rets, axis=0)), index=idx, columns=cols)
    return closes, rng


def _predictive_factor(closes, rng, sign=1.0, noise=1.0):
    """Next-5-session return plus noise — a deliberately cheating factor."""
    fwd = fis.forward_returns(closes, 5)
    return sign * fwd + rng.normal(0.0, noise * 0.02, size=fwd.shape)


# ── pure helpers ──────────────────────────────────────────────────────────────

def test_round_trip_cost_is_two_way():
    assert fis.round_trip_cost(10.0) == pytest.approx(0.0020)


def test_forward_returns_are_strictly_forward():
    idx = pd.bdate_range(end='2026-01-30', periods=10)
    closes = pd.DataFrame({'A': np.arange(10, dtype=float) + 100.0}, index=idx)
    fwd = fis.forward_returns(closes, 2)
    assert fwd['A'].iloc[0] == pytest.approx((102.0 - 100.0) / 100.0)
    assert pd.isna(fwd['A'].iloc[-1]) and pd.isna(fwd['A'].iloc[-2])


def test_rebalance_dates_space_by_sessions_not_list_positions():
    idx = pd.bdate_range(end='2026-06-30', periods=60)
    cols = [f'T{i}' for i in range(30)]
    factor = pd.DataFrame(np.nan, index=idx, columns=cols)
    factor.iloc[::5] = 1.0                      # populated every 5th session
    factor.iloc[::5] += np.arange(30) * 0.01    # non-degenerate cross-section
    picked = fis.rebalance_dates(factor, idx, horizon=21, step=5)
    positions = [idx.get_loc(d) for d in picked]
    assert all(b - a >= 21 for a, b in zip(positions, positions[1:]))
    picked5 = fis.rebalance_dates(factor, idx, horizon=5, step=5)
    assert len(picked5) > len(picked)


# ── verdicts ──────────────────────────────────────────────────────────────────

def test_predictive_factor_scores_pass():
    closes, rng = _panel()
    factor = _predictive_factor(closes, rng)
    res = fis.compute_ic_screen(factor, closes)
    assert res['verdict'] == 'pass', res
    assert res['ic']['5'] > 0.25
    assert res['icir']['5'] > 0.30
    assert res['ls_q5q1'] > res['cost_per_rebalance']
    assert res['monotonic'] is True


def test_pure_noise_scores_flat():
    closes, rng = _panel()
    factor = pd.DataFrame(rng.normal(0.0, 1.0, size=closes.shape),
                          index=closes.index, columns=closes.columns)
    res = fis.compute_ic_screen(factor, closes)
    assert abs(res['ls_q5q1']) < res['cost_per_rebalance'], res
    assert abs(res['ic']['5']) < 0.01, res
    assert res['verdict'] == 'flat'
    assert res['reason'] == 'ic_below_noise_and_ls_below_cost'


def test_reversed_factor_scores_negative_ic():
    closes, rng = _panel()
    factor = _predictive_factor(closes, rng, sign=-1.0)
    res = fis.compute_ic_screen(factor, closes)
    assert res['ic']['5'] < -0.25, res
    assert res['ls_q5q1'] < 0
    assert res['verdict'] != 'flat'


def test_thin_cross_section_scores_skipped_not_flat():
    """A long-only decile strategy: 12 names, 2 distinct values. Must NOT be
    scored flat — that would false-block the whole decile class."""
    closes, _rng = _panel()
    factor = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    picks = list(closes.columns[:12])
    factor.loc[:, picks[:6]] = 1.0
    factor.loc[:, picks[6:]] = 0.6
    res = fis.compute_ic_screen(factor, closes)
    assert res['verdict'] == 'skipped'
    assert res['reason'] == 'insufficient_cross_section'


def test_low_icir_scores_weak():
    """Enough cross-section and a non-trivial spread, but an IC series whose
    sign flips constantly."""
    closes, rng = _panel()
    fwd = fis.forward_returns(closes, 5)
    flip = pd.Series(np.where(np.arange(len(closes)) % 10 < 5, 1.0, -1.0), index=closes.index)
    factor = fwd.mul(flip, axis=0) + rng.normal(0.0, 0.02, size=fwd.shape)
    res = fis.compute_ic_screen(factor, closes)
    assert res['verdict'] in ('weak', 'flat'), res
    if res['verdict'] == 'weak':
        assert abs(res['icir']['5']) < 0.30
        assert res['reason'] == 'icir_below_threshold'


def test_output_shape_carries_every_spec_field():
    closes, rng = _panel()
    res = fis.compute_ic_screen(_predictive_factor(closes, rng), closes)
    for key in ('ic', 'icir', 'ic_half', 'rank_ac', 'ls_q5q1', 'monotonic',
                'turnover', 'cost_per_rebalance', 'quintile_means',
                'n_rebalances', 'n_qualifying_rebalances', 'verdict', 'reason'):
        assert key in res, f'missing {key}'
    assert set(res['ic']) == {'5', '10', '21'}
    assert set(res['icir']) == {'5', '10', '21'}
    assert set(res['ic_half']) == {'first', 'second'}
    import json
    json.dumps(res)   # must be JSON-serialisable for the orchestrator


# ── factor_from_signals ───────────────────────────────────────────────────────

def test_factor_from_signals_signs_and_magnitudes():
    from strategies.base import Signal
    dates = list(pd.bdate_range(end='2026-01-30', periods=2))
    universe = ['A', 'B', 'C']
    daily = [
        [Signal(ticker='A', direction='LONG', entry_price=10.0, stop_loss=9.0,
                target_1=11.0, target_2=12.0, target_3=13.0, confidence='HIGH',
                position_size_pct=0.02)],
        [Signal(ticker='B', direction='SHORT', entry_price=20.0, stop_loss=22.0,
                target_1=18.0, target_2=17.0, target_3=16.0, confidence='LOW',
                position_size_pct=0.0)],
    ]
    f = fis.factor_from_signals(daily, dates, universe)
    assert f.loc[dates[0], 'A'] == pytest.approx(0.02)
    assert f.loc[dates[1], 'B'] == pytest.approx(-0.3)   # LOW weight, no size
    assert pd.isna(f.loc[dates[0], 'C'])


def test_run_ic_screen_reads_prices_only_via_load_price_window(monkeypatch, tmp_path):
    """The one sanctioned reader must be the only one called."""
    import textwrap
    from backtest import factor_prescreen as fp

    closes, _rng = _panel()
    calls = []

    def _fake_loader(days, max_tickers, lookback=None):
        calls.append((days, max_tickers, lookback))
        return closes, list(closes.columns), 'fallback'

    monkeypatch.setattr(fp, 'load_price_window', _fake_loader)

    strat = tmp_path / 'zz_ic.py'
    strat.write_text(textwrap.dedent('''
        from typing import List
        from strategies.base import BaseStrategy, Signal

        class ZzIc(BaseStrategy):
            id = 'zz_ic'
            name = 'ZzIc'
            description = 'ic screen fixture'
            min_lookback = 20

            def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
                if prices is None or len(prices) < 30:
                    return []
                mom = prices.iloc[-1] / prices.iloc[-21] - 1.0
                picks = mom.dropna().nlargest(40)
                return [Signal(ticker=t, direction='LONG', entry_price=float(prices[t].iloc[-1]),
                               stop_loss=float(prices[t].iloc[-1]) * 0.95,
                               target_1=float(prices[t].iloc[-1]) * 1.05,
                               target_2=float(prices[t].iloc[-1]) * 1.10,
                               target_3=float(prices[t].iloc[-1]) * 1.20,
                               confidence='MED', position_size_pct=float(v))
                        for t, v in picks.items()]
    '''))

    res = fis.run_ic_screen(str(strat), sessions=200, max_tickers=300, step=5)
    assert calls, 'load_price_window must be the price source'
    assert calls[0][0] == 200
    assert res['verdict'] in ('pass', 'weak', 'flat', 'skipped')
