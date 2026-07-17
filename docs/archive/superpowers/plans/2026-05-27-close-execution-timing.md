# Close-Execution (close[t]-proxy) + Action-Label — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (or executing-plans). Steps use `- [ ]` checkboxes.

**Goal:** Generate daily signals from a ~3 PM `close[t]`-proxy snapshot and execute market orders into the 4 PM close, mirroring the backtests' decide-and-fill-at-`close[t]`. Add a dashboard-only SOD refresh, formalize the collect-free redeploy, and add a human-readable `action` label. **No** min-signal-count guard (legitimate empty → liquidation is correct); a *failed* step must abort, not emit empty.

**Architecture:** Split the LangGraph daily cycle into a ~3:10 PM **compute** phase (`collect…trade`, signals run against an injected `close[t]` proxy row) and a ~3:55 PM **execute** phase (`alpaca…health`, market-into-close). Regime = intraday HMM (fresh mid-RTH, already wired). Master parquet stays append-only; proxy is transient in-memory.

**Tech stack:** Python (engine, sizer, ingestion), Node (LangGraph cycle, cron, dashboard), Alpaca CLI, Postgres, Redis. Spec: `docs/superpowers/specs/2026-05-27-open-execution-timing-and-action-label-design.md`.

**Worktree setup:** symlink `data/master` into the worktree; `chown` worktree → `claudebot`; run Python with `OPENCLAW_DIR`=worktree. Prod `.env` is at `/root/openclaw/.env`.

---

### Task 1: `close[t]`-proxy snapshot util

**Files:** Create `src/ingestion/close_proxy_snapshot.py`; Test `tests/test_close_proxy_snapshot.py`.

- [ ] **Test first** — with `alpaca` subprocess monkeypatched to return a canned `multi-snapshots` JSON, `fetch_close_proxy(['AAPL','MSFT'], asof)` returns `{'AAPL': <latestTrade.p>, 'MSFT': ...}`; a ticker missing from the response is omitted (not raised); a **total fetch failure (non-zero rc / empty) raises `CloseProxyError`** (never returns `{}` silently).
- [ ] **Implement** — `fetch_close_proxy(universe: list[str], asof_date) -> dict[str,float]`:
  - Chunk `universe` into batches of 50; for each run `alpaca data multi-snapshots --symbols <csv>` (env auth; `ALPACA_CLI_BIN`); parse JSON; price = `latestTrade.p` (fallback `minuteBar.c`, then `dailyBar.c`).
  - Normalize broker symbols (`BRK.B`→`BRK-B`, `BTC/USD`→`BTC-USD`) to match parquet tickers.
  - Crypto tickers (`-USD`) → `alpaca data crypto latest-trades`.
  - If **all** chunks fail → `raise CloseProxyError`. Partial (some tickers missing) is OK.
- [ ] Run tests; commit.

### Task 2: Inject the proxy row in `engine.load_prices`

**Files:** Modify `src/execution/engine.py:195-213` (`load_prices`) + callsite `:1108`; Test `tests/test_engine_close_proxy_injection.py`.

- [ ] **Test first** — with a fixture `prices.parquet` (dates ≤ t−1) and a monkeypatched `fetch_close_proxy` returning `{ticker: price}`: with `OPENCLAW_CLOSE_PROXY_SNAPSHOT=1`, `load_prices(universe)` returns a wide panel whose **last index row = today (ET)** with values = the snapshot; with the flag **unset/0**, the panel is **byte-identical** to the no-injection path (regression). **`CloseProxyError` must propagate** — test with `pytest.raises(CloseProxyError)`, NOT an empty-DataFrame assertion.
- [ ] **CRITICAL — restructure to avoid the broad `except` swallowing `CloseProxyError`.** As-shipped (`engine.py:201-213`) the whole body is inside `try/except Exception: return pd.DataFrame()`. Injecting inside it would catch `CloseProxyError` → empty frame → liquidation (the forbidden empty-by-failure path). Fix:
  ```python
  master_path = ROOT / 'data' / 'master' / 'prices.parquet'
  if not master_path.exists():
      logger.warning(...); return pd.DataFrame()
  try:
      df = pd.read_parquet(master_path, columns=['ticker','date','close'])
      wide = df.pivot(index='date', columns='ticker', values='close')
      wide.index = pd.to_datetime(wide.index); wide.sort_index(inplace=True)
      cols = [c for c in universe if c in wide.columns]
      if cols: wide = wide[cols]
  except (OSError, ValueError, KeyError) as e:        # narrow: parquet read only
      logger.error(f"Failed to load prices: {e}"); return pd.DataFrame()
  # proxy injection OUTSIDE the try — CloseProxyError propagates and aborts the step
  if os.environ.get('OPENCLAW_CLOSE_PROXY_SNAPSHOT') == '1':
      from ingestion.close_proxy_snapshot import fetch_close_proxy
      today = pd.Timestamp.now(tz='America/New_York').normalize().tz_localize(None)  # ET, matches parquet convention
      proxy = fetch_close_proxy(list(wide.columns), today)
      if today not in wide.index:
          wide.loc[today] = pd.Series(proxy).reindex(wide.columns)
          wide.sort_index(inplace=True)
  logger.info(f"Prices loaded: {wide.shape[1]} tickers × {wide.shape[0]} dates")
  return wide
  ```
