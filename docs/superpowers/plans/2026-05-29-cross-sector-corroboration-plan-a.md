# Plan A — Confirmation Framework + Options-Flow + Sector-Flow Strategies

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reusable pure-function corroboration framework and two standalone equity strategies (`S_options_flow_confirmed_momentum`, `S_sector_flow_confirmed_momentum`), backtest each, and prove the corroboration adds alpha (lift test) before any promotion.

**Architecture:** A new `src/strategies/confirmation/` package of pure, deterministic, I/O-free functions (`momentum_base`, `options_flow`, `sector_map`, `sector_flow`). Two `BaseStrategy` subclasses compute the shared momentum base then filter/rank via the confirmation functions. Both trade the underlying equity (`instrument_class='equity'`). Strategies are backtested via `unified_backtest --strategy-file` (works before registration); registration into `_IMPL_MAP` + manifest `candidate` happens only after metrics pass.

**Tech Stack:** Python 3, pandas/numpy, pytest. Strategy contract `src/strategies/base.py` (`BaseStrategy.generate_signals(prices, regime, universe, aux_data) -> List[Signal]`). Backtest `src/backtest/unified_backtest.py`. Spec: `docs/superpowers/specs/2026-05-29-cross-sector-corroboration-strategies-design.md`.

**Conventions (verified in-repo):**
- Strategy file import header (matches 103/118 impls):
  ```python
  import sys, os
  sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
  from strategies.base import BaseStrategy, Signal
  ```
  This puts `src/` on the path, so `from strategies.confirmation.options_flow import confirm` resolves.
- Confirmation modules (under `src/strategies/confirmation/`) are imported by tests as `from strategies.confirmation.X import ...`. Tests run from repo root with `src/` importable (no pythonpath in pytest.ini, but existing `tests/strategies/*` import this way — match them).
- `aux_data['options'][ticker]` keys: `pc_ratio` (reliable, = put/call vol), `skew`/`skew_20d` (suspect), `otm_put_iv`, `otm_call_iv`, `iv_rank`, `spot`/`last_price`. Point-in-time safe.
- ETFs (SPY/QQQ/XL*) are columns in the wide `prices` DataFrame — no aux plumbing.
- Equity promotion gate (`lifecycle.PROMOTION_THRESHOLDS`): **Sharpe ≥ 0.5, MaxDD ≤ 0.20**.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/strategies/confirmation/__init__.py` | Package marker |
| `src/strategies/confirmation/momentum_base.py` | Shared cross-sectional momentum base signal (pure) |
| `src/strategies/confirmation/options_flow.py` | PCR-primary / skew-secondary directional confirm (pure) |
| `src/strategies/confirmation/sector_map.py` | Static GICS ticker→sector + sector→SPDR-ETF maps |
| `src/strategies/confirmation/sector_flow.py` | Sector/broad-market trend-alignment confirm (pure) |
| `src/strategies/implementations/S_options_flow_confirmed_momentum.py` | Strategy ① |
| `src/strategies/implementations/S_options_flow_confirmed_momentum.requirements.json` | ① data reqs |
| `src/strategies/implementations/S_sector_flow_confirmed_momentum.py` | Strategy ② |
| `src/strategies/implementations/S_sector_flow_confirmed_momentum.requirements.json` | ② data reqs |
| `tests/strategies/test_confirmation_framework.py` | Unit tests for the 4 confirmation modules |
| `tests/strategies/test_cross_sector_strategies.py` | Unit tests for ① and ② generate_signals |
| `src/strategies/registry.py` | Add `_IMPL_MAP` entries (Task 9, gated on metrics) |
| `src/strategies/manifest.json` | Add `candidate` entries (Task 9, gated on metrics) |

---

## Task 1: Confirmation package + shared momentum base

**Files:**
- Create: `src/strategies/confirmation/__init__.py`
- Create: `src/strategies/confirmation/momentum_base.py`
- Test: `tests/strategies/test_confirmation_framework.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_confirmation_framework.py
import numpy as np
import pandas as pd
import pytest
from strategies.confirmation import momentum_base as mb


def _ramp_prices():
    # 120 business days; AAA rises 50%, BBB flat, CCC falls 30%
    idx = pd.bdate_range('2024-01-01', periods=120)
    return pd.DataFrame({
        'AAA': np.linspace(100, 150, 120),
        'BBB': np.full(120, 100.0),
        'CCC': np.linspace(100, 70, 120),
    }, index=idx)


def test_momentum_scores_signs():
    p = _ramp_prices()
    scores = mb.momentum_scores(p, ['AAA', 'BBB', 'CCC'], lookback=63, skip=5)
    assert scores['AAA'] > 0
    assert scores['CCC'] < 0
    assert abs(scores['BBB']) < 1e-9


def test_momentum_scores_skips_short_history():
    p = _ramp_prices().iloc[:30]   # < lookback+skip
    scores = mb.momentum_scores(p, ['AAA'], lookback=63, skip=5)
    assert scores == {}


