# Spec — Per-bar exit hook, Phase 2 (live mirror)

**Status:** DRAFT for operator review 2026-08-28. Implements §3 / §5-live / §6-row-2 of
`docs/specs/2026-08-28-per-bar-exit-hook-spec.md` (the parent spec, approved 08-28) with the
detail that only the live code can supply. Phase 1 (backtest) landed `2d955fa..3695211`.

**Grounding:** every claim verified against the working tree at `3695211` on 2026-08-28.
Symbol names are the stable reference; line numbers drift.

---

## 0. What exists today (live side)

| where | fact |
|---|---|
| `engine.main()` (:2180) | order of operations: `regime = load_regime(cur)` (:2205) → `strategies = load_approved_strategies(cur)` (:2211, instantiated `BaseStrategy` objects for registry `status='approved'`) → `prices = load_prices(universe)` (:2268, wide close panel, trading-day `DatetimeIndex`, today's close-proxy row appended when `OPENCLAW_CLOSE_PROXY_SNAPSHOT=1`) → `aux_data = load_aux_data(universe, as_of=run_date)` (:2269) → `run_strategies(...)` (:2284) → **`update_pnl(cur, prices, run_date)` (:2395)** → `fire_report_triggers` (:2415) → `log_run` (:2104). All four objects the hook needs are in scope at the `update_pnl` call. |
| `engine.update_pnl` (:1896) | SELECT `id, strategy_id, ticker, direction, entry_price, mark_entry_price, target_date, lifecycle_state, stop_loss, target_1, signal_date` FROM `execution_signals` WHERE `status='open'` AND lifecycle NULL/`FILLED`. **`signal_params` is not selected.** Per row: `entry` = `mark_entry_price` else `entry_price`; `days_held = (run_date − target_date|signal_date).days` — **calendar days**; `current = prices[ticker].dropna().iloc[-1]`; stop/target inference with `STOP_TRIGGER_PCT`/`TARGET1_TRIGGER_PCT`; UPSERT `signal_pnl` (`close_reason` text, free-form) and `UPDATE execution_signals SET status='closed'`; closed ids returned for post-commit OUE classification. No condition-based or time-based close exists. |
| `load_regime` (:~660) | returns `{'state', 'vix_level', 'vix_percentile', 'regime_data', 'updated_at'}` — NOT the backtest's `{'state','date','one_hot','transition_probs'}`. This shape difference already exists for `generate_signals`. |
| `regime_param_resolver.max_hold_days_override(sid, regime)` | per-regime `strategy_regime_params.max_hold_days`; `unified_backtest._configured_max_hold_days` (:1104) = MAX over regimes, gated on `regime_param_override.gate_on()`, default `DEFAULT_MAX_HOLD_DAYS=21`. |
| downstream of a close | 15:55 `trade` step carried set = `lifecycle_state='APPROVED'` rows (`regime_blended_sizer._load_approved_carried_signals`) ⇒ a closed signal leaves targets ⇒ `_classify_position_deltas` emits `orphan_close` (`__close_orphan__`, tier-0) ⇒ executor cancel-before-close + close (08-18). `fire_report_triggers` / `send_report` / `b1_order_source` switch on `close_reason` with `elif` / `IS DISTINCT FROM` chains — unknown reasons fall through. |
| stop cooldown | `aux_data_loader._recent_stop_outs` is backtest-only (`run_stop_history`); live returns `{}`. Nothing to exclude. |
| health digest | `src/engine/daily-health-digest.js#buildDigest` (:47) composes fixed lines (`posLine`, `pnlLine` from `signal_pnl` 7-day closes, doctor line…) posted by `src/pipeline/daily_health_digest.js`. `execution_runs` has no free column besides `errors jsonb` (list of strings). |
| tests | `tests/execution/test_engine_oue_ordering.py` — `_FakeCursor` stand-in (returns canned open rows on the `status = 'open'` SELECT, records every execute) + `_prices_at(px)`; the pattern for `update_pnl` unit tests. |
| promotion guard (Phase 1) | `promotion_service.js` refuses candidate→live when the primary run's `config_json.exit_hook` is true and `process.env.OPENCLAW_EXIT_HOOK_LIVE !== '1'`. **Consequence: no hook strategy can be live before the flag is flipped.** |

## 1. Scope

In: the live hook evaluation and time stop inside `update_pnl`, behind a kill switch; observability (log, `execution_runs.errors`, digest line); the parity test (spec §5); a live-code replay verification; the Phase 1 residuals that parity needs (SHORT end-to-end test; full regime payload in the backtest hook call); the flag-flip runbook.
Out: partial exits, intraday evaluation, options legs, a time stop for non-hook strategies (see D6), any change to `signal_pnl.days_held` semantics.

## 2. Design

### 2.1 `update_pnl` signature and gating
```python
def update_pnl(cur, prices, run_date, *, strategies=None, regime=None, aux_data=None) -> tuple[int, list]
```
`main()` passes `strategies=strategies, regime=regime, aux_data=aux_data`. Defaults keep every existing caller/test byte-identical.

Flag `OPENCLAW_EXIT_HOOK_LIVE` (read once per call, logged once): `'1'` ⇒ hook + time stop evaluated; anything else ⇒ the new branches are skipped and `update_pnl` is byte-identical to today (existing tests `test_engine_oue_ordering.py` etc. remain the guard).

### 2.2 Row data
Add `signal_params` to the SELECT (jsonb ⇒ dict via the RealDictCursor; `None`/non-dict ⇒ `{}`). No migration.

### 2.3 Strategy instance for a row
`by_id = {s.id: s for s in strategies or []}`. For an open row whose `strategy_id` is absent (strategy demoted after entry — positions outlive approval), lazily `load_strategy_class(strategy_id)()` (from `strategies.registry`) once per call and cache; a load failure is counted (`hook_load_failed`) and the row is treated as non-hook (HOLD). Skip unless `getattr(strat, 'exit_hook', False)`.

### 2.4 Per-row order (after the existing stop/target inference, only when `close_reason is None`)
1. **Hook.** `reason = strat.should_exit(position, prices, regime, aux_data)` wrapped in try/except: exception ⇒ counted (`hook_raised`), first message kept, HOLD. `reason` truthy ⇒ `close_reason = f'strategy_exit:{reason}'`, `close_status='closed'`, `realized_pct = unrealized_pct`.
2. **Time stop.** If still open and `bars_held >= hold_cap` ⇒ `close_reason='max_hold'`, closed, realized as above.
Both closes use the existing UPSERT/UPDATE code path unchanged, so the ids join `newly_closed_ids` and OUE classification runs post-commit exactly as for stop/target closes.

### 2.5 `position` dict (live construction — spec §1 contract)
```python
position = {
  'ticker': ticker, 'direction': direction,           # 'LONG' | 'SHORT'
  'entry_price': entry,                               # mark_entry_price else entry_price (existing precedence)
  'entry_date': entry_dt,                             # target_date else signal_date
  'days_held': bars_held,                             # see D4
  'stop_loss': stop_loss, 'target_1': target_1,
  'signal_params': signal_params,                     # dict from the jsonb
}
bars_held = int(((prices.index > pd.Timestamp(entry_dt)) & (prices.index <= pd.Timestamp(run_date))).sum())
```
**D4 — bars, not calendar days.** The backtest's `holding_days` counts stepped bars (fill bar excluded). `prices.index` is the trading-day axis (parquet dates + today's proxy row), so `bars_held` is the same quantity. `signal_pnl.days_held` keeps its existing calendar semantics — untouched.

