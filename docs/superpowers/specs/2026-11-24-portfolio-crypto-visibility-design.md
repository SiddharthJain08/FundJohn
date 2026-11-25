# Portfolio page: crypto positions in Active Positions (BTC benchmark section) — design (short)

> **Scope ruling (operator 2026-11-24 20:26Z):** build §3.1 + the positions-endpoint wiring and tests only. The account-row split, the Risk & Sizing pooled-target split and the input-validation change (§3.2–§3.4) are NOT built: the operator confirmed Invested / λ actual already include crypto and asked for nothing further there.

**Date:** 2026-11-24 · **Operator request:** "make sure crypto positions are listed on the portfolio page with benchmark BTC listed in a similar way to SPY. Crypto NAV should be included in the target value" (λ actual, Risk & Sizing target, and the Invested $ amount). Plus: the asset-corr de-gross input raised a "save failed" toast.

## 1. What is wrong today (measured 2026-11-24 20:15Z)
- `/api/portfolio/positions` keys broker positions by Alpaca symbol (`BTCUSD`) while `execution_signals.ticker` is `BTC-USD`. The broker-truth filter (`brokerPositions[r.ticker]`) therefore drops every crypto row as a phantom. The broker holds 0.148 BTC ($8,845) and the registry already flags `S_beta_btc` (`universe {BTC-USD}`, `benchmark_sleeve=true`), so once the row survives, the existing `BENCHMARK — <ticker>` section renders it exactly like SPY. Resting crypto exit legs come back as `BTC/USD` and are keyed the same wrong way, so the stop level would also be missing.
- Account row: `Invested` = Alpaca `long_market_value` ($45,244) and `λ actual` = (|long|+|short|)/equity (0.75×). Both ALREADY include crypto (equity longs $36,399 + crypto $8,845 = $45,244, verified), but nothing on the page says so, and the pooled split is invisible.
- Risk & Sizing `→ target in <regime>` = λ × liquidity_param (1.00× in LOW_VOL). Under `OPENCLAW_CRYPTO_POOLED_SIZING=1` that budget is split: BTC sleeve floor `benchmark_min_nav_frac_crypto` (0.10 NAV) first, equity gets the rest (×0.90 today). The page shows only the total.
- De-gross threshold input: the 500 ms debounce PUTs whatever `parseFloat(input.value)` gives, including out-of-range intermediates (typing `0.5` passes through `0`), the server answers 400 and the page toasts "update failed" without the reason. The final value 0.5 did land (20:14:05Z).

## 2. Approaches considered
- **A (chosen): normalise broker symbols at the API layer, extend the two config/account endpoints, keep the frontend changes to labels.** One place for the symbol rule; the benchmark section needs no new code.
- B: normalise in the frontend `_groupByTicker`. Rejected: brackets and pending-exit sets are keyed server-side too; three places to patch.
- C: store broker symbols in `execution_signals`. Rejected: the engine convention is `BASE-USD` everywhere (crypto spec D-1); the dashboard adapts, not the ledger.

## 3. Design
1. `src/channels/api/broker_symbols.js` (new, pure): `engineTicker(symbol, assetClass)` → for `assetClass === 'crypto'` or a symbol containing `/`: strip `/` and `-`, upper-case, must end in `USD`, return `BASE-USD`; otherwise return the symbol unchanged (equities untouched). Used by `/api/portfolio/positions` for `brokerPositions`, `brokerBrackets`/`absorbLeg` and `pendingExitSyms`, and by `/api/portfolio/account`.
2. `/api/portfolio/account`: one extra `position list` call (fail-open): adds `crypto_market_value`, `equity_long_market_value` (= long − crypto), and `leverage.js realizedLeverage` gains `gross_equity`, `gross_crypto` (crypto MV / equity). Nulls when the list is unavailable.
3. `/api/config/regime-sizing`: adds `crypto: { pooled: <env flag === '1'>, floor_nav_frac: pipeline_config.benchmark_min_nav_frac_crypto (default 0.10, clamp 0–0.25), cap_nav_frac: benchmark_max_nav_frac_crypto (default 0.25) }`. New pure helper `pooledTargetSplit(eff, floorFrac)` → `{ total: eff, equity: max(eff − floorFrac, 0), crypto: min(floorFrac, eff) }` (the sizer's rule: floor is carved out of λ·liq·NAV before the equity normaliser).
4. Frontend (portfolio page):
   - Account row `Invested` sub-label: `equity $36,399 · crypto $8,845 · short $20,978 · λ actual 0.75× (eq 0.65× + crypto 0.10×)`; falls back to today's text when the split is null.
   - Risk & Sizing: when `crypto.pooled`, the `→ target in LOW VOL: 1.00×` readout gains `(equity 0.90× + crypto 0.10×)` and each regime card's `target leverage` line gains the same split; the live λ-slider preview updates the split too. `λ actual` is shown beside the target with its own split so the two are comparable.
   - Positions: no change — BTC-USD rows now arrive and the `BENCHMARK — BTC-USD` section renders above the treemaps (same stats as SPY: side, qty, value, % NAV, P&L, S_adj, alpha bars). Non-benchmark crypto (e.g. ADA-USD) renders as ordinary LONG tiles.
   - Numeric risk inputs: validate against min/max client-side before the PUT (out-of-range intermediates are ignored silently, no toast); on a non-2xx response the toast carries the server's `error` text.
5. Tests: node unit tests for `engineTicker`, `realizedLeverage` split, `pooledTargetSplit`; an endpoint test for `/api/portfolio/positions` with a stubbed `runAlpaca` returning `BTCUSD` + an open `BTC-USD` intent asserting the row survives with `benchmark: true` (pattern: `routes_crypto._setRunAlpacaForTests`).

## 4. Non-goals
No change to sizing, the sizer's symbol handling, or the Crypto page. No new crypto stats beyond what the SPY section shows.

## 5. Risks / open
- Alpaca `long_market_value` includes crypto today (verified); if that ever changes the equity/crypto split still comes from the position list, so `λ actual` stays correct.
- The 0.10 split is the floor; if BTC own conviction exceeds the floor the sizer uses the larger weight — the readout labels the crypto share as `≥`.
