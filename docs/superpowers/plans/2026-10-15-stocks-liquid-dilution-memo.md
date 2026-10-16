# Memo — stocks_liquid "under-performance" in the parity report (2026-10-15)

Follow-up to the accepted universe-parity report (`universe-parity-report-20261013.md`, operator-accepted 10-13) and the
investigation scoped in `2026-10-13-session-handoff.md` §4. Question: the Phase-2 opt-in tier `stocks_liquid` was the one
group whose median ΔSharpe fell (−0.045, n=17, 7 positive), and three of its strategies turned non-positive with ~3× the
trades on a 4× smaller universe. Was that cross-sectional rank dilution, and does it indict the tier?

## Answer
**No tier change is warranted and no dilution occurred.** The pre-epoch "baseline" Sharpes of the three strategies were
measured on a small, gate-filtered residue of their signals (≈270 distinct names out of a 12.9k universe), because the
parity asset gate blocked roughly two-thirds of the unbounded universe from ever trading. The stocks_liquid runs are the
first measurement of these strategies on a panel where every pick is tradeable — they trade 2.5× more distinct names and
3× more round trips, and they lose money. Their own run history since the fund/junk rows were removed (late July) is
negative in most runs; the +0.07 … +0.19 "pre" values were the outliers, not the norm. All three are ineligible in
every regime before and after, so there is no live exposure. Keep them ineligible; the fix, if anyone wants one, is in
the strategies (the paper-faithful quintile/tercile construction has no edge net of spread costs at ~190 bps drag), not in
the tier.

## (a) Do the three use cross-sectional percentiles? Yes.
| strategy | selection rule (source) |
|---|---|
| `S_fama_french_anomaly_dissection` | `rank(pct=True)` on momentum, −accruals, −NSI → composite → top/bottom **quintile** (`:103–126`) |
| `S_value_momentum_everywhere` | composite → `nanpercentile(33/67)` → top/bottom **tercile** (`:111–113`) |
| `S_ptree_panel_tangency` | `rank(pct=True, na_option='keep')` per factor → top/bottom **quintile** (`:115–128`) |

A quantile rule selects a constant *fraction* of the panel, so a smaller panel selects *fewer* names per rebalance —
dilution would predict fewer trades, not three times more. The data went the other way, so (a) alone cannot be the story.

## (d) Was the pre-epoch baseline inflated by fund/ETF rows? Partly — but the gate residue is the bigger effect.
Trades joined to the FMP profile cache (`report_fund_exposure.ticker_security_type`), per run:

| strategy | run | universe | trades | distinct names | fund share | Sharpe |
|---|---|---|---|---|---|---|
| fama_french | 07-16 (pre asset gate / fund filters) | 12,492 | 64,934 | 825 | **17.7 %** | +1.61 |
| fama_french | 07-30 | 12,515 | 19,691 | 280 | 6.3 % | −1.08 |
| fama_french | 09-06 | 12,882 | 18,367 | 264 | 5.8 % | −0.86 |
| fama_french | **09-11 (report "pre")** | 12,884 | 18,506 | **266** | 6.2 % | **+0.07** |
| fama_french | **10-08 (report "post", stocks_liquid)** | 3,374 | 53,007 | **665** | 0.0 % | **−0.94** |
| value_momentum | 07-17 | 12,492 | 66,301 | 809 | 17.6 % | +1.87 |
| value_momentum | 08-01 | 12,517 | 20,001 | 286 | 4.1 % | −0.78 |
| value_momentum | 09-10 | 12,883 | 19,311 | 279 | 4.6 % | −0.57 |
| value_momentum | **09-15 ("pre")** | 12,885 | 19,283 | **274** | 6.4 % | **+0.11** |
| value_momentum | **10-10 ("post")** | 3,375 | 53,804 | **652** | 0.0 % | **−0.74** |
| ptree_panel | 07-23 | 12,510 | 119,530 | 1,379 | 15.0 % | +1.00 |
| ptree_panel | 07-31 | 12,515 | 45,276 | 594 | 4.9 % | −1.18 |
| ptree_panel | 09-08 | 12,882 | 42,549 | 577 | 3.7 % | −0.92 |
| ptree_panel | **09-13 ("pre")** | 12,884 | 43,217 | **582** | 4.9 % | **+0.19** |
| ptree_panel | **10-10 ("post")** | 3,081 | 91,541 | **930** | 0.0 % | **−0.58** |

