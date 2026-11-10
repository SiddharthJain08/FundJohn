# Crypto Engine Phase 2 — 24/7 Crypto Drawdown Breaker: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A 5-minute, 24/7 breaker that evaluates the crypto book's drawdown from broker positions, closes the sleeve (T4a) or a single position (T4b) when armed, alerts on data blackout (T5), records every evaluation, and publishes a hold-flat state that the crypto order paths and the dashboard honour.

**Architecture:** One module `src/execution/crypto_breaker.py` (pure rule functions + an I/O `run_once`) spawned every 5 minutes from the johnbot in-process scheduler under a memory cap and a `timeout`. Positions come from `alpaca position list` normalised through the Phase 1 symbol normaliser; closes go through the existing crypto executor path (`alpaca_executor.execute_single` with a `close_only` order) and are polled with `regime_liquidator._poll_to_terminal`; audits land in `alpaca_liquidations` (gaining an `asset_class` column, migration 169) and a new append-only `crypto_breaker_evals` table; hold-flat lives in Redis with a `pipeline_config` mirror and is read by the existing crypto redeploy sizer, the crypto executor branch, and the Crypto page.

**Tech Stack:** Python 3.13, psycopg2, redis-py, the Alpaca CLI at `/root/go/bin/alpaca`; Node 24 for the scheduler block and the router change; pytest + `node --test`.

**Spec:** `docs/specs/2026-11-06-crypto-engine-whale-flow-design.md` §4, §5.3c, §6.6, §7 row P2 (operator decisions §9: drawdown triggers only). Interface facts below were grep-verified on 2026-11-10.

## Global Constraints

