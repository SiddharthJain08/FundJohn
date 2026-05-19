# Regime change triggers pipeline redeploy, not liquidation

Plan for shifting OpenClaw's regime-transition behavior from full-portfolio
liquidation to delta-based pipeline redeploy, plus operationalizing the
fixes surfaced in the 2026-05-18 OPG-expiry incident.

## Outcome

- Confirmed intraday regime transitions trigger a sizer→executor redeploy
  that rebalances to the new regime via deltas. No more blow-out + reopen.
- Daily 9 AM regime detector remains as a read-only updater (writes
  `regime_latest.json` + `market_regime` row) — it no longer auto-fires
  the liquidator. To revisit retirement after intraday HMM is calibrated.
- Total-portfolio liquidation can only be initiated by the user via the
  existing `scripts/run_forced_liquidation.sh` (now reliability-patched).
- After-hours regime transitions still trigger redeploy: signals + sizing
  run to completion; the executor uses extended-hours limit orders for
  eligible symbols and defers the rest to the next morning cycle.
- Lessons from 2026-05-18 (OPG paper-fill failure, audit-status overload)
  are baked into the manual flatten path so the same incident can't recur.

## Locked design decisions (from 2026-05-19 brainstorm)

1. **After-hours policy**: redeploy still runs ext-hours; alpaca_executor
   submits LIMIT orders with `--extended-hours` during 4:00–9:30 ET and
   16:00–20:00 ET; non-eligible symbols skip. Fully-closed overnight
   (20:00–04:00 ET): refuse submission, log, surface in next morning's
   reconcile.
2. **Daily HMM**: stays as read-only updater, no liquidator hook.
   Decision deferred — 30-day comparative backtest vs intraday HMM after
   intraday-HMM bootstrap completes (~6 trading days from 2026-05-19).
3. **Manual flatten fix**: included in this work. `regime_liquidator.py`
   refuses pre-open submission (no more OPG path) and polls each order
   to terminal state for accurate `result_status`.

## Verified facts (2026-05-19 inspection)

- `regime_blended_sizer.py:421-432` already produces deltas
  (`delta = target - broker_current`); orphan tickers get `delta=−current`.
  No change needed to make redeploy delta-based.
- `pipeline_orchestrator.py` STEPS at lines 59-65 are the production
  cycle: `collect → signals → handoff → trade → alpaca → trade_parity_capture → correlation_sidecar → reconcile → report → pyportfolioopt_shadow → health`.
- `alpaca_executor.py:459`: `tif = 'day' if in_market_hours() else 'opg'`.
  Outside RTH it uses OPG — same paper-fill failure as the May-18 incident.
- Alpaca CLI supports `--extended-hours` on `order submit`. Extended-hours
  orders must be limit + day; brackets aren't supported.
- Intraday HMM cron is `*/5 9-16 * * 1-5` (cron-schedule.js:325). To trigger
  redeploys in the after-hours session we extend to `*/5 9-19 * * 1-5`.
- Intraday HMM is in bootstrap (474 RTH rows < 500 threshold). Sunday
  refit cron at line 353 will train it once threshold is met (1-2 more
  trading days).

## Phases (subagent-driven; one task per subagent, two-stage review)

### Phase 0 — Pipeline subset entry point (Task 59)

Add `--steps` and `--reason` flags to `pipeline_orchestrator.py`.
`--steps signals,handoff,trade,alpaca,reconcile` runs that subset only;
`--reason` propagates to log lines and Discord posts. Default = full cycle.
Skip the doctor pre-flight when `--steps` excludes `collect` (data already
freshly collected this morning). Lock-naming: `pipeline:lock:{date}` stays
shared so a redeploy can't collide with the 10 AM cycle in progress.

Tests: `pipeline_orchestrator.py --steps signals,handoff --date 2026-05-19 --dry-run` runs 2 steps only.

### Phase 1 — Strip auto-liquidation hooks (Task 60)

- `cron-schedule.js:293`: delete the `runPython('src/execution/regime_liquidator.py')` block. Daily HMM block still runs `run_market_state.py`.
- `run_intraday_market_state.py:310-340`: remove the `liquidate_on_regime_change(...)` call. Replace with a TODO marker pointing at the Phase 2 redeploy trigger (Phase 2 will fill it in).
- Update log lines / Discord messages: 'Regime liquidator complete' → 'Daily regime refresh complete' (already says this).
- `regime_liquidator.py` keeps the public `liquidate_on_regime_change()` function for the manual `--force` path; only the auto callers are removed.