def test_rank_long_short_directionality():
    scores = {'AAA': 0.5, 'BBB': 0.0, 'CCC': -0.3, 'DDD': 0.4, 'EEE': -0.2}
    longs, shorts = mb.rank_long_short(scores, decile=0.4, max_each=10)
    assert 'AAA' in longs and 'DDD' in longs
    assert 'CCC' in shorts and 'EEE' in shorts
    assert 'BBB' not in longs and 'BBB' not in shorts   # zero momentum filtered
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strategies.confirmation'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/strategies/confirmation/__init__.py
"""Reusable, pure-function signal-corroboration framework (cross-sector strategies)."""
```

```python
# src/strategies/confirmation/momentum_base.py
"""Shared cross-sectional momentum base signal for corroboration strategies.

Pure / deterministic: no I/O, no clock, no randomness. Same inputs → same outputs.
Used by S_options_flow_confirmed_momentum and S_sector_flow_confirmed_momentum so the
'base signal' is identical across both (DRY) and the corroboration lift is measured cleanly.
"""
from __future__ import annotations
from typing import Dict, List, Tuple
import pandas as pd


def momentum_scores(prices: pd.DataFrame, universe: List[str],
                    lookback: int = 63, skip: int = 5) -> Dict[str, float]:
    """{ticker: trailing return over [t-lookback-skip, t-skip]} (skip last `skip` days).

    Skips tickers absent from `prices`, with < lookback+skip history, or non-positive start.
    """
    out: Dict[str, float] = {}
    need = lookback + skip
    for t in universe:
        if t not in prices.columns:
            continue
        s = prices[t].dropna()
        if len(s) < need:
            continue
        start = float(s.iloc[-(lookback + skip)])
        end = float(s.iloc[-(skip + 1)])
        if start <= 0:
            continue
        out[t] = end / start - 1.0
    return out


def rank_long_short(scores: Dict[str, float], decile: float = 0.1,
                    max_each: int = 25) -> Tuple[List[str], List[str]]:
    """Top `decile` fraction → longs, bottom `decile` → shorts (abs-momentum filtered)."""
    if not scores:
        return [], []
    ser = pd.Series(scores).sort_values(ascending=False)
    n = max(1, int(len(ser) * decile))
    longs = [t for t in ser.head(n).index if scores[t] > 0][:max_each]
    shorts = [t for t in ser.tail(n).index if scores[t] < 0][:max_each]
    return longs, shorts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/confirmation/__init__.py src/strategies/confirmation/momentum_base.py tests/strategies/test_confirmation_framework.py
git commit -m "feat(confirmation): shared cross-sectional momentum base signal"
```

---

## Task 2: Options-flow confirmation (PCR primary, skew soft secondary)

**Files:**
- Create: `src/strategies/confirmation/options_flow.py`
- Test: `tests/strategies/test_confirmation_framework.py` (append)

- [ ] **Step 1: Write the failing test (append to the file)**

```python
# append to tests/strategies/test_confirmation_framework.py
from strategies.confirmation import options_flow as of


def test_options_flow_long_confirmed_by_low_pcr():
    passes, score = of.confirm('LONG', {'pc_ratio': 0.5, 'skew_20d': -0.03})
    assert passes is True
    assert score > 0


def test_options_flow_long_rejected_by_high_pcr():
    passes, _ = of.confirm('LONG', {'pc_ratio': 1.4})
    assert passes is False


def test_options_flow_short_confirmed_by_high_pcr():
    passes, score = of.confirm('SHORT', {'pc_ratio': 1.3, 'skew_20d': 0.04})
    assert passes is True
    assert score > 0


def test_options_flow_missing_data_is_unconfirmed():
    assert of.confirm('LONG', None) == (False, 0.0)
    assert of.confirm('LONG', {}) == (False, 0.0)
    assert of.confirm('LONG', {'pc_ratio': None}) == (False, 0.0)


def test_options_flow_pcr_gates_even_without_skew():
    # skew absent → score driven by PCR only, gate still applies
    passes, score = of.confirm('LONG', {'pc_ratio': 0.6})
    assert passes is True
    assert score > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py -k options_flow -v`
Expected: FAIL — `ImportError: cannot import name 'options_flow'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/strategies/confirmation/options_flow.py
"""Options-flow corroboration of a directional equity signal.

PCR (pc_ratio = put_vol/call_vol) is the PRIMARY gate — it is volume-derived and reliable.
IV skew (otm_put_iv - otm_call_iv) is a SOFT SECONDARY that only nudges the score, never the
gate, because this file's vendor IV was flagged low-fidelity in SP-4 (SPY iv30 vs VIX corr 0.375).

Pure / deterministic.
"""
from __future__ import annotations
from typing import Optional, Tuple

PCR_BULL_MAX = 0.85   # LONG confirmed when pc_ratio <= this (heavy call demand)
PCR_BEAR_MIN = 1.05   # SHORT confirmed when pc_ratio >= this (heavy put demand)
SKEW_WEIGHT = 0.35    # downweight the suspect IV-skew leg


def _pcr_score(direction: str, pcr: float) -> float:
    if direction == 'LONG':
        return max(min((1.0 - pcr) / 0.5, 1.0), -1.0)
    return max(min((pcr - 1.0) / 0.5, 1.0), -1.0)


def _skew_score(direction: str, skew: Optional[float]) -> float:
    if skew is None:
        return 0.0
    s = -skew if direction == 'LONG' else skew   # LONG wants calls bid (negative skew)
    return max(min(s / 0.05, 1.0), -1.0)