- **Two flags, both inert by default**: `OPENCLAW_CRYPTO_BREAKER_TICK=1` enables the 5-min job (unset ⇒ the `_sched` callback returns immediately); `OPENCLAW_CRYPTO_BREAKER=1` ARMS orders (unset/0 ⇒ **shadow**: evaluate, record, post on a would-fire, place no orders). `--dry-run` on the CLI forces shadow for that run.
- **Triggers are drawdown only** (operator): `T4a` sleeve Σ`unrealized_pl` / Σ`cost_basis` ≤ **−0.08**; `T4b` any single crypto position `unrealized_plpc` ≤ **−0.15** (close that position only). `T5` data blackout (crypto regime JSON `ts_utc` AND the latest `crypto_flow_snapshot.ts_utc` both older than **3 h** while holding crypto) ⇒ Discord alert only, **never orders**. Thresholds live in module constants overridable by env `OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD` / `_POSITION_DD` / `_BLACKOUT_H`.
- **Cooldowns** in Redis: `crypto_breaker:fired:{YYYY-MM-DD}:{reason}` TTL 6 h (`reason ∈ sleeve_drawdown | position_drawdown:<ticker> | data_blackout`).
- **Hold-flat contract** (spec §5.3c): Redis key `crypto_breaker:hold_flat` = JSON `{"since": iso, "reason": str, "scope": "sleeve"}` with TTL 24 h; per-ticker `crypto_breaker:hold_ticker:{BTC-USD}` = JSON `{"since", "reason"}` TTL 7 d. Mirror: `pipeline_config` key `crypto_breaker_hold_flat` (JSON or `""`), rewritten by every tick from Redis so the dashboard and the sizer (Phase 3) can read it without Redis. Cleared by `OPENCLAW_CRYPTO_BREAKER_REARM=<iso of since>` (accept the `_iso_variants` forms used by `account_breaker.py:811`) or by TTL expiry.
- **Executor facts**: crypto closes require `OPENCLAW_INSTRUMENT_CLASS_ROUTING=1` (ON in production); `alpaca_executor.execute_single(sess=None, equity, order, run_date)` with order `{'ticker': 'BTC-USD', 'direction': 'short', 'notional_usd': cur, 'current_usd': cur, 'strategy_id': '__crypto_breaker__', 'close_only': True}` performs a FULL close (`notional_usd == current_usd` skips the partial branch) and returns `{'status': 'submitted', 'order_id': …}` or `{'status': 'rejected', 'reason': …}`; it cancels resting crypto stops itself; it does NOT poll — use `regime_liquidator._poll_to_terminal(order_id, timeout_s=90)` and `_classify_outcome`. The executor has no dry-run: the breaker decides before calling it.
- **Broker positions**: `alpaca position list` (no flags; JSON list) fields used: `symbol, asset_class, qty, avg_entry_price, current_price, market_value, cost_basis, unrealized_pl, unrealized_plpc`. Crypto rows are those with `asset_class == 'crypto'` OR whose `symbol` normalises via `ingestion.crypto_flow.universe.normalize_broker_symbol` (handles `BTCUSD` and `BTC/USD`). Equity rows are ignored entirely.
- **Audit**: `regime_liquidator._write_liquidation_audit(row)` gains an optional `asset_class` key written to the new column (migration 169 `ALTER TABLE alpaca_liquidations ADD COLUMN IF NOT EXISTS asset_class TEXT`); `close_result.source = 'crypto_breaker'`. Evaluations go to `crypto_breaker_evals` (migration 169), append-only.
- **Discord**: `regime_liquidator._post_to_discord('trade-reports', msg)`; message prefixes `[crypto_breaker] shadow …`, `[crypto_breaker] FIRED …`, `[crypto_breaker] BLACKOUT …`, `[crypto_breaker] re-armed …`.
- **Box**: 2-core/8 GB; the tick is spawned via `capped_spawn.wrapCapped('timeout', ['--signal=TERM','--kill-after=15','240', PYTHON, 'src/execution/crypto_breaker.py'], {memoryMax: env OPENCLAW_CRYPTO_BREAKER_MEMORY_MAX || '400M'})`, `os.nice(10)` inside (it must stay responsive: it is a risk control, not a batch job). No parquet reads. Skips cheaply when there are no crypto positions (one CLI call, no DB write, log line only).
- **Safety**: the breaker never opens, never touches equities, never runs inside the 15:55–16:05 ET crypto execution window of the daily lane (skip reason `exec_window`), and refuses to fire when the position payload is empty/failed (skip `positions_unavailable`).
- **Tests**: `pytest tests/execution/test_crypto_breaker*.py -q -W error` (no network, no DB, no Redis — inject fakes); `node --check src/engine/cron-schedule.js`; `node --test tests/test_routes_crypto.test.js`. Never the full suite.
- **Deployment**: johnbot is user-scope; restart only outside 15:00–16:30 ET and 22:00–23:30Z. Migration 169 applied at merge by the controller.

## Review Focus

1. **A broker payload with a crypto row under an unexpected symbol form or missing `cost_basis`.** Expected: the row is still classified crypto via the normaliser, `cost_basis` falls back to `qty × avg_entry_price`, and a row that cannot be parsed is skipped with a counter rather than crashing the tick. Pinned in Task 2 (`test_positions_parse_both_symbol_forms`, `test_position_missing_cost_basis_falls_back`).
2. **Shadow mode with a would-fire.** Expected: evaluation row written with `action='shadow'`, Discord shadow line posted once per cooldown, zero executor calls. Pinned in Task 4 (`test_shadow_never_calls_executor`).
3. **Armed fire where the close is rejected or never fills.** Expected: audit row with `result_status` reflecting `rejected|pending`, hold-flat still set (the intent to be flat stands), Discord says so, no retry storm (cooldown set on attempt, not on fill). Pinned in Task 4 (`test_fire_rejected_close_sets_hold_and_cooldown`).
4. **Re-arm token for an OLD halt.** Expected: a token that matches a previous `since` does not clear a newer hold. Pinned in Task 3 (`test_rearm_token_scoped_to_since`).
5. **Data blackout while holding crypto.** Expected: alert only, no orders, even when a drawdown trigger is ALSO true (blackout means the drawdown numbers may be stale — the tick still evaluates T4 from broker data, which is live; ruling: T4 fires on broker data regardless of blackout, T5 just adds an alert). Pinned in Task 4 (`test_blackout_alert_does_not_block_t4_but_never_orders_itself`).

