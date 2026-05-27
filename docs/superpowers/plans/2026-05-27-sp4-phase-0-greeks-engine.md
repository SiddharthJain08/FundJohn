# SP-4 Phase 0 — Synthetic Greeks Options Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a synthetic Black-Scholes options backtest engine (priced from 10y underlying history + a calibrated IV model), greeks-aware sizing, an `option_spec` signal contract, parity validation against the 7-week real chain, empirically-calibrated promotion thresholds, and a short-straddle VRP reference strategy — so SP-4 can later originate option strategies end-to-end.

**Architecture:** A new `instrument_class='option'` simulation path (`src/backtest/options_backtest.py`) returns the *same* dict shape as the existing `_per_bar_simulate`, so `run_backtest`'s metrics + DB-write are reused unchanged. Option contracts are synthesized from underlying closes via `py_vollib` BS pricing + a realized-vol×VRP IV model, calibrated to minimize parity error vs `options_eod.parquet`. Equity/etp/crypto paths stay byte-identical (dispatch branches only on `'option'`).

**Tech Stack:** Python, `py_vollib` (BS price/greeks/IV — verified installed), `numpy`/`pandas`, `pyarrow` (parquet), `psycopg2` (existing DB write path), pytest.

## Operator gates (human-in-loop — the subagent driver MUST PAUSE, not auto-clear)

These steps are NOT subagent-executable. The driver must stop and surface to the operator:
- **Task 10 Step 4** — if parity FAILS, surface before any calibration (do not proceed to Task 11).
- **Task 11 Step 3** — proposed `PROMOTION_THRESHOLDS` numbers need operator sign-off before the `lifecycle.py` commit (they gate real promotions).
- **Task 12 Step 2** — `./scripts/regen-integrity-manifest.sh` runs on the **VPS** (manifest is gitignored/local-per-VPS), not in the worktree.
- **Task 12 Step 5** — merge / push / VPS-pull / promote-reference are operator decisions. Do NOT merge or deploy.

---

## Grounding facts (verified against live source 2026-05-27)

- `Signal` dataclass: `src/strategies/base.py:21` — fields `ticker, direction, entry_price, stop_loss, target_1, target_2, target_3, position_size_pct, confidence, signal_params={}, features={}`. `direction` vocab already includes `SELL_VOL | BUY_VOL | FLAT`.
- `BaseStrategy`: `src/strategies/base.py` — `id/name/min_lookback/instrument_class/active_in_regimes/MAX_SIGNALS` class attrs; `generate_signals(self, prices, regime, universe, aux_data=None)`; helpers `position_scale(regime_state)`, `compute_stops_and_targets(...)`.
- Backtest entry: `src/backtest/unified_backtest.py:580` `run_backtest(strategy_id, *, filepath=None, start_date=DEFAULT_START_DATE, end_date=None, max_hold_days=DEFAULT_MAX_HOLD_DAYS, conn=None, commit=True, resolver=None, instrument_class='equity')`. `DEFAULT_START_DATE='2016-04-11'`, `DEFAULT_MAX_HOLD_DAYS=21`.
- The simulate call site (inside `run_backtest`, ~line 620): `sim = _per_bar_simulate(instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt, strategy_id=strategy_id, resolver=resolver, max_hold_days=max_hold_days)`. Returns `{'trades','universe_sizes','days_processed','days_with_signals','static_universe','min_lookback'}`.
- Trade dict shape consumed by `aggregate_metrics`: each trade needs `pnl_pct` (fractional return), `holding_days` (int), `entry_date`; plus `ticker/direction/entry_date/exit_date/entry_price/exit_price/exit_reason/entry_regime` for the trades table. `total_max_dd_pct` is written as a PERCENTAGE (×100); promotion thresholds are FRACTIONS.
- `load_prices_panels()` → `(close_wide, bars_by_ticker)`; `bars_by_ticker[T]` is a DataFrame indexed by date with `open/high/low/close`. Underlying must exist there.
- `PROMOTION_THRESHOLDS` at `src/strategies/lifecycle.py:97`; option = `TODO(SP-4)` placeholder (line 102); crypto `min_sharpe` = `TODO(SP-3.2)` placeholder (line 104), MaxDD=0.70 operator-set.
- Sizer: `src/execution/instrument_class_sizer.py:apply_instrument_class_sizing(order, instrument_class)`; option branch scales `order['notional_usd']` by `|delta|` fail-open. `_PASS_THROUGH={equity,etp,crypto}`.
- Registry: `src/strategies/registry.py:_IMPL_MAP` dict `id → (module_path, ClassName)`, e.g. `'S_btc_momentum': ('strategies.implementations.S_btc_momentum','BtcMomentum')`.
- Manifest: `src/strategies/manifest.json` → `strategies` is a list of `[id, {state, state_since, metadata:{canonical_file,class,description}, history:[...], instrument_class}]`.
- `.requirements.json`: `{"strategy_id":..., "required":[...], "optional":[...]}`.
- Real options data: `data/master/options_eod.parquet` (708k rows, 2026-04-08→2026-05-26, greeks ~99.8% non-zero, IV ~99.1%, OI 0%) with cols `ticker,date,expiry,strike,option_type,market_price,implied_volatility,delta,gamma,theta,vega,...`.
- `py_vollib` API: `from py_vollib.black_scholes import black_scholes`; `from py_vollib.black_scholes.greeks import analytical` (`.delta/.gamma/.theta/.vega`); `from py_vollib.black_scholes.implied_volatility import implied_volatility`. Flag `'c'|'p'`, signature `(flag, S, K, t, r, sigma)`; IV `(price, S, K, t, r, flag)`.
- Test import pattern: `ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'src'))`. No conftest/pytest.ini. Run via `python3 -m pytest tests/<file> -v` from repo root.

## PnL convention (used throughout the engine)

To keep option returns comparable with equity returns for `aggregate_metrics`, **one contract per signal on a 100× multiplier**, and `pnl_pct` is computed **per unit of underlying notional**:

```
pnl_pct = cycle_pnl_dollars / (entry_underlying_price * 100.0)
```

`cycle_pnl_dollars` = signed option mark change over the cycle (entry→roll/expiry) + delta-hedge PnL − costs. This yields small equity-scale returns (a short straddle collecting ~3% premium reads ~3%), so Sharpe/MaxDD are interpretable on the same scale as equity strategies. Real position sizing happens at the sizer/live layer, not in the backtest.

## File structure

| File | Responsibility |
|---|---|
| `src/strategies/base.py` (modify) | Add `OptionSpec` dataclass + optional `option_spec` field on `Signal` |
| `src/backtest/options_pricing.py` (new) | Thin `py_vollib` wrapper: price, greeks, IV inversion, strike-from-target-delta, monthly-expiry calendar |
| `src/backtest/synthetic_iv.py` (new) | `realized_vol` + `synthetic_iv` (realized-vol×VRP, VIX-anchor hook) |
| `src/backtest/options_backtest.py` (new) | `simulate(...)` priced-contract path returning the `_per_bar_simulate` dict shape |
| `src/backtest/unified_backtest.py` (modify) | `run_backtest` dispatch to options path on `instrument_class=='option'` |
| `src/execution/instrument_class_sizer.py` (modify) | Real greeks-aware option sizing (inert live until an option strategy is live) |
| `src/strategies/implementations/S_short_straddle_vrp.py` (+`.requirements.json`) (new) | Reference strategy |
| `src/strategies/registry.py` (modify) | `_IMPL_MAP` entry for the reference strategy |
| `src/strategies/manifest.json` (modify) | Candidate manifest entry, `instrument_class='option'` |
| `scripts/options_parity_check.py` (new) | Synthetic vs real-chain PnL parity + VRP calibration |
| `scripts/calibrate_option_thresholds.py` (new) | Threshold calibration after parity passes |
| `tests/test_options_pricing.py`, `tests/test_synthetic_iv.py`, `tests/test_options_backtest.py`, `tests/test_options_sizer.py` (new) | Unit/regression tests |

---

## Task 0: Worktree setup (precondition)

Execution happens in an isolated worktree off `feat/sp4-phase-0-greeks-engine` (created via the using-git-worktrees skill / `EnterWorktree`). The worktree does NOT contain the gitignored master data or `.env`, so:

- [ ] **Step 1: Symlink the master data (gitignored parquets the backtest/parity tasks read)**

```bash
# from the worktree root
mkdir -p data
[ -e data/master ] || ln -s /root/openclaw/data/master data/master
ls -l data/master/options_eod.parquet data/master/prices.parquet  # both must resolve
```