### 2.6 Time-stop cap
`hold_cap = min(int(signal_params['hold_days']), configured_max_hold_days(strategy_id))` with the same fallbacks as `backtest/open_book.resolve_hold_cap` (missing/invalid/<1 ⇒ configured max). `configured_max_hold_days(sid)` is extracted from `unified_backtest._configured_max_hold_days` into `execution/regime_param_resolver.py` (MAX over `CANONICAL_REGIMES` of `max_hold_days_override`, gated on `regime_param_override.gate_on()`, default 21, lookup failure logged ⇒ default); `unified_backtest._configured_max_hold_days` becomes a one-line delegate (byte-identical; `tests/backtest/test_backtest_max_hold_config.py` is the guard).

**D6 — time stop for `exit_hook` strategies only.** Their backtests (open-book path) honor `hold_days`/`max_hold`, so live must too for parity. Non-hook strategies are unchanged: live has never closed them on `max_hold` (a pre-existing backtest≠live gap: `simulate_trade` exits at `max_hold_days`, live positions persist until stop/target/`stale_tracker`). Closing that gap fleet-wide is a separate decision with fleet-wide P&L consequences — recorded as OWED, not smuggled in here.

### 2.7 Regime payload
The hook receives the same `regime` dict `run_strategies` gives `generate_signals` live (`{'state', 'vix_level', 'vix_percentile', 'regime_data', 'updated_at'}`). Backtest hook calls (Phase 1 pass `{'state','date'}`) are changed to pass the full backtest `regime_payload` (`{'state','date','one_hot','transition_probs'}`). Contract for hook authors, written into `BaseStrategy.should_exit`'s docstring: **rely on `regime['state']` only** — the two sides agree on `state` and on nothing else (same tolerance `generate_signals` already lives with).

