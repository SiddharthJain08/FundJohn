# SP-4 Phase 0 — Synthetic Greeks Options Engine (design)

**Date:** 2026-05-27
**Status:** Design — pending user review, then writing-plans.
**Program:** `2026-05-27-sp4-weekly-research-uplift-design.md` (SP-4 decomposition).
**Depends on:** SP-3 asset-class rails (`instrument_class` enum, `PROMOTION_THRESHOLDS`, `run_backtest(instrument_class=...)`, `instrument_class_sizer.py`).

---

## 1. Goal

Make an **options strategy's backtest trustworthy enough to promote on**, and make **greeks-aware sizing real**, so SP-4's later phases can originate option strategies that flow through the standard candidate→live lifecycle. Today the option path is inert/fail-open: the sizer scales notional by `|delta|` fail-open (`instrument_class_sizer.py`), the backtest cost is record-only (`INSTRUMENT_COST_BPS['option']=5.0`), no engine prices options, and `PROMOTION_THRESHOLDS['option']` is an equity-valued `TODO(SP-4)` placeholder.

Real options history is only **~7 weeks** (`options_eod.parquet`: 2026-04-08→2026-05-26, 708k rows, greeks ~99.8% populated, OI 0%). That is far too short for a long-horizon Sharpe/MaxDD. Phase 0 therefore **synthesizes** a long options history from 10y underlying prices (`prices.parquet` 2016→2026) + a calibrated IV model, and **validates the synthetic prices against the 7 weeks of real chain data** before trusting any number it produces.

## 2. Scope

**In:** synthetic BS backtest engine; IV model; `option_spec` signal contract; greeks-aware sizing logic; parity validation; threshold calibration (option + crypto `min_sharpe`); short-straddle VRP reference strategy (candidate-only); tests.

**Out (later phase, reuses this work):** live options order execution (real Alpaca chain contract selection + option order routing). Not needed while the reference strategy stays candidate. Noted in the program doc §5.

## 3. Architecture

```
src/backtest/
  options_backtest.py     # NEW — priced-contract simulation path for instrument_class='option'
  synthetic_iv.py         # NEW — IV(underlying, date, tenor) = realized_vol × VRP_factor (calibrated)
  options_pricing.py      # NEW — thin BS wrapper over py_vollib: price, greeks, IV inversion, strike-from-delta
  unified_backtest.py     # EDIT — run_backtest() dispatches to options_backtest when instrument_class='option'
src/execution/
  instrument_class_sizer.py  # EDIT — option branch: real greeks-aware sizing (inert live until an option strategy is live)
src/strategies/
  signal.py (or wherever the signal dataclass lives)  # EDIT — add optional option_spec field (backward-compatible)
  implementations/S_short_straddle_vrp.py (+ .requirements.json)  # NEW — reference strategy
scripts/
  options_parity_check.py      # NEW — synthetic vs real-chain PnL on the 7-week overlap (gates calibration)
  calibrate_option_thresholds.py  # NEW — produces calibrated PROMOTION_THRESHOLDS values
```

`unified_backtest.run_backtest` branches on `instrument_class`: `option` → `options_backtest.simulate(...)`; everything else keeps the existing `_per_bar_simulate` path **byte-identical** (regression-tested). No migration (the `strategy_backtest_*` tables already accept the metrics; `instrument_class` already on `StrategyRecord`).

> **Grounding TODO before plan dispatch:** confirm the exact module/class where the signal dataclass is defined (`grep -rn "entry_price" src/strategies | grep -i "class\|dataclass"`) and how `instance.generate_signals(...)` / `instance.MAX_SIGNALS` are declared on the base strategy, so the `option_spec` field and the options strategy interface match live source.

## 4. IV model (`synthetic_iv.py`)

`IV(underlying, date, tenor) = realized_vol(underlying, trailing W days) × VRP_factor`.

- `realized_vol` = annualized close-to-close stdev over a trailing window `W` (default 21 trading days; configurable per tenor).
- `VRP_factor` (implied typically exceeds realized) is **calibrated** in §7, not guessed. Single scalar to start; may extend to a small per-regime or per-tenor table if parity demands.
- **Index options** (^GSPC/^SPX-style underlyings) anchored to VIX / `cboe_vol_indices` where a clean series exists; per-name options use the realized-vol×VRP proxy. (Note: doctor currently flags VIX freshness — the calibration script must tolerate gaps and log coverage.)
- No smile/skew in v1 (flat IV across strikes for a given underlying/date/tenor). Parity (§6) measures the cost of this simplification; if MAE fails, add a coarse moneyness skew term before widening scope.

## 5. `option_spec` signal contract (the Phase B/C boundary)

Add an **optional** `option_spec` to the signal object (equity/crypto/etp signals leave it `None` → existing behavior byte-identical). Proposed dataclass:

```python
@dataclass
class OptionSpec:
    underlying: str            # e.g. 'SPY', '^GSPC'
    right: str                 # 'call' | 'put'   (per leg; see structure)
    strike_rule: str = 'target_delta'   # 'target_delta' | 'atm' | 'fixed_moneyness'
    target_delta: float = 0.30          # used when strike_rule='target_delta'
    moneyness: float | None = None      # used when strike_rule='fixed_moneyness' (K/S)
    dte_target: int = 30                # nearest expiry >= this many calendar days
    structure: str = 'single'           # 'single' | 'straddle' | 'strangle'
    hedge: str = 'none'                 # 'none' | 'delta'
    hedge_cadence: str = 'daily'        # rehedge frequency when hedge='delta'
    roll_dte: int = 7                   # roll when DTE drops to/below this
```