---

### Task 1: Migration 169 and audit column plumbing

**Files:**
- Create: `src/database/migrations/169_crypto_breaker.sql`
- Modify: `src/execution/regime_liquidator.py:935-990` (`_write_liquidation_audit`)
- Test: `tests/execution/test_crypto_breaker_audit.py`

**Interfaces:**
- Produces: table `crypto_breaker_evals (id BIGSERIAL PK, ts_utc TIMESTAMPTZ NOT NULL, mode TEXT NOT NULL CHECK (mode IN ('shadow','armed','dry_run')), n_positions INT NOT NULL, sleeve_cost_basis NUMERIC, sleeve_unrealized_pl NUMERIC, sleeve_dd NUMERIC, worst_ticker TEXT, worst_plpc NUMERIC, triggers JSONB NOT NULL DEFAULT '[]', action TEXT NOT NULL, skipped TEXT, hold_flat BOOLEAN NOT NULL DEFAULT FALSE, note TEXT)` + index `(ts_utc DESC)`; `ALTER TABLE alpaca_liquidations ADD COLUMN IF NOT EXISTS asset_class TEXT`; `_write_liquidation_audit(row)` accepts optional `row['asset_class']` and `row['source']` (default `'close_subset'` to keep existing callers byte-identical).

- [ ] **Step 1: Write the failing test** — fake conn/cursor (copy the `_Conn/_Cur` fakes from `tests/ingestion/test_crypto_flow_store.py`): `test_write_liquidation_audit_includes_asset_class_when_given` asserts the INSERT SQL contains `asset_class` and the params include `'crypto'` and that `close_result` JSON has `"source": "crypto_breaker"`; `test_write_liquidation_audit_default_source_unchanged` asserts the old shape (`source: close_subset`, no `asset_class` param).
- [ ] **Step 2: Run** `pytest tests/execution/test_crypto_breaker_audit.py -q` → FAIL (`asset_class` not in SQL).
- [ ] **Step 3: Write migration 169** (header in the style of 168; idempotent) and extend `_write_liquidation_audit` (monkeypatchable `psycopg2.connect`; add the two optional keys; keep the signature).
- [ ] **Step 4: Run** → PASS. Also `pytest tests/execution/test_regime_liquidator.py -q -W error` must stay green.
- [ ] **Step 5: Commit** `crypto-breaker: migration 169 (evals table, liquidations.asset_class), audit writer accepts asset_class/source`

---

### Task 2: Position loader, rule functions, state (pure core)

**Files:**
- Create: `src/execution/crypto_breaker.py`
- Test: `tests/execution/test_crypto_breaker_rules.py`

