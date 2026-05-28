# S12 Insider Sell-Cluster Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the live `S12_insider` strategy (class `InsiderClusterBuy` in `src/strategies/implementations/s12_insider.py`) so it also emits `direction='SHORT'` signals when a sell-cluster pattern fires, guarded by a default-OFF env gate so the live behavior is byte-identical to today until the operator explicitly flips it.

**Architecture:** Single-file surgical modification. Existing BUY-cluster code path untouched. New SELL branch added in parallel inside `generate_signals()`, reading `OPENCLAW_S12_SELL_CLUSTER=1` per-call (since the engine instantiates and calls fresh each cycle). Symmetric to BUY logic but with tighter thresholds (≥5 distinct sellers / zero buys / ≥$2M net) and inverted stop/target geometry. New parameters land in the existing `default_parameters()` dict (project convention) rather than as module-level constants.

**Tech Stack:** Python 3.11 + pandas + the existing `Signal` dataclass + `unittest`-style pytest.

**Spec reference:** `docs/superpowers/specs/2026-05-28-s12-insider-sell-cluster-design.md` (commit `a11c23e`).

---

## Resolved open questions (from spec §10)

1. **Constants location:** the existing strategy keeps its BUY thresholds in `default_parameters()` (a method returning a dict), NOT as module-level constants. Plan adds the SELL thresholds to the SAME dict to follow house convention. Test pinning is done via `InsiderClusterBuy().default_parameters()['min_sell_insiders']` etc.

2. **`transaction_type` enum values (verified against live `insider.parquet`):** exactly `'P-Purchase'` for buys and `'S-Sale'` for sells. Existing code does `txn_type.upper()` (line 70 of `s12_insider.py`) — the new SELL filter must mirror that. The robust check is `'SALE' in txn_type.upper()` for sells and `'PURCHASE' in txn_type.upper() or 'BUY' in txn_type.upper()` for buys (matches existing pattern).

3. **Backtest runner CLI:** correct invocation is
   `python3 -m src.backtest.unified_backtest --strategy-id S12_insider`
   (NOT `run_backtest --strategy`). Writes to Postgres tables `strategy_backtest_runs`, `strategy_backtest_regimes`, `strategy_backtest_trades` and prints `run_id` to stdout.

---

## Existing code shape (from grounding — read first before editing)

`src/strategies/implementations/s12_insider.py` — 121 lines. Class is `InsiderClusterBuy`. Strategy id literal is `'S12_insider'`.

```python
# Approximate shape — read the real file first
class InsiderClusterBuy(BaseStrategy):
    id = 'S12_insider'

    def default_parameters(self) -> dict:
        return {
            'min_insiders':      3,
            'lookback_days':     20,
            'min_net_buy_value': 500_000,
            'min_buy_value':     50_000,
            'base_size_pct':     0.03,
        }

    def generate_signals(self, prices, regime, universe, aux_data):
        params = self.default_parameters()
        txns = aux_data.get('insider_txns')
        if txns is None or txns.empty:
            return []
        # ... 20-day window filter ...
        # Per-ticker loop: filter to buys with txn_type.upper() containing BUY/PURCHASE
        # Aggregate distinct_insiders, net_buy_value, buy_count
        # Compute confidence='HIGH' if distinct >= 5 and net >= 2M else 'MED'
        # Emit Signal(ticker, direction='LONG', entry_price=current_price,
        #             stop_loss=stops['stop'], target_1=stops['t1'], ...,
        #             position_size_pct=..., confidence=..., signal_params={...})
        return signals
```

The `stops` dict comes from a helper (likely `calculate_stops(ticker, prices, direction)` or similar — verify in the actual file). For SHORT signals the helper must be called with `direction='SHORT'` so stop_loss is ABOVE current_price and targets are BELOW.

---

## File structure

| File | Status | Responsibility |
|------|--------|----------------|
| `src/strategies/implementations/s12_insider.py` | CHANGED (additive) | Add 3 new entries to `default_parameters()`; add SELL branch inside `generate_signals()` gated by `OPENCLAW_S12_SELL_CLUSTER` env var. Existing BUY logic untouched. |
| `tests/strategies/test_s12_insider_sell_cluster.py` | NEW | 8 unit tests covering gate-OFF regression, threshold boundaries, GLW fixture, and BUY+SELL interaction. |
| `docs/superpowers/runs/2026-05-XX-s12-sell-cluster-backtest.json` | NEW (operator-generated during Task 3) | Backtest verdict before/after metrics + FLIP/HOLD/REJECT decision. |

