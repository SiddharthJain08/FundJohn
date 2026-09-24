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


def test_forward_returns_replaces_inf_with_nan_on_zero_close():
    """Ruling R2: a 0 close makes close[t+H]/close[t] a division by zero
    (+/-inf). +/-inf is not valid JSON (allow_nan=False raises on it), so it
    must collapse to NaN here — the same 'no opinion' value used everywhere
    else — not propagate as a raw inf."""
    idx = pd.bdate_range(end='2026-01-30', periods=10)
    closes = pd.DataFrame({'A': np.arange(10, dtype=float) + 100.0}, index=idx)
    closes.iloc[3, 0] = 0.0
    fwd = fis.forward_returns(closes, 2)
    assert not np.isinf(fwd['A'].to_numpy(dtype=float)).any(), fwd
    assert pd.isna(fwd['A'].iloc[3])


def test_zero_close_does_not_poison_compute_ic_screen():
    """R2, end-to-end: a 0 close anywhere in the panel must not raise out of
    compute_ic_screen, and the result must stay JSON-safe under
    allow_nan=False — exactly the guarantee main() now relies on."""
    closes, rng = _panel()
    closes.iloc[100, 5] = 0.0
    factor = _predictive_factor(closes, rng)
    res = fis.compute_ic_screen(factor, closes)
    import json
    json.dumps(res, allow_nan=False)   # must not raise


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
    # Pin the ledger's binding T7-ICIR ruling itself (|ICIR| < 0.3, not a
    # signed comparison): a strongly NEGATIVE ICIR must still reach `pass`,
    # not `weak` — `verdict != 'flat'` alone would not catch a regression to
    # a signed `icir0 < WEAK_ICIR_ABS` comparison (a large negative icir0
    # would then wrongly satisfy that and land on `weak`).
    assert res['icir']['5'] < -0.30, res
    assert res['verdict'] == 'pass', res


def test_thin_cross_section_scores_skipped_not_flat():
    """A long-only decile strategy: 12 names, 2 distinct values — below even
    the rebalance-date cross-section floor (MIN_CROSS_SECTION=20), so no
    rebalance date is ever formed at all. Must NOT be scored flat — that
    would false-block the whole decile class."""
    closes, _rng = _panel()
    factor = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    picks = list(closes.columns[:12])
    factor.loc[:, picks[:6]] = 1.0
    factor.loc[:, picks[6:]] = 0.6
    res = fis.compute_ic_screen(factor, closes)
    assert res['verdict'] == 'skipped'
    assert res['reason'] == 'insufficient_cross_section'
    assert res['n_rebalances'] == 0


def test_wide_but_low_distinct_cross_section_scores_skipped():
    """The brief's actual motivating case: a decile strategy with enough
    NAMES to clear the rebalance-date cross-section floor (40 >= 20) but
    only 2 distinct values — ties dominate rank IC and pd.qcut cannot form 5
    quantiles. Must be caught by MIN_DISTINCT_VALUES inside the qualifying
    check, NOT by rebalance_dates' coarser notna-count filter (that filter
    alone is exercised by the 12-name test above, where it gates everything
    before this check is ever reached)."""
    closes, _rng = _panel()
    factor = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    picks = list(closes.columns[:40])
    factor.loc[:, picks[:20]] = 1.0
    factor.loc[:, picks[20:]] = 0.6
    res = fis.compute_ic_screen(factor, closes)
    assert res['n_rebalances'] > 0, res       # cleared the notna>=20 floor
    assert res['n_qualifying_rebalances'] == 0, res  # but nunique < 5 every time
    assert res['verdict'] == 'skipped'
    assert res['reason'] == 'insufficient_cross_section'


def test_low_icir_scores_weak():
    """Enough cross-section and a non-trivial spread, but an IC series whose
    sign flips constantly. Left as an in-('weak','flat') tolerance check —
    on SEED=7 this symmetric flip's mean IC lands close enough to zero that
    it actually scores `flat` (see test_weak_icir_verdict_is_reachable below
    for a deterministic pin of the `weak` branch itself)."""
    closes, rng = _panel()
    fwd = fis.forward_returns(closes, 5)
    flip = pd.Series(np.where(np.arange(len(closes)) % 10 < 5, 1.0, -1.0), index=closes.index)
    factor = fwd.mul(flip, axis=0) + rng.normal(0.0, 0.02, size=fwd.shape)
    res = fis.compute_ic_screen(factor, closes)
    assert res['verdict'] in ('weak', 'flat'), res
    if res['verdict'] == 'weak':
        assert abs(res['icir']['5']) < 0.30
        assert res['reason'] == 'icir_below_threshold'