- [ ] **Step 2: Confirm DB + broker creds are reachable (read .env, never `source` it — unquoted parens break bash)**

```bash
grep -E '^(POSTGRES_URI|ALPACA_API_KEY|ALPACA_SECRET)' /root/openclaw/.env >/dev/null && echo "creds present"
# Tasks 9/11 need POSTGRES_URI exported in the shell that runs them:
export POSTGRES_URI="$(grep -E '^POSTGRES_URI=' /root/openclaw/.env | cut -d= -f2-)"
python3 -c "import os,psycopg2; psycopg2.connect(os.environ['POSTGRES_URI']).close(); print('db ok')"
```

- [ ] **Step 3: Confirm the test runner works from the worktree root**

```bash
python3 -m pytest tests/test_instrument_class_sizer.py -q   # baseline green before any change
```

No commit (environment setup only).

---

## Task 1: `OptionSpec` dataclass + optional `option_spec` on `Signal`

**Files:**
- Modify: `src/strategies/base.py` (after the `Signal` dataclass, ~line 38)
- Test: `tests/test_options_backtest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_backtest.py
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from strategies.base import Signal, OptionSpec  # noqa


def test_signal_backward_compatible_without_option_spec():
    s = Signal(ticker='AAPL', direction='LONG', entry_price=100.0,
               stop_loss=93.0, target_1=108.0, target_2=0.0, target_3=0.0,
               position_size_pct=0.05, confidence='MED')
    assert s.option_spec is None


def test_option_spec_defaults():
    spec = OptionSpec(underlying='SPY', right='call')
    assert spec.strike_rule == 'target_delta'
    assert spec.target_delta == 0.30
    assert spec.dte_target == 30
    assert spec.structure == 'single'
    assert spec.hedge == 'none'
    assert spec.roll_dte == 7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_backtest.py -v`
Expected: FAIL — `ImportError: cannot import name 'OptionSpec'`

- [ ] **Step 3: Implement `OptionSpec` + add field to `Signal`**

In `src/strategies/base.py`, add after the imports the dataclass and a field on `Signal`:

```python
@dataclass
class OptionSpec:
    """Declarative option contract spec carried on a Signal (SP-4 Phase 0).

    The backtest engine (options_backtest.py) and a future live executor read
    the SAME spec to select a contract — parity by construction. Equity/crypto/
    etp signals leave Signal.option_spec=None and behave byte-identically.
    """
    underlying:    str                       # e.g. 'SPY'
    right:         str = 'call'              # 'call' | 'put' (per-leg; ignored for straddle/strangle)
    strike_rule:   str = 'target_delta'      # 'target_delta' | 'atm' | 'fixed_moneyness'
    target_delta:  float = 0.30              # used when strike_rule='target_delta'
    moneyness:     Optional[float] = None    # K/S, used when strike_rule='fixed_moneyness'
    dte_target:    int = 30                  # nearest monthly expiry >= this many calendar days
    structure:     str = 'single'            # 'single' | 'straddle' | 'strangle'
    hedge:         str = 'none'              # 'none' | 'delta'
    hedge_cadence: str = 'daily'             # rehedge frequency when hedge='delta'
    roll_dte:      int = 7                   # roll when remaining DTE <= this
    hold_to_expiry: bool = False             # income legs may hold to expiry instead of rolling
```

Then add to the `Signal` dataclass (after the `features` field):

```python
    # SP-4 Phase 0: option contract spec for instrument_class='option' strategies.
    # None for equity/crypto/etp — keeps every existing strategy byte-identical.
    option_spec: Optional['OptionSpec'] = None
```

Note: `OptionSpec` must be defined ABOVE `Signal`, or use the forward-ref string `'OptionSpec'` (shown above) with `OptionSpec` defined either before or after — define it immediately before `Signal` to be safe. `Optional` is already imported in base.py.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_backtest.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add src/strategies/base.py tests/test_options_backtest.py
git commit -m "feat(sp4-p0): OptionSpec dataclass + optional option_spec on Signal"
```

---

## Task 2: `options_pricing.py` — BS wrapper + strike-from-delta + expiry calendar

**Files:**
- Create: `src/backtest/options_pricing.py`
- Test: `tests/test_options_pricing.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_pricing.py
import sys, math
from pathlib import Path
from datetime import date
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest.options_pricing import (
    bs_price, bs_greeks, implied_vol, strike_for_target_delta,
    nearest_monthly_expiry, RISK_FREE,
)


def test_bs_price_atm_call_known_value():
    # S=K=100, t=0.5, r=0.01, sigma=0.2 → ~5.88 (py_vollib reference)
    p = bs_price('c', 100, 100, 0.5, 0.2)
    assert abs(p - 5.876) < 0.01


def test_bs_greeks_atm_call_delta_near_half():
    g = bs_greeks('c', 100, 100, 0.5, 0.2)
    assert 0.50 < g['delta'] < 0.58
    assert g['vega'] > 0


def test_iv_round_trip():
    price = bs_price('c', 100, 100, 0.5, 0.30)
    iv = implied_vol(price, 'c', 100, 100, 0.5)
    assert abs(iv - 0.30) < 1e-3


def test_strike_for_target_delta_calls_otm():
    # 30-delta call must be above spot (OTM)
    K = strike_for_target_delta('c', S=100, t=30/365, sigma=0.2, target_delta=0.30)
    assert K > 100
    # verify the produced strike actually has ~0.30 delta
    g = bs_greeks('c', 100, K, 30/365, 0.2)
    assert abs(g['delta'] - 0.30) < 0.03


def test_nearest_monthly_expiry_at_least_dte():
    exp = nearest_monthly_expiry(date(2024, 1, 2), dte_target=30)
    assert (exp - date(2024, 1, 2)).days >= 30
    assert exp.weekday() == 4  # Friday
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_pricing.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `options_pricing.py`**

```python
# src/backtest/options_pricing.py
"""Black-Scholes pricing/greeks wrapper over py_vollib for the synthetic
options backtest engine (SP-4 Phase 0). All functions are pure + deterministic.
Risk-free rate is a fixed constant — the realized-vol×VRP IV model dominates
pricing error far more than the short-rate, and we have no clean historical
short-rate series wired in. Revisit if parity (scripts/options_parity_check.py)
shows rate sensitivity matters.
"""
from __future__ import annotations
from datetime import date, timedelta
from scipy.optimize import brentq
from py_vollib.black_scholes import black_scholes as _bs
from py_vollib.black_scholes.greeks import analytical as _greeks

RISK_FREE = 0.04  # flat annual risk-free; see module docstring


def bs_price(flag: str, S: float, K: float, t: float, sigma: float,
             r: float = RISK_FREE) -> float:
    """flag 'c'|'p'; t in years. Guards degenerate t/sigma."""
    t = max(float(t), 1e-6)
    sigma = max(float(sigma), 1e-4)
    return float(_bs(flag, float(S), float(K), t, r, sigma))


def bs_greeks(flag: str, S: float, K: float, t: float, sigma: float,
              r: float = RISK_FREE) -> dict:
    t = max(float(t), 1e-6)
    sigma = max(float(sigma), 1e-4)
    return {
        'delta': float(_greeks.delta(flag, S, K, t, r, sigma)),
        'gamma': float(_greeks.gamma(flag, S, K, t, r, sigma)),
        'theta': float(_greeks.theta(flag, S, K, t, r, sigma)),
        'vega':  float(_greeks.vega(flag, S, K, t, r, sigma)),
    }


def implied_vol(price: float, flag: str, S: float, K: float, t: float,
                r: float = RISK_FREE) -> float:
    from py_vollib.black_scholes.implied_volatility import implied_volatility
    return float(implied_volatility(float(price), float(S), float(K),
                                    max(float(t), 1e-6), r, flag))


def strike_for_target_delta(flag: str, S: float, t: float, sigma: float,
                            target_delta: float, r: float = RISK_FREE) -> float:
    """Solve for the strike whose |delta| == target_delta at (S, t, sigma).
    Calls: strike increases as delta decreases (OTM). Puts: |delta|.
    """
    t = max(float(t), 1e-6)
    sigma = max(float(sigma), 1e-4)
    td = abs(float(target_delta))

    def f(K):
        return abs(_greeks.delta(flag, S, K, t, r, sigma)) - td

    # Bracket: deep ITM (high |delta|) to deep OTM (low |delta|).
    lo, hi = S * 0.30, S * 3.0
    # Ensure sign change; f is monotonic in K for fixed flag.
    try:
        return float(brentq(f, lo, hi, maxiter=100, xtol=1e-4))
    except ValueError:
        # Fall back to ATM if the target delta is unreachable in the bracket.
        return float(S)


def nearest_monthly_expiry(as_of: date, dte_target: int) -> date:
    """Nearest standard monthly expiry (3rd Friday) at least `dte_target`
    calendar days after as_of."""
    def third_friday(year: int, month: int) -> date:
        d = date(year, month, 1)
        # weekday(): Mon=0..Sun=6; Friday=4. First Friday:
        offset = (4 - d.weekday()) % 7
        first_friday = d + timedelta(days=offset)
        return first_friday + timedelta(days=14)

    earliest = as_of + timedelta(days=int(dte_target))
    y, m = as_of.year, as_of.month
    for _ in range(18):  # search up to 18 months out
        tf = third_friday(y, m)
        if tf >= earliest:
            return tf
        m += 1
        if m > 12:
            m = 1; y += 1
    return third_friday(y, m)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_pricing.py -v`
