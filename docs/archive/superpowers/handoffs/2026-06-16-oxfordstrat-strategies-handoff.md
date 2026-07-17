# Oxfordstrat Strategies → Research Candidates — Operator Handoff

**Date:** 2026-06-16
**Branch:** `feat/oxfordstrat-strategies` (worktree `/root/.config/superpowers/worktrees/oxfordstrat-strategies`, off live HEAD `acc4a94`)
**Status:** COMPLETE on the branch. Candidates only — nothing promoted, `_IMPL_MAP` untouched. **Appearing on the live dashboard requires the operator merge + johnbot restart below.**

## What was built

- **30 curated oxfordstrat.com strategies** (20 faithful trend/oscillator/structure rules + 10 stop-entry "confirmed-breakout daily-bar adaptations"), each a thin `BaseStrategy` subclass over a shared helper.
- `src/strategies/oxford_crabel.py` — `OxfordBaseStrategy` (lazily self-loads + caches liquid-ETF-basket OHLC from the master parquet; iterates the basket, never the `universe` arg) + ~25 Crabel/DeMark indicators (formulas fetched per-strategy from the Oxford pages).
- **Liquid-ETF basket** (40 tickers verified in `prices.parquet`): SPY/QQQ/IWM/DIA/EFA/EEM/VTI, TLT/IEF/SHY/LQD/HYG/AGG, GLD/SLV/USO/UNG/GDX, XLE–XLY sectors (full 10y) + DBC/DBA/DBB/CPER/PALL/PPLT/CORN/WEAT/SOYB/MDY/UUP/UDN/FXF (~5y).
- **Migration 135** (applied to the live DB; additive) — persists `total_sortino`/`total_calmar`/`total_avg_pnl_pct` on `strategy_backtest_runs` and `sortino`/`calmar` on `strategy_backtest_regimes`. These were already *computed* by `aggregate_metrics`/`aggregate_per_regime` but dropped at the INSERT — **this fix benefits every strategy on the page, not just these 30.** Page read wired through `server.js` + `strategy_row.js`; candidate list sorted by Sharpe.
- **Registration:** all 30 in `manifest.json` as `state=candidate`/`instrument_class=etp` + 30 `strategy_registry` rows `status=pending_approval`.
- **Tests:** 121 passed / 1 skipped (generic contract auto-covers all 30 incl. a `does_not_depend_on_universe_arg` guard; per-indicator golden tests; Phase-0 persistence test; t+1 backtest regression).

## Backtest results (all 30, full 10y, house brackets, sorted by Sharpe)

| Strategy | Sharpe | Sortino | Calmar | Ret% | MaxDD% | Trades |
|---|--:|--:|--:|--:|--:|--:|
| oxf_rsi2_meanrev | 1.09 | 1.78 | 0.63 | 234 | 20.1 | 1857 |
| oxf_vortex | 0.96 | 2.21 | 0.96 | 301 | 11.8 | 2757 |
| oxf_smash_day_b | 0.74 | 1.97 | 0.59 | 220 | 14.3 | 7330 |
| oxf_sma_filter | 0.58 | 2.27 | 0.40 | 141 | 17.4 | 54385 |
| oxf_false_breakout | 0.46 | 1.42 | 0.28 | 186 | 29.2 | 4039 |
| oxf_td_sequential | 0.30 | 1.26 | 0.33 | 140 | 21.5 | 2346 |
| oxf_dual_momentum_roc | 0.19 | 1.80 | 0.38 | 120 | 15.0 | 55350 |
| oxf_price_momentum | 0.18 | 1.77 | 0.39 | 118 | 14.8 | 55350 |
| oxf_nr7 | 0.05 | 1.35 | 0.44 | 110 | 11.9 | 11302 |
| oxf_zero_lag_ma | 0.03 | 1.61 | 0.32 | 97 | 16.0 | 48279 |
| oxf_heikin_ashi | 0.01 | 2.01 | 0.88 | 107 | 5.8 | 63125 |
| oxf_frama | −0.12 | 1.42 | 0.31 | 89 | 14.5 | 36523 |
| oxf_linreg_slope | −0.12 | 1.46 | 0.20 | 90 | 22.8 | 53398 |
| oxf_macd_zero | −0.20 | 1.25 | 0.31 | 83 | 13.6 | 62875 |
| oxf_ross_hook | −0.24 | 0.55 | 0.11 | 50 | 27.4 | 4382 |
| oxf_bull_oops | −0.33 | 1.18 | 0.45 | 73 | 8.5 | 21342 |
| oxf_keltner | −0.35 | 1.09 | 0.30 | 70 | 12.6 | 42281 |
| oxf_greatest_swing_value | −0.39 | 1.30 | 0.30 | 72 | 12.8 | 32887 |
| oxf_gap_a | −0.41 | 0.54 | 0.11 | 46 | 25.1 | 6255 |
| oxf_orbp_momentum | −0.42 | 1.15 | 0.30 | 68 | 12.2 | 43618 |
| oxf_hook | −0.48 | 0.25 | 0.06 | 15 | 26.2 | 890 |
| oxf_adaptive_ma | −0.48 | 1.07 | 0.26 | 60 | 12.9 | 61856 |
| oxf_bollinger_momentum | −0.59 | 0.18 | 0.03 | 14 | 34.9 | 11639 |
| oxf_livermore | −0.62 | 0.57 | 0.11 | 40 | 22.3 | 39144 |
| oxf_dow_theory | −0.63 | 0.68 | 0.15 | 43 | 16.2 | 36419 |
| oxf_donchian_breakout | −0.71 | 0.07 | 0.01 | 4 | 30.5 | 9537 |
| oxf_wyckoff_meanrev | −0.76 | 0.70 | 0.12 | 40 | 19.0 | 30226 |
| oxf_aroon_breakout | −0.82 | 0.73 | 0.13 | 37 | 16.3 | 54435 |
| oxf_hull_ma | −0.86 | 0.39 | 0.04 | 18 | 33.9 | 48194 |
| oxf_welles_wilder_breakout | −0.98 | 0.27 | 0.06 | 15 | 16.9 | 31711 |