def test_weak_icir_verdict_is_reachable():
    """Deterministically pins the `weak` branch itself (not just tolerated
    as one of two possible outcomes, as in test_low_icir_scores_weak above).
    A slightly asymmetric sign-flip (+0.06 bias, still flipping sign every 5
    sessions) keeps the IC real (|IC_5| clears the 0.01 flat floor with
    margin) but unstable enough that its annualized ICIR stays under 0.30
    with margin on both sides — empirically verified on SEED=7 at
    ic_5≈0.0226, icir_5≈0.194."""
    closes, rng = _panel()
    fwd = fis.forward_returns(closes, 5)
    flip = pd.Series(np.where(np.arange(len(closes)) % 10 < 5, 1.0, -1.0), index=closes.index)
    factor = fwd.mul(flip + 0.06, axis=0) + rng.normal(0.0, 0.02, size=fwd.shape)
    res = fis.compute_ic_screen(factor, closes)
    assert abs(res['ic']['5']) >= 0.015, res
    assert abs(res['icir']['5']) < 0.30, res
    assert res['verdict'] == 'weak', res
    assert res['reason'] == 'icir_below_threshold'


def test_narrow_one_sided_coverage_scores_skipped():
    """Ruling R1: a one-directional (all-LONG) strategy whose picks cover
    only a MINORITY of the universe (30 of 300) gets ranked only against
    itself by position_size_pct — a quantity carrying no cross-sectional
    alpha — so IC collapses toward 0 for a reason that has nothing to do
    with the factor being bad. Must be caught by the dedicated
    `one_sided_partial_coverage` skip reason, not fall through to `flat`."""
    closes, rng = _panel()
    picks = list(closes.columns[:30])
    factor = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    factor.loc[:, picks] = np.abs(rng.normal(1.0, 0.3, size=(len(closes), len(picks))))
    res = fis.compute_ic_screen(factor, closes)
    assert res['one_sided'] is True, res
    assert res['coverage'] == pytest.approx(30 / 300), res
    assert res['verdict'] == 'skipped', res
    assert res['reason'] == 'one_sided_partial_coverage', res


def test_wide_one_sided_coverage_is_not_skipped_by_this_reason():
    """Same one-directional shape as above but covering 200/300 (>= the
    ONE_SIDED_MIN_COVERAGE=0.5 floor) — must NOT trigger
    one_sided_partial_coverage; the existing guards decide instead."""
    closes, rng = _panel()
    picks = list(closes.columns[:200])
    factor = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    factor.loc[:, picks] = np.abs(rng.normal(1.0, 0.3, size=(len(closes), len(picks))))
    res = fis.compute_ic_screen(factor, closes)
    assert res['one_sided'] is True, res
    assert res['coverage'] == pytest.approx(200 / 300), res
    assert res['reason'] != 'one_sided_partial_coverage', res


def test_mixed_direction_narrow_coverage_is_not_skipped_by_this_reason():
    """Same narrow 30/300 coverage as the first R1 test, but LONG and SHORT
    mixed — one_sided must be False, so this reason must not fire even
    though coverage is low."""
    closes, rng = _panel()
    picks = list(closes.columns[:30])
    long_picks, short_picks = picks[:15], picks[15:]
    factor = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    factor.loc[:, long_picks] = np.abs(rng.normal(1.0, 0.3, size=(len(closes), len(long_picks))))
    factor.loc[:, short_picks] = -np.abs(rng.normal(1.0, 0.3, size=(len(closes), len(short_picks))))
    res = fis.compute_ic_screen(factor, closes)
    assert res['one_sided'] is False, res
    assert res['reason'] != 'one_sided_partial_coverage', res


def test_trailing_return_factor_is_not_spuriously_predictive():
    """Ruling R8a — negative alignment control. A BACKWARD-looking factor
    (this period's own already-realized trailing 5-day return) must NOT
    appear predictive of the FORWARD return. A silent off-by-one/alignment
    bug in forward_returns or rebalance_dates would show up here as a
    spuriously high |IC|, since trailing and forward windows would overlap."""
    closes, _rng = _panel()
    trailing = closes / closes.shift(5) - 1.0
    res = fis.compute_ic_screen(trailing, closes)
    assert abs(res['ic']['5']) < 0.10, res


def test_noiseless_forward_return_factor_gives_near_unit_ic():
    """Ruling R8b — positive control. Using the EXACT forward return as its
    own factor must give (near) perfect rank correlation with itself at
    H=5, proving the IC computation isn't systematically attenuated."""
    closes, _rng = _panel()
    factor = fis.forward_returns(closes, 5)
    res = fis.compute_ic_screen(factor, closes)
    assert res['ic']['5'] > 0.95, res


def test_too_few_rebalance_dates_scores_skipped_not_thin_cross_section():
    """Ruling R8c — isolates the OTHER half of `insufficient_cross_section`:
    cross-section WIDTH is fine (300 names, continuous, way past
    MIN_DISTINCT_VALUES=5) but there are only 11 usable rebalance dates —
    one short of MIN_REBALANCES=12. Also pins ruling R3: with a full,
    always-non-NaN forward-return panel at these positions, qualifying
    dates must equal all 11 rebalance dates (no fwd-side date is lost)."""
    closes, rng = _panel()
    factor = pd.DataFrame(np.nan, index=closes.index, columns=closes.columns)
    positions = list(range(0, 11 * 5, 5))   # 11 dates, spaced exactly 5 apart
    for pos in positions:
        d = closes.index[pos]
        factor.loc[d] = rng.normal(0.0, 1.0, size=len(closes.columns))
    res = fis.compute_ic_screen(factor, closes)
    assert res['n_rebalances'] == 11, res
    assert res['n_qualifying_rebalances'] == 11, res
    assert res['verdict'] == 'skipped', res
    assert res['reason'] == 'insufficient_cross_section', res