def confirm(direction: str, opts_row: Optional[dict],
            pcr_bull_max: float = PCR_BULL_MAX,
            pcr_bear_min: float = PCR_BEAR_MIN) -> Tuple[bool, float]:
    """Return (passes, score in [-1,1]). False when options data is missing/unusable."""
    if not opts_row:
        return False, 0.0
    pcr = opts_row.get('pc_ratio')
    if pcr is None:
        return False, 0.0
    if direction == 'LONG':
        gate = pcr <= pcr_bull_max
    elif direction == 'SHORT':
        gate = pcr >= pcr_bear_min
    else:
        return False, 0.0
    skew = opts_row.get('skew_20d', opts_row.get('skew'))
    score = (1 - SKEW_WEIGHT) * _pcr_score(direction, pcr) + SKEW_WEIGHT * _skew_score(direction, skew)
    return gate, round(score, 4)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py -k options_flow -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/confirmation/options_flow.py tests/strategies/test_confirmation_framework.py
git commit -m "feat(confirmation): PCR-primary options-flow directional confirm"
```

---

## Task 3: Static sector map (ticker→sector→SPDR ETF)

**Files:**
- Create: `src/strategies/confirmation/sector_map.py`
- Test: `tests/strategies/test_confirmation_framework.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/strategies/test_confirmation_framework.py
from strategies.confirmation import sector_map as sm


def test_sector_map_known_tickers():
    assert sm.TICKER_SECTOR['AAPL'] == 'Tech'
    assert sm.TICKER_SECTOR['JPM'] == 'Fin'
    assert sm.SECTOR_ETF['Tech'] == 'XLK'
    assert sm.SECTOR_ETF['Fin'] == 'XLF'


def test_etf_for_ticker():
    assert sm.etf_for_ticker('AAPL') == 'XLK'
    assert sm.etf_for_ticker('XOM') == 'XLE'
    assert sm.etf_for_ticker('UNKNOWN_TICKER') is None


def test_every_sector_has_an_etf():
    for sector in set(sm.TICKER_SECTOR.values()):
        assert sector in sm.SECTOR_ETF, f'sector {sector} missing ETF'


def test_constituents_reverse_lookup():
    tech = sm.constituents('Tech')
    assert 'AAPL' in tech and 'MSFT' in tech
    assert all(sm.TICKER_SECTOR[t] == 'Tech' for t in tech)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py -k sector_map -v`
Expected: FAIL — `ImportError: cannot import name 'sector_map'`

- [ ] **Step 3: Write minimal implementation**

Copy the verified `_SECTOR_MAP` dict **verbatim** from `src/strategies/implementations/S_industry_momentum_moskowitz.py:11-59` as `TICKER_SECTOR` (it is the in-repo source of truth — ~160 liquid names across 11 GICS groups: Tech/CommSvc/ConsDis/ConsStap/Fin/Health/Indus/Energy/Matls/Utils/REIT). Then add the sector→ETF map and helpers:

```python
# src/strategies/confirmation/sector_map.py
"""Static GICS ticker→sector and sector→SPDR-ETF maps for the sector-flow strategy.

TICKER_SECTOR is lifted verbatim from S_industry_momentum_moskowitz._SECTOR_MAP (the
established in-repo precedent; no DB/FMP dependency, point-in-time stable). SECTOR_ETF maps
each GICS group to its State Street SPDR sector ETF (all present in prices.parquet, 10y daily).
"""
from __future__ import annotations
from typing import Dict, List, Optional

TICKER_SECTOR: Dict[str, str] = {
    # <<< paste the dict body from S_industry_momentum_moskowitz.py:11-59 here >>>
}

SECTOR_ETF: Dict[str, str] = {
    'Tech': 'XLK',
    'CommSvc': 'XLC',
    'ConsDis': 'XLY',
    'ConsStap': 'XLP',
    'Fin': 'XLF',
    'Health': 'XLV',
    'Indus': 'XLI',
    'Energy': 'XLE',
    'Matls': 'XLB',
    'Utils': 'XLU',
    'REIT': 'XLRE',
}

# Broad-market reference ETFs (present in prices.parquet)
BROAD_MARKET_ETFS = ('SPY', 'QQQ')


def etf_for_ticker(ticker: str) -> Optional[str]:
    """SPDR sector ETF for a ticker, or None if unmapped."""
    sector = TICKER_SECTOR.get(ticker)
    return SECTOR_ETF.get(sector) if sector else None


def constituents(sector: str) -> List[str]:
    """All mapped tickers in a sector."""
    return sorted(t for t, s in TICKER_SECTOR.items() if s == sector)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py -k sector_map -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/confirmation/sector_map.py tests/strategies/test_confirmation_framework.py
git commit -m "feat(confirmation): static GICS ticker->sector->SPDR-ETF map"
```

---

## Task 4: Sector-flow confirmation (trend alignment)

**Files:**
- Create: `src/strategies/confirmation/sector_flow.py`
- Test: `tests/strategies/test_confirmation_framework.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/strategies/test_confirmation_framework.py
from strategies.confirmation import sector_flow as sf


def _trend_prices(direction_map):
    """direction_map: {ticker: 'up'|'down'|'flat'} → 260-day wide price frame."""
    idx = pd.bdate_range('2024-01-01', periods=260)
    cols = {}
    for t, d in direction_map.items():
        if d == 'up':
            cols[t] = np.linspace(80, 120, 260)
        elif d == 'down':
            cols[t] = np.linspace(120, 80, 260)
        else:
            cols[t] = np.full(260, 100.0)
    return pd.DataFrame(cols, index=idx)


