# SP-7 Price Adjustment Convention

## 1. Canonical convention

Effective at SP-7 Phase A merge, the canonical price convention for
`data/master/prices.parquet` and all daily Alpaca bar appends is
**split-adjusted only** (`--adjustment split`).

Prior to this change the daily collector used `--adjustment all` (splits +
dividends). The Phase-B backfiller (`scripts/backfill_universe_5y.py`,
`--adjustment split`) already used the split-only convention, creating a
mixed-basis file.

## 2. Why split-adjusted only?

Dividend adjustment (`--adjustment all`) restates **all historical bars** on
every dividend event: a dividend paid today changes every prior close for
that ticker. Under the append-only master-store invariant (no overwrites, no
deletes) this is incompatible — each daily append would silently invalidate
every previously-appended row for dividend-paying tickers, corrupting the
panel without any flag or warning.

Split adjustment is safe: a split on date D restates history before D, which
the split-watcher (see §4) detects and queues for a deliberate operator-run
supersede re-backfill.

Dividends are separately available in `data/master/corporate_actions.parquet`
(action_type = 'cash_dividend'). Backtest engines that need total-return
figures should use `src/backtest/total_return.py:total_return_close` (§5).

## 3. Legacy mixed history

Bars collected before the SP-7 Phase A merge date are a mix of:
- Split-only adjustment (backfilled rows via `backfill_universe_5y.py`)
- Split+dividend adjustment (daily appends via the old collector)

These are **not restated** after the flip. The mixed region converges forward
from the merge date as new split-only bars are appended. Strategies with
long look-back windows should treat pre-flip bars as having a slight downward
bias in dividend-heavy tickers (prior closes were dividend-deflated; the new
basis removes that deflation). For most momentum/regime strategies the effect
is negligible; income strategies should note this.

## 4. Splits: detection and supersede re-backfill

Split-adjusted history is stable day-to-day **except** when a new split
occurs: on the ex-date, all prior bars for that ticker are restated by the
split ratio. Without intervention the historical panel becomes inconsistent
(old appended bars at the old ratio, new appends at the new ratio).

The daily `scripts/split_watcher.py` timer (systemd: `sp7-split-watcher.timer`,
21:15 UTC Mon–Fri) detects split ex-dates on covered tickers and:

1. Appends `data/.pending_split_rebackfills.txt` with the ticker and ratio.
2. Posts a notification to #data-alerts (via `DISCORD_DATA_ALERTS_WEBHOOK`
   env override or stdout fallback; repo canonical = DB-backed channel).

The operator then runs the sanctioned supersede re-backfill per
`docs/sp2-backfill-runbook.md` (v2 path: `OPENCLAW_BACKFILL_ALLOW_OVERWRITE=1`
+ `--source-tag backfill_5y_vN` + `--supersede-quarantine`). This is
**deliberate and audited** — the split-watcher only detects; it never
overwrites data on its own.

## 5. Total return

Backtests wanting dividend-inclusive returns should use:

```python
from backtest.total_return import total_return_close

tr = total_return_close(prices_df, actions_df, "AAPL")
# Returns DataFrame[date, close, tr_factor, tr_close]
```

`tr_factor` on an ex-date = `(close + dividend) / close`, then cumulatively
shifted so each dividend is reinvested into every subsequent close. See
`src/backtest/total_return.py` for the full docstring and convention notes.

This helper is a **documented utility only** in Phase A; backtest engines
adopt it in their own projects.
