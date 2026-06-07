# SP-6 — Fill-Model Counterfactual Study: open[t+1] vs close[t+1] (PRE-REGISTERED)

Date: 2026-06-07. Status: **PRE-REGISTERED — committed before any open-fill
backtest has been run.** Companion to Phase-1f (drift atlas). Operator
question: should the system fill at/near the open instead of the 3:55 close
window? This study answers it at the level that matters — realized strategy
alpha under each fill model — with backtest/live parity preserved by
construction (if the fill model ever changes, backtest and live move
TOGETHER).

## 0. Scope + caveats (pre-stated)

- ENTRY fill timing only. Exits keep their current semantics (bracket levels
  + time-exit at close) except the open-fill variant's exit walk must include
  the fill bar itself (H/L occur after an open fill). Exit-timing variants
  are out of scope.
- Daily-bar granularity: an open fill captures day t+1's full intraday move —
  a positive Δ may be favorable exposure-window (beta) timing over the
  sample, not execution skill. Per-regime breakdown reported for this reason.
- Costs are NOT re-modeled (identical across variants ⇒ Δ is cost-neutral in
  the sim). Pre-stated: REAL open-window spreads are the widest of the day;
  any positive Δ must be read net of plausible open-cost drag before any
  decision (avg per-trade gross reported to scale this).
- COUNTERFACTUAL ONLY: `commit=False` everywhere; zero writes to
  strategy_backtest_* tables, no primary-window demotion, no panel rebuild.

## 1. Implementation (frozen seams, from grounding)

- `unified_backtest.py`: thread `fill_model: str = "close"` through
  `run_backtest` → `_simulate_for` → `_per_bar_simulate`. Line ~591 branches
  the entry price column on fill_model. **Default 'close' must be
  byte-identical to current behavior — regression-tested.**
- Exit walk (~line 237): under fill_model='open' the walk starts AT the fill
  bar (`>= fill_date`) so bracket H/L on the fill bar are eligible
  (intra-bar stop/target ordering convention inherited unchanged); under
  'close' the walk stays `> fill_date` (unchanged).
- `entry_regime` stays stamped from the SIGNAL day (t) — untouched (per-
  regime aggregation invariant).
- `_reanchor_bracket` operates on the new fill price unchanged.
- Driver `scripts/backtest_fill_model_study.py`: for each manifest
  state='live' strategy — ONE SUBPROCESS per strategy (OOM lesson; RSS frees
  between) running BOTH variants paired at the same code version
  (`run_backtest(sid, fill_model=..., commit=False, return_metrics=True)`),
  appending one JSON line per strategy to
  `analysis/fill_model_study/results.jsonl` — **RESUMABLE: sids already
  present are skipped** (session-kill lesson). Sequential, nice -19,
  detached (systemd-run).

## 2. Captured per (strategy, fill_model)

sharpe, cagr, max_drawdown, n_trades, win_rate, avg_trade_gross_pct,
per-regime sharpe (the 4 canonical regimes, where available).

## 3. PRE-COMMITTED readout

- **Trades-parity gate**: |n_trades_open − n_trades_close| / n_trades_close
  ≤ 2% per strategy (signals identical; only fills differ). Strategies
  breaching it are flagged SIM-SUSPECT and excluded from the headline (a
  breach count > 5 invalidates the run — fix the sim, rerun).
- **Headline**: median ΔSharpe (open − close) across the live book; counts of
  ΔSharpe ≥ +0.10 vs ≤ −0.10 (house materiality threshold).
- **Consideration bar (pre-committed)**: a live fill-model change becomes a
  DISCUSSION only if median ΔSharpe ≥ +0.10 AND ≥60% of strategies positive
  AND trades-parity clean. Anything less ⇒ close-fill stands, question
  closed. Even if met: operator decision + forward shadow REQUIRED before
  any live change. Nothing auto-deploys.

## 4. Out of scope

Candidate strategies (live book only, ~54), exit-timing variants,
intermediate fill times (15:00 etc. — revisit only if the drift atlas shows
TIMING-STRUCTURE that survives its dev_sys guard), cost-model changes.