**Non-touch:** `src/strategies/registry.py` (manifest stays as-is), `src/strategies/manifest.json` (lifecycle state stays `live`), `src/strategies/lifecycle.py`, `src/execution/regime_blended_sizer_live.py` (sizer math handles SHORT signals via existing path), and every `feat/sp[1-5]-*` branch.

---

### Task 1: Write all 8 failing unit tests

**Files:**
- Create: `tests/strategies/test_s12_insider_sell_cluster.py`

This is the TDD failing-test step. The strategy doesn't have SELL behavior yet, so tests 2-8 must fail; test 1 (gate-OFF regression) is expected to pass even before the SELL code lands because it pins existing BUY-only behavior.

- [ ] **Step 1: Create the directory if needed and confirm the test file location**

```bash
cd /root/openclaw && mkdir -p tests/strategies && ls tests/strategies/
```

If there's no `tests/strategies/__init__.py`, create an empty one:

```bash
test -f tests/strategies/__init__.py || touch tests/strategies/__init__.py
```

- [ ] **Step 2: Read the real `s12_insider.py` end-to-end FIRST**

Before writing any test, read the file so the fixtures match the actual `aux_data['insider_txns']` shape, the actual `Signal(...)` constructor arguments, and the actual class/method names:

```bash
cd /root/openclaw && cat src/strategies/implementations/s12_insider.py
```

Note in particular: column names of the txns DataFrame the strategy reads (`ticker`, `date`, `transaction_date`, `insider_name`, `transaction_type`, `net_value`, etc.), and the precise `Signal` field names.

- [ ] **Step 3: Write the test file**

