"""tests/backtest/test_benchmark_baseline.py — task R1 (five-repo-adoptions,
2026-08-24): benchmark-relative promotion criterion (excess Sharpe vs a
regime-conditioned SPY baseline).

Written TDD-style alongside:
  - src/backtest/benchmark_baseline.py           (regime_benchmark_sharpe)
  - src/backtest/regime_qualification.py         (qualifies_regime, +bench leg)
  - src/strategies/lifecycle.py                  (MIN_EXCESS_SHARPE_VS_BENCHMARK*)
  - src/lib/promotion_service.js                 (judgeRegimeSleeve, +bench leg)

Synthetic parquet fixtures only, written under tmp_path — NEVER touches real
data/master/*.parquet. ZZT-prefixed ticker for the synthetic benchmark
symbol, matching this repo's synthetic-test-ticker convention (AAA is a
real ETF; ZZT is not a real symbol). Loaders are swapped via monkeypatch on
benchmark_baseline's module-level path constants / functions, per that
module's "loaders as separate functions so tests monkeypatch them" design.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest import benchmark_baseline as bb              # noqa: E402
from backtest.regime_qualification import qualifies_regime  # noqa: E402

BENCHMARK_TICKER = 'ZZT_SPY'


def _write_regimes(path, dates, regimes):
    pd.DataFrame({
        'date': [d.date() for d in dates],
        'vix': 15.0,
        'vix_smoothed': 15.0,
        'regime': regimes,
    }).to_parquet(path)


def _write_prices(path, dates, closes, ticker=BENCHMARK_TICKER):
    pd.DataFrame({
        'ticker': ticker,
        'date': [d.strftime('%Y-%m-%d') for d in dates],
        'open': closes, 'high': closes, 'low': closes, 'close': closes,
        'volume': 1000.0, 'vwap': closes, 'transactions': 10.0,
        'source': 'synthetic',
    }).to_parquet(path)


def _alternating_closes(n, r=0.01, start=100.0):
    """Deterministic close series whose daily returns alternate +r/-r.
    Arithmetic mean of an alternating +r/-r series is exactly 0 over an
    EVEN number of terms; an odd count (e.g. a block whose leading return
    was dropped at a window boundary) leaves a small residual. Tests below
    treat "benchmark flat" loosely (see test docstring) for exactly this
    reason — only the gate OUTCOMES are asserted exactly."""
    price = start
    closes = [price]
    for i in range(1, n):
        step = r if i % 2 == 1 else -r
        price = price * (1 + step)
        closes.append(price)
    return closes


@pytest.fixture
def alternating_252(tmp_path, monkeypatch):
    """252 business days, six alternating 42-day LOW_VOL/HIGH_VOL blocks
    (TRANSITIONING/CRISIS never tagged); ZZT_SPY alternates +-1% daily."""
    dates = pd.bdate_range('2020-01-02', periods=252)
    block = 42
    labels = ['LOW_VOL', 'HIGH_VOL']
    regimes = [labels[(i // block) % 2] for i in range(len(dates))]
    regimes_path = tmp_path / 'historical_regimes.parquet'
    prices_path = tmp_path / 'prices.parquet'
    _write_regimes(regimes_path, dates, regimes)
    _write_prices(prices_path, dates, _alternating_closes(len(dates)))
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(regimes_path))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(prices_path))
    return dates


@pytest.fixture
def thin_crisis_fixture(tmp_path, monkeypatch):
    """60 days: first 10 tagged CRISIS (-> 9 usable close-to-close returns,
    well under min_obs=40), remaining 50 tagged LOW_VOL (-> 50 usable
    returns, clears min_obs)."""
    dates = pd.bdate_range('2021-01-04', periods=60)
    regimes = ['CRISIS'] * 10 + ['LOW_VOL'] * 50
    regimes_path = tmp_path / 'historical_regimes.parquet'
    prices_path = tmp_path / 'prices.parquet'
    _write_regimes(regimes_path, dates, regimes)
    _write_prices(prices_path, dates, _alternating_closes(len(dates), r=0.005))
    monkeypatch.setattr(bb, 'REGIMES_PARQUET', str(regimes_path))
    monkeypatch.setattr(bb, 'PRICES_PARQUET', str(prices_path))
    return dates


# ── regime_benchmark_sharpe ──────────────────────────────────────────────────

def test_regime_benchmark_sharpe_near_zero_both_regimes_and_untagged_are_none(alternating_252):
    dates = alternating_252
    out = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    assert out['LOW_VOL'] is not None
    assert out['HIGH_VOL'] is not None
    # "benchmark flat (Sharpe ~= 0)" per the task brief -- loose, not exact.
    # An alternating +-r return series cancels to EXACTLY 0 mean only over
    # an EVEN observation count; the first block loses its leading return
    # at the window's left edge, which can leave one block at an odd count
    # (empirically ~0.13 here). Bound generously -- the exact assertions
    # this module cares about are the gate OUTCOMES below, not this number.
    assert abs(out['LOW_VOL']) < 0.5
    assert abs(out['HIGH_VOL']) < 0.5
    # Never tagged in this fixture -> 0 usable returns -> thin -> None.
    assert out['TRANSITIONING'] is None
    assert out['CRISIS'] is None


def test_thin_regime_10_tagged_days_is_none(thin_crisis_fixture):
    dates = thin_crisis_fixture
    out = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    assert out['CRISIS'] is None          # 10 tagged days -> 9 usable returns < min_obs
    assert out['LOW_VOL'] is not None     # 50 tagged days -> 50 usable returns >= min_obs


def test_load_failure_returns_empty_dict(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError('synthetic parquet read failure')
    monkeypatch.setattr(bb, 'load_regime_tags', _raise)
    assert bb.regime_benchmark_sharpe('2020-01-01', '2020-12-31', benchmark=BENCHMARK_TICKER) == {}


def test_load_failure_in_prices_loader_also_returns_empty_dict(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError('synthetic prices read failure')
    monkeypatch.setattr(bb, 'load_benchmark_closes', _raise)
    assert bb.regime_benchmark_sharpe('2020-01-01', '2020-12-31', benchmark=BENCHMARK_TICKER) == {}


# ── qualifies_regime + the benchmark leg (fail-open contract) ───────────────

def test_upward_drift_sleeve_passes_benchmark_gate(alternating_252):
    dates = alternating_252
    bench = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    assert qualifies_regime(1.2, 150, 10.0, 'equity',
                            benchmark_sharpe=bench['LOW_VOL'],
                            sid='S_drift', regime='LOW_VOL') is True


def test_benchmark_as_its_own_sleeve_fails_excess_not_positive(alternating_252):
    dates = alternating_252
    bench = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    bench_low_vol = bench['LOW_VOL']
    # Sleeve sharpe EQUALS the benchmark's own Sharpe -> excess is exactly
    # 0, and MIN_EXCESS_SHARPE_VS_BENCHMARK requires a STRICT >, so a tie
    # fails ("excess 0 is not > 0" per the task brief).
    assert qualifies_regime(bench_low_vol, 150, 10.0, 'equity',
                            benchmark_sharpe=bench_low_vol,
                            sid='S_is_benchmark', regime='LOW_VOL') is False
    # Confirm it's specifically the benchmark leg doing the failing: the
    # SAME sleeve passes when no benchmark_sharpe is supplied (legacy-only).
    assert qualifies_regime(bench_low_vol, 150, 10.0, 'equity') is True


def test_thin_regime_skips_criterion_sleeve_passes_on_legacy_alone(thin_crisis_fixture):
    dates = thin_crisis_fixture
    out = bb.regime_benchmark_sharpe(dates[0], dates[-1], benchmark=BENCHMARK_TICKER, min_obs=40)
    assert out['CRISIS'] is None
    assert qualifies_regime(0.01, 150, 10.0, 'equity',
                            benchmark_sharpe=out['CRISIS'],
                            sid='S_thin', regime='CRISIS') is True


def test_load_failure_all_sleeves_skip_criterion(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError('synthetic parquet read failure')
    monkeypatch.setattr(bb, 'load_regime_tags', _raise)
    out = bb.regime_benchmark_sharpe('2020-01-01', '2020-12-31', benchmark=BENCHMARK_TICKER)
    assert out == {}
    assert qualifies_regime(0.01, 150, 10.0, 'equity',
                            benchmark_sharpe=out.get('LOW_VOL'),
                            sid='S_infra_down', regime='LOW_VOL') is True


def test_nonfinite_persisted_benchmark_treated_as_absent_not_a_gate_failure():
    """A NaN/inf benchmark_sharpe (e.g. Decimal('NaN') round-tripped through
    psycopg2/NUMERIC) must be treated exactly like NULL -- fail OPEN, never
    closed. `benchmark_sharpe is None` alone would miss this and would also
    desync from the JS twin's isNaN-based skip in judgeRegimeSleeve."""
    assert qualifies_regime(0.01, 150, 10.0, 'equity', benchmark_sharpe=float('nan')) is True
    assert qualifies_regime(0.01, 150, 10.0, 'equity', benchmark_sharpe=float('inf')) is True