**Interfaces:**
- Produces:
  ```python
  SLEEVE_DD = float(env 'OPENCLAW_CRYPTO_BREAKER_SLEEVE_DD' or -0.08); POSITION_DD = … or -0.15; BLACKOUT_H = … or 3.0
  COOLDOWN_TTL_S = 6*3600; HOLD_FLAT_TTL_S = 24*3600; HOLD_TICKER_TTL_S = 7*86400
  ARM_ENV='OPENCLAW_CRYPTO_BREAKER'; TICK_ENV='OPENCLAW_CRYPTO_BREAKER_TICK'; REARM_ENV='OPENCLAW_CRYPTO_BREAKER_REARM'
  @dataclass class CryptoPos: ticker: str; symbol: str; qty: float; avg_entry: float|None; price: float|None; market_value: float; cost_basis: float; unrealized_pl: float; unrealized_plpc: float|None
  def parse_positions(payload: list[dict]) -> tuple[list[CryptoPos], dict]   # (crypto rows, counters {'equity_rows','crypto_rows','unparsed'}); cost_basis fallback qty×avg_entry; plpc fallback unrealized_pl/cost_basis
  def load_crypto_positions(run_cli=None) -> tuple[list[CryptoPos] | None, dict]   # None on CLI failure
  def sleeve_drawdown(pos: list[CryptoPos]) -> tuple[float|None, float, float]   # (dd, Σcost, Σpl); dd None when Σcost ≤ 0
  def evaluate(pos: list[CryptoPos], *, now: datetime, regime_ts: datetime|None, flow_ts: datetime|None, thr=None) -> dict
      # {'sleeve_dd', 'sleeve_cost', 'sleeve_pl', 'worst_ticker', 'worst_plpc', 'triggers': [ {'id':'T4a','reason':'sleeve_drawdown'} | {'id':'T4b','reason':'position_drawdown:ETH-USD','ticker':'ETH-USD'} | {'id':'T5','reason':'data_blackout','ages_h':{...}} ], 'blackout': bool}
      # T4b fires for EVERY position ≤ POSITION_DD (list), but when T4a also fires, T4b entries are dropped (sleeve close covers them)
  def mode(env=os.environ, dry_run=False) -> str   # 'dry_run' | 'armed' | 'shadow'
  def in_exec_window(now_utc) -> bool   # 15:55 ≤ ET < 16:05 Mon–Fri
  # state (Redis-backed, all fail-open to "not held"/"not cooling"):
  def cooldown_active(r, reason, now) -> bool; def set_cooldown(r, reason, now) -> None
  def hold_flat_get(r) -> dict|None; def hold_flat_set(r, reason, now) -> dict; def hold_ticker_set(r, ticker, reason, now); def hold_ticker_get(r, ticker) -> dict|None
  def rearm_requested(env, hold: dict|None) -> bool   # token ∈ _iso_variants(hold['since'])
  def clear_hold(r) -> None
  def mirror_hold_to_pipeline_config(conn, hold: dict|None) -> None   # upsert key 'crypto_breaker_hold_flat' = json or ''
  ```
- [ ] **Step 1: Failing tests** (pure, no I/O; a `FakeRedis` dict with `get/set(ex=)/delete/ttl`):
  ```python
  def test_positions_parse_both_symbol_forms():  # 'BTCUSD' (asset_class crypto) and 'ETH/USD' → tickers BTC-USD, ETH-USD; 'AAPL' us_equity ignored → counters equity_rows 1
  def test_position_missing_cost_basis_falls_back():  # no cost_basis → qty×avg_entry; no plpc → pl/cost
  def test_sleeve_dd_and_triggers():  # cost 10000, pl -900 → dd -0.09 → T4a; a -16% position present → T4b dropped because T4a; another case: dd -0.03 with one -16% → only T4b for that ticker
  def test_blackout_requires_both_sources_stale_and_positions():  # regime 4h old + flow 1h → no T5; both 4h → T5; no positions → evaluate not even called by run_once (tested in Task 4)
  def test_mode_precedence():  # dry_run arg wins; env '1' → armed; else shadow
  def test_exec_window():  # 2026-11-10 20:58Z (15:58 ET) → True; 21:06Z → False; Saturday → False
  def test_cooldown_and_hold_keys():  # set/get round-trip, TTLs 21600/86400/604800, reason in key
  def test_rearm_token_scoped_to_since():  # token == since → True; token of an older since → False; hold None → False
  ```
- [ ] **Step 2: Run** → FAIL (ImportError).
- [ ] **Step 3: Implement** the pure core (≤ 220 lines for this part). `_iso_variants` imported from `execution.account_breaker` (it is a module-level function at :811) — if importing `account_breaker` pulls heavy deps, copy the 6-line helper instead and say so.
- [ ] **Step 4: Run** → PASS. **Step 5: Commit** `crypto-breaker: position loader, drawdown rules, hold-flat/cooldown state (pure core)`

---

### Task 3: Actions — flatten / close via the executor, audit, Discord

**Files:**
- Modify: `src/execution/crypto_breaker.py`
- Test: `tests/execution/test_crypto_breaker_actions.py`