Two regimes are visible. Before late July (fund share 15–18 %, SPAC/warrant/secondary rows present) all three were
strongly positive on 800–1,400 names — the ETF/junk artefact Phase 2 was built to remove. From 07-30 on, the unbounded
runs trade only 260–600 names and are negative in 3 of 4 runs; the single positive run per strategy is the one the
report happened to use as "pre".

## Why the unbounded runs traded so few names — the asset gate
The fleet log for the 09-15 value_momentum run (`logs/fleet_overnight_resume.log:1145`) records the parity asset gate on
the unbounded panel: **14,963 symbols mapped; 10,242 long-blocked, 10,243 short-blocked.** (first_candle's unbounded
runs show the same ratio: 15,181 mapped / 10,434 blocked.) With a quintile rule over 12.9k names, most of the extreme
composite ranks land on exactly the illiquid/untyped/secondary names the gate blocks, so the executed trades were a thin
residue of liquid survivors — and their Sharpe is not the strategy's Sharpe. Bounding to stocks_liquid (3.4k common
stocks that pass the gate) lets every pick trade: distinct names 266 → 665, trades 18.5k → 53k, cost drag 187 bps on
fama_french, Sharpe −0.94.

## (b) Ad-hoc comparison run (tier_liquid vs stocks_liquid) — the tier makes no difference
Non-committing probe (`bt_probe_nocommit.py`: `run_backtest(..., conn, commit=False)` inside a transaction that is rolled
back; canonical primary `26c11ecf` verified untouched), transient unit `bt-probe-vme-tierliquid`, MemoryMax 4500M,
2026-10-15 22:44–23:06Z, 22.2 min:

| `S_value_momentum_everywhere` | universe | trades | names | Sharpe | LOW_VOL | TRANS | HIGH_VOL | CRISIS | cost drag |
|---|---|---|---|---|---|---|---|---|---|
| unbounded ("pre", 09-15) | 12,885 | 19,283 | 274 | +0.11 | +0.07 | −0.55 | +0.47 | −0.12 | — |
| **tier_liquid (probe)** | 4,556 | 51,729 | 637 | **−0.71** | −1.41 | −1.19 | −0.08 | −1.52 | 187 bps |
| stocks_liquid (canonical, 10-10) | 3,375 | 53,804 | 652 | **−0.74** | −1.45 | −1.15 | +0.03 | −0.25 | 187 bps |

tier_liquid adds the ETFs/ADRs/secondaries back and reproduces the stocks_liquid result to within 0.03 Sharpe and 4 %
of trades. The probe's own gate line on the unbounded map was again "15,202 symbols mapped; 10,529 long-blocked" and
"skipped 20,170 entries on execution-ineligible symbols". So the gap is entirely bounded-vs-unbounded (gate residue),
not stocks-only-vs-mixed. Phase 2's tier choice is exonerated.

## Decision
- Keep `stocks_liquid` for the 21 Phase-2 strategies. The tier is not the cause; it exposed a measurement artefact.
- The three strategies stay ineligible (they already are, in every regime). Any revival is a strategy-level change
  (absolute signal thresholds, liquidity-aware ranking, or a wider rebalance cadence to cut the 190 bps drag) and goes
  through the normal candidate path.
- Lesson for future parity reports: compare *distinct names traded* and the asset-gate blocked count alongside Sharpe;
  a Sharpe on a gate-filtered residue is not comparable to a Sharpe on a bounded panel.
