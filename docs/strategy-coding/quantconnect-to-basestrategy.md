# Porting QuantConnect / LEAN algorithms to FundJohn `BaseStrategy`

This is the mapping reference for **porting** an already-coded QuantConnect / LEAN
algorithm into our strategy contract. It is used by StrategyCoder when a research
candidate arrives from a git blueprint (`origin='git_blueprint'`) carrying the
original LEAN source in `REFERENCE_IMPLEMENTATION`.

> **Clean-room rule (non-negotiable).** Re-implement from the **RULE**, not the code.
> Read the reference to recover the exact entry/exit logic, parameters, rebalance
> cadence and universe — then write fresh code against *our* contract. Do **not**
> copy LEAN code verbatim; it targets a different framework and event model. Cite
> the original `SOURCE_URL` in the class docstring (e.g. the quantpedia.com URL).
> The reference is for params/logic only.

---

## 0. The two execution models, side by side

| Concern | QuantConnect / LEAN | FundJohn `BaseStrategy` |
|---|---|---|
| Entry point | `class Foo(QCAlgorithm)` event-driven | `class Foo(BaseStrategy)` pure function |
| Per-tick callback | `OnData(self, data)` | `generate_signals(self, prices, regime, universe, aux_data)` |
| Data delivery | streamed `Slice` per bar | full history handed in once: `prices` (wide `date × ticker` closes) |
| Warmup | `SetWarmUp(n)` + per-symbol indicator objects | none — you have full history in `prices`; slice the tail yourself |
| Indicators | `self.SMA(sym, n)`, `self.RSI(sym, n)` rolling objects | compute directly with pandas (or house helpers) on the price series |
| Hold a position | `self.SetHoldings(sym, w)` | return a `Signal` with `position_size_pct = w` |
| Exit / go flat | `self.Liquidate(sym)` | **omit** the symbol from the returned list (absent = not held) |
| Fill timing | configurable fill model | engine fills at **`close[t+1]`** (next bar's close) — daily bars |
| Stops / TP | `StopMarketOrder`, `self.LimitOrder` | set `stop_loss` / `target_1..3` on the `Signal` (or `compute_stops_and_targets`) |
| Schedule | `self.Schedule.On(...)` | date logic over the daily bars inside `generate_signals` |
| Regime gate | (usually none) | `self.should_run(regime_state)` |

The single biggest mental shift: **we are not event-driven.** `generate_signals`
is called once per cycle with all history available. You return the *target book
for today*. There is no order object, no portfolio object you mutate, no
streaming. A symbol you want held → return a `Signal` for it. A symbol you want
flat → just don't return one.

---

## 1. `Initialize` / `SetWarmUp` / `AddEquity` / indicator constructors

LEAN:
```python
def Initialize(self):
    self.SetStartDate(2010, 1, 1)
    self.SetWarmUp(200)
    self.spy = self.AddEquity("SPY", Resolution.Daily).Symbol
    self.sma = self.SMA(self.spy, 200, Resolution.Daily)
```

Port: there is **no `Initialize`, no warmup object, no per-symbol indicator
object.** You receive `prices: pd.DataFrame` (wide: index = dates, columns =
tickers, values = closes) and compute indicators directly on the tail.

```python
close = prices[ticker].dropna()
if len(close) < 200:            # your own "warmup" — skip until enough history
    return []
sma200 = close.rolling(200).mean().iloc[-1]
last    = close.iloc[-1]
```

- `AddEquity(sym)` → the ticker just needs to be in `universe` / present as a
  column in `prices`. You do not "add" symbols; you read the ones handed in.
- `self.SMA(...)`, `self.RSI(...)`, `self.EMA(...)`, etc. → plain pandas
  (`.rolling(n).mean()`, an RSI from `.diff()`, `.ewm(span=n).mean()`), or a
  house indicator if one exists. Always guard for insufficient history with a
  length check (replaces `SetWarmUp`).
- `SetStartDate` / `SetCash` / `SetBenchmark` → **drop them.** Backtest window,
  capital and benchmark are framework concerns here, not strategy code.

---

## 2. `OnData` + `SetHoldings` / `Liquidate` → `List[Signal]`

LEAN:
```python
def OnData(self, data):
    if self.sma.IsReady and self.Securities[self.spy].Price > self.sma.Current.Value:
        self.SetHoldings(self.spy, 1.0)
    else:
        self.Liquidate(self.spy)
```

Port — build a list and return it. `SetHoldings(sym, w)` becomes a `Signal` with
`position_size_pct = w`; `Liquidate` / "don't hold" becomes the **absence** of a
`Signal` for that symbol.

```python
signals: List[Signal] = []
if last > sma200:
    signals.append(Signal(
        ticker            = ticker,
        direction         = 'LONG',
        entry_price       = float(last),
        ...,                                   # stops/targets — section 4
        position_size_pct = 1.0,               # the SetHoldings weight
        confidence        = 'MED',
    ))
# else: emit nothing for `ticker` → it will not be held
return signals
```

- `position_size_pct` is a fraction **0.0–1.0** of the portfolio. It is further
  scaled by the regime downstream (and by the sizer); you generally pass the raw
  target weight from the rule (e.g. `1/N` for equal-weight) and let the system
  scale it. You may multiply by `self.position_scale(regime_state)` if the rule
  itself is regime-conditional, but do not double-apply the regime scale.
- A **short** is `direction='SHORT'` (equity/etp). `FLAT` is rarely returned —
  prefer simply omitting the symbol. (For `option` strategies the directions are
  `SELL_VOL` / `BUY_VOL`; for `crypto` use `LONG` / `FLAT` on `BTC-USD`/`ETH-USD`.)
- LEAN's negative `SetHoldings(sym, -0.5)` → `direction='SHORT'`,
  `position_size_pct=0.5` (the magnitude; direction carries the sign).

---

## 3. Rebalance cadence (monthly / weekly / day-of-month)

LEAN expresses cadence with `self.Time.month`, `self.Schedule.On(...)`, or a
"have we rebalanced this month" flag. We have no scheduler — replicate it with
**date logic over the daily bars**: act only on the rebalance bar, and on every
other bar re-emit the currently-held book unchanged (or emit nothing new and let
positions persist — the engine nets `target − current`, so re-emitting the same
target is a no-op).

Monthly (act on the first available trading bar of each month):
```python
idx   = close.index                       # DatetimeIndex of the price history
today = idx[-1]
# the first bar of `today`'s month that exists in the data:
month_start_bar = idx[(idx.year == today.year) & (idx.month == today.month)][0]
is_rebalance_day = (today == month_start_bar)
if not is_rebalance_day:
    return []          # hold through the month; the engine keeps existing positions
```

- "First trading day of the month" = the **earliest bar in the data** whose
  `(year, month)` matches today — not calendar day 1 (markets close on weekends/
  holidays). The pattern above handles that.
- Weekly → gate on `today.isocalendar().week` change, or `today.weekday()`.
- N-day → track bar count since the last rebalance via a parameter and the
  length of the slice.
- Because `generate_signals` is stateless across cycles, derive "is it a
  rebalance bar?" purely from the date axis of `prices`, never from instance
  state.

---

## 4. Brackets — use the house helper, not QC stop objects

Do **not** port `StopMarketOrder`, `TrailingStopOrder`, `self.LimitOrder`, or any
LEAN order object. Our `Signal` carries the bracket inline. Two options:

**(a) House helper (preferred for ATR-style stops).** `compute_stops_and_targets`
returns regime-scaled stop/targets:
```python
st = self.compute_stops_and_targets(
    prices_series = close,          # the ticker's close Series
    direction     = 'LONG',         # or 'SHORT'
    current_price = float(last),
    atr_multiplier= 2.0,
    regime_state  = regime_state,   # scales the ATR stop to preserve R:R
)
# st == {'stop': ..., 't1': ..., 't2': ..., 't3': ...}
Signal(..., stop_loss=st['stop'], target_1=st['t1'], target_2=st['t2'], target_3=st['t3'], ...)
```

**(b) Explicit per-Signal fields** when the rule specifies its own levels
(e.g. fixed-percent or a model target):
```python
Signal(
    ...,
    stop_loss = float(last) * 0.90,   # 10% stop
    target_1  = float(last) * 1.05,
    target_2  = float(last) * 1.10,
    target_3  = float(last) * 1.20,
)
```

A pure rotation / always-rebalanced strategy with no explicit stop can set the
stop wide and targets at sensible defaults (or reuse the helper) — but every
`Signal` field is required, so always populate all four.

---

## 5. Regime gating + the daily-bar fill model

- **Gate with `should_run`.** Set `active_in_regimes` on the class (a subset of
  the four canonical tags `LOW_VOL`, `TRANSITIONING`, `HIGH_VOL`, `CRISIS`) and
  early-return when the regime doesn't match:
  ```python
  regime_state = regime.get('state', 'LOW_VOL')
  if not self.should_run(regime_state):
      return []
  ```
  Most LEAN ports have no regime view — pick the tags the thesis actually works
  in (trend/momentum → `['LOW_VOL', 'TRANSITIONING']`; all-weather → all four).
- **Fill model.** The engine fills each signal at the **next** bar's close
  (`close[t+1]`), daily bars. So: do not assume you trade at the signal-bar price;
  `entry_price` is the decision-bar close (the backtest re-anchors the bracket to
  the actual fill, preserving pct shape). Signals on the very last available bar
  are dropped (no `t+1` to fill into) — that's expected, not a bug.
- No intraday timing, no limit-at-open, no `MarketOnOpenOrder` — those LEAN
  constructs have no analog at the daily-bar grain.

---

## 6. The `Signal` contract — exact fields (do NOT invent fields)

Source of truth: `src/strategies/base.py`. The `Signal` dataclass fields:

| Field | Type | Notes |
|---|---|---|
| `ticker` | `str` | e.g. `'SPY'` |
| `direction` | `str` | `'LONG'` \| `'SHORT'` \| `'FLAT'` for equity/etp; `'SELL_VOL'`/`'BUY_VOL'` for option; `'LONG'`/`'FLAT'` for crypto |
| `entry_price` | `float` | decision-bar close (engine re-anchors to `close[t+1]` fill) |
| `stop_loss` | `float` | required |
| `target_1` | `float` | required |
| `target_2` | `float` | required |
| `target_3` | `float` | required |
| `position_size_pct` | `float` | 0.0–1.0 fraction of portfolio; regime-scaled downstream |
| `confidence` | `str` | `'HIGH'` \| `'MED'` \| `'LOW'` — **never** a float, never None |
| `signal_params` | `dict` | optional extra params (default `{}`) |
| `features` | `dict` | optional strategy-specific features (default `{}`) |
| `option_spec` | `OptionSpec` \| `None` | `None` for equity/etp/crypto (leave it out) |

`direction` values for equity/etp ports: **`LONG` / `SHORT` / `FLAT`** only.
Do not introduce new direction strings. There is no separate "quantity",
"order type", or "side" field — direction + `position_size_pct` express the
whole order; the bracket fields express the exit.

---

## 7. Worked example — Asset-Class Trend Following (10-month SMA rotation)

**The rule (from the blueprint):** hold each of `SPY` (US equity), `EFA`
(developed-international), `IEF` (7–10y Treasuries), `VNQ` (REITs), `GSG`
(commodities) **only while its price is above its own 10-month SMA**; equal-weight
the assets that pass; **rebalance monthly**; assets below their SMA sit in cash
(simply not held). Source: cite the blueprint URL in the docstring.

LEAN would express this with `AddEquity` per ETF, an `SMA(sym, 10, Resolution.Daily)`
*on monthly-resampled bars*, a monthly `Schedule.On`, and `SetHoldings(sym, 1/n)` /
`Liquidate(sym)`. Here is the clean-room port:

```python
from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

INSTRUMENT_CLASS = "etp"   # SPY/EFA/IEF/VNQ/GSG are ETPs

_SLEEVE = ["SPY", "EFA", "IEF", "VNQ", "GSG"]
_SMA_MONTHS = 10


class AssetClassTrendFollowing(BaseStrategy):
    """Asset-class trend following: hold each sleeve ETF only while above its
    10-month SMA, equal-weight the survivors, rebalance monthly.

    Clean-room port of the LEAN blueprint. Source: <SOURCE_URL>.
    Reference implementation used for rule + parameters only; not copied.
    """
    id   = "S_asset_class_trend_following"
    name = "Asset-Class Trend Following (10mo SMA)"
    active_in_regimes = ["LOW_VOL", "TRANSITIONING", "HIGH_VOL"]

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get("state", "LOW_VOL")
        if not self.should_run(regime_state):
            return []

        idx = prices.index
        if len(idx) == 0:
            return []
        today = idx[-1]

        # Monthly cadence: act only on the first available trading bar of the
        # current month (replicates QC's monthly Schedule.On). Other days: hold.
        month_bars = idx[(idx.year == today.year) & (idx.month == today.month)]
        if len(month_bars) == 0 or today != month_bars[0]:
            return []

        # Resample to month-end closes, then 10-month SMA == QC's monthly SMA(10).
        survivors = []
        for ticker in _SLEEVE:
            if ticker not in prices.columns:
                continue
            close = prices[ticker].dropna()
            monthly = close.resample("ME").last().dropna()   # month-end closes
            if len(monthly) < _SMA_MONTHS:
                continue                                       # "warmup" guard
            sma = monthly.rolling(_SMA_MONTHS).mean().iloc[-1]
            last = float(close.iloc[-1])
            if last > float(sma):                              # above 10mo SMA → hold
                survivors.append((ticker, last))

        if not survivors:
            return []                                          # all in cash this month

        weight = 1.0 / len(survivors)                          # equal-weight survivors
        signals: List[Signal] = []
        for ticker, last in survivors:
            st = self.compute_stops_and_targets(
                prices[ticker].dropna(), "LONG", last,
                atr_multiplier=2.0, regime_state=regime_state,
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = "LONG",
                entry_price       = last,
                stop_loss         = st["stop"],
                target_1          = st["t1"],
                target_2          = st["t2"],
                target_3          = st["t3"],
                position_size_pct = weight,
                confidence        = "MED",
            ))

        print(f"[debug] signals={len(signals)}", file=sys.stderr)
        return signals
```

What ported to what:
- `AddEquity` per ETF → just read the columns from `prices` (sleeve list).
- monthly `SMA(10)` indicator object → `resample("ME").last()` then
  `.rolling(10).mean()` — computed inline, no warmup object.
- monthly `Schedule.On` → the "first available bar of the month" date check.
- `SetHoldings(sym, 1/n)` → `Signal(position_size_pct = 1/len(survivors))`.
- `Liquidate(sym)` / below-SMA → the symbol is simply **absent** from the list.
- (no LEAN stop in the original) → defaulted via `compute_stops_and_targets`.

---

## 8. Porting checklist

1. Read `REFERENCE_IMPLEMENTATION`; extract rule, parameters, cadence, universe,
   long/short logic, any stops/targets. **Do not copy it.**
2. Write a fresh `BaseStrategy` subclass; `generate_signals` returns `List[Signal]`.
3. Cite `SOURCE_URL` in the class docstring.
4. Replace warmup/indicator objects with pandas on `prices` + a length guard.
5. `SetHoldings → position_size_pct`; `Liquidate → omit the symbol`.
6. Replicate the schedule with date logic over the daily bars.
7. Brackets via `compute_stops_and_targets` or explicit `stop_loss`/`target_1..3`.
8. Gate with `should_run(regime_state)`; pick canonical `active_in_regimes`.
9. Handle empty/short history → return `[]` (never raise); add the
   `[debug] signals=` stderr line before the return.
10. Use only the `Signal` fields in §6 — invent nothing.