Expected: PASS (5 tests). If `test_bs_price_atm_call_known_value` is off, print the actual `bs_price('c',100,100,0.5,0.2)` and adjust the assertion to the real py_vollib value (~5.876) — do NOT change the implementation to hit a guessed number.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/options_pricing.py tests/test_options_pricing.py
git commit -m "feat(sp4-p0): BS pricing/greeks wrapper + strike-from-delta + expiry calendar"
```

---

## Task 3: `synthetic_iv.py` — realized-vol × VRP IV model

**Files:**
- Create: `src/backtest/synthetic_iv.py`
- Test: `tests/test_synthetic_iv.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_synthetic_iv.py
import sys
from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from backtest.synthetic_iv import realized_vol, synthetic_iv


def _series(n=300, daily_sigma=0.01, seed=0):
    rng = np.random.default_rng(seed)
    rets = rng.normal(0, daily_sigma, n)
    px = 100 * np.cumprod(1 + rets)
    return pd.Series(px, index=pd.date_range('2020-01-01', periods=n, freq='D'))


def test_realized_vol_recovers_input_sigma():
    s = _series(daily_sigma=0.01)
    rv = realized_vol(s, window=60)
    # annualized ~ 0.01*sqrt(252) ≈ 0.159
    assert 0.10 < rv < 0.22


def test_synthetic_iv_applies_vrp_markup():
    s = _series(daily_sigma=0.01)
    rv = realized_vol(s, window=60)
    iv = synthetic_iv(s, vrp_factor=1.2, window=60)
    assert abs(iv - rv * 1.2) < 1e-9


def test_synthetic_iv_has_floor():
    s = pd.Series([100.0] * 100, index=pd.date_range('2020-01-01', periods=100))
    iv = synthetic_iv(s, vrp_factor=1.0, window=60)
    assert iv >= 0.05  # floor prevents zero-vol degenerate pricing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_synthetic_iv.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `synthetic_iv.py`**

```python
# src/backtest/synthetic_iv.py
"""Synthetic implied-vol model for the options backtest engine (SP-4 Phase 0).

IV(underlying, as_of, tenor) = realized_vol(trailing window) * VRP_factor,
floored. The VRP_factor (and window) are CALIBRATED by
scripts/options_parity_check.py against the ~7 weeks of real chain IV in
options_eod.parquet — do not treat the defaults here as authoritative until
that script has run and recorded a value.

Index options (^-prefixed underlyings) MAY be anchored to a vol index via the
optional vix_series hook; per-name options use the realized-vol proxy.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

IV_FLOOR = 0.05
DEFAULT_WINDOW = 21          # trading days
DEFAULT_VRP_FACTOR = 1.15    # placeholder; calibrated by options_parity_check.py
TRADING_DAYS = 252


def realized_vol(prices: pd.Series, window: int = DEFAULT_WINDOW) -> float:
    """Annualized close-to-close realized vol over the trailing `window` days."""
    s = prices.dropna()
    if len(s) < 3:
        return IV_FLOOR
    rets = s.pct_change().dropna().iloc[-window:]
    if len(rets) < 2:
        return IV_FLOOR
    return float(rets.std(ddof=1) * np.sqrt(TRADING_DAYS))


def synthetic_iv(prices: pd.Series, vrp_factor: float = DEFAULT_VRP_FACTOR,
                 window: int = DEFAULT_WINDOW) -> float:
    """Modeled IV for an underlying as of the last bar in `prices`."""
    rv = realized_vol(prices, window=window)
    return max(IV_FLOOR, float(rv) * float(vrp_factor))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_synthetic_iv.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/backtest/synthetic_iv.py tests/test_synthetic_iv.py
git commit -m "feat(sp4-p0): synthetic realized-vol x VRP IV model"
```

---

## Task 4: `options_backtest.simulate` — single-leg pricing path

**Files:**
- Create: `src/backtest/options_backtest.py`
- Test: `tests/test_options_backtest.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_backtest.py`:

```python
import numpy as np, pandas as pd
from strategies.base import BaseStrategy, Signal, OptionSpec
from backtest import options_backtest


def _trending_panels(n=400, drift=0.0008, seed=1):
    rng = np.random.default_rng(seed)
    rets = rng.normal(drift, 0.01, n)
    px = 100 * np.cumprod(1 + rets)
    idx = pd.date_range('2022-01-03', periods=n, freq='B')
    close = pd.Series(px, index=idx)
    close_wide = pd.DataFrame({'SPY': close})
    bars = pd.DataFrame({'open': close, 'high': close * 1.005,
                         'low': close * 0.995, 'close': close}, index=idx)
    return close_wide, {'SPY': bars}


class _LongCallStrat(BaseStrategy):
    id = 'T_long_call'; name = 'test long call'; min_lookback = 30
    instrument_class = 'option'; MAX_SIGNALS = 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def generate_signals(self, prices, regime, universe, aux_data=None):
        if 'SPY' not in prices.columns or len(prices) < self.min_lookback:
            return []
        # one-shot signal on the first eligible bar only
        if len(prices) != self.min_lookback + 5:
            return []
        S = float(prices['SPY'].iloc[-1])
        return [Signal(ticker='SPY', direction='LONG', entry_price=S,
                       stop_loss=S * 0.9, target_1=S * 1.1, target_2=0.0, target_3=0.0,
                       position_size_pct=0.05, confidence='MED',
                       option_spec=OptionSpec(underlying='SPY', right='call',
                                              structure='single', dte_target=30))]


def test_single_leg_long_call_produces_trades():
    close_wide, bars = _trending_panels()
    regimes = pd.Series('LOW_VOL', index=close_wide.index)
    inst = _LongCallStrat(); inst.active_in_regimes = list(['LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'])
    out = options_backtest.simulate(inst, close_wide, bars, regimes,
                                    close_wide.index[0], close_wide.index[-1],
                                    strategy_id='T_long_call', vrp_factor=1.1)
    assert out['days_with_signals'] >= 1
    assert len(out['trades']) >= 1
    t = out['trades'][0]
    for k in ('ticker', 'direction', 'entry_date', 'exit_date', 'pnl_pct', 'holding_days', 'entry_regime'):
        assert k in t
    assert isinstance(t['pnl_pct'], float)
    # long call in an up-trending tape should not be catastrophically negative
    assert t['pnl_pct'] > -1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_backtest.py::test_single_leg_long_call_produces_trades -v`
Expected: FAIL — `options_backtest` has no `simulate`.

- [ ] **Step 3: Implement `options_backtest.py` (single-leg)**