```python
# tests/strategies/test_s12_insider_sell_cluster.py
"""Unit tests for S12_insider sell-cluster extension.

The existing strategy emits LONG signals on insider-buy clusters.
This file tests the NEW sell-cluster path gated by OPENCLAW_S12_SELL_CLUSTER.

Test #1 pins existing behavior (gate-OFF regression).
Tests #2-#8 cover the new SELL branch with the gate ON.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pandas as pd
import pytest

from src.strategies.implementations.s12_insider import InsiderClusterBuy


def _make_txns(rows: list[dict]) -> pd.DataFrame:
    """Build an insider_txns DataFrame in the shape the strategy reads.

    Each row dict has: ticker, transaction_date (datetime-like or 'YYYY-MM-DD'),
    insider_name, transaction_type ('S-Sale' | 'P-Purchase' | etc.),
    net_value (float).
    """
    if not rows:
        return pd.DataFrame(columns=[
            'ticker', 'date', 'transaction_date', 'insider_name',
            'transaction_type', 'shares', 'price_per_share', 'net_value',
        ])
    df = pd.DataFrame(rows)
    if 'date' not in df.columns:
        df['date'] = pd.to_datetime(df['transaction_date'])
    df['transaction_date'] = pd.to_datetime(df['transaction_date'])
    for col in ('shares', 'price_per_share'):
        if col not in df.columns:
            df[col] = 0.0
    return df


def _make_prices(ticker: str, current_price: float = 100.0) -> pd.DataFrame:
    """Minimal prices DataFrame indexed by date with a 'close' column.

    The strategy is expected to read the latest close for entry_price.
    Adapt this builder if the actual strategy expects a different prices
    contract (e.g., multi-index by ticker).
    """
    dates = pd.date_range('2026-04-01', '2026-05-27', freq='B')
    return pd.DataFrame({
        'ticker': ticker,
        'date': dates,
        'open':  current_price,
        'high':  current_price * 1.01,
        'low':   current_price * 0.99,
        'close': current_price,
        'volume': 1_000_000,
    })


def _make_universe(tickers: list[str]) -> list[str]:
    return list(tickers)


def _make_aux(txns_df: pd.DataFrame) -> dict:
    return {'insider_txns': txns_df}


def _five_sellers_3m(ticker: str = 'GLW') -> list[dict]:
    """5 distinct insiders, $600K each = $3M total, 0 buys."""
    return [
        {'ticker': ticker, 'transaction_date': '2026-05-12',
         'insider_name': f'Officer_{i}', 'transaction_type': 'S-Sale',
         'net_value': 600_000.0}
        for i in range(5)
    ]


# -------- Test 1: gate-OFF regression --------

@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '0'}, clear=False)
def test_gate_off_yields_no_short_signals_even_with_perfect_cluster():
    """With the gate OFF, no SHORT signals fire even on a GLW-like cluster."""
    strat = InsiderClusterBuy()
    sells = _five_sellers_3m('GLW')
    # Add one more big-ticket sell to make it $35M-ish so the temptation is high
    sells.append({
        'ticker': 'GLW', 'transaction_date': '2026-05-22',
        'insider_name': 'CTO_Jaymin_Amin', 'transaction_type': 'S-Sale',
        'net_value': 5_260_000.0,
    })
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime='LOW_VOL',
        universe=_make_universe(['GLW']),
        aux_data=_make_aux(_make_txns(sells)),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == [], f'expected zero SHORTs with gate OFF, got {shorts}'


# -------- Tests 2-8: gate ON --------

@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_five_sellers_3m_zero_buys_emits_one_short():
    strat = InsiderClusterBuy()
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime='LOW_VOL',
        universe=_make_universe(['GLW']),
        aux_data=_make_aux(_make_txns(_five_sellers_3m('GLW'))),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert len(shorts) == 1
    short = shorts[0]
    assert short.ticker == 'GLW'
    assert short.direction == 'SHORT'
    # Reason / signal_params should mention the breadth and dollar value
    params = getattr(short, 'signal_params', {}) or {}
    text = repr(params) + ' ' + getattr(short, 'reason', '')
    assert '5' in text or 'sellers' in text.lower() or 'insider' in text.lower()


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_four_sellers_blocks_breadth_gate():
    strat = InsiderClusterBuy()
    sells = _five_sellers_3m('GLW')[:4]  # only 4 distinct
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime='LOW_VOL',
        universe=_make_universe(['GLW']),
        aux_data=_make_aux(_make_txns(sells)),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == []


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_five_sellers_plus_one_buy_blocks_zero_buys_gate():
    strat = InsiderClusterBuy()
    sells = _five_sellers_3m('GLW')
    sells.append({
        'ticker': 'GLW', 'transaction_date': '2026-05-15',
        'insider_name': 'Lone_Buyer', 'transaction_type': 'P-Purchase',
        'net_value': 60_000.0,
    })
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime='LOW_VOL',
        universe=_make_universe(['GLW']),
        aux_data=_make_aux(_make_txns(sells)),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == [], 'zero-buys gate must block when any P-Purchase is in window'


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_five_sellers_below_dollar_threshold_blocks():
    strat = InsiderClusterBuy()
    sells = [
        {'ticker': 'GLW', 'transaction_date': '2026-05-12',
         'insider_name': f'Officer_{i}', 'transaction_type': 'S-Sale',
         'net_value': 250_000.0}  # 5 * 250K = $1.25M, below $2M
        for i in range(5)
    ]
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime='LOW_VOL',
        universe=_make_universe(['GLW']),
        aux_data=_make_aux(_make_txns(sells)),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == []


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_one_seller_many_tranches_blocks_breadth_gate():
    """A single insider unloading in 5 separate transactions must NOT pass."""
    strat = InsiderClusterBuy()
    sells = [
        {'ticker': 'GLW', 'transaction_date': f'2026-05-{10 + i:02d}',
         'insider_name': 'Solo_CFO',  # SAME name, 5 tranches
         'transaction_type': 'S-Sale',
         'net_value': 600_000.0}
        for i in range(5)
    ]
    signals = strat.generate_signals(
        prices=_make_prices('GLW'),
        regime='LOW_VOL',
        universe=_make_universe(['GLW']),
        aux_data=_make_aux(_make_txns(sells)),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert shorts == [], 'distinct_insiders must use set; 1 person != 5 sellers'


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_glw_historical_fixture():
    """The actual GLW 2026-05 pattern: 10 distinct officers, $35M, 0 buys."""
    strat = InsiderClusterBuy()
    glw_rows = [
        ('Zhang John Z',           '2026-05-06', 2_770_000.0),
        ('Schlesinger Edward A',   '2026-05-07', 4_200_000.0),
        ('STEVERSON LEWIS A',      '2026-05-08', 5_440_000.0),
        ('Gullo Michelle L',       '2026-05-08', 1_000_000.0),
        ('Becker Stefan',          '2026-05-08', 3_950_000.0),
        ('TILLMAN MICHAUNE D',     '2026-05-12',   674_000.0),
        ('Seetharam Soumya',       '2026-05-12', 4_120_000.0),
        ('Verkleeren Ronald L',    '2026-05-14', 2_080_000.0),
        ('Nelson Avery H III',     '2026-05-18', 3_920_000.0),
        ('Amin Jaymin',            '2026-05-22', 5_260_000.0),
    ]
    sells = [
        {'ticker': 'GLW', 'transaction_date': d,
         'insider_name': name, 'transaction_type': 'S-Sale', 'net_value': v}
        for (name, d, v) in glw_rows
    ]
    signals = strat.generate_signals(
        prices=_make_prices('GLW', current_price=192.0),
        regime='LOW_VOL',
        universe=_make_universe(['GLW']),
        aux_data=_make_aux(_make_txns(sells)),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    assert len(shorts) == 1
    s = shorts[0]
    assert s.ticker == 'GLW'
    # Stop must be ABOVE current price for a short
    if getattr(s, 'stop_loss', None) is not None:
        assert s.stop_loss > 192.0, 'SHORT stop_loss must be above current price'
    # Target_1 must be BELOW current price for a short
    if getattr(s, 'target_1', None) is not None:
        assert s.target_1 < 192.0, 'SHORT target_1 must be below current price'


@patch.dict(os.environ, {'OPENCLAW_S12_SELL_CLUSTER': '1'}, clear=False)
def test_gate_on_buy_cluster_and_sell_cluster_simultaneously():
    """Contrived: in the SAME 20d window, a ticker has BOTH 5 sellers AND
    3 buyers. The SELL branch's zero-buys gate must block; the existing BUY
    branch should still fire normally."""
    strat = InsiderClusterBuy()
    sells = _five_sellers_3m('ZZZ')
    buys = [
        {'ticker': 'ZZZ', 'transaction_date': '2026-05-13',
         'insider_name': f'Buyer_{i}', 'transaction_type': 'P-Purchase',
         'net_value': 200_000.0}
        for i in range(3)
    ]
    signals = strat.generate_signals(
        prices=_make_prices('ZZZ'),
        regime='LOW_VOL',
        universe=_make_universe(['ZZZ']),
        aux_data=_make_aux(_make_txns(sells + buys)),
    )
    shorts = [s for s in signals if getattr(s, 'direction', '') == 'SHORT']
    longs  = [s for s in signals if getattr(s, 'direction', '') == 'LONG']
    assert shorts == [], 'zero-buys gate must block SHORT when buys are present'
    # The LONG branch's own gates may or may not fire (depends on per-buyer min),
    # but the test pins the SHORT-side behavior, which is what we're shipping.
```