### 2.8 Observability
- Per close: `logger.info('[exit_hook] %s %s %s bars_held=%d', sid, ticker, close_reason, bars_held)`.
- Per run: `update_pnl` keeps its `(n_updates, newly_closed_ids)` return (callers and tests pin it); the counters (`strategy_exit`, `max_hold`, `hook_raised`, `first_hook_raise`, `loaded_on_demand`, `hook_load_failed`) are published as the module-level dict `engine.LAST_EXIT_HOOK_STATS` (reset at the start of every call). `main()` logs `[exit_hook] closes: %d strategy_exit, %d max_hold; hook errors %d; instances loaded on demand %d` and appends `f'exit_hook: {n} hook errors (first: {msg})'` to `execution_runs.errors` only when `hook_raised > 0`.
- Digest: `buildDigest` gains one line, shown only when non-zero: `🪝 Exit hook: N strategy exits (a z_revert / b pair_decohered / …), M max_hold` from `SELECT close_reason, COUNT(*) FROM signal_pnl WHERE status='closed' AND closed_at::date = CURRENT_DATE AND (close_reason LIKE 'strategy_exit:%' OR close_reason = 'max_hold') GROUP BY 1`. No schema change.

### 2.9 Kill switch and flip
`.env`: `OPENCLAW_EXIT_HOOK_LIVE=0` until Phase 2 verification passes (§4). Both the engine (`EnvironmentFile=/root/openclaw/.env` on the pipeline units) and the JS promotion guard (API process under `johnbot.service`, `dotenv`) must see the same value — the flip runbook (§4) verifies both.

## 3. Phase 1 residuals folded in
- **SHORT end-to-end test** through `_per_bar_simulate` (open-book path) — required so the parity test compares both directions.
- **Full regime payload** to the backtest hook (§2.7): build `regime_payload` once at the top of the day loop and reuse it for both the advance and `generate_signals`; identical dict content ⇒ non-hook path unchanged (guarded by the pre-Phase-1 golden `tests/backtest/fixtures/open_book_identity_golden.json`).
- `unified_backtest._configured_max_hold_days` delegates to the new shared helper (§2.6).

## 4. Verification (spec §6 row 2, made concrete)

**D14 — no hook strategy can be live before the flip, so "one paper day with a hook strategy" cannot be the gate.** Verification is three-layered; the backtest side is authoritative (08-07 ruling):