```python
# src/backtest/options_backtest.py
"""Synthetic options backtest path (SP-4 Phase 0). Dispatched from
unified_backtest.run_backtest when instrument_class='option'. Returns the SAME
dict shape as unified_backtest._per_bar_simulate so the downstream metrics +
DB-write are reused unchanged.

Contracts are synthesized from underlying closes via Black-Scholes
(options_pricing) + a calibrated realized-vol×VRP IV (synthetic_iv). One
contract per signal on a 100x multiplier; pnl_pct = cycle_pnl / (entry_S*100)
(see plan "PnL convention"). Single-leg here; straddle/strangle + delta-hedge
+ roll/expiry land in Tasks 5-6.
"""
from __future__ import annotations
import pandas as pd
from datetime import date

from backtest.options_pricing import (bs_price, bs_greeks, strike_for_target_delta,
                                       nearest_monthly_expiry, RISK_FREE)
from backtest.synthetic_iv import synthetic_iv

MULTIPLIER = 100.0
COST_PER_CONTRACT_BPS = 5.0  # mirrors INSTRUMENT_COST_BPS['option']; on premium notional


def _as_date(ts) -> date:
    return ts.date() if hasattr(ts, 'date') else ts


def _select_strike(spec, S: float, t_years: float, sigma: float, flag: str) -> float:
    if spec.strike_rule == 'atm':
        return float(S)
    if spec.strike_rule == 'fixed_moneyness' and spec.moneyness:
        return float(S * spec.moneyness)
    return strike_for_target_delta(flag, S, t_years, sigma, spec.target_delta)


def _flag_for(right: str) -> str:
    return 'p' if str(right).lower().startswith('p') else 'c'


def _price_single_cycle(spec, close: pd.Series, entry_dt, sign: int,
                        vrp_factor: float, window: int, max_hold_days: int) -> dict:
    """Price ONE single-leg cycle from entry_dt forward. Returns a trade dict
    or None if it can't be priced. sign +1 = long the option, -1 = short."""
    idx = close.index
    fut = idx[idx > entry_dt]
    if len(fut) == 0:
        return None
    S0 = float(close.loc[entry_dt])
    sigma0 = synthetic_iv(close.loc[:entry_dt], vrp_factor=vrp_factor, window=window)
    flag = _flag_for(spec.right)
    expiry = nearest_monthly_expiry(_as_date(entry_dt), spec.dte_target)
    t0 = max((expiry - _as_date(entry_dt)).days / 365.0, 1e-6)
    K = _select_strike(spec, S0, t0, sigma0, flag)
    entry_premium = bs_price(flag, S0, K, t0, sigma0)
    if entry_premium <= 0:
        return None

    # Walk forward to expiry / roll_dte / max_hold, marking the option each bar.
    exit_dt, exit_premium, reason = None, None, None
    held = 0
    for dt in fut[:max_hold_days]:
        held += 1
        cur_date = _as_date(dt)
        dte = (expiry - cur_date).days
        S = float(close.loc[dt])
        if dte <= 0:
            exit_premium = max(0.0, (S - K) if flag == 'c' else (K - S))  # intrinsic
            exit_dt, reason = dt, 'expiry'
            break
        if (not spec.hold_to_expiry) and dte <= spec.roll_dte:
            sig_t = synthetic_iv(close.loc[:dt], vrp_factor=vrp_factor, window=window)
            exit_premium = bs_price(flag, S, K, max(dte / 365.0, 1e-6), sig_t)
            exit_dt, reason = dt, 'roll'
            break
    if exit_dt is None:  # ran out of window
        dt = fut[:max_hold_days][-1]
        S = float(close.loc[dt]); cur_date = _as_date(dt)
        dte = max((expiry - cur_date).days, 0)
        sig_t = synthetic_iv(close.loc[:dt], vrp_factor=vrp_factor, window=window)
        exit_premium = (bs_price(flag, S, K, max(dte / 365.0, 1e-6), sig_t)
                        if dte > 0 else max(0.0, (S - K) if flag == 'c' else (K - S)))
        exit_dt, reason = dt, 'max_hold'

    cost = (entry_premium + exit_premium) * (COST_PER_CONTRACT_BPS / 1e4)
    cycle_pnl = sign * (exit_premium - entry_premium) * MULTIPLIER - cost * MULTIPLIER
    pnl_pct = cycle_pnl / (S0 * MULTIPLIER)
    return {
        'entry_date': _as_date(entry_dt), 'exit_date': _as_date(exit_dt),
        'entry_price': round(entry_premium, 4), 'exit_price': round(exit_premium, 4),
        'exit_reason': reason, 'holding_days': held, 'pnl_pct': float(pnl_pct),
        'strike': round(K, 2), 'expiry': expiry.isoformat(), 'iv_entry': round(sigma0, 4),
    }


def simulate(instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt, *,
             strategy_id=None, resolver=None, max_hold_days=21,
             vrp_factor=None, window=None):
    from backtest.synthetic_iv import DEFAULT_VRP_FACTOR, DEFAULT_WINDOW
    vrp_factor = DEFAULT_VRP_FACTOR if vrp_factor is None else vrp_factor
    window = DEFAULT_WINDOW if window is None else window

    min_lookback = getattr(instance, 'min_lookback', 20)
    static_universe = list(close_wide.columns)
    oos_dates = close_wide.loc[start_dt:end_dt].index

    trades, days_processed, days_with_signals = [], 0, 0
    SIGN = {'LONG': 1, 'BUY': 1, 'BUY_VOL': 1, 'SHORT': -1, 'SELL': -1, 'SELL_VOL': -1}

    for current_date in oos_dates:
        prices_to_date = close_wide.loc[:current_date]
        if len(prices_to_date) < min_lookback + 5:
            continue
        regime_state = regimes.get(current_date, None)
        if regime_state is None or pd.isna(regime_state):
            continue
        regime_payload = {'state': str(regime_state), 'date': _as_date(current_date).isoformat()}
        try:
            signals = instance.generate_signals(prices_to_date, regime_payload,
                                                static_universe, aux_data={'options': {}})
        except TypeError:
            signals = instance.generate_signals(prices_to_date, regime_payload, static_universe)
        except Exception:
            continue
        days_processed += 1
        if not signals:
            continue
        days_with_signals += 1

        for sig in signals[:instance.MAX_SIGNALS]:
            spec = getattr(sig, 'option_spec', None)
            if spec is None or spec.structure != 'single':
                continue  # Task 5 handles straddle/strangle
            sign = SIGN.get(str(sig.direction).upper(), 0)
            if sign == 0:
                continue
            ul = spec.underlying
            if ul not in close_wide.columns:
                continue
            cyc = _price_single_cycle(spec, close_wide[ul].dropna(), current_date,
                                      sign, vrp_factor, window, max_hold_days)
            if cyc is None:
                continue
            cyc.update({'ticker': ul,
                        'direction': 'long' if sign > 0 else 'short',
                        'entry_regime': str(regime_state)})
            trades.append(cyc)

    return {'trades': trades, 'universe_sizes': [], 'days_processed': days_processed,
            'days_with_signals': days_with_signals, 'static_universe': static_universe,
            'min_lookback': min_lookback}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_backtest.py -v`
Expected: PASS (all Task-1 + this test).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/options_backtest.py tests/test_options_backtest.py
git commit -m "feat(sp4-p0): options_backtest.simulate single-leg priced-contract path"
```

---

## Task 5: straddle/strangle multi-leg + delta-hedge loop

**Files:**
- Modify: `src/backtest/options_backtest.py`
- Test: `tests/test_options_backtest.py` (extend)

- [ ] **Step 1: Write the failing test**

Append:

```python
class _ShortStraddleStrat(BaseStrategy):
    id = 'T_short_straddle'; name = 'test straddle'; min_lookback = 30
    instrument_class = 'option'; MAX_SIGNALS = 1
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def generate_signals(self, prices, regime, universe, aux_data=None):
        if 'SPY' not in prices.columns or len(prices) != self.min_lookback + 5:
            return []
        S = float(prices['SPY'].iloc[-1])
        return [Signal(ticker='SPY', direction='SELL_VOL', entry_price=S,
                       stop_loss=S * 0.9, target_1=S * 1.1, target_2=0.0, target_3=0.0,
                       position_size_pct=0.05, confidence='MED',
                       option_spec=OptionSpec(underlying='SPY', structure='straddle',
                                              hedge='delta', dte_target=30, roll_dte=7))]


def test_short_straddle_with_delta_hedge_produces_trade():
    # Calm tape (low realized vol) but priced with a VRP markup → short vol earns the premium.
    close_wide, bars = _trending_panels(drift=0.0, seed=7)
    regimes = pd.Series('LOW_VOL', index=close_wide.index)
    inst = _ShortStraddleStrat()
    out = options_backtest.simulate(inst, close_wide, bars, regimes,
                                    close_wide.index[0], close_wide.index[-1],
                                    strategy_id='T_short_straddle', vrp_factor=1.3)
    assert len(out['trades']) >= 1
    t = out['trades'][0]
    assert t['direction'] == 'short'
    assert 'hedge_pnl_pct' in t
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_backtest.py::test_short_straddle_with_delta_hedge_produces_trade -v`
Expected: FAIL — the `spec.structure != 'single'` branch currently `continue`s, so zero trades.

- [ ] **Step 3: Implement the multi-leg + delta-hedge cycle**

Add to `options_backtest.py` a `_price_multileg_cycle(...)` and route to it in `simulate` when `spec.structure in ('straddle','strangle')`:

```python
def _legs_for(spec, S, t_years, sigma):
    """Return list of (flag, K) legs for a straddle/strangle."""
    if spec.structure == 'straddle':
        return [('c', S), ('p', S)]  # ATM call + put
    # strangle: OTM call + OTM put at target_delta
    Kc = strike_for_target_delta('c', S, t_years, sigma, spec.target_delta)
    Kp = strike_for_target_delta('p', S, t_years, sigma, spec.target_delta)
    return [('c', Kc), ('p', Kp)]