- [ ] **Step 4: Run the tests to verify Test 1 passes and Tests 2-8 fail**

```bash
cd /root/openclaw && pytest tests/strategies/test_s12_insider_sell_cluster.py -v
```

Expected:
- Test 1 (`test_gate_off_yields_no_short_signals_even_with_perfect_cluster`) **PASSES** — confirms the current strategy emits no SHORTs (and won't until Task 2 ships).
- Tests 2-8 **FAIL** — the SELL branch doesn't exist yet.

If Test 1 fails, the test's contract with the actual `generate_signals` signature is wrong — fix the fixture builder (probably `_make_prices` or `_make_aux` shape) before proceeding.

- [ ] **Step 5: Commit the failing tests**

```bash
cd /root/openclaw
git add tests/strategies/__init__.py tests/strategies/test_s12_insider_sell_cluster.py
git commit -m "test(s12-sell-cluster): failing tests for sell-cluster extension"
```

---

### Task 2: Add the SELL branch to `s12_insider.py`

**Files:**
- Modify: `src/strategies/implementations/s12_insider.py`

This task makes Tests 2-8 pass without disturbing Test 1 (gate-OFF regression) or any existing BUY behavior.

- [ ] **Step 1: Read the real file end-to-end**

```bash
cd /root/openclaw && cat src/strategies/implementations/s12_insider.py
```

Locate exactly:
- The `default_parameters()` method.
- The line range of the BUY branch inside `generate_signals()` (currently lines ~60-117 approximately).
- The exact name of the stop-calculation helper (probably `calculate_stops(...)` or similar — note its signature).
- The exact `Signal(...)` constructor — note all field names it accepts.

If the actual code structure deviates significantly from the shape in the spec's "Existing code shape" section, ADAPT the changes below to fit — do not blindly paste.

- [ ] **Step 2: Add the SELL constants to `default_parameters()`**

Add three new entries to the dict returned by `default_parameters()` (insert after the existing BUY constants):

```python
def default_parameters(self) -> dict:
    return {
        # --- existing BUY constants (do not change) ---
        'min_insiders':      3,
        'lookback_days':     20,
        'min_net_buy_value': 500_000,
        'min_buy_value':     50_000,
        'base_size_pct':     0.03,
        # --- new SELL-cluster constants (added 2026-05-28) ---
        'min_sell_insiders':   5,
        'min_net_sell_value':  2_000_000,
        'require_zero_buys':   True,
    }
```

The existing BUY constants remain at their current values. Use the same `lookback_days` (20) for the SELL window — already shared.

- [ ] **Step 3: Add the SELL branch in `generate_signals()`**

After the existing BUY-emission loop (which appends LONG `Signal` objects to a `signals` list), add an env-gated SELL block. The exact location is "after the BUY signals are appended but before `return signals`":

```python
        # -------- SELL-CLUSTER BRANCH (gated; default OFF) --------
        if os.environ.get('OPENCLAW_S12_SELL_CLUSTER') == '1':
            sell_signals = self._generate_sell_signals(
                params=params,
                txns=txns_in_window,   # the same window-filtered DF the BUY branch uses
                prices=prices,
                universe=universe,
            )
            signals.extend(sell_signals)
        # -------- end SELL-CLUSTER BRANCH --------

        return signals
```

`txns_in_window` is whatever variable name the existing BUY branch already uses for the 20-day-filtered txns DataFrame — DO NOT re-filter; reuse the existing variable to keep the windowing logic DRY.

- [ ] **Step 4: Add the `_generate_sell_signals` helper method**

This is the new method that does the symmetric SELL-cluster math. Add it to the `InsiderClusterBuy` class, right after the existing BUY helper (or just before `default_parameters()` — match local convention).

```python
    def _generate_sell_signals(
        self,
        params: dict,
        txns: pd.DataFrame,
        prices: pd.DataFrame,
        universe: list[str],
    ) -> list:
        """Emit SHORT signals on insider sell-clusters.

        Gates: distinct_sellers >= params['min_sell_insiders'],
               len(buys) == 0 (when params['require_zero_buys'] is True),
               net_sell_value >= params['min_net_sell_value'].
        """
        if txns is None or txns.empty:
            return []

        out = []
        for ticker in universe:
            sub = txns[txns['ticker'] == ticker]
            if sub.empty:
                continue

            ttype_upper = sub['transaction_type'].astype(str).str.upper()
            sells_mask  = ttype_upper.str.contains('SALE', na=False)
            buys_mask   = ttype_upper.str.contains('PURCHASE|BUY', na=False, regex=True)

            sells = sub[sells_mask]
            buys  = sub[buys_mask]

            if sells.empty:
                continue
            if params.get('require_zero_buys', True) and not buys.empty:
                continue

            distinct_sellers = sells['insider_name'].nunique()
            if distinct_sellers < params['min_sell_insiders']:
                continue

            net_sell_value = float(sells['net_value'].sum())
            if net_sell_value < params['min_net_sell_value']:
                continue

            current_price = self._get_current_price(prices, ticker)
            if current_price is None or current_price <= 0:
                continue

            # Mirror the BUY branch's stop/target helper but with direction='SHORT'.
            # Substitute the actual helper name and signature from the real file.
            stops = self._calculate_stops(  # rename if helper has a different name
                ticker=ticker,
                prices=prices,
                entry_price=current_price,
                direction='SHORT',
            )

            confidence = 'HIGH'  # SELL gates are already at the HIGH-buy thresholds
            out.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=current_price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops.get('t2'),
                target_3=stops.get('t3'),
                position_size_pct=params['base_size_pct'],
                confidence=confidence,
                signal_params={
                    'distinct_insiders': int(distinct_sellers),
                    'net_sell_value':    net_sell_value,
                    'sell_count':        int(len(sells)),
                    'lookback_days':     int(params['lookback_days']),
                    'cluster_kind':      'SELL',
                },
            ))
        return out
```

**Important compatibility notes:**

- If `_calculate_stops` is named differently (e.g., `_compute_stops`, `compute_targets`, `_bracket_for`), use the actual name. Find it via `grep -n "def _.*stop\|def _.*target" src/strategies/implementations/s12_insider.py`.
- If the helper does NOT accept a `direction='SHORT'` parameter, you'll need to add one (small additive change) OR construct the inverted stop/target manually:
  ```python
  # Manual fallback if the helper is LONG-only:
  buffer = 0.02
  stops = {
      'stop': current_price * (1 + buffer * 2),    # above current
      't1':   current_price * (1 - buffer * 2),    # below current
      't2':   current_price * (1 - buffer * 4),
      't3':   current_price * (1 - buffer * 6),
  }
  ```
  If you must use the manual fallback, note it in the commit message.
- If `_get_current_price` is named differently, find the actual price-lookup helper the existing BUY branch uses (probably `prices[ticker].iloc[-1]['close']` or similar). Mirror that pattern.
- The `Signal(...)` constructor's exact field set is what the existing BUY branch uses — match it. If `target_2`/`target_3` are required (not optional), provide them; if any field name differs, use the actual one.

- [ ] **Step 5: Ensure `import os` and `import pandas as pd` exist at top of file**

Most likely both are already imported. Verify:

```bash
cd /root/openclaw && head -15 src/strategies/implementations/s12_insider.py
```

If `os` is missing, add `import os` to the imports block. `pandas` is virtually certain to already be there.

- [ ] **Step 6: Run all tests to verify they now pass**

```bash
cd /root/openclaw && pytest tests/strategies/test_s12_insider_sell_cluster.py -v
```

Expected: **8 passed**.

If any of Tests 2-8 still fail, the discrepancy is between the test's assumption and the real strategy's behavior. Most common causes:
- The `_make_prices` builder doesn't match what the strategy actually reads (try printing the DataFrame the strategy sees).
- The `Signal` dataclass doesn't have a field like `signal_params` or has stricter type validation than the test assumes.
- The strategy filters universe differently (e.g., requires the ticker to be in a price index, not just the universe list).

Fix the test fixture to reflect the real contract, NOT the production code. Tests adapt to the system; production code should not bend to flawed tests.

- [ ] **Step 7: Regression — run any existing strategy or pipeline tests that touch S12 with gate OFF**

```bash
cd /root/openclaw && OPENCLAW_S12_SELL_CLUSTER=0 pytest tests/ -k "s12 or insider" -v 2>&1 | tail -20
```

Expected: any pre-existing tests pass; the new file's 8 tests pass. No regression.

- [ ] **Step 8: Commit the strategy modification**

```bash
cd /root/openclaw
git add src/strategies/implementations/s12_insider.py
git commit -m "feat(s12-sell-cluster): SHORT branch gated by OPENCLAW_S12_SELL_CLUSTER (default OFF)"
```

---

### Task 3: Operator-driven backtest validation

**Files:**
- Create: `docs/superpowers/runs/2026-05-28-s12-sell-cluster-backtest.json`

This is the validation step required by the spec before the gate flips ON in production. It is operator-driven (human runs the backtest, captures the result, decides FLIP/HOLD/REJECT).

- [ ] **Step 1: Capture the current (BUY-only) baseline metrics**

```bash
cd /root/openclaw
OPENCLAW_S12_SELL_CLUSTER=0 python3 -m src.backtest.unified_backtest \
    --strategy-id S12_insider \
  | tee /tmp/s12_baseline.txt
```

Capture the `run_id` printed at the end of stdout. Query Postgres for the metrics:

```bash
python3 -c "
import os, psycopg2
RUN_ID = '<paste run_id here>'
with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
    cur.execute('SELECT sharpe_ratio, max_drawdown, trade_count FROM strategy_backtest_runs WHERE id = %s', (RUN_ID,))
    print(cur.fetchone())
"
```

Record: `baseline_sharpe`, `baseline_max_drawdown`, `baseline_trade_count`.

- [ ] **Step 2: Capture the modified (BUY + SELL) metrics**

```bash
cd /root/openclaw
OPENCLAW_S12_SELL_CLUSTER=1 python3 -m src.backtest.unified_backtest \
    --strategy-id S12_insider \
  | tee /tmp/s12_modified.txt
```

Capture the new `run_id` and query the same way. Record: `modified_sharpe`, `modified_max_drawdown`, `modified_trade_count`.

- [ ] **Step 3: Apply acceptance criteria**

Decision logic (FLIP = ship live, HOLD = leave gate OFF and iterate, REJECT = drop the SELL path):

- **FLIP** iff: `modified_sharpe >= 0.5` AND `modified_max_drawdown <= 0.20` AND `modified_sharpe >= baseline_sharpe`.
- **HOLD** if any acceptance criterion fails but the difference is small (within ~10%) — operator iterates on thresholds.
- **REJECT** if `modified_sharpe < baseline_sharpe` by more than 10% AND the modified MaxDD exceeds the baseline meaningfully.

- [ ] **Step 4: Write the verdict file**

Create `docs/superpowers/runs/2026-05-28-s12-sell-cluster-backtest.json` with the actual numbers from Steps 1-2 and the decision from Step 3. Schema:

```json
{
  "date": "2026-05-28",
  "spec": "docs/superpowers/specs/2026-05-28-s12-insider-sell-cluster-design.md",
  "baseline": {
    "run_id": "<uuid>",
    "sharpe_ratio": 0.0,
    "max_drawdown": 0.0,
    "trade_count": 0
  },
  "modified": {
    "run_id": "<uuid>",
    "sharpe_ratio": 0.0,
    "max_drawdown": 0.0,
    "trade_count": 0
  },
  "decision": "FLIP | HOLD | REJECT",
  "rationale": "<2-3 sentences explaining the decision>"
}
```

Replace the zeros and UUIDs with the actual captured values. Replace the decision and rationale with the actual operator finding.

- [ ] **Step 5: Commit the verdict file**

```bash
cd /root/openclaw
git add docs/superpowers/runs/2026-05-28-s12-sell-cluster-backtest.json
git commit -m "docs(s12-sell-cluster): backtest verdict for SELL-cluster extension"
```

- [ ] **Step 6 (conditional on FLIP): flip the production gate**

Only if the verdict is FLIP. On the production VPS:

```bash
# Edit /root/openclaw/.env, add or set:
#   OPENCLAW_S12_SELL_CLUSTER=1
# Then restart the relevant services so the env var loads:
systemctl restart johnbot.service  # or whichever service consumes the strategies
```

Do NOT commit `.env` changes — `.env` is gitignored by convention. The flip is a runtime operator action, not a code change.

If the verdict is HOLD or REJECT, leave the gate at `0` in production and skip Step 6.

---

## Self-review

**Spec coverage:**
- §1 goals → Tasks 1-2 ship the env-gated SHORT branch with the GLW-class threshold.
- §2 architecture → Task 2 (single-file modification, gate-OFF byte-identical behavior).
- §3 trigger thresholds → Task 2 Step 2 (constants in `default_parameters()`) and Task 2 Step 4 (math in `_generate_sell_signals`).
- §4 backtest validation → Task 3.
- §5 env-var gate → Task 2 Step 3 (env read inside `generate_signals`).
- §6 edge cases → covered by Tests 3, 4, 5, 6, 8 in Task 1.
- §7 testing strategy → Task 1 (eight tests).
- §8 files touched → matches the File Structure table.
- §9 rollout plan → Task 3 Step 6.
- §10 open questions → all three resolved at the top of this plan (default_parameters location, txn_type values, backtest CLI).

**Placeholder scan:** Task 3 Step 4 has `<uuid>` and `<2-3 sentences explaining the decision>` placeholders for the operator to fill in — these are intentional (operator-supplied values from real backtest output), not hidden TBDs. No other placeholders.

**Type consistency:**
- `OPENCLAW_S12_SELL_CLUSTER` gate name stable across Task 1 (env patches), Task 2 (env read), Task 3 (backtest invocation).
- Threshold names (`min_sell_insiders`, `min_net_sell_value`, `require_zero_buys`) stable across Task 2 Step 2 and Task 2 Step 4.
- `_generate_sell_signals(params, txns, prices, universe)` signature consistent within Task 2.
- `Signal` constructor field set inherited from existing BUY branch (Task 2 Step 4 explicitly says "match the actual constructor; substitute names if the real one differs").

**Implementer verification reminders (NOT blockers, but read first):**
- Task 2 Step 1: read the real `s12_insider.py` end-to-end before editing — class name, method names, helper names may differ from approximation.
- Task 2 Step 4: the stop/target helper's exact name and signature. The plan provides a manual fallback if the helper is LONG-only.
- Task 3 Step 1: confirm `unified_backtest.py` actually accepts `--strategy-id` and writes to `strategy_backtest_runs` — verified by the spec-grounding step but worth a quick `--help` confirmation before consuming a backtest hour.
