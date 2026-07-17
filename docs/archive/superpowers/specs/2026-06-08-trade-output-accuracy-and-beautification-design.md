# Trade-output accuracy & beautification — design

**Date:** 2026-06-08
**Status:** approved (operator picked all recommended approaches 2026-06-08)
**Author:** BotJohn

Four operator-requested changes to trade reporting/output. All read-only-to-build
on a branch; all need a johnbot restart to go live (the sizer/reconcile parts
land on the next EOD compute). Sequence simplest-first: ① + ④, then ② + ③.

---

## ① Greenlist → boxed cards

**Where:** `src/execution/send_report.py::_fmt_greenlist(run_date, sized)`.

**Now:** a code-block table; `strategy_id` is column 2; one row per order.

**Change:** rebuild as one boxed card per order, with the contributing
strategies LAST and listed VERTICALLY:

```
🟢 Greenlist — 2026-06-08
LOW_VOL · 15 orders · gross 0.6× NAV
━━━━━━━━━━━━━━━━━━━━━━━━━━━
 AMZN   ▲ LONG     $185.20
 size 4.2%  EV +1.80%  p(T1) 62%
 contributing strategies (3):
   • S_momentum_breakout
   • S_news_sentiment_ls
   • S_pcr_momentum
━━━━━━━━━━━━━━━━━━━━━━━━━━━
 ...
```

- Header banner: regime · order count · gross leverage (Σ|pct_nav|).
- Per card: ticker, ▲LONG/▼SHORT arrow, entry; metrics line (size% NAV, EV%,
  p(T1)); then `contributing strategies (N):` + bullet list from the order's
  existing `contributing_strategies` (fallback to `[strategy_id]`).
- Pure formatting. No data change. TDD the formatter against sample orders
  (multi-strategy, single-strategy, missing-field, empty-orders).

## ④ Circuit-breaker → its own channel

**Where:** `src/execution/position_circuit_breaker.py` (posts via
`_post_to_discord('trade-reports', …)`); webhook registry in
`agent_registry.webhook_urls`; channel webhooks auto-created by
`src/channels/discord/agent-personas.js::initWebhooks()`.

**Change:**
- Create `#circuit-breaker` Discord channel + webhook (operator authorized
  self-creation); register the URL in `agent_registry.webhook_urls` under key
  `circuit-breaker` for the persona the breaker posts as.
- Add `circuit-breaker` to that agent's `channelKeys` + the channel map so the
  webhook is (re)created idempotently on every startup.
- Repoint the breaker's `_post_to_discord(...)` channel arg `'trade-reports'`
  → `'circuit-breaker'`.

## ② Closed positions → source fix (single source of truth)

**Root cause:** `engine.update_pnl` (engine.py ~1319-1355) writes `signal_pnl`
closed rows ONLY when price crosses stop/target. Broker-side closes —
liquidations (682/30d), circuit-breaker (22), regime-redeploy flattens, orphan
closes — close the broker position but leave `signal_pnl` `status='open'`, so
both #trade-reports (`_load_closed_positions`) and the dashboard
(`signal_pnl`-derived closed stats / win-rate) silently undercount, and stale
signals phantom-mark.

**Fix:** a new reconcile pass in the EOD pipeline (after the broker reconcile;
in/adjacent to `src/execution/parity_mark.py`). For every signal still
`status='open'` whose ticker is **flat at the broker**:
- Close it in `signal_pnl` (status='closed') reusing the existing
  `src/execution/open_reconcile.py` upsert helpers
  (`drop_signal_close`/`flatten_signal_close`) — same upsert shape as
  `engine.update_pnl`, also flips `execution_signals.status='closed'` to stop
  phantom re-marking.
- `close_reason` derivation (priority): `circuit_breaker_fires` for ticker+date
  → `circuit_breaker`; else `alpaca_liquidations` → `liquidation`; else a
  cycle close order in `alpaca_submissions` → `manual_close`; fallback
  `manual_close`.
- `realized_pnl_pct` from the actual close-fill price (broker fill /
  `alpaca_submissions`) vs the signal's entry, signed by direction. If no fill
  price is recoverable, fall back to the last mark.
- Idempotent (ON CONFLICT (signal_id, pnl_date)); never double-closes a row
  already closed by stop/target.

**Net:** report + dashboard read `signal_pnl` unchanged → both auto-include all
closes; the #trade-reports `close_reason` buckets gain
liquidation/breaker/manual rows. TDD the pass (reason derivation, PnL sign,
flat-detection, idempotency) with a fake cursor + broker stub.

## ③ Per-strategy alpha → only corr-gate contributors

**Now:** `server.js` `/api/portfolio/ticker-alpha/:ticker` decomposes alpha
across EVERY strategy that ever traded the ticker (up to ~30).

**Want:** only the strategies the sizer's correlation gate counts as
contributing to the gate-passing cum_sharpe.

**Fix:**
- Migration **130**: `cycle_contributing_strategies(run_date date, ticker text,
  strategies text[], updated_at timestamptz, PRIMARY KEY(run_date, ticker))`.
- `src/execution/regime_blended_sizer_live.py` persists, per cycle, the
  per-ticker `contributing_strategies` the sizer already computes (corr-gate
  passers, deflated cum_sharpe ≥ floor). Append-only-friendly upsert.
- `ticker-alpha` endpoint: filter the per-strategy breakdown to the LATEST
  cycle's contributing set for the ticker (fall back to current all-strategy
  behavior when no row exists yet, so it degrades gracefully pre-accrual).
- TDD: migration applies; persist writes the set; endpoint filters to it.

---

## Testing & deployment

- TDD each unit (formatter, reconcile pass, persist, endpoint filter).
- Build on branch `feat/trade-output-accuracy`; hand operator an atomic merge.
- Restart johnbot to activate (send_report, server.js, position_circuit_breaker
  are loaded by the long-running process); ② + ③ run inside the EOD pipeline.
- ④ webhook creation is a one-time live action (creates the channel + registers
  the URL); safe + idempotent.