**Interfaces:**
- Produces:
  ```python
  def close_position(pos: CryptoPos, *, equity: float, run_date: str, reason: str, executor=None, poller=None, audit=None) -> dict
      # builds the close_only order (Global Constraints), calls executor.execute_single(None, equity, order, run_date); on 'submitted' polls poller(order_id, timeout_s=90) → _classify_outcome; writes audit row {run_date, regime_from:'CRYPTO', regime_to:'CRYPTO_BREAKER', ticker, direction:'long_close'|'short_close', qty, result_status, market_value_usd, filled_qty, avg_fill_price, asset_class:'crypto', source:'crypto_breaker', error_msg} via audit(row); returns {'ticker','status','order_id','filled_qty','error'}
  def flatten_crypto(pos: list[CryptoPos], *, equity, run_date, reason, **deps) -> list[dict]   # close_position for each, sequential, continue on error
  def post(msg: str, poster=None) -> None   # regime_liquidator._post_to_discord('trade-reports', msg)
  def fmt_fire_msg(mode, triggers, results, hold) -> str; def fmt_shadow_msg(ev, triggers) -> str; def fmt_blackout_msg(ev) -> str
  ```
- [ ] **Step 1: Failing tests** with a recording fake executor (`execute_single` returns submitted/rejected per ticker), fake poller returning `{'status':'filled','filled_qty':…,'filled_avg_price':…}` or `None`, fake audit list: `test_close_position_full_close_order_shape` (asserts `notional_usd == current_usd == market_value`, `close_only True`, `strategy_id '__crypto_breaker__'`, direction short for a long), `test_close_position_rejected_is_audited_with_error` (status rejected → audit `result_status 'rejected'`, error_msg present), `test_close_position_pending_when_poll_none`, `test_flatten_continues_after_one_failure`, `test_messages_contain_reason_count_notional`.
- [ ] **Step 2–5:** fail → implement → pass → commit `crypto-breaker: close/flatten via crypto executor path, audit rows, Discord messages`

---

### Task 4: `run_once` orchestration + CLI

**Files:**
- Modify: `src/execution/crypto_breaker.py`
- Test: `tests/execution/test_crypto_breaker_run_once.py`

**Interfaces:**
- Produces:
  ```python
  def run_once(*, now=None, env=None, dry_run=False, deps: dict | None = None) -> dict   # deps keys: run_cli, redis, conn, executor, poller, audit, poster, account_equity (callable), regime_ts (callable→datetime|None), flow_ts (callable→datetime|None)
  def main(argv=None) -> int   # --dry-run, --force (ignore exec window)
  ```
  `run_once` order: (1) mode; (2) hold = hold_flat_get; if `rearm_requested(env, hold)` → clear_hold, post `re-armed`, mirror; (3) positions = load_crypto_positions; None → eval row `skipped='positions_unavailable'`, return; empty → mirror hold, print `[crypto_breaker] no crypto positions`, return WITHOUT a DB eval row; (4) if `in_exec_window(now)` and not force → eval row `skipped='exec_window'`, return; (5) `ev = evaluate(...)`; (6) blackout → `fmt_blackout_msg` posted unless cooldown `data_blackout`; set cooldown; (7) for each trigger not in cooldown: shadow/dry_run → post shadow msg, action `'shadow'`; armed → `T4a`: `flatten_crypto` + `hold_flat_set(reason)`; `T4b`: `close_position` + `hold_ticker_set`; set cooldown on ATTEMPT; post fire msg; (8) write eval row (`mode, n_positions, sleeve_*, worst_*, triggers, action ∈ {'none','shadow','flatten','close:<ticker>', …}, hold_flat`); (9) mirror hold to pipeline_config; (10) print one summary line `[crypto_breaker] mode=… n=… dd=… worst=…@… triggers=… action=… hold=…`. Every DB/Redis step is try/except with a printed warning; a failure never prevents the T4 action when armed, and never causes an action when not.