def _price_multileg_cycle(spec, close, entry_dt, sign, vrp_factor, window, max_hold_days):
    """sign +1 long the structure, -1 short. Delta-hedged daily when spec.hedge=='delta'.
    Option PnL and hedge PnL are tracked separately; pnl_pct sums both over the cycle."""
    idx = close.index
    fut = idx[idx > entry_dt]
    if len(fut) == 0:
        return None
    S0 = float(close.loc[entry_dt])
    sigma0 = synthetic_iv(close.loc[:entry_dt], vrp_factor=vrp_factor, window=window)
    expiry = nearest_monthly_expiry(_as_date(entry_dt), spec.dte_target)
    t0 = max((expiry - _as_date(entry_dt)).days / 365.0, 1e-6)
    legs = _legs_for(spec, S0, t0, sigma0)
    entry_prem = sum(bs_price(f, S0, K, t0, sigma0) for f, K in legs)
    if entry_prem <= 0:
        return None

    def net_delta(S, t, sig):
        return sum(bs_greeks(f, S, K, max(t, 1e-6), sig)['delta'] for f, K in legs)

    hedge_units = 0.0   # shares of underlying held to offset option delta (per 1 structure)
    hedge_cost_bps = 1.0
    hedge_pnl = 0.0
    prev_S = S0
    # initial hedge: short option position has delta = sign * net_delta; hedge flattens it
    if spec.hedge == 'delta':
        target_units = -sign * net_delta(S0, t0, sigma0) * MULTIPLIER
        hedge_pnl -= abs(target_units - hedge_units) * S0 * (hedge_cost_bps / 1e4)
        hedge_units = target_units

    exit_dt = exit_prem = reason = None
    held = 0
    for dt in fut[:max_hold_days]:
        held += 1
        cur = _as_date(dt); S = float(close.loc[dt]); dte = (expiry - cur).days
        # hedge mark-to-market on underlying move
        hedge_pnl += hedge_units * (S - prev_S)
        prev_S = S
        if dte <= 0:
            exit_prem = sum(max(0.0, (S - K) if f == 'c' else (K - S)) for f, K in legs)
            exit_dt, reason = dt, 'expiry'; break
        sig_t = synthetic_iv(close.loc[:dt], vrp_factor=vrp_factor, window=window)
        if (not spec.hold_to_expiry) and dte <= spec.roll_dte:
            exit_prem = sum(bs_price(f, S, K, dte / 365.0, sig_t) for f, K in legs)
            exit_dt, reason = dt, 'roll'; break
        # daily re-hedge
        if spec.hedge == 'delta':
            target_units = -sign * net_delta(S, dte / 365.0, sig_t) * MULTIPLIER
            hedge_pnl -= abs(target_units - hedge_units) * S * (hedge_cost_bps / 1e4)
            hedge_units = target_units
    if exit_dt is None:
        dt = fut[:max_hold_days][-1]; S = float(close.loc[dt]); cur = _as_date(dt)
        dte = max((expiry - cur).days, 0)
        sig_t = synthetic_iv(close.loc[:dt], vrp_factor=vrp_factor, window=window)
        exit_prem = (sum(bs_price(f, S, K, max(dte / 365.0, 1e-6), sig_t) for f, K in legs)
                     if dte > 0 else sum(max(0.0, (S - K) if f == 'c' else (K - S)) for f, K in legs))
        exit_dt, reason = dt, 'max_hold'

    cost = (entry_prem + exit_prem) * (COST_PER_CONTRACT_BPS / 1e4)
    option_pnl = sign * (exit_prem - entry_prem) * MULTIPLIER - cost * MULTIPLIER
    cycle_pnl = option_pnl + hedge_pnl
    base = S0 * MULTIPLIER
    return {
        'entry_date': _as_date(entry_dt), 'exit_date': _as_date(exit_dt),
        'entry_price': round(entry_prem, 4), 'exit_price': round(exit_prem, 4),
        'exit_reason': reason, 'holding_days': held,
        'pnl_pct': float(cycle_pnl / base),
        'option_pnl_pct': float(option_pnl / base),
        'hedge_pnl_pct': float(hedge_pnl / base),
        'expiry': expiry.isoformat(), 'iv_entry': round(sigma0, 4),
    }