def test_benchmark_never_rescues_a_legacy_failure():
    """The benchmark leg is ANDed onto the legacy result -- it can only
    TIGHTEN the gate. A sleeve that fails legacy (here: trade_count < 100)
    stays failed no matter how favorable the benchmark comparison is."""
    assert qualifies_regime(5.0, 10, 10.0, 'equity',
                            benchmark_sharpe=-5.0,        # trivially beaten
                            sid='S_low_trades', regime='LOW_VOL') is False


# ── Twin-sync guard (python <-> JS) ──────────────────────────────────────────

LIFECYCLE_PATH = ROOT / 'src' / 'strategies' / 'lifecycle.py'
JS_PATH = ROOT / 'src' / 'lib' / 'promotion_service.js'
CLASSES = ('equity', 'etp', 'option', 'crypto')


def test_min_excess_sharpe_vs_benchmark_value_synced_python_js():
    """Cheap regex tripwire against future drift -- follows the pattern of
    tests/strategies/test_promotion_activation_threshold_parity.py (the
    existing PROMOTION_THRESHOLDS JS/py sync test) without touching that
    file, since MIN_EXCESS_SHARPE_VS_BENCHMARK is deliberately its OWN
    structure (see lifecycle.py's header comment on why it isn't folded
    into PROMOTION_THRESHOLDS)."""
    py_src = LIFECYCLE_PATH.read_text()
    js_src = JS_PATH.read_text()

    py_const = re.search(r'MIN_EXCESS_SHARPE_VS_BENCHMARK\s*:\s*float\s*=\s*([0-9.]+)', py_src)
    assert py_const, 'MIN_EXCESS_SHARPE_VS_BENCHMARK constant not found in lifecycle.py'
    assert float(py_const.group(1)) == 0.0

    js_const = re.search(r'const\s+MIN_EXCESS_SHARPE_VS_BENCHMARK\s*=\s*([0-9.]+)', js_src)
    assert js_const, 'MIN_EXCESS_SHARPE_VS_BENCHMARK constant not found in promotion_service.js'
    assert float(js_const.group(1)) == 0.0

    py_block = re.search(
        r'MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS\s*:\s*dict\[str,\s*float\]\s*=\s*\{(.+?)\}',
        py_src, re.DOTALL)
    assert py_block, 'MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS dict not found in lifecycle.py'
    py_classes = set(re.findall(r'"(\w+)"\s*:\s*MIN_EXCESS_SHARPE_VS_BENCHMARK\b', py_block.group(1)))

    js_block = re.search(
        r'MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS\s*=\s*\{(.+?)\}',
        js_src, re.DOTALL)
    assert js_block, 'MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS object not found in promotion_service.js'
    js_classes = set(re.findall(r'(\w+)\s*:\s*MIN_EXCESS_SHARPE_VS_BENCHMARK\b', js_block.group(1)))

    for cls in CLASSES:
        assert cls in py_classes, f'lifecycle.py MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS missing {cls!r}'
        assert cls in js_classes, f'promotion_service.js MIN_EXCESS_SHARPE_VS_BENCHMARK_BY_CLASS missing {cls!r}'


def test_promotion_thresholds_dict_shape_unchanged():
    """Guard the deviation documented in this task's report: the new
    threshold must NOT be folded into strategies.lifecycle.PROMOTION_THRESHOLDS
    (that would break the exact-dict-equality assertions in
    tests/strategies/test_promotion_thresholds_per_class.py and
    tests/execution/test_phase_d_crypto.py, both out of this task's scope)."""
    import sys as _sys
    _sys.path.insert(0, str(ROOT / 'src'))
    from strategies.lifecycle import PROMOTION_THRESHOLDS
    for cls, thr in PROMOTION_THRESHOLDS.items():
        assert 'min_excess_sharpe_vs_benchmark' not in thr, (
            f'PROMOTION_THRESHOLDS[{cls!r}] gained min_excess_sharpe_vs_benchmark -- '
            'this would break the exact-equality tests noted above.')