For multi-leg structures (`straddle`/`strangle`) the engine instantiates both legs from one `OptionSpec` (straddle = ATM call+put; strangle = OTM call+put at `target_delta`). The signal's `direction` sets long/short the structure. The same `OptionSpec` is what a future live executor reads to select the real Alpaca contract — parity by construction.

## 6. Simulation (`options_backtest.py`)

Per signal-day, per signal:

1. **Contract selection.** From `OptionSpec` + modeled IV at the underlying/date/tenor: pick expiry = nearest listed-style monthly ≥ `dte_target` (synthetic calendar; standard 3rd-Friday monthly grid); pick strike per `strike_rule` (for `target_delta`, invert BS to the strike whose |delta| ≈ `target_delta`).
2. **Daily pricing.** Mark the contract each bar via BS using the underlying close + modeled IV + time-decay; PnL = signed (long/short) change in option mark, net of `INSTRUMENT_COST_BPS['option']` on traded notional.
3. **Delta-neutral hedge** (`hedge='delta'`): each `hedge_cadence` bar, trade the underlying to flatten net position delta; accrue hedge PnL + equity-bps cost on hedge notional.
4. **Roll / expiry.** Roll when `DTE <= roll_dte` → close current, open a fresh `dte_target` contract (income legs may instead **hold to expiry**). At expiry, cash-settle at intrinsic (assignment modeled as cash settlement — no physical share delivery in backtest).
5. Emit trades into the same `strategy_backtest_trades` shape the equity path uses (with option-specific fields in metadata), so `aggregate_metrics` / `strategy_backtest_runs` works unchanged.

**Delta-neutral defaults (accepted in brainstorm; tunable per strategy via `OptionSpec`):** daily re-hedge · roll at 7-DTE → 30-DTE target · 30-delta directional / ATM straddle · cash-settle assignment · hedge cost at equity bps.

## 7. Parity validation — **gates calibration** (`scripts/options_parity_check.py`)

The load-bearing check. Run the synthetic engine over the ~7-week real-chain overlap and compare, on **identical contracts/signals**, synthetic-priced PnL vs real-chain-priced PnL (from `options_eod.parquet`).

- Metric: mean absolute error of daily option marks (and of realized trade PnL) as a fraction of contract price. **Engine is "trusted" iff MAE ≤ threshold** (proposed ≤ 15%; final value set in the plan after first measurement — record the empirical number, don't hardcode optimistically).
- The check **doubles as the VRP-factor calibration target**: sweep `VRP_factor` (and `W`) to minimize parity MAE on the overlap; that's the calibrated value §4 uses.
- If parity fails even at best fit → add a coarse skew term (§4) and/or restrict supported underlyings to those that parity-pass; **do not** proceed to threshold calibration on an untrusted engine.

## 8. Threshold calibration (`scripts/calibrate_option_thresholds.py`)

Only after §7 passes. Run the trusted engine across the supported archetypes (and a small grid of plausible option strategies) to observe Sharpe/MaxDD distributions, then set:

- `PROMOTION_THRESHOLDS['option']` = `{min_sharpe, max_drawdown}` — empirically grounded, replacing the `TODO(SP-4)` placeholder (`lifecycle.py:102`).
- `PROMOTION_THRESHOLDS['crypto']['min_sharpe']` — replace the `TODO(SP-3.2)` placeholder (`lifecycle.py:104`); MaxDD stays at the operator-signed-off 0.70. (Use S_btc_momentum's backtest + the accruing live track record as the anchor.)

Record the rationale inline in `lifecycle.py` (mirroring the existing crypto-MaxDD operator-sign-off comment) and in CLAUDE.md Recent Changes.

## 9. Reference strategy — `S_short_straddle_vrp`

Hand-written delta-hedged short-straddle volatility-risk-premium harvester on a liquid underlying (e.g., SPY/^GSPC). `instrument_class='option'`, `OptionSpec(structure='straddle', hedge='delta', ...)`. Exercises the full engine (multi-leg + delta-hedge + roll). Backtested through the synthetic engine; **candidate-only** until operator promotes. Mirrors `S_btc_momentum`'s role for crypto. Needs `S_short_straddle_vrp.py` + `.requirements.json` + a committed `registry.py:_IMPL_MAP` entry (auto-synced at runtime but must be committed or a redeploy reverts it) + a manifest entry with `instrument_class='option'`.

## 10. Testing / safety

- **Unit:** BS price + greeks vs textbook reference values; IV-inversion round-trip; strike-from-target-delta; roll/expiry/assignment edge cases; delta-hedge flattening.
- **Parity test:** §7 as an automated check with a recorded threshold.
- **Regression:** equity/crypto/etp `run_backtest` byte-identical when `instrument_class != 'option'` (the dispatch must not perturb existing paths).
- **Sizer:** `instrument_class_sizer.py` option branch change is **inert for live** until an option strategy is live (gate routes equity/etp/crypto pass-through; no option strategy is live). Add a regression asserting equity/etp/crypto sizing unchanged.
- **No master-data writes** beyond existing `strategy_backtest_*`. No migration expected. No secrets. Stage specific files.

## 11. Risks / open items for the plan

- **VRP single-scalar may be too crude** for some underlyings → parity MAE high. Mitigation: per-regime/per-tenor table or coarse skew; restrict underlyings to parity-passers.
- **VIX freshness** (doctor FAIL) → index-option anchoring may have gaps; calibration must tolerate and log.
- **Monthly-expiry synthetic calendar** vs real listed expiries → small mismatch; parity measures it.
- **Final parity MAE threshold and calibrated VRP/thresholds are empirical** — the plan records measured values; this spec does not pre-commit optimistic numbers.