def test_sector_flow_long_confirmed_when_sector_and_market_up():
    p = _trend_prices({'AAPL': 'up', 'XLK': 'up', 'SPY': 'up', 'QQQ': 'up'})
    passes, score = sf.confirm('LONG', 'AAPL', p, sm, as_of=p.index[-1])
    assert passes is True
    assert score > 0


def test_sector_flow_long_rejected_when_sector_down():
    p = _trend_prices({'AAPL': 'up', 'XLK': 'down', 'SPY': 'up', 'QQQ': 'up'})
    passes, _ = sf.confirm('LONG', 'AAPL', p, sm, as_of=p.index[-1])
    assert passes is False


def test_sector_flow_short_confirmed_when_sector_and_market_down():
    p = _trend_prices({'XOM': 'down', 'XLE': 'down', 'SPY': 'down', 'QQQ': 'down'})
    passes, _ = sf.confirm('SHORT', 'XOM', p, sm, as_of=p.index[-1])
    assert passes is True


def test_sector_flow_unmapped_ticker_unconfirmed():
    p = _trend_prices({'ZZZZ': 'up', 'SPY': 'up', 'QQQ': 'up'})
    assert sf.confirm('LONG', 'ZZZZ', p, sm, as_of=p.index[-1]) == (False, 0.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py -k sector_flow -v`
Expected: FAIL — `ImportError: cannot import name 'sector_flow'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/strategies/confirmation/sector_flow.py
"""Sector / broad-market trend-alignment corroboration.

A LONG stock signal is confirmed only when BOTH its SPDR sector ETF and the broad market
are in an aligned uptrend (price above its trailing SMA). SHORT mirrors. ETF closes come from
the wide `prices` frame (no aux plumbing). Pure / deterministic apart from reading `prices`.
"""
from __future__ import annotations
from typing import Tuple
import pandas as pd

SMA_WINDOW = 50           # trend reference window (trading days)
MARKET_ALIGN_MIN = 1      # how many of (SPY, QQQ) must align with the direction


def _trend(prices: pd.DataFrame, sym: str, as_of, window: int = SMA_WINDOW):
    """+1 if last close > SMA(window), -1 if below, 0 if missing/insufficient."""
    if sym not in prices.columns:
        return 0
    s = prices.loc[:as_of, sym].dropna()
    if len(s) < window + 1:
        return 0
    last = float(s.iloc[-1])
    sma = float(s.iloc[-window:].mean())
    if last > sma:
        return 1
    if last < sma:
        return -1
    return 0


def confirm(direction: str, ticker: str, prices: pd.DataFrame, sector_map,
            as_of, window: int = SMA_WINDOW,
            market_align_min: int = MARKET_ALIGN_MIN) -> Tuple[bool, float]:
    """Return (passes, score). False when the ticker is unmapped or ETFs are missing."""
    etf = sector_map.etf_for_ticker(ticker)
    if etf is None:
        return False, 0.0
    want = 1 if direction == 'LONG' else -1 if direction == 'SHORT' else 0
    if want == 0:
        return False, 0.0

    sector_t = _trend(prices, etf, as_of, window)
    market_aligned = sum(1 for m in sector_map.BROAD_MARKET_ETFS
                         if _trend(prices, m, as_of, window) == want)

    passes = (sector_t == want) and (market_aligned >= market_align_min)
    score = want * (0.5 * sector_t + 0.5 * (market_aligned / len(sector_map.BROAD_MARKET_ETFS)))
    return passes, round(float(score), 4)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py -v`
Expected: PASS (all confirmation tests green)

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/confirmation/sector_flow.py tests/strategies/test_confirmation_framework.py
git commit -m "feat(confirmation): sector + broad-market trend-alignment confirm"
```

---

## Task 5: Strategy ① — S_options_flow_confirmed_momentum

**Files:**
- Create: `src/strategies/implementations/S_options_flow_confirmed_momentum.py`
- Create: `src/strategies/implementations/S_options_flow_confirmed_momentum.requirements.json`
- Test: `tests/strategies/test_cross_sector_strategies.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/strategies/test_cross_sector_strategies.py
import numpy as np
import pandas as pd
from strategies.implementations.S_options_flow_confirmed_momentum import OptionsFlowConfirmedMomentum


def _prices():
    idx = pd.bdate_range('2023-06-01', periods=120)
    return pd.DataFrame({
        'AAA': np.linspace(100, 160, 120),   # strong up momentum
        'BBB': np.linspace(100, 60, 120),    # strong down momentum
        'CCC': np.full(120, 100.0),
    }, index=idx)


def test_options_strategy_longs_confirmed_by_bullish_flow():
    s = OptionsFlowConfirmedMomentum()
    regime = {'state': 'LOW_VOL'}
    aux = {'options': {
        'AAA': {'pc_ratio': 0.5, 'skew_20d': -0.03},   # bullish flow confirms LONG
        'BBB': {'pc_ratio': 1.3, 'skew_20d': 0.04},    # bearish flow confirms SHORT
    }}
    sigs = s.generate_signals(_prices(), regime, ['AAA', 'BBB', 'CCC'], aux)
    by_dir = {sig.ticker: sig.direction for sig in sigs}
    assert by_dir.get('AAA') == 'LONG'
    assert by_dir.get('BBB') == 'SHORT'


def test_options_strategy_skips_when_flow_contradicts():
    s = OptionsFlowConfirmedMomentum()
    regime = {'state': 'LOW_VOL'}
    aux = {'options': {'AAA': {'pc_ratio': 1.5}}}   # bearish flow contradicts up-momentum LONG
    sigs = s.generate_signals(_prices(), regime, ['AAA', 'BBB', 'CCC'], aux)
    assert all(sig.ticker != 'AAA' for sig in sigs)


def test_options_strategy_no_options_no_signals():
    s = OptionsFlowConfirmedMomentum()
    sigs = s.generate_signals(_prices(), {'state': 'LOW_VOL'}, ['AAA', 'BBB'], {'options': {}})
    assert sigs == []


def test_options_strategy_empty_prices_safe():
    s = OptionsFlowConfirmedMomentum()
    assert s.generate_signals(pd.DataFrame(), {'state': 'LOW_VOL'}, ['AAA'], None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_cross_sector_strategies.py -k options -v`
Expected: FAIL — `ModuleNotFoundError` for the strategy module

- [ ] **Step 3: Write minimal implementation**

```python
# src/strategies/implementations/S_options_flow_confirmed_momentum.py
"""S_options_flow_confirmed_momentum — cross-sectional momentum corroborated by options flow.

Base: cross-sectional momentum (top/bottom decile, abs-momentum filtered).
Corroboration: keep a candidate only if options flow agrees — low PCR confirms LONG
(heavy call demand), high PCR confirms SHORT (heavy put demand); IV skew nudges the score.
Trades the underlying equity (instrument_class='equity'); never an option contract.

Lift test: set env OPENCLAW_CONFIRM_BASE_ONLY=1 to run the base momentum signal WITHOUT the
options gate, so backtest A/B isolates the corroboration's contribution.

Zero LLM tokens. Backtest window must be <= 2026-04-22 (options aux-loader forward-fills after).
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from strategies.confirmation import momentum_base as mb
from strategies.confirmation import options_flow as of

INSTRUMENT_CLASS = 'equity'


class OptionsFlowConfirmedMomentum(BaseStrategy):
    id = 'S_options_flow_confirmed_momentum'
    name = 'Options-Flow Confirmed Momentum'
    description = 'Cross-sectional momentum corroborated by put-call ratio / IV skew (LONG & SHORT)'
    tier = 2
    signal_frequency = 'daily'
    min_lookback = 70                       # lookback 63 + skip 5 + buffer
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    data_requirements = ['prices', 'options_aggregates']

    def default_parameters(self) -> dict:
        return {'lookback': 63, 'skip': 5, 'decile': 0.10, 'max_each': 20}

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        if len(prices) < self.min_lookback:
            return []

        p = self.parameters
        base_only = os.environ.get('OPENCLAW_CONFIRM_BASE_ONLY') == '1'
        opts_map = (aux_data or {}).get('options', {})
        if not base_only and not opts_map:
            return []

        scores = mb.momentum_scores(prices, universe, p['lookback'], p['skip'])
        longs, shorts = mb.rank_long_short(scores, p['decile'], p['max_each'])
        scale = self.position_scale(regime_state)

        signals: List[Signal] = []
        for tickers, direction in ((longs, 'LONG'), (shorts, 'SHORT')):
            for t in tickers:
                if t not in prices.columns:
                    continue
                if not base_only:
                    passes, _score = of.confirm(direction, opts_map.get(t))
                    if not passes:
                        continue
                ts = prices[t].dropna()
                if len(ts) < 2:
                    continue
                cur = float(ts.iloc[-1])
                if cur <= 0:
                    continue
                stops = self.compute_stops_and_targets(ts, direction, cur, regime_state=regime_state)
                signals.append(Signal(
                    ticker=t, direction=direction, entry_price=cur,
                    stop_loss=stops['stop'], target_1=stops['t1'],
                    target_2=stops['t2'], target_3=stops['t3'],
                    position_size_pct=round((1.0 / max(p['max_each'], 1)) * scale, 4),
                    confidence='MED',
                    signal_params={'momentum': round(float(scores.get(t, 0.0)), 4),
                                   'pc_ratio': (opts_map.get(t) or {}).get('pc_ratio'),
                                   'base_only': base_only},
                ))
        return signals[:self.MAX_SIGNALS]
```

```json
// src/strategies/implementations/S_options_flow_confirmed_momentum.requirements.json
{
  "strategy_id": "S_options_flow_confirmed_momentum",
  "required": ["prices", "options_aggregates"],
  "optional": []
}
```

- [ ] **Step 4: Run test + validate_strategy**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_cross_sector_strategies.py -k options -v`
Expected: PASS (4 passed)
Run: `cd /root/openclaw && python3 src/strategies/validate_strategy.py src/strategies/implementations/S_options_flow_confirmed_momentum.py`
Expected: JSON `{"ok": true, ...}`

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/implementations/S_options_flow_confirmed_momentum.py src/strategies/implementations/S_options_flow_confirmed_momentum.requirements.json tests/strategies/test_cross_sector_strategies.py
git commit -m "feat(strategy): S_options_flow_confirmed_momentum (PCR/skew-corroborated momentum)"
```

---

## Task 6: Backtest ① + lift test

**Files:** none created (analysis task; records metrics into the plan's notes / commit message).

- [ ] **Step 1: Confirmed backtest (options gate ON), bounded to real options window**

Run:
```bash
cd /root/openclaw && python3 -m backtest.unified_backtest \
  --strategy-file src/strategies/implementations/S_options_flow_confirmed_momentum.py \
  --start-date 2024-04-22 --end-date 2026-04-22 2>&1 | tail -5
```
Expected: a line like `[unified_backtest] wrote run_id=... total_sharpe=<X> trades=<N> regimes=[...]`. Record Sharpe / MaxDD / return / trades.

- [ ] **Step 2: Base-only backtest (gate OFF) for the lift comparison**

Run:
```bash
cd /root/openclaw && OPENCLAW_CONFIRM_BASE_ONLY=1 python3 -m backtest.unified_backtest \
  --strategy-file src/strategies/implementations/S_options_flow_confirmed_momentum.py \
  --start-date 2024-04-22 --end-date 2026-04-22 2>&1 | tail -5
```
Expected: a second run_id with base-only metrics. Record them.

- [ ] **Step 3: Evaluate against gate + lift**

- Gate (confirmed run): **Sharpe ≥ 0.5 AND MaxDD ≤ 0.20** → pass.
- Lift: confirmed Sharpe > base-only Sharpe **OR** confirmed MaxDD < base-only MaxDD → corroboration adds value.
- Record both runs' metrics and the verdict in the Task-9 commit message. If it fails the gate, STOP and report to operator (do not register); the corroboration thresholds (`PCR_BULL_MAX`/`PCR_BEAR_MIN` in options_flow.py, `decile`/`lookback` params) are the first tuning levers — note this in the report rather than silently re-tuning to fit.

- [ ] **Step 4: No commit** (analysis only; results carried to Task 9).

---

## Task 7: Strategy ② — S_sector_flow_confirmed_momentum (both modes, long & short)

**Files:**
- Create: `src/strategies/implementations/S_sector_flow_confirmed_momentum.py`
- Create: `src/strategies/implementations/S_sector_flow_confirmed_momentum.requirements.json`
- Test: `tests/strategies/test_cross_sector_strategies.py` (append)

- [ ] **Step 1: Write the failing test (append)**

```python
# append to tests/strategies/test_cross_sector_strategies.py
from strategies.implementations.S_sector_flow_confirmed_momentum import SectorFlowConfirmedMomentum


def _sector_prices():
    idx = pd.bdate_range('2023-01-01', periods=300)
    # XLK + market up; AAPL up. XLE + nothing else.
    return pd.DataFrame({
        'AAPL': np.linspace(100, 170, 300),
        'MSFT': np.linspace(100, 160, 300),
        'XOM':  np.linspace(150, 90, 300),
        'XLK':  np.linspace(80, 130, 300),
        'XLE':  np.linspace(130, 85, 300),
        'SPY':  np.linspace(380, 520, 300),
        'QQQ':  np.linspace(300, 460, 300),
    }, index=idx)


def test_sector_strategy_confirmation_mode_longs_aligned_names():
    s = SectorFlowConfirmedMomentum({'mode': 'confirm'})
    sigs = s.generate_signals(_sector_prices(), {'state': 'LOW_VOL'},
                              ['AAPL', 'MSFT', 'XOM'], None)
    dirs = {sig.ticker: sig.direction for sig in sigs}
    # AAPL/MSFT (up, sector XLK up, market up) → LONG; XOM (down, sector down) → SHORT
    assert dirs.get('AAPL') == 'LONG'
    assert dirs.get('XOM') == 'SHORT'


def test_sector_strategy_basket_mode_emits_constituents_both_sides():
    s = SectorFlowConfirmedMomentum({'mode': 'basket', 'top_sectors': 1})
    sigs = s.generate_signals(_sector_prices(), {'state': 'LOW_VOL'},
                              ['AAPL', 'MSFT', 'XOM'], None)
    dirs = {sig.ticker: sig.direction for sig in sigs}
    # strongest sector (XLK) constituents LONG; weakest (XLE) constituents SHORT
    assert dirs.get('AAPL') == 'LONG'
    assert dirs.get('XOM') == 'SHORT'


def test_sector_strategy_empty_prices_safe():
    s = SectorFlowConfirmedMomentum()
    assert s.generate_signals(pd.DataFrame(), {'state': 'LOW_VOL'}, ['AAPL'], None) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_cross_sector_strategies.py -k sector -v`
Expected: FAIL — `ModuleNotFoundError` for the strategy module

- [ ] **Step 3: Write minimal implementation**

```python
# src/strategies/implementations/S_sector_flow_confirmed_momentum.py
"""S_sector_flow_confirmed_momentum — momentum corroborated by sector/broad-market alignment.

Two modes (param 'mode'), both long & short:
  - 'confirm' (default): per-stock momentum kept only when sector_flow.confirm() agrees
    (LONG needs aligned up-sector + up-market; SHORT needs aligned down-sector + down-market).
  - 'basket': rank sectors by SPDR-ETF momentum; LONG the constituents of the strongest
    top_sectors and SHORT the constituents of the weakest top_sectors (symmetric).

Lift test (confirm mode): OPENCLAW_CONFIRM_BASE_ONLY=1 drops the sector gate.
Trades the underlying equity. Zero LLM tokens.
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from strategies.confirmation import momentum_base as mb
from strategies.confirmation import sector_flow as sf
from strategies.confirmation import sector_map as sm

INSTRUMENT_CLASS = 'equity'


class SectorFlowConfirmedMomentum(BaseStrategy):
    id = 'S_sector_flow_confirmed_momentum'
    name = 'Sector-Flow Confirmed Momentum'
    description = 'Momentum corroborated by sector/broad-market ETF alignment; confirm + basket modes (LONG & SHORT)'
    tier = 2
    signal_frequency = 'daily'
    min_lookback = 70
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    data_requirements = ['prices']

    def default_parameters(self) -> dict:
        return {'mode': 'confirm', 'lookback': 63, 'skip': 5, 'decile': 0.10,
                'max_each': 20, 'top_sectors': 2, 'sector_lookback': 63}

    def _mk_signal(self, prices, t, direction, regime_state, scale, extra):
        ts = prices[t].dropna()
        if len(ts) < 2:
            return None
        cur = float(ts.iloc[-1])
        if cur <= 0:
            return None
        stops = self.compute_stops_and_targets(ts, direction, cur, regime_state=regime_state)
        return Signal(ticker=t, direction=direction, entry_price=cur,
                      stop_loss=stops['stop'], target_1=stops['t1'],
                      target_2=stops['t2'], target_3=stops['t3'],
                      position_size_pct=round((1.0 / max(self.parameters['max_each'], 1)) * scale, 4),
                      confidence='MED', signal_params=extra)

    def generate_signals(self, prices, regime, universe, aux_data=None) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        if len(prices) < self.min_lookback:
            return []
        p = self.parameters
        scale = self.position_scale(regime_state)
        as_of = prices.index[-1]
        signals: List[Signal] = []

        if p['mode'] == 'basket':
            # Rank sectors by their SPDR-ETF momentum
            etf_scores = mb.momentum_scores(prices, list(sm.SECTOR_ETF.values()),
                                            p['sector_lookback'], p['skip'])
            sector_by_etf = {v: k for k, v in sm.SECTOR_ETF.items()}
            ranked = sorted(etf_scores.items(), key=lambda kv: kv[1], reverse=True)
            top = [sector_by_etf[e] for e, _ in ranked[:p['top_sectors']]]
            bot = [sector_by_etf[e] for e, _ in ranked[-p['top_sectors']:]]
            for sector, direction in ([(s, 'LONG') for s in top] + [(s, 'SHORT') for s in bot]):
                for t in sm.constituents(sector):
                    if t in universe and t in prices.columns:
                        sig = self._mk_signal(prices, t, direction, regime_state, scale,
                                              {'mode': 'basket', 'sector': sector})
                        if sig:
                            signals.append(sig)
            return signals[:self.MAX_SIGNALS]

        # confirm mode
        base_only = os.environ.get('OPENCLAW_CONFIRM_BASE_ONLY') == '1'
        scores = mb.momentum_scores(prices, universe, p['lookback'], p['skip'])
        longs, shorts = mb.rank_long_short(scores, p['decile'], p['max_each'])
        for tickers, direction in ((longs, 'LONG'), (shorts, 'SHORT')):
            for t in tickers:
                if t not in prices.columns:
                    continue
                if not base_only:
                    passes, _ = sf.confirm(direction, t, prices, sm, as_of)
                    if not passes:
                        continue
                sig = self._mk_signal(prices, t, direction, regime_state, scale,
                                      {'mode': 'confirm', 'momentum': round(float(scores.get(t, 0.0)), 4),
                                       'base_only': base_only})
                if sig:
                    signals.append(sig)
        return signals[:self.MAX_SIGNALS]
```

```json
// src/strategies/implementations/S_sector_flow_confirmed_momentum.requirements.json
{
  "strategy_id": "S_sector_flow_confirmed_momentum",
  "required": ["prices"],
  "optional": []
}
```

- [ ] **Step 4: Run test + validate_strategy**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_cross_sector_strategies.py -k sector -v`
Expected: PASS (3 passed)
Run: `cd /root/openclaw && python3 src/strategies/validate_strategy.py src/strategies/implementations/S_sector_flow_confirmed_momentum.py`
Expected: JSON `{"ok": true, ...}`

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/implementations/S_sector_flow_confirmed_momentum.py src/strategies/implementations/S_sector_flow_confirmed_momentum.requirements.json tests/strategies/test_cross_sector_strategies.py
git commit -m "feat(strategy): S_sector_flow_confirmed_momentum (sector/market-aligned momentum, confirm+basket)"
```

---

## Task 8: Backtest ② + lift test

**Files:** none (analysis task).

- [ ] **Step 1: Confirmed backtest (confirm mode, default), full window**

Run:
```bash
cd /root/openclaw && python3 -m backtest.unified_backtest \
  --strategy-file src/strategies/implementations/S_sector_flow_confirmed_momentum.py 2>&1 | tail -5
```
Record Sharpe / MaxDD / return / trades + per-regime breakdown.

- [ ] **Step 2: Base-only backtest (gate OFF) for the lift comparison**

Run:
```bash
cd /root/openclaw && OPENCLAW_CONFIRM_BASE_ONLY=1 python3 -m backtest.unified_backtest \
  --strategy-file src/strategies/implementations/S_sector_flow_confirmed_momentum.py 2>&1 | tail -5
```
Record base-only metrics.

- [ ] **Step 3: Evaluate**

Same rule as Task 6 Step 3: gate **Sharpe ≥ 0.5 AND MaxDD ≤ 0.20** on the confirmed run; lift = confirmed beats base-only on Sharpe or MaxDD. Record verdict for Task 9. If failing, STOP and report — do not register or silently overfit thresholds.

- [ ] **Step 4: No commit** (analysis only).

---

## Task 9: Conditional registration (only strategies that PASSED)

**Files:**
- Modify: `src/strategies/registry.py` (add `_IMPL_MAP` entries)
- Modify: `src/strategies/manifest.json` (add `candidate` entries)

> Only register a strategy if its Task-6/Task-8 confirmed run passed the gate (Sharpe ≥ 0.5, MaxDD ≤ 0.20). A failing strategy is reported to the operator, not registered.

- [ ] **Step 1: Add `_IMPL_MAP` entries** (for each passing strategy) in `src/strategies/registry.py`, alongside the existing canonical entries:

```python
    'S_options_flow_confirmed_momentum': ('strategies.implementations.S_options_flow_confirmed_momentum', 'OptionsFlowConfirmedMomentum'),
    'S_sector_flow_confirmed_momentum':  ('strategies.implementations.S_sector_flow_confirmed_momentum',  'SectorFlowConfirmedMomentum'),
```

- [ ] **Step 2: Verify registry loads the class**

Run:
```bash
cd /root/openclaw && python3 -c "from src.strategies.registry import load_strategy_class as L; print(L('S_options_flow_confirmed_momentum')); print(L('S_sector_flow_confirmed_momentum'))"
```
Expected: prints the two class objects (not None).

- [ ] **Step 3: Register as `candidate` in the manifest** via the lifecycle API (idempotent; backfills metadata from the backtest). Use the manifest's existing `register`/lifecycle helper rather than hand-editing JSON:

```bash
cd /root/openclaw && python3 - <<'PY'
from src.strategies.lifecycle import LifecycleStateMachine, StrategyState
lsm = LifecycleStateMachine.from_manifest('src/strategies/manifest.json')
for sid, cls, cf in [
    ('S_options_flow_confirmed_momentum', 'OptionsFlowConfirmedMomentum', 'S_options_flow_confirmed_momentum.py'),
    ('S_sector_flow_confirmed_momentum',  'SectorFlowConfirmedMomentum',  'S_sector_flow_confirmed_momentum.py'),
]:
    if sid not in lsm.manifest.get('strategies', {}):
        lsm.register(sid, canonical_file=cf, class_name=cls, instrument_class='equity',
                     state=StrategyState.CANDIDATE, actor='botjohn',
                     reason='cross-sector corroboration strategy — backtest passed equity gate')
lsm.save_manifest('src/strategies/manifest.json')
print('registered')
PY
```
> NOTE for the implementer: confirm `LifecycleStateMachine.register(...)`'s exact signature in `src/strategies/lifecycle.py` before running — match its real parameter names. If `register` does not accept an initial `state`, register then `transition(... to_state=StrategyState.CANDIDATE ...)`. Do NOT invent parameters.

- [ ] **Step 4: Full regression — confirm nothing else broke**

Run: `cd /root/openclaw && python3 -m pytest tests/strategies/test_confirmation_framework.py tests/strategies/test_cross_sector_strategies.py -v`
Expected: all PASS.
Run: `cd /root/openclaw && git diff --stat src/strategies/manifest.json` — expected: only the two new candidate entries added (no other strategy rows touched).

- [ ] **Step 5: Commit**

```bash
cd /root/openclaw
git add src/strategies/registry.py src/strategies/manifest.json
git commit -m "feat(strategy): register options-flow & sector-flow momentum as candidates

Backtest metrics (confirmed vs base-only):
- S_options_flow_confirmed_momentum: <Sharpe/MaxDD/trades> | base-only <Sharpe/MaxDD> | lift <yes/no>
- S_sector_flow_confirmed_momentum:  <Sharpe/MaxDD/trades> | base-only <Sharpe/MaxDD> | lift <yes/no>"
```

---

## Self-Review (completed by author)

- **Spec coverage:** §5 confirmation framework → Tasks 1-4. §6 Strategy ① → Tasks 5-6. §7 Strategy ② (both modes, symmetric long/short) → Tasks 7-8. §10 validation protocol (TDD, validate_strategy, backtest+per-regime, lift test, equity gate, operator-gated registration) → Tasks 6/8/9. (§8-9 backfill+③ are Plan B.)
- **Placeholder scan:** the only deferred items are (a) pasting the verified `_SECTOR_MAP` dict from a named file:line in Task 3, and (b) confirming `register()`'s real signature in Task 9 — both are explicit "use this exact existing artifact" instructions, not invent-it placeholders.
- **Type consistency:** `momentum_scores`/`rank_long_short` signatures match between Task 1 and Tasks 5/7; `of.confirm(direction, opts_row)` and `sf.confirm(direction, ticker, prices, sector_map, as_of)` match between Tasks 2/4 and 5/7; `sm.etf_for_ticker`/`constituents`/`SECTOR_ETF`/`BROAD_MARKET_ETFS` match between Tasks 3/4/7.
- **Lift-test mechanism:** `OPENCLAW_CONFIRM_BASE_ONLY=1` read in `generate_signals` for both ① and ② — consistent.
