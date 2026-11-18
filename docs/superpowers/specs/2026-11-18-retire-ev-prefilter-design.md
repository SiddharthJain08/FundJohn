# Retire the handoff EV / p_t1 prefilter and its quality gates — short design

**Date:** 2026-11-18 · **Status:** APPROVED by the operator 2026-11-18 21:15Z ("lets retire the EV and probability gate and executor kill switch using them, we may retire the EV and p_t1 as their information is not going to be that useful anyways").

## Why
`trade_handoff_builder.compute_features` scores every signal with a GBM two-barrier model whose geometry is long-only (shorts get sign-inverted risk/reward and swapped barriers) and whose drift divides a 12-month return by the holding period in days (~50× daily). The resulting EV/p_t1 "prefilter" is in practice a 12-month-momentum-sign test. It never gated sizing — `_sharpe_cadence_path` loads the open signal window from `execution_signals` and ignores the handoff list — but it drove: the trade-reports greenlist split, `daily_signal_summary.{ev_pos, ev_neg, avg_ev, avg_p_t1, high_conv_count}`, the health digest's "trade step eligible" line, Mastermind research inputs, and two day-level kill switches (`alpaca_executor.check_signal_quality`, `pipeline_orchestrator.check_signal_quality`: block if avg EV < −0.5 % and zero green). Its original consumer, the TradeJohn LLM confirmer, was retired 2026-07-20. The OUE classifier reads `ev_gbm` as its expectation baseline, but the OUE multiplier left sizing on 2026-05-29 (audit only).

## Goals
- G1 The handoff admits every tradable signal: the only prefilter reason left is `untradable_at_alpaca`.
- G2 `ev_gbm`, `p_t1`, `exp_exit_days/date`, `MIN_EV_GBM`, `MIN_P_T1`, `HOLDING_PERIOD`/`DEFAULT_HP` are removed from the builder; the remaining geometry fields (`risk_pct`, `t1_pct`, `t2_pct`, `rr1`) become direction-aware (positive magnitudes for shorts too).
- G3 Both `check_signal_quality` gates and `SIGNAL_QUALITY_THRESHOLDS` (and the memo fallback) are removed; the trade and alpaca steps no longer consult `daily_signal_summary`.
- G4 `daily_signal_summary` keeps receiving one row per handoff (append-only table; schema untouched): `n_signals`, `regime`, metadata `{green, prefiltered, d1_matches, ev_gate: 'retired 2026-11-18'}`; `ev_pos`/`ev_neg`/`high_conv_count` = 0, `avg_ev`/`avg_p_t1` = NULL.
- G5 The health digest and the trade-reports cards stop printing EV/green/p_t1; the digest keeps the zero-signal alarm and the signal-count delta.
- G6 OUE classification keeps working with expectation baseline 0 when `ev_gbm` is absent (same as `backtest_panel`), so `hv21` must stay in the handoff.
- G7 The sized-handoff order payload drops the placeholder `ev`/`p_t1` keys (sizer + live wrapper); nothing downstream reads them.

## Non-goals
No change to sizing, to `execution_signals`, to `veto_log`'s schema, to the premarket news gate (`gate_verdict`), or to the retired `trade_agent.py` (left as is).

## Anti-goals
A trade or alpaca step that can still be blocked by the summary row; a shorts row with negative `risk_pct`; a handoff without `hv21` (breaks OUE); a `daily_signal_summary` INSERT that violates NOT NULL (`ev_pos`, `ev_neg`, `high_conv_count`).

## Approach
Single approach: delete, don't flag. The gate has no live consumer worth a kill switch, and a flag would keep the misleading numbers alive. Rollout: merge to main; the next handoff (15:00 ET) writes the new summary row; the executor change is live at the next alpaca step; the health digest (Node, run by the `health` step / maintenance) picks up the file on its next run; johnbot needs no restart for Python steps.