Tests: cron-schedule loads without referencing regime_liquidator; intraday HMM detector still ticks cleanly with model_loaded=false (no regression).

### Phase 2 — Intraday-triggered pipeline redeploy (Task 61)

`scripts/redeploy_pipeline.py`:
- Thin wrapper. Args: `--reason` (required), `--date` (default today).
- Builds Redis cooldown + sentinel key based on `--reason`:
  - `redeploy:cooldown:{date}` TTL 3600s (60 min): blocks back-to-back redeploys
  - `redeploy:fired:{date}:{reason}` TTL 86400s (24h): same-transition idempotency
- If either key is present, exits cleanly with action='blocked'.
- Spawns `pipeline_orchestrator.py --steps signals,handoff,trade,alpaca,reconcile --reason <reason> --date <date>` synchronously.
- On success: sets both Redis keys, posts to `#intraday-regime` Discord with step-by-step status.
- Exit code mirrors the underlying orchestrator (0 OK, 1 partial, 2 fatal).

`run_intraday_market_state.py`: on confirmed transition + cooldown-clear + `OPENCLAW_INTRADAY_HMM_LIVE=1`, spawn redeploy_pipeline.py detached (matching the existing intraday spawn pattern). Audit row `transition_tag` becomes `INTRADAY_HMM_REDEPLOY_<from>_<to>` to distinguish from the old `INTRADAY_HMM_<from>_<to>` liquidation tags.

`cron-schedule.js`: extend the intraday cron from `*/5 9-16 * * 1-5` to `*/5 9-19 * * 1-5` so after-hours redeploys can fire. Confidence/hysteresis gates already filter noise; extra coverage of 16-20 ET is the new behavior.

Tests: `scripts/redeploy_pipeline.py --reason TEST_FOO --date 2026-05-19 --dry-run` runs the full 5-step subset against a DRY-RUN orchestrator; verifies Redis cooldown set on success.

### Phase 3 — Extended-hours execution (Task 62)

`alpaca_executor.py`:
- Add `_alpaca_session_kind()` returning one of `rth | premarket | afterhours | closed`, derived from `alpaca clock` payload.
- Replace `tif = 'day' if in_market_hours() else 'opg'` with:
  ```
  session = _alpaca_session_kind()
  if session == 'rth':
      tif, order_class, ext_hours, order_type = 'day', 'bracket', False, 'market'
  elif session in ('premarket', 'afterhours'):
      tif, order_class, ext_hours, order_type = 'day', 'simple', True, 'limit'
      limit_price = _pick_limit_price(ticker, side)
  else:  # closed
      log(f'  ✗ {ticker}: market fully closed (no submit)')
      continue
  ```
- New `_pick_limit_price(ticker, side)`: fetch latest quote via `alpaca data stock quotes latest --symbol <X>`; use mid for buys, mid for sells, fall back to last trade ±0.5%. Use a conservative offset so the order is marketable in the thin ext-hours book.
- `_submit_order_via_cli`: pass `--extended-hours` when `ext_hours=True`, pass `--type {market|limit}`, pass `--limit-price <p>` when type=limit.
- Skip-not-eligible logic: filter to `asset_class == 'us_equity'` AND `tradable == true`. Symbols whose asset_class is `crypto`/`option`/etc. get skipped with logged reason. Track skipped symbols in the run summary so the next morning cycle can see them.

`pipeline_orchestrator.py`: no change — alpaca step still runs; the session detection happens inside the executor.

Tests:
- Mock `alpaca clock` returning is_open=False + extended_hours=True → executor uses limit+extended_hours path.
- Mock fully-closed clock → executor logs and submits zero orders.
- Mock RTH → unchanged from today's behavior.

### Phase 4 — Manual-flatten reliability (Task 63)