```

In `simulate`, replace the single-leg-only branch:

```python
            spec = getattr(sig, 'option_spec', None)
            if spec is None:
                continue
            sign = SIGN.get(str(sig.direction).upper(), 0)
            if sign == 0:
                continue
            ul = spec.underlying
            if ul not in close_wide.columns:
                continue
            if spec.structure == 'single':
                cyc = _price_single_cycle(spec, close_wide[ul].dropna(), current_date,
                                          sign, vrp_factor, window, max_hold_days)
            else:
                cyc = _price_multileg_cycle(spec, close_wide[ul].dropna(), current_date,
                                            sign, vrp_factor, window, max_hold_days)
            if cyc is None:
                continue
            cyc.update({'ticker': ul, 'direction': 'long' if sign > 0 else 'short',
                        'entry_regime': str(regime_state)})
            trades.append(cyc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_backtest.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/options_backtest.py tests/test_options_backtest.py
git commit -m "feat(sp4-p0): straddle/strangle multi-leg + daily delta-hedge cycle"
```

---

## Task 6: roll-then-reopen (continuous holding across rolls)

**Files:**
- Modify: `src/backtest/options_backtest.py`
- Test: `tests/test_options_backtest.py` (extend)

Rationale: Tasks 4-5 close the position at `roll_dte` and stop. A real continuously-held strategy re-opens a fresh contract after each roll until the strategy's signal changes or `max_hold_days` is exhausted, producing a *sequence* of cycles per signal. This keeps holding-day accounting honest for the portfolio curve.

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_roll_produces_multiple_cycles_over_long_hold():
    close_wide, bars = _trending_panels(n=400, drift=0.0, seed=3)
    regimes = pd.Series('LOW_VOL', index=close_wide.index)
    inst = _ShortStraddleStrat()
    out = options_backtest.simulate(inst, close_wide, bars, regimes,
                                    close_wide.index[0], close_wide.index[-1],
                                    strategy_id='T_short_straddle', vrp_factor=1.3,
                                    max_hold_days=120)
    # 120 trading days / ~30-DTE rolls → expect >1 cycle from the single signal
    assert len(out['trades']) >= 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_backtest.py::test_roll_produces_multiple_cycles_over_long_hold -v`
Expected: FAIL — only one cycle per signal today.

- [ ] **Step 3: Implement roll-then-reopen loop**

In `simulate`, wrap the per-signal cycle call in a loop that re-enters a new cycle starting the day after the previous cycle's `exit_date` when `exit_reason == 'roll'`, until dates are exhausted or `max_hold_days` total is consumed:

```python
            # roll-then-reopen: keep re-entering while the prior cycle ended on a roll
            cursor = current_date
            remaining = max_hold_days
            while remaining > 0 and cursor is not None:
                series = close_wide[ul].dropna()
                if spec.structure == 'single':
                    cyc = _price_single_cycle(spec, series, cursor, sign,
                                              vrp_factor, window, remaining)
                else:
                    cyc = _price_multileg_cycle(spec, series, cursor, sign,
                                                vrp_factor, window, remaining)
                if cyc is None:
                    break
                cyc.update({'ticker': ul, 'direction': 'long' if sign > 0 else 'short',
                            'entry_regime': str(regime_state)})
                trades.append(cyc)
                remaining -= cyc['holding_days']
                if cyc['exit_reason'] != 'roll':
                    break
                # next cycle starts at the bar AFTER this cycle's exit
                later = series.index[series.index > pd.Timestamp(cyc['exit_date'])]
                cursor = later[0] if len(later) else None
```

Remove the prior single-call block this loop replaces.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_backtest.py -v`
Expected: PASS (all). If a signal fires every bar (test strats fire once via the `len==min_lookback+5` guard) confirm no duplicate-signal explosion; real strategies must self-limit re-signalling (documented in Task 9 reference strategy).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/options_backtest.py tests/test_options_backtest.py
git commit -m "feat(sp4-p0): roll-then-reopen continuous holding across rolls"
```

---

## Task 7: wire dispatch into `run_backtest` + equity regression

**Files:**
- Modify: `src/backtest/unified_backtest.py` (the `sim = _per_bar_simulate(...)` call site, ~line 620)
- Test: `tests/test_options_backtest.py` (extend) + run existing `tests/test_instrument_class_backtest.py`

- [ ] **Step 1: Write the failing test**

Append:

```python
def test_simulate_dispatch_selects_correct_path():
    # The dispatch is a module-level selector so it's verifiable without a DB.
    from backtest import unified_backtest as ub
    from backtest import options_backtest as ob
    assert ub._simulate_for('option') is ob.simulate
    assert ub._simulate_for('equity') is ub._per_bar_simulate
    assert ub._simulate_for('crypto') is ub._per_bar_simulate
    assert ub._simulate_for('etp') is ub._per_bar_simulate
```

(Full end-to-end DB run is exercised by Task 9's reference-strategy run, which needs `prices.parquet` + Postgres.)

- [ ] **Step 2: Run the existing equity regression to capture the baseline**

Run: `python3 -m pytest tests/test_instrument_class_backtest.py tests/test_backtest_as_of.py -v`
Expected: PASS (these must stay green — the dispatch edit must not perturb equity).

- [ ] **Step 3: Implement the dispatch**

In `unified_backtest.py`, add a top-level import (near the other `from backtest...`/local imports; `options_backtest` does NOT import `unified_backtest`, so no circular import) and a module-level selector:

```python
from backtest import options_backtest  # SP-4 Phase 0


def _simulate_for(instrument_class: str):
    """SP-4 Phase 0 dispatch: pick the simulate fn for an instrument_class.
    Only 'option' diverges; everything else uses the existing equity path."""
    if instrument_class == 'option':
        return options_backtest.simulate
    return _per_bar_simulate
```

Then replace the single `sim = _per_bar_simulate(...)` call in `run_backtest` with:

```python
    sim = _simulate_for(instrument_class)(
        instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt,
        strategy_id=strategy_id, resolver=resolver, max_hold_days=max_hold_days,
    )
```

(`_per_bar_simulate` must be defined above `_simulate_for`, or reference it lazily — it already is, at line ~427.)

- [ ] **Step 4: Run tests to verify**

Run: `python3 -m pytest tests/test_options_backtest.py tests/test_instrument_class_backtest.py tests/test_backtest_as_of.py -v`
Expected: PASS. Equity path byte-identical (only the `'option'` branch is new).

- [ ] **Step 5: Commit**

```bash
git add src/backtest/unified_backtest.py tests/test_options_backtest.py
git commit -m "feat(sp4-p0): dispatch run_backtest to options engine on instrument_class=option"
```

---

## Task 8: greeks-aware option sizing in `instrument_class_sizer.py`

**Files:**
- Modify: `src/execution/instrument_class_sizer.py` (option branch)
- Test: `tests/test_options_sizer.py`

Note: this branch is INERT for live until an option strategy is promoted live (gate `OPENCLAW_INSTRUMENT_CLASS_ROUTING` routes equity/etp/crypto pass-through; no option strategy is live). The improvement makes sizing size to a delta-dollar target instead of the crude `notional × |delta|`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_options_sizer.py
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution.instrument_class_sizer import apply_instrument_class_sizing


def test_equity_etp_crypto_passthrough_unchanged():
    o = {'ticker': 'AAPL', 'notional_usd': 1000.0}
    for ic in ('equity', 'etp', 'crypto'):
        assert apply_instrument_class_sizing(dict(o), ic) == o


def test_option_delta_dollar_sizing():
    # target delta-dollar = notional_usd; contracts sized so |delta|*S*100*n ≈ notional
    o = {'ticker': 'SPY', 'notional_usd': 5000.0, 'delta': 0.5,
         'underlying_price': 500.0}
    out = apply_instrument_class_sizing(o, 'option')
    # delta-dollar per contract = 0.5 * 500 * 100 = 25000 → contracts = 5000/25000 = 0.2
    assert abs(out['contracts'] - 0.2) < 1e-6


def test_option_fail_open_without_delta():
    o = {'ticker': 'SPY', 'notional_usd': 5000.0}
    out = apply_instrument_class_sizing(o, 'option')
    assert out['notional_usd'] == 5000.0  # unchanged, fail-open
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_sizer.py -v`
Expected: FAIL — `contracts` key not produced.

- [ ] **Step 3: Implement delta-dollar sizing in the option branch**

Replace the `option` branch body in `apply_instrument_class_sizing`. The new delta-dollar path activates only when `underlying_price` is present; otherwise it FALLS BACK to the legacy `|delta|`-notional scaling (so the existing `test_instrument_class_sizer.py::test_option_scales_by_delta_when_present`, which passes a delta-only order and expects `notional_usd==500`, stays green):

```python
    if instrument_class == "option":
        delta = order.get("delta")
        try:
            d = abs(float(delta)) if delta is not None else None
        except (TypeError, ValueError):
            d = None
        if d is None or d <= 0:
            logger.warning("[instrument_class_sizer] option order %s has no usable "
                           "delta; using raw notional (fail-open)", order.get("ticker"))
            return order
        # Preferred (SP-4 Phase 0): greeks-aware delta-dollar sizing when the
        # underlying price is known — size contracts so delta-dollar == notional.
        S = order.get("underlying_price")
        try:
            S = float(S) if S is not None else None
        except (TypeError, ValueError):
            S = None
        if S and S > 0:
            scaled = dict(order)
            delta_dollar_per_contract = d * S * 100.0
            scaled["contracts"] = round(float(order["notional_usd"]) / delta_dollar_per_contract, 6)
            scaled["delta_dollar"] = round(float(order["notional_usd"]), 2)
            return scaled
        # Fallback (no underlying price): legacy |delta|-notional scaling (SP-3 behavior).
        scaled = dict(order)
        scaled["notional_usd"] = round(float(order["notional_usd"]) * d, 2)
        return scaled
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_sizer.py tests/test_instrument_class_sizer.py -v`
Expected: PASS (new + existing sizer tests; equity/etp/crypto unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/execution/instrument_class_sizer.py tests/test_options_sizer.py
git commit -m "feat(sp4-p0): greeks-aware delta-dollar option sizing (inert live until an option strategy is live)"
```

---

## Task 9: reference strategy `S_short_straddle_vrp`

**Files:**
- Create: `src/strategies/implementations/S_short_straddle_vrp.py` + `.requirements.json`
- Modify: `src/strategies/registry.py` (`_IMPL_MAP`), `src/strategies/manifest.json`
- Test: backtest run (needs `prices.parquet` + Postgres)

- [ ] **Step 1: Write the strategy**

```python
# src/strategies/implementations/S_short_straddle_vrp.py
"""Short-straddle volatility-risk-premium harvester — reference option strategy
for SP-4 Phase 0. Sells the ATM straddle on a liquid underlying and delta-hedges
daily; harvests the gap between implied (priced with a VRP markup) and realized
vol. instrument_class='option'. NOT a tuned production alpha — it proves the
synthetic options engine flows end-to-end (signal -> options_backtest -> metrics).

Re-signalling: fires at most once per N trading days so the engine's
roll-then-reopen loop (not repeated signals) drives continuous holding.
"""
from __future__ import annotations
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal, OptionSpec

UNDERLYING = 'SPY'
RESIGNAL_GAP = 21  # trading days between fresh signals


class ShortStraddleVRP(BaseStrategy):
    id                = 'S_short_straddle_vrp'
    name              = 'Short Straddle VRP'
    description        = ('Delta-hedged short ATM straddle on SPY harvesting the '
                          'volatility risk premium; reference option strategy.')
    tier              = 3
    signal_frequency  = 'daily'
    min_lookback      = 30
    instrument_class  = 'option'
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']   # avoid short-vol in HIGH_VOL/CRISIS
    MAX_SIGNALS       = 1

    def default_parameters(self) -> dict:
        return {'resignal_gap': RESIGNAL_GAP, 'dte_target': 30, 'roll_dte': 7}

    def generate_signals(self, prices: pd.DataFrame, regime: dict,
                         universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty or UNDERLYING not in prices.columns:
            return []
        if not self.should_run(regime.get('state', 'LOW_VOL')):
            return []
        series = prices[UNDERLYING].dropna()
        if len(series) < self.min_lookback:
            return []
        gap = int(self.parameters.get('resignal_gap', RESIGNAL_GAP))
        # only fire when the bar index is on the cadence grid (deterministic)
        if (len(series) % gap) != 0:
            return []
        S = float(series.iloc[-1])
        return [Signal(
            ticker=UNDERLYING, direction='SELL_VOL', entry_price=S,
            stop_loss=S * 0.90, target_1=S * 1.10, target_2=0.0, target_3=0.0,
            position_size_pct=0.05, confidence='MED',
            option_spec=OptionSpec(
                underlying=UNDERLYING, structure='straddle', hedge='delta',
                dte_target=int(self.parameters.get('dte_target', 30)),
                roll_dte=int(self.parameters.get('roll_dte', 7))),
        )]
```

```json
// src/strategies/implementations/S_short_straddle_vrp.requirements.json
{
  "strategy_id": "S_short_straddle_vrp",
  "required": ["prices"],
  "optional": []
}
```

- [ ] **Step 2: Register in `_IMPL_MAP` + add manifest entry**

In `src/strategies/registry.py:_IMPL_MAP`, add (alongside the other entries):

```python
    'S_short_straddle_vrp':              ('strategies.implementations.S_short_straddle_vrp',              'ShortStraddleVRP'),
```

In `src/strategies/manifest.json`, append to the `strategies` list a candidate entry (use a psycopg2-free Python edit, not a hand-merge — verify it parses):

```python
# one-shot helper run from repo root:
python3 - <<'PY'
import json, datetime
p='src/strategies/manifest.json'; m=json.load(open(p))
m['strategies'].append(['S_short_straddle_vrp', {
  'state':'candidate',
  'state_since': datetime.datetime.utcnow().isoformat()+'Z',
  'metadata':{'canonical_file':'S_short_straddle_vrp.py','class':'ShortStraddleVRP',
              'description':'Delta-hedged short ATM straddle on SPY harvesting the volatility risk premium; reference option strategy.'},
  'history':[],
  'instrument_class':'option'}])
json.dump(m, open(p,'w'), indent=2)
print('appended; total strategies:', len(m['strategies']))
PY
```

- [ ] **Step 3: Verify `SPY` exists in `prices.parquet`, then run the backtest**

Run:
```bash
# SPY must exist AND extend back near DEFAULT_START_DATE (2016-04-11) for a stable calibration.
python3 -c "
import pyarrow.parquet as pq
df=pq.read_table('data/master/prices.parquet',columns=['ticker','date']).to_pandas()
spy=df[df['ticker']=='SPY']
print('SPY present:', not spy.empty, '| min date:', spy['date'].min() if not spy.empty else None)"
python3 -c "
import sys; sys.path.insert(0,'.'); sys.path.insert(0,'src')
from backtest.unified_backtest import run_backtest
rid = run_backtest('S_short_straddle_vrp', instrument_class='option', commit=False)
print('run_id:', rid)
"
```
Expected: `True`, then a `run_id` + a logged `total_sharpe`/`total_max_dd_pct`. (If `SPY` is absent, pick a liquid underlying that IS present — re-verify from the ticker list — and update `UNDERLYING`.) `commit=False` avoids writing during the smoke; the calibration run (Task 11) commits.

- [ ] **Step 4: Sanity-check the metrics are finite**

The run must produce a non-None `total_sharpe` and a `total_max_dd_pct` in a plausible range (not 0, not 99%). If degenerate, debug the engine before proceeding (do NOT calibrate on a broken curve).

- [ ] **Step 5: Commit**

```bash
git add src/strategies/implementations/S_short_straddle_vrp.py \
        src/strategies/implementations/S_short_straddle_vrp.requirements.json \
        src/strategies/registry.py src/strategies/manifest.json
git commit -m "feat(sp4-p0): S_short_straddle_vrp reference option strategy (candidate)"
```

---

## Task 10: parity check — synthetic vs real chain (gates calibration)

**Files:**
- Create: `scripts/options_parity_check.py`
- Test: `tests/test_options_backtest.py` (a unit test on the comparison helper with synthetic fixtures)

- [ ] **Step 1: Write the failing unit test for the comparison helper**

Append to `tests/test_options_backtest.py`:

```python
def test_parity_mae_helper():
    from scripts.options_parity_check import mae_fraction
    synth = [1.0, 2.0, 3.0]; real = [1.1, 1.8, 3.3]
    m = mae_fraction(synth, real)
    # mean(|.1|/1.1, |.2|/1.8, |.3|/3.3) ≈ mean(0.0909,0.111,0.0909)=0.0976
    assert abs(m - 0.0976) < 0.01
```

(Add `sys.path.insert(0, str(ROOT))` already present; `scripts` is importable from repo root.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_backtest.py::test_parity_mae_helper -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `scripts/options_parity_check.py`**

```python
# scripts/options_parity_check.py
"""SP-4 Phase 0 parity check + VRP calibration.

Over the ~7-week real-chain overlap (options_eod.parquet), for a sample of
real (ticker, date, expiry, strike, option_type) contracts, compute the
synthetic BS price using synthetic_iv and compare to the real market_price.
Reports mean-absolute-error-as-fraction-of-price, swept over a grid of
(vrp_factor, window) to find the value that minimizes MAE.

CALIBRATION GATE: the engine is "trusted" only if best-fit MAE <= MAE_THRESHOLD.
Record the measured best (vrp_factor, window, MAE) — DO NOT hardcode an
optimistic number. The chosen vrp_factor becomes synthetic_iv.DEFAULT_VRP_FACTOR
(applied in Task 11) and threshold calibration may not proceed until this passes.
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import numpy as np, pandas as pd, pyarrow.parquet as pq

from backtest.options_pricing import bs_price
from backtest.synthetic_iv import realized_vol

MAE_THRESHOLD = 0.15  # PROPOSED gate; the plan records the measured value, revise if justified


def mae_fraction(synth: list[float], real: list[float]) -> float:
    s = np.asarray(synth, float); r = np.asarray(real, float)
    mask = r > 1e-6
    return float(np.mean(np.abs(s[mask] - r[mask]) / r[mask]))


def _load_underlying_closes():
    df = pq.read_table('data/master/prices.parquet', columns=['ticker', 'date', 'close']).to_pandas()
    df['date'] = pd.to_datetime(df['date'])
    return {t: g.set_index('date')['close'].sort_index() for t, g in df.groupby('ticker')}


def synth_price_for_row(row, closes, vrp_factor, window):
    s = closes.get(row['ticker'])
    if s is None:
        return None
    asof = pd.Timestamp(row['date'])
    hist = s.loc[:asof]
    if len(hist) < 5:
        return None
    S = float(hist.iloc[-1])
    sigma = max(0.05, realized_vol(hist, window=window) * vrp_factor)
    t = max((pd.Timestamp(row['expiry']) - asof).days / 365.0, 1e-6)
    flag = 'c' if str(row['option_type']).lower().startswith('c') else 'p'
    try:
        return bs_price(flag, S, float(row['strike']), t, sigma)
    except Exception:
        return None


def run(sample=4000):
    opt = pq.read_table('data/master/options_eod.parquet',
                        columns=['ticker', 'date', 'expiry', 'strike', 'option_type',
                                 'market_price', 'implied_volatility']).to_pandas()
    opt = opt.dropna(subset=['market_price', 'strike', 'expiry'])
    opt = opt[opt['market_price'] > 0.05]
    if len(opt) > sample:
        opt = opt.sample(sample, random_state=0)
    closes = _load_underlying_closes()

    best = None
    for vrp in [1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.4]:
        for window in [10, 21, 42]:
            synth, real = [], []
            for _, row in opt.iterrows():
                p = synth_price_for_row(row, closes, vrp, window)
                if p is not None:
                    synth.append(p); real.append(float(row['market_price']))
            if len(synth) < 100:
                continue
            m = mae_fraction(synth, real)
            if best is None or m < best['mae']:
                best = {'vrp_factor': vrp, 'window': window, 'mae': m, 'n': len(synth)}
            print(f'vrp={vrp} window={window} n={len(synth)} MAE={m:.4f}')

    print('\nBEST:', best)
    if best is None:
        print('PARITY: INSUFFICIENT DATA'); return 2
    status = 'PASS' if best['mae'] <= MAE_THRESHOLD else 'FAIL'
    print(f"PARITY {status} (MAE={best['mae']:.4f} vs threshold {MAE_THRESHOLD})")
    return 0 if status == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(run())
```

- [ ] **Step 4: Run unit test + run the parity script (record results)**

Run: `python3 -m pytest tests/test_options_backtest.py::test_parity_mae_helper -v` → PASS.
Then: `python3 scripts/options_parity_check.py` → record the printed BEST `{vrp_factor, window, mae}` and PASS/FAIL.
**Checkpoint:** if FAIL, surface to the operator BEFORE Task 11 — options consider: coarse moneyness-skew term in `synthetic_iv`, restricting underlyings to parity-passers, or revising the threshold with justification. Do not calibrate thresholds on a failed engine.

- [ ] **Step 5: Commit**

```bash
git add scripts/options_parity_check.py tests/test_options_backtest.py
git commit -m "feat(sp4-p0): options synthetic-vs-real parity check + VRP calibration"
```

---

## Task 11: threshold calibration + apply to `lifecycle.py`

**Files:**
- Create: `scripts/calibrate_option_thresholds.py`
- Modify: `src/strategies/lifecycle.py` (`PROMOTION_THRESHOLDS` option + crypto `min_sharpe`)

**Gate:** Task 10 parity must PASS first. Apply the calibrated `vrp_factor`/`window` from Task 10 to `synthetic_iv.DEFAULT_VRP_FACTOR`/`DEFAULT_WINDOW` as the first step here.

- [ ] **Step 1: Apply calibrated IV defaults**

Edit `src/backtest/synthetic_iv.py` to set `DEFAULT_VRP_FACTOR` and `DEFAULT_WINDOW` to the Task-10 measured best, with a comment citing the parity MAE + date.

- [ ] **Step 2: Implement the calibration script**

```python
# scripts/calibrate_option_thresholds.py
"""SP-4 Phase 0 threshold calibration. Runs the (parity-validated) synthetic
engine across a small grid of option archetypes and reports the Sharpe/MaxDD
distribution, proposing PROMOTION_THRESHOLDS['option'] = {min_sharpe, max_drawdown}.
Also reports a proposed crypto min_sharpe from S_btc_momentum's backtest + live.

OUTPUT IS A PROPOSAL for operator sign-off; the lifecycle.py edit is applied
manually (Step 3) with the measured numbers + a sign-off comment. The thresholds
are FRACTIONS; backtest total_max_dd_pct is a PERCENTAGE (divide by 100).
"""
from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from backtest.unified_backtest import run_backtest, load_strategy_class, find_strategy_file
from backtest.unified_backtest import _per_bar_simulate  # noqa  (not used; ensures import path)

# Archetype strategies to characterize (add more as Phase C lands templates).
OPTION_STRATS = ['S_short_straddle_vrp']


def run():
    print('=== OPTION archetype backtests ===')
    for sid in OPTION_STRATS:
        rid = run_backtest(sid, instrument_class='option', commit=True)
        print(f'{sid}: run_id={rid}')
    print('\nInspect strategy_backtest_runs for total_sharpe/total_max_dd_pct of the above,')
    print('then set PROMOTION_THRESHOLDS[\'option\'] in lifecycle.py with operator sign-off.')
    print('For crypto min_sharpe: use S_btc_momentum total_sharpe (1.02) + accruing live track record.')


if __name__ == '__main__':
    run()
```

- [ ] **Step 3: Run it, then apply thresholds to `lifecycle.py`**

Run: `python3 scripts/calibrate_option_thresholds.py` and read back `total_sharpe`/`total_max_dd_pct` via:
```bash
python3 -c "
import os, psycopg2
c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
cur.execute(\"SELECT strategy_id,total_sharpe,total_max_dd_pct FROM strategy_backtest_runs WHERE strategy_id='S_short_straddle_vrp' ORDER BY created_at DESC LIMIT 1\")
print(cur.fetchone())"
```
Then edit `src/strategies/lifecycle.py` `PROMOTION_THRESHOLDS`:

```python
    "option": {"min_sharpe": <CALIBRATED>,   # SP-4 2026-05-27: calibrated from synthetic engine
               "max_drawdown": <CALIBRATED>}, #   (parity MAE <recorded>); operator sign-off
    "crypto": {"min_sharpe": <CALIBRATED>,   # SP-4 2026-05-27: from S_btc_momentum (Sharpe 1.02) + live
               "max_drawdown": 0.70},        #   operator sign-off 2026-05-26 (BTC 60-80% DD asset)
```

Replace `<CALIBRATED>`/`<recorded>` with the measured numbers. **Surface the proposed numbers to the operator for sign-off before committing** (these gate real promotions).

> **Do NOT pattern-match the option threshold to equity's 0.5.** Option-strategy cycles span ~30 days vs equity's ~10, so the equal-weighted daily-return scale that `aggregate_metrics` builds differs between classes. Sharpe is scale-invariant *within* a strategy, but the calibrated option `min_sharpe` may legitimately land above or below 0.5 — let the empirical distribution decide, don't anchor to the equity value.

- [ ] **Step 4: Run the lifecycle tests + threshold sanity**

Run: `python3 -m pytest tests/test_lifecycle_instrument_class.py -v` (+ any `tests/test_lifecycle*.py`)
Expected: PASS. Confirm an option strategy with metadata `sharpe`/`max_drawdown` near the new thresholds transitions/blocks as intended (the percentage-vs-fraction conversion is the classic trap — `max_drawdown` in metadata is a FRACTION).

- [ ] **Step 5: Commit (after operator sign-off)**

```bash
git add scripts/calibrate_option_thresholds.py src/backtest/synthetic_iv.py src/strategies/lifecycle.py
git commit -m "feat(sp4-p0): calibrate option + crypto min_sharpe PROMOTION_THRESHOLDS (operator-signed)"
```

---

## Task 12: docs + integrity manifest

**Files:**
- Modify: `/root/openclaw/CLAUDE.md` (Recent Changes — prepend an SP-4 Phase 0 entry)
- Run: `./scripts/regen-integrity-manifest.sh` (CLAUDE.md is manifest-covered)

- [ ] **Step 1: Prepend a CLAUDE.md Recent Changes entry**

Summarize: synthetic BS options engine (`options_backtest.py`), IV model (`synthetic_iv.py`, calibrated VRP=<x>, window=<n>, parity MAE=<m>), `OptionSpec` signal contract, greeks-aware delta-dollar sizing (inert live), `S_short_straddle_vrp` reference (candidate), calibrated `PROMOTION_THRESHOLDS` option=<...>/crypto min_sharpe=<...>. Note dispatch branches only on `instrument_class='option'` (equity/etp/crypto byte-identical). Note live options EXECUTION is still out of scope (reuses `OptionSpec` when first option strategy promotes).

- [ ] **Step 2: Regenerate integrity manifest (on the VPS)**

Run: `./scripts/regen-integrity-manifest.sh`
Expected: clears the startup `[SECURITY_ALERT] integrity: HASH MISMATCH` for CLAUDE.md. (The manifest is gitignored/local-per-VPS — do not commit it.)

- [ ] **Step 3: Full test sweep**

Run: `python3 -m pytest tests/test_options_pricing.py tests/test_synthetic_iv.py tests/test_options_backtest.py tests/test_options_sizer.py tests/test_instrument_class_backtest.py tests/test_instrument_class_sizer.py tests/test_lifecycle_instrument_class.py -v`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md
git commit -m "docs(sp4-p0): CLAUDE.md Recent Changes — synthetic greeks options engine"
```

- [ ] **Step 5: Surface for review/merge**

Do NOT merge or deploy. Surface to the operator: branch `feat/sp4-phase-0-greeks-engine`, the parity MAE result, the calibrated thresholds (already signed off in Task 11), and the candidate reference strategy. Operator decides merge → push → VPS pull, and whether to promote `S_short_straddle_vrp`.

---

## Self-review notes

- **Spec coverage:** §3 engine → T2/3/4/5/6/7; §4 IV model → T3 + calibrated in T10/11; §5 `option_spec` → T1; §6 simulation (selection/pricing/hedge/roll/expiry) → T4/5/6; §7 parity gate → T10; §8 calibration (option + crypto min_sharpe) → T11; §9 reference strategy → T9; §10 testing/safety → tests across T1-12 + equity regression in T7 + sizer regression in T8 + no-migration/no-master-write honored (engine reads parquet, writes only existing `strategy_backtest_*`).
- **Out of scope (per spec §2):** live options order execution — not in any task; noted in T12 Step 5.
- **Empirical values:** parity MAE threshold, VRP factor/window, and thresholds are measured in T10/T11 and recorded — not pre-committed.
- **Operator surfacing gates:** T10 Step 4 (parity FAIL), T11 Step 3 (threshold sign-off), T12 Step 5 (merge/deploy/promotion).
