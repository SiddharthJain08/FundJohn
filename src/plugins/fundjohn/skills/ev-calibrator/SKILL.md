# Skill: fundjohn:ev-calibrator
**Trigger**: `/ev-calibrator` or `/calibrate`

## Purpose
Keep TradeJohn's sizing honest by checking each strategy's recent
realized performance before applying Kelly. Every daily handoff carries
a pre-computed **ev_calibration** block per strategy over the trailing
30 days; this skill defines the rules TradeJohn must apply.

The block uses observables only — `hit_rate`, `realized_pnl_avg`, a
drift signal between the last 15 and prior 15 days. No new migration,
no reconstruction of predicted EV from implicit state.

## Input (already in the handoff)

`handoff.ev_calibration` is keyed by `strategy_id`:

```json
{
  "S_HV13_call_put_iv_spread": {
    "n_closed":         18,
    "hit_rate":         0.56,
    "realized_pnl_avg": 0.032,
    "realized_pnl_stdev": 0.041,
    "sharpe_proxy":     0.78,
    "recent_pnl_avg":   -0.012,
    "prior_pnl_avg":    0.051,
    "drift_score":     -0.063,
    "window_days":      30,
    "last_updated":     "2026-04-23"
  },
  "S9_dual_momentum": { ... }
}
```

- `hit_rate`        = fraction of closed trades with positive realized P&L
- `realized_pnl_avg`= mean realized P&L across closed trades (fraction, not %)
- `sharpe_proxy`    = `realized_pnl_avg / realized_pnl_stdev` over the window
- `recent_pnl_avg`  = mean realized P&L over the most recent 15 days
- `prior_pnl_avg`   = mean realized P&L over days 15–30 before today
- `drift_score`     = `recent_pnl_avg - prior_pnl_avg` (negative = worsening)
- `n_closed`        = sample size; treat `< 8` as "insufficient data, no adjustment"

## Adjustment Rules (apply in order, top wins)

1. **Insufficient data** — `n_closed < 8`:
   - Use raw `ev_gbm` from the signal as-is.
   - `size_explanation` suffix: `" (uncalibrated, n<8)"`.

2. **Hard kill** — `realized_pnl_avg < 0` AND `hit_rate < 0.40` AND `n_closed ≥ 10`:
   - Auto-veto every signal from this strategy this cycle.
   - Veto reason: `"calibration_kill"` (logged to `veto_log`).
   - Memo header: `⚠️ {strategy_id}: negative 30d P&L ({pnl_avg}) + hit rate {pct}% — all signals vetoed pending operator review.`

3. **Severe drift** — `drift_score < -0.05` OR `recent_pnl_avg < -0.03`:
   - Halve effective NAV: `pct_nav_out = pct_nav_sized * 0.5`.
   - `size_explanation` suffix: `" (cal: drift -X.X%, halved)"`.

4. **Mild drift** — `-0.05 ≤ drift_score < -0.02`:
   - Scale NAV to 75%: `pct_nav_out = pct_nav_sized * 0.75`.
   - `size_explanation` suffix: `" (cal: drift -X.X%, -25%)"`.

5. **Neutral** — `|drift_score| ≤ 0.02`:
   - No change. `size_explanation` suffix: `" (cal: neutral)"`.

6. **Improving** — `drift_score > 0.02`:
   - Leave sized NAV alone but flag in memo:
     `⚡ {strategy_id}: realized P&L trending +X.X% over last 15d`
   - No sizing bonus. We never size up from calibration.

## Position-sizer coupling

Apply these adjustments **after** `fundjohn:position-sizer` computes the
final sized NAV. Calibration is a governor on top of Kelly, not a
replacement for it. Apply the hard per-signal cap (`MAX_POSITION_PCT =
0.05`) again after the calibration scale.

## Memo header section

When at least one adjustment fired, include one line each:

```
Calibration actions:
  • S_HV13 (n=18, hit=56%): drift -6.3% → 2 signals halved
  • S9     (n=25, hit=52%): neutral
```

Omit this section if no adjustments fired.

## Hard Rules

- Never UP-adjust sized NAV from calibration — only DOWN-adjust, or
  pass through, or hard-kill.
- Never override a regime or SO-4 (negative-EV) veto with calibration.
- If `ev_calibration` block is missing entirely for a strategy, size as
  if `n_closed = 0` (rule 1).
- Calibration window is fixed at 30 days; do not recompute.