- [ ] **Step 1: Failing tests** (all deps faked): `test_no_positions_skips_without_eval_row`, `test_shadow_never_calls_executor` (dd −0.09, shadow → executor calls == 0, eval action 'shadow', one post), `test_armed_sleeve_fire_flattens_sets_hold_and_cooldown`, `test_armed_position_fire_closes_only_that_ticker_and_sets_hold_ticker`, `test_fire_rejected_close_sets_hold_and_cooldown` (executor rejects → hold set, cooldown set, audit rejected), `test_cooldown_suppresses_second_fire`, `test_exec_window_skips_with_eval_row`, `test_blackout_alert_does_not_block_t4_but_never_orders_itself` (both stale + dd −0.09 armed → flatten happens AND blackout post; both stale + dd −0.01 → only blackout post, no orders), `test_rearm_clears_hold_and_posts`, `test_positions_unavailable_writes_skip_row_and_no_action`.
- [ ] **Step 2–5:** fail → implement → pass → commit `crypto-breaker: run_once orchestration (shadow/armed/dry-run), CLI`

---

### Task 5: Hold-flat consumers — redeploy sizer and crypto executor branch

**Files:**
- Modify: `src/execution/crypto_redeploy_sizer.py:37-116` (`size_crypto_positions`)
- Modify: `src/execution/alpaca_executor.py:1300-1364` (`_route_crypto_order`)
- Create: `src/execution/crypto_hold.py` (tiny shared reader so both consumers do not import the breaker)
- Test: `tests/execution/test_crypto_hold_consumers.py`

**Interfaces:**
- Produces: `crypto_hold.hold_state(conn=None, redis=None) -> dict` = `{'sleeve': dict|None, 'tickers': {ticker: dict}}` reading Redis first (`crypto_breaker:hold_flat`, `crypto_breaker:hold_ticker:*` via SCAN), falling back to `pipeline_config.crypto_breaker_hold_flat` when Redis is unavailable; fail-open to empty. `crypto_hold.is_held(ticker, state) -> bool`.
- `size_crypto_positions(..., hold_state=None)`: when `hold_state['sleeve']` → every target 0 (so only close orders are emitted, tagged `strategy_id='__crypto_breaker_hold__'`); a held ticker → its target 0. Log `[crypto_redeploy] hold_flat active: targets zeroed`.
- `_route_crypto_order`: for a BUY (`direction=='long'` and not close_only) when `crypto_hold.is_held(raw_ticker, crypto_hold.hold_state())` → return `{'ticker': api_symbol, 'status': 'refused', 'reason': 'hold_flat', …}` and log `refused:hold_flat`; closes always allowed. The read is wrapped so a Redis/DB error never blocks an order (fail-open = allow, with a warning).
- [ ] **Step 1: Failing tests** — fake Redis/pipeline_config: `test_hold_state_prefers_redis_then_pipeline_config`, `test_sizer_zeroes_targets_under_sleeve_hold` (uses the existing `test_crypto_redeploy.py` fixtures pattern), `test_sizer_zeroes_only_held_ticker`, `test_executor_refuses_crypto_buy_under_hold_but_allows_close` (use `_fake_cli` from `tests/execution/test_crypto_execution_lane.py`).
- [ ] **Step 2–5:** fail → implement → pass (also keep `tests/execution/test_crypto_redeploy.py tests/execution/test_crypto_execution_lane.py` green) → commit `crypto-breaker: hold-flat honoured by the crypto redeploy sizer and the crypto executor buy path`

---

### Task 6: Scheduler, health, dashboard risk block, env example

**Files:**
- Modify: `src/engine/cron-schedule.js` (after the `crypto-flow-tick` block)
- Modify: `src/system_checks/checks/scheduled_jobs.py:38` (`CRON_POLICIES['crypto-breaker-tick'] = ('hours', 1)`)
- Modify: `src/channels/api/routes_crypto.js` (`/api/crypto/risk` breaker block)
- Modify: `src/channels/api/server.js` (hero breaker chip + risk panel read the new fields — tiny)
- Modify: `.env.example`, `docs/reference/crypto-flow-data.md` (one "Breaker" section)
- Test: `tests/execution/test_crypto_breaker_cron_wiring.py` (mirror `test_account_breaker_cron_wiring.py`), `tests/test_routes_crypto.test.js` (extend), `tests/system_checks/test_scheduled_jobs_overdue.py` (pinned count +1), `tests/test_dashboard_crypto_page.test.js` (chip reads `risk.breaker.status`)