- [ ] Run tests; commit.

### Task 2b: Propagate the abort through `main()` + the graph node

**Files:** Audit/modify `src/execution/engine.py:main()` (`:1052`); smoke `test/daily-cycle-abort.js` (or pytest on engine exit code).

- [ ] **Audit** `main()` for a top-level `except Exception: ...; return`/`sys.exit(0)` that would mask `CloseProxyError`. Ensure a `CloseProxyError` (or any `load_prices` raise) exits **non-zero**.
- [ ] **Test** — engine run with a forced `CloseProxyError` exits non-zero; and a compute-phase graph run with the signals step failing **writes no handoff** (so the execute phase's freshness gate later skips). Verify the LangGraph chain halts on a node's non-zero exit (add a smoke if absent).
- [ ] Commit.

### Task 3: `action` label derivation in the sizer

**Files:** Modify `src/execution/regime_blended_sizer.py` (orders.append `:550`) + the `close_only` order in `src/execution/regime_blended_sizer_live.py:96-118`; Test `tests/test_sizer_action_label.py`.

- [ ] **Test first** — `_derive_action(kind, out_current, out_target, dir_sign)` returns each of the 8 spec rows: open_long/short, add_long/short, reduce_long/short, flip_to_long/short, close_long/short. And: the GLW case (`kind='delta'`, current>0, |target|<|current|) → `reduce_long`. Assert `direction` value unchanged.
- [ ] **Implement** — add `_derive_action(...)` helper (in `regime_blended_sizer.py`), add `'action': _derive_action(kind, out_current, out_target, dir_sign)` to the orders dict; mirror the same `action` onto the `order_oc` close-only dict in `regime_blended_sizer_live.py` (use its `o['current_usd']`/`target_usd`, kind=`orphan_close`/reduce).
- [ ] Run tests; commit.

### Task 4: Parameterize the cycle by a step subset

**Files:** Modify `src/agent/graphs/daily-cycle.js` (`runDailyCycleGraph` `:147`, graph build `:116-121`); Test `test/daily-cycle-subset.js`.

- [ ] **Test first** — `runDailyCycleGraph({runDate, reason, steps:['collect','signals']})` builds/executes a graph with only those nodes in order; omitting `steps` defaults to full `STEPS_IN_ORDER` (regression).
- [ ] **Implement** — accept `input.steps` (default `STEPS_IN_ORDER`); build nodes/edges over that subset; thread `reason` into the trace tag. Export unchanged.
- [ ] Run tests; commit.

### Task 5: Compute/execute crons — GATED so merge ≠ live-flip

**Files:** Modify `src/engine/cron-schedule.js:253` (+ SOD near `:294`).

- [ ] **Gate all new behavior behind `OPENCLAW_CLOSE_EXEC_LIVE` (default OFF).** Merging the code must NOT change live trading; the operator flips the gate after the §10 parity check.
  - `const closeExecLive = process.env.OPENCLAW_CLOSE_EXEC_LIVE === '1';`
  - **When OFF:** keep the existing `'0 10 * * 1-5'` cycle exactly as today (byte-identical) and register **no** SOD/compute/execute crons.
  - **When ON:** skip the 10:00 cron; register:
    - `'10 15 * * 1-5'` → `runDailyCycleGraph({runDate, reason:'scheduled-compute', steps:['collect','sentiment','signals','ic_gate','handoff','trade']})`.
    - `'55 15 * * 1-5'` → `runDailyCycleGraph({runDate, reason:'scheduled-execute', steps:['alpaca','reconcile','report','pyportfolioopt_shadow','health']})`.
    - `'35 9 * * 1-5'` → SOD refresh (Task 7).
- [ ] Manual `node -e` smoke: gate OFF registers the 10:00 cron only; gate ON registers the three new crons and not the 10:00. Commit.

### Task 6: Handoff-freshness gate (execute phase)

**Files:** Modify `src/execution/alpaca_executor.py:main()` start; Test in `tests/test_alpaca_executor_*`.

- [ ] **Test first** — when `output/handoffs/<today>_sized.json` is missing or its filename date ≠ today, the executor logs + exits 0 **without submitting**; present-and-today → proceeds.
- [ ] **Implement** — at executor entry (after arg parse), resolve the expected handoff path for `run_date`; if absent/stale → log `'[executor] no fresh handoff for <date>; skipping'`, post to #trade-reports, exit 0. Commit.

### Task 7: SOD refresh (dashboard-only)

**Files:** Create `src/pipeline/run_sod_refresh.py` (or JS step); wire into cron (Task 5). Test minimal.

- [ ] **Ground first** the dashboard's intraday-price source (grep `src/channels/api/` + `src/channels/dashboard/`); write opening prices (`multi-snapshots` → `dailyBar.o`) to **that** surface only — do **not** touch `prices.parquet` or the signal path.
- [ ] **Test** — the writer targets the dashboard store and is a no-op against `data/master/`. Commit.

### Task 8: Execute-phase concurrency lock

**Files:** Modify `src/execution/alpaca_executor.py` (set/clear lock) + `scripts/redeploy_pipeline.py` (check/defer); Test `tests/test_redeploy_inflight_lock.py`.

- [ ] **Test first** — when Redis key `execute:close:inflight:{date}` is set, `redeploy_pipeline` early-exits (`deferred_execute_inflight`); when absent, it proceeds.
- [ ] **Implement** — executor (RTH close path) sets `execute:close:inflight:{date}` (TTL 300 s) before submitting and deletes on completion; `redeploy_pipeline.py` checks it at entry (alongside its cooldown checks) and defers if set. Commit.

### Task 9: Collect-free redeploy regression test

**Files:** Test `tests/test_redeploy_steps.py`.

- [ ] **Test** — import `scripts/redeploy_pipeline.py`; assert `'collect' not in REDEPLOY_STEPS.split(',')` and the set == `{signals,handoff,trade,alpaca,reconcile}`. Commit. (No code change — locks current behavior.)

### Task 10: Surface `action` in reports + dashboard

**Files:** Modify `src/execution/alpaca_executor.py:_post_executor_summary` + dashboard positions/trades view (ground the file); Test display-string assertion.

- [ ] **Test first** — given an order with `action='reduce_long'`, the #trade-reports line shows `reduce_long` (not `short`).
- [ ] **Implement** — read `order.get('action')` for the human label; fall back to `direction` if absent (old rows). Update the dashboard cell similarly. Commit.

### Task 11: Final integration check (no live flip)

- [ ] Run the full test suite (`pytest tests/ -q` + `node test/*.js` relevant); confirm green incl. existing executor/sizer/DTBP regressions.
- [ ] **Dry-run parity (read-only):** `OPENCLAW_CLOSE_PROXY_SNAPSHOT=1 PIPELINE_DRY_RUN=1` run of the compute steps in the worktree (with `data/master` symlink); confirm a healthy signal set + sane handoff with `action` labels; **do not execute live, do not flip crons** — surface results to the operator for the live-flip decision (spec §10).

---

## Self-review
- Spec coverage: snapshot (§4.1)=T1-2; action (§9)=T3,T10; split+crons (§4)=T4-5; SOD (§4)=T7; freshness (§8.2)=T6; lock (§8.3)=T8; collect-free (§7)=T9; min-signal removed = built-in (never added) + step-error-abort=T1-2. ✓
- No placeholders except T7's grounded dashboard target (resolved in-task).
- Types consistent: `fetch_close_proxy`/`CloseProxyError`/`_derive_action`/`OPENCLAW_CLOSE_PROXY_SNAPSHOT`/`execute:close:inflight:{date}` used uniformly.
- Out of scope: live cron flip + parity sign-off = operator (T11 surfaces, stops).