def test_output_shape_carries_every_spec_field():
    closes, rng = _panel()
    res = fis.compute_ic_screen(_predictive_factor(closes, rng), closes)
    for key in ('ic', 'icir', 'ic_half', 'rank_ac', 'ls_q5q1', 'monotonic',
                'turnover', 'cost_per_rebalance', 'quintile_means',
                'coverage', 'one_sided',
                'n_rebalances', 'n_qualifying_rebalances', 'verdict', 'reason'):
        assert key in res, f'missing {key}'
    assert set(res['ic']) == {'5', '10', '21'}
    assert set(res['icir']) == {'5', '10', '21'}
    assert set(res['ic_half']) == {'first', 'second'}
    import json
    json.dumps(res)   # must be JSON-serialisable for the orchestrator
    # The CLI's main() hardens this with allow_nan=False (a bare `NaN` token
    # is invalid JSON for the orchestrator's Node-side JSON.parse) — prove
    # the same guarantee holds on the pure core's own output, not just that
    # the permissive default succeeds.
    json.dumps(res, allow_nan=False)


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


def test_flat_direction_leaves_factor_cell_nan():
    """Ruling R4: a FLAT (or any unrecognized) direction must NOT write a
    0.0 into the factor panel — that would claim a real, neutral opinion
    the IC/quintile machinery would rank alongside actual LONG/SHORT
    values, when FLAT really means the same 'no opinion' as a name the
    strategy never mentioned."""
    from strategies.base import Signal
    dates = list(pd.bdate_range(end='2026-01-30', periods=1))
    universe = ['A', 'B']
    daily = [
        [Signal(ticker='A', direction='FLAT', entry_price=10.0, stop_loss=9.0,
                target_1=11.0, target_2=12.0, target_3=13.0, confidence='HIGH',
                position_size_pct=0.02)],
    ]
    f = fis.factor_from_signals(daily, dates, universe)
    assert pd.isna(f.loc[dates[0], 'A'])
    assert pd.isna(f.loc[dates[0], 'B'])


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
    # Ruling R1, proven through REAL Signal objects + factor_from_signals
    # (not just the synthetic-panel unit tests above): this fixture always
    # emits direction='LONG' and picks only 40 of the 300-ticker universe,
    # so it must land on the dedicated one_sided_partial_coverage skip, not
    # be left to fall through to `flat`.
    assert res['one_sided'] is True, res
    assert res['coverage'] == pytest.approx(40 / 300, abs=0.02), res
    assert res['reason'] == 'one_sided_partial_coverage', res


def test_main_on_raising_loader_exits_1_and_prints_one_infra_fail_json_line(monkeypatch, capsys):
    """Ruling R8d: main() on an infra failure must still print exactly one
    parseable JSON line to stdout, tagged with the `ic_screen_infra_fail`
    reason the module's own docstring promises — a line-oriented stdout
    consumer must never see empty output, only exit code 1 with nothing to
    parse. The stderr diagnostic (pre-existing behavior) stays too."""
    def _raise(*_a, **_kw):
        raise RuntimeError('boom: fake loader failure')

    monkeypatch.setattr(fis, 'run_ic_screen', _raise)
    rc = fis.main(['--strategy-file', '/nonexistent/does-not-matter.py'])
    assert rc == 1

    import json
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1, captured.out
    payload = json.loads(lines[0])
    assert payload['reason'] == 'ic_screen_infra_fail'
    assert payload['verdict'] is None
    assert 'ic screen infra error' in captured.err


def test_main_on_non_finite_result_exits_1_via_same_infra_fail_path(monkeypatch, capsys):
    """Ruling R2: json.dumps(..., allow_nan=False) must live INSIDE main()'s
    try, so a non-finite value anywhere in the result (compute_ic_screen
    itself never produces one, but a future regression could) is caught by
    the SAME infra-failure path as a raising loader — exit 1 + one stdout
    JSON line — not a raw ValueError traceback out of main(). The 0-close
    test above never reaches this branch (forward_returns already scrubs
    inf before it gets anywhere near json.dumps), so this pins the move
    itself directly."""
    monkeypatch.setattr(fis, 'run_ic_screen', lambda *_a, **_kw: {'x': float('inf')})
    rc = fis.main(['--strategy-file', '/nonexistent/does-not-matter.py'])
    assert rc == 1

    import json
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1, captured.out
    payload = json.loads(lines[0])
    assert payload['reason'] == 'ic_screen_infra_fail'