**Read:** 6 clear by Sharpe (rsi2_meanrev, vortex, smash_day_b, sma_filter, false_breakout, td_sequential); mean-reversion/momentum lead, always-on trend/breakout rules lag. 0 NULL/NaN. Sortino is positive for nearly all (positive total return, high vol).

## Operator activation (to make them appear on the LIVE dashboard)

The live johnbot reads `/root/openclaw`'s manifest + server.js, NOT the worktree. The backtest metrics + registry rows are already in the shared DB. To surface the 30 candidates on the live research page:

1. Review the branch: `git -C /root/.config/superpowers/worktrees/oxfordstrat-strategies log --oneline acc4a94..HEAD`
2. Merge `feat/oxfordstrat-strategies` into the running branch (or cherry-pick), `git pull` in `/root/openclaw`.
3. Restart johnbot (root user-systemd): `systemctl --user restart johnbot.service` (or the system unit per your setup). Verify `:3000` 200.
4. Confirm: `curl -s localhost:3000/api/strategies | grep -c oxf_` → 30; spot-check `backtest_sortino`/`backtest_calmar` populated.

Migration 135 is already applied to the shared DB (idempotent `ADD COLUMN IF NOT EXISTS`); no separate DB step. **Confirm `/root/openclaw` hasn't independently grown a different `135_*.sql` before merge** (re-apply is safe regardless).

## Caveats (read before promoting any)

- **House brackets, not Oxford brackets.** Brackets use the house `compute_stops_and_targets` (regime-scaled ATR(14)×2 stop + 5/10/20% targets) — the SAME risk management every other candidate uses, so metrics are comparable. Oxford designed many of these for ATR×6 stops, so the always-on trend/breakout rules are **stop-dominated (~73%)** and score negative here. That is the comparable house view, not a bug. Any promising rule could be re-tested with a wider per-strategy stop as a follow-up (would break cross-candidate comparability).
- **Stop-entry rules are daily-bar adaptations.** The 10 "adaptation" strategies model the intraday stop trigger as an end-of-signal-day OHLC confirmation (engine fills at close[t+1]); their docstrings say so. They are faithful in direction/timing/filtering, NOT tick-exact replicas.
- **Weekend refresh cost.** `unified_backtest --all-live` (weekend `refresh_backtests.sh`) re-backtests live + candidate + staging, so these 30 add ~30 full-history backtests/week on the 2-core/8GB box. Per-strategy chunking + nice mitigate, but the window lengthens. Gate them out of the refresh if undesired, or accept the cost. (One strategy, `oxf_hull_ma`, is O(bars²) — the slowest.)
- **One OOM during the build** (`bull_oops`, transient) — re-ran clean; all 30 now have runs.

## Follow-ups (optional)

- Per-strategy wider-stop variants for the promising trend rules.
- Add an intraday-stop fill model to the backtest engine so the breakout adaptations can be re-tested faithfully (out of scope here).
- The 4 partial Oxford strategies (Volume Filters ×3, Intermarket Pathfinder) need volume / open-interest / a 2nd-market — deferred.