`regime_liquidator.py`:
- `_close_symbol`: drop the OPG branch entirely. If not RTH at submission time, log clear error and skip the symbol. Bubble up "X symbols skipped — rerun during RTH" to the run-summary Discord post.
- After each successful submit, poll `alpaca order get --order-id <id>` until terminal state (`filled | partial | canceled | expired | rejected`) with 90s timeout. The actual fill outcome — not the submission ack — becomes `result_status`:
  - `filled` (all qty filled)
  - `partial` (some qty filled, then terminal)
  - `pending` (still open after 90s budget)
  - `rejected` (terminal but not filled)
  - `submit_error` (CLI returned non-zero)
- Sentinel guard from May 9 (`fire_succeeded = n_close_ok > 0 or len(close_results) == 0`) keeps its current semantics, but `n_close_ok` now counts terminal-fills, not submission-acks.
- `scripts/run_forced_liquidation.sh` unchanged — still exec's `regime_liquidator.py --force`. Behavior improves transparently.

Tests:
- Mock pre-market clock → liquidator refuses, audit rows all `result_status='rejected'` reason='not_rth'.
- Mock RTH + filled response → audit row `result_status='filled'`.
- Mock RTH + expired response → audit row `result_status='rejected'`.

### Phase 5 — Tests, docs, memory (Task 64)

- New tests as listed above.
- `tests/test_pipeline_orchestrator_steps.py`: --steps arg coverage.
- `tests/test_redeploy_pipeline.py`: redeploy wrapper coverage.
- `tests/test_alpaca_executor_ext_hours.py`: session detection + extended-hours path.
- `tests/test_regime_liquidator.py`: extended for RTH-only guard + poll audit.
- Regression: existing 10 AM full-cycle harness must still pass unchanged.
- `CLAUDE.md`: update Recent Changes section and System Overview's regime-handling paragraphs.
- Memory: amend `feedback_opg_paper_unreliable` to note the OPG path is removed; amend `feedback_liquidator_audit_status_overloaded` to note the poll-audit pattern is now standard.

## Critical files

| File | Action | Phase |
|---|---|---|
| `src/execution/pipeline_orchestrator.py` | modify (add --steps, --reason) | 0 |
| `src/engine/cron-schedule.js` | modify (strip liquidator call, extend intraday cron) | 1, 2 |
| `scripts/run_intraday_market_state.py` | modify (replace liquidator call with redeploy spawn) | 1, 2 |
| `scripts/redeploy_pipeline.py` | new | 2 |
| `src/execution/alpaca_executor.py` | modify (session-aware TIF/type/ext-hours) | 3 |
| `src/execution/regime_liquidator.py` | modify (RTH-only + poll audit) | 4 |
| `tests/test_pipeline_orchestrator_steps.py` | new | 5 |
| `tests/test_redeploy_pipeline.py` | new | 5 |
| `tests/test_alpaca_executor_ext_hours.py` | new | 5 |
| `tests/test_regime_liquidator.py` | extend | 5 |
| `CLAUDE.md` | modify (Recent Changes + overview) | 5 |

## Risks + mitigations

- **Redeploy collides with 10 AM cycle**: pipeline_orchestrator's existing `pipeline:lock:{date}` Redis lock prevents concurrent runs. A 10:05 ET intraday redeploy will wait/skip if 10 AM is still active.
- **Extended-hours limit prices stale**: 5-min-old quotes can be wide. Mitigated by `--time-in-force day` so unfilled limits expire at end of session; next morning cycle resubmits as market orders.
- **Redeploy cooldown blocks legitimate consecutive transitions**: 60 min is the same as today's liquidation cooldown; data showed no historic case of two transitions inside 60 min. If it bites, raise to 90 min.
- **Daily HMM no longer fires anything** but writes `market_regime` row: the 10 AM cycle still reads this row, so the regime context is preserved. No functional change to the morning cycle.
- **Manual flatten now refuses pre-open**: if an operator needs an emergency flatten before open, they have to wait until 9:30 ET or use the broker UI directly. Document this in the runbook.

## Rollout

1. Implement phases 0-4 on a feature branch off main.
2. CI green + smoke tests.
3. Merge with `OPENCLAW_INTRADAY_HMM_LIVE` still at 0 (DRY-RUN). Production behavior unchanged until flag flips.
4. Monitor intraday HMM detector for 2-3 trading days; verify the redeploy spawn appears in logs as DRY-RUN.
5. Once intraday HMM finishes bootstrap (~6 trading days) and the Sunday refit produces a real model, flip `OPENCLAW_INTRADAY_HMM_LIVE=1` to arm live redeploys.