**Interfaces:**
- `_sched('crypto-breaker-tick', '*/5 * * * *', …, { timezone: 'UTC' })`: return unless `process.env.OPENCLAW_CRYPTO_BREAKER_TICK === '1'`; spawn via `wrapCapped('timeout', ['--signal=TERM','--kill-after=15','240', PYTHON, 'src/execution/crypto_breaker.py'], { memoryMax: process.env.OPENCLAW_CRYPTO_BREAKER_MEMORY_MAX || '400M', fallback: '400M' })`, detached, log `logs/crypto_breaker_<date>.log` (fd closed after spawn).
- `/api/crypto/risk` → `breaker: {tick_enabled: '1'|'0'|'unset', armed: '1'|'0'|'unset', status: 'off'|'shadow'|'armed', hold_flat: <json|null> (from pipeline_config), thresholds: {sleeve_dd:-0.08, position_dd:-0.15, blackout_h:3}, last_evals: [ last 10 rows of crypto_breaker_evals ]}` (DB errors → `last_evals: []`, `hold_flat: null`).
- Hero chip text: `breaker: off|shadow|armed` (+ ` · HOLD FLAT since <age>` in red when `hold_flat`); risk panel lists `last_evals` (ts, mode, dd, worst, action).
- [ ] **Step 1: Failing tests** — cron wiring (source-text regex: job name, `*/5 * * * *`, `OPENCLAW_CRYPTO_BREAKER_TICK`, `crypto_breaker.py`, `timeout`), routes (`breaker.status` tri-state from env; `last_evals` from fake query; DB error path), dashboard text test (hero reads `risk.breaker.status`, `HOLD FLAT`), pinned count.
- [ ] **Step 2–5:** fail → implement → `node --check` both files, run the three test files → commit `crypto-breaker: 5-min UTC scheduler job under timeout+MemoryMax, health policy, /api/crypto/risk breaker block, dashboard chip`

---

### Task 7 (controller, merge phase): migrate, enable tick in shadow, verify

- [ ] `npm run db:migrate` → `crypto_breaker_evals` exists; `alpaca_liquidations.asset_class` exists.
- [ ] `OPENCLAW_CRYPTO_BREAKER_TICK=1` in `.env` (ARM flag stays unset); restart user-scope johnbot outside the protected windows; wait for the first `:x5` tick; `tail logs/crypto_breaker_<date>.log` shows `no crypto positions` (there are none today) and no errors; `/api/crypto/risk` shows `status: shadow`.
- [ ] Manual shadow verification with a synthetic position payload: `python3 src/execution/crypto_breaker.py --dry-run --force` under an env `OPENCLAW_CRYPTO_BREAKER_FAKE_POSITIONS=<path to a JSON file>` (add this test-only hook in Task 4: when set, `load_crypto_positions` reads the file instead of the CLI, and the mode is forced to `dry_run`) → eval row written with `mode='dry_run'`, Discord shadow line posted once.
- [ ] Changelog + memory. The two-week shadow starts when Phase 3 first sizes crypto positions (there is nothing to evaluate until then).

## Self-review notes (2026-11-10)
- Spec §4 coverage: triggers T4a/T4b/T5 (T2), actions + audit + Discord (T3), cooldowns + hold-flat + re-arm (T2/T4), shadow/armed (T4), scheduler (T6), §5.3c consumers (T5 — the Phase 3 pooled sizer will add itself as a third consumer), §6.6 risk panel (T6), §7 P2 rollout (T7).
- Types: `CryptoPos`, `evaluate()` dict keys, `hold_state()` shape and `is_held()` are used identically in T2–T6.
- Review Focus 1–5 pinned in T2/T3/T4 as named.
- Rulings recorded here: tick enable flag separate from the arm flag (inert-by-default rule); T4 evaluates on live broker data even during a data blackout (blackout is about the FLOW/REGIME feeds, not the broker); cooldown set on attempt not fill; no eval row when there are no positions (keeps the table small; the `__tick__`-style heartbeat is the log line and the job-stat row).