1. **Parity test** (`tests/execution/test_exit_hook_live_parity.py`): synthetic two-ticker panel (40 bars) + fixture `exit_hook=True` strategy (deterministic rule on the panel, e.g. exit when `prices[ticker].iloc[-1] >= level` on bar k; `signal_params={'hold_days': 6}`); LONG and SHORT cases. (a) backtest: `_per_bar_simulate` on the panel; (b) live harness: for each date `d` after entry, call `update_pnl(_FakeCursor(open_rows), prices=panel.loc[:d], run_date=d, strategies=[fixture], regime={'state':'LOW_VOL'})` with `OPENCLAW_EXIT_HOOK_LIVE=1` and `OPENCLAW_BACKTEST_COUPLED_RECS=0`, removing rows the cursor recorded as closed. Assert identical `{(ticker, exit_date, exit_reason)}` and identical `bars_held` at exit vs backtest `holding_days`, for hook exits AND the `max_hold` time stop. Flag `'0'` ⇒ harness records no closes.
2. **Live-code replay on real data** (`scripts/exit_hook_live_replay.py --strategy S_coint_pairs_sector_v2 --run-id 3a470001-… --dates <d1>..<dN>`): for each date, build synthetic open rows from that run's `strategy_backtest_trades` open on the date (entry_date < d ≤ exit_date, with `signal_params` re-derived from the ledger snapshot: pair, beta, alpha, z, hold_days), call `update_pnl` with a `_FakeCursor`, the REAL `load_prices(universe)` panel truncated to `d`, the real strategy instance, and compare the closes the live branch would issue against the backtest's recorded `exit_date`/`exit_reason` for those trades. Report agreement per reason. This exercises the live code on production data with zero broker/DB side effects (read-only; runs outside 13:00–20:15 UTC).
3. **Flip + first real hook strategy** (activation checklist, not a Phase 2 gate): when the first `exit_hook` strategy is promoted after the flip, watch one cycle: `signal_pnl` closes with `strategy_exit:*`/`max_hold` at 15:00 ET → 15:55 sizer log shows `orphan_close` for those tickers → executor fills → next morning `alpaca position list --symbols …` shows them flat → digest line present.

**Flip runbook:** parity test green + replay agreement ≥ the backtest's own exit set for the sampled dates ⇒ set `OPENCLAW_EXIT_HOOK_LIVE=1` in `.env`; `systemctl --user restart johnbot.service` (user scope; `XDG_RUNTIME_DIR=/run/user/0`) so the API process re-reads `.env`; confirm with one `computeQualifyingRegimes` dry-run that an `exit_hook` run is no longer blocked and one engine `--dry-run` log line showing `[exit_hook] enabled`.

## 5. Testing matrix
- `tests/execution/test_update_pnl_exit_hook.py` (new): flag off ⇒ no hook call, byte-identical executes; flag on: hook reason ⇒ `strategy_exit:<reason>` UPSERT + status update + id in `newly_closed_ids`; stop beats hook (existing inference first); raising hook ⇒ hold + counter; `bars_held` computed from the panel (weekend gap does not count); time stop at `hold_cap` from `signal_params['hold_days']`; demoted strategy loaded on demand; non-hook strategy untouched; `signal_params` NULL tolerated.
- `tests/execution/test_configured_max_hold_days.py` (new): helper mirrors the old function (gate off ⇒ 21; MAX over regimes; failure ⇒ 21) + `unified_backtest._configured_max_hold_days` delegates.
- `tests/execution/test_exit_hook_live_parity.py` (new, §4.1).
- `tests/backtest/test_open_book.py`: SHORT end-to-end; full regime payload reaches the hook.
- Digest: `tests/engine/test_daily_health_digest_exit_hook.test.js` (node) — line present when counts > 0, absent otherwise.
- Existing guards: `test_engine_oue_ordering.py`, `test_engine_run_stats.py`, `test_dry_run_dataflow.py`, `test_backtest_max_hold_config.py`, the pre-Phase-1 golden.

## 6. Decisions recorded (operator may override)
- **D4** `position['days_held']` = trading bars (parity with backtest), computed from the prices index; `signal_pnl.days_held` unchanged.
- **D6** live time stop applies to `exit_hook` strategies only; fleet-wide `max_hold` parity is OWED separately.
- **D7** hook/time-stop closes reuse the existing close path (UPSERT + status + OUE classification); no new tables/columns.
- **D11** flag `'1'` only; `'shadow'` deliberately not offered — a shadow mode with no live hook strategy proves nothing and a promoted hook strategy under shadow would trade without its exits.
- **D14** verification = parity test + live-code replay on real data; the paper-day watch becomes the first hook strategy's activation checklist. Alternative if you prefer a true paper canary: `force`-promote X1 to paper for one cycle after the flip (it fails the gates; `force` bypasses them by design) and demote it the next day.
- **D-regime** hooks may rely on `regime['state']` only.
