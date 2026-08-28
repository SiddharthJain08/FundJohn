# Spec — Per-bar strategy exit hook (`BaseStrategy.should_exit`)

**Status:** Phase 1 LANDED (`2d955fa..3695211`); Phase 2 code LANDED
(`656c9aa..ceeb8a0`), `OPENCLAW_EXIT_HOOK_LIVE=0`, flip blocked pending the
replay re-run — see §6 and the Phase 2 spec
(`docs/specs/2026-08-28-per-bar-exit-hook-phase2-spec.md`). Design approved by
operator 2026-08-28 (approach A of three; scope "backtest + live, phased").

**Why:** the engines can express only three exits — intra-bar stop/target
brackets and a uniform `max_hold_days` — so any strategy whose edge lives in
a *condition-based* exit cannot be tested or traded. First casualty:
`S_coint_pairs_sector_v2` (X1). Run `655c4bdb` (2026-08-28) exited 772/1107
trades on the base-class 2×ATR per-leg stop; run `32ff3475`, after
spread-implied stops (`7ee5ae9`), was a faithful "enter at |z| ≥ 2, hold 25
days" test and still failed every gate (Sharpe −0.08). The spec'd exit —
flatten at |z| ≤ 0.5 — is where a pairs strategy's edge normally lives and is
not engine-expressible today. X1 is PARKED pending this feature.

**Grounding:** every claim verified against the working tree on 2026-08-28
(`4dfe878..0f15ada`). Line numbers drift; symbol names are the stable reference.

---

## 0. What exists today (the gap, precisely)

| side | where | behaviour |
|---|---|---|
| backtest | `unified_backtest._per_bar_simulate` (:691) | per day: `generate_signals(prices_to_date)`; each entry immediately calls `simulate_trade` (:371), which walks that ticker's *entire future* bar-by-bar for stop/target/`max_hold_days`. Nothing runs per bar for an open trade; the walk never sees the panel or the other leg. |
| backtest | `_signal_to_long_short` (:359) | `Signal.direction='FLAT'` → 0 → entry silently skipped. FLAT is a dead direction. |
| backtest | `run_backtest` (:1081) | `max_hold_days` is one top-level value (`DEFAULT_MAX_HOLD_DAYS=21`, or `strategy_regime_params` via `regime_param_resolver.max_hold_days_override`); per-signal `signal_params['hold_days']` is ignored. |
| live | `engine.update_pnl(cur, prices, run_date)` (:1896, called from `main()` at :2395 in the 15:00 ET `signals` step) | per open `execution_signals` row: infers `stop_loss` / `target_1` closes off the close-proxy panel, UPSERTs `signal_pnl`, updates `execution_signals.status`. No condition-based or time-based close. |
| live | `regime_blended_sizer._load_approved_carried_signals` (:615) | the 15:55 `trade` step's carried set = `lifecycle_state='APPROVED'`, `DISTINCT ON (strategy_id, ticker)` newest `target_date`. A closed signal leaves the set ⇒ ticker leaves targets ⇒ `_classify_position_deltas` (:1030) emits `orphan_close` (`strategy_id='__close_orphan__'`, tier-0). Executor cancel-before-close removes OCO legs (08-18 fix). |
| live | `open_reconcile.drop_signal_close(cur, signal_id, ticker, closed_price, reason)` (:68) | reusable close primitive (signal_pnl closed + execution_signals `CLOSED_AT_OPEN`). |
| both | `BaseStrategy.cadence_reset(regime)` (base.py:187) | precedent for an engine-called strategy hook. |

The exit-side gap is already declared in the X1 module docstring ("EXITS — what's
engine-expressible today vs. approximated").

## 1. Strategy interface (`src/strategies/base.py`)

```python
class BaseStrategy(ABC):
    exit_hook: bool = False            # class attribute; explicit opt-in

    def should_exit(self, position: dict, prices: pd.DataFrame,
                    regime: dict, aux_data: dict | None = None) -> str | None:
        """Return an exit reason (short snake_case token) to flatten this
        position at TODAY's close, or None to keep holding. Pure function of
        its arguments: no mutation, no I/O beyond the same point-in-time
        reads generate_signals may do, look-ahead-safe (`prices` ends at the
        evaluation bar)."""
        return None
```

`position` — identical shape on both sides:

| key | backtest source | live source |
|---|---|---|
| `ticker`, `direction` (`'LONG'`/`'SHORT'`) | trade record | `execution_signals.ticker/direction` |
| `entry_price` | actual fill (`entry_fill`) | `mark_entry_price`, fallback `entry_price` (the precedence `update_pnl` already uses) |
| `entry_date` | fill date | `target_date`, fallback `signal_date` (ditto) |
| `days_held` (int) | bars since fill, fill bar = 0 | `update_pnl`'s `days_held` |
| `stop_loss`, `target_1` | re-anchored bracket (`_reanchor_bracket`) | row values |
| `signal_params` (dict) | `Signal.signal_params` captured at entry | `execution_signals.signal_params` JSONB |

Rules:
- `exit_hook=True` with `should_exit` not overridden ⇒ error at class-definition time in `__init_subclass__` (base.py:133, where `active_in_regimes` is normalised). Overriding `should_exit` without setting `exit_hook=True` ⇒ hook never called (opt-in is the flag, not the override).
- A hook that raises is caught, logged once per (strategy, ticker, date), counted, and treated as `None` — the position is HELD (the bracket still protects). The counter is surfaced exactly like `bars_raised` in backtest and as a line in the daily health digest live.
- Hook exits never feed the stop-out cooldown: `run_stop_history` (backtest) and `aux_data_loader._recent_stop_outs` (live) key on `exit_reason == 'stop'` / `close_reason == 'stop_loss'` only. Unchanged.
- Reason tokens are free-form but persisted as `'strategy_exit:<reason>'` in `strategy_backtest_trades.exit_reason` and `signal_pnl.close_reason`. The `strategy_exit:` prefix is the contract consumers may match on.
- **`regime` payload shape (Phase 1 measured):** the backtest hook receives `{'state': <regime string or None>, 'date': <ISO date string>}` — NOT the richer `one_hot` / `transition_probs` payload `generate_signals` sees on some paths. A hook may only rely on those two keys. Phase 2's live mirror must pass the same shape or the two sides diverge silently (a hook reading `regime['one_hot']` would work live and be a `KeyError` — i.e. a HOLD — in backtest).
- **Per-signal time stop (same mechanism, same phase):** the open-book path honors `signal_params['hold_days']` as `min(int(hold_days), max_hold_days)`; missing/invalid ⇒ `max_hold_days` (today's behaviour). Reason stays `'max_hold'`. Live mirror: `update_pnl` closes with `close_reason='max_hold'` when `days_held >= min(hold_days, max_hold)` — live has NO time-based close today (close_reason census: `stale_tracker`, `target_1`, `stop_loss`, `rolled_continuation`, `signal_dropped`, `circuit_breaker`), so this is new behaviour gated with the hook flag (§3).

## 2. Backtest — open-book path (`src/backtest/unified_backtest.py`)

`_per_bar_simulate` branches once per run on `instance.exit_hook`:

**`exit_hook=False` (all 140 fleet strategies today):** untouched. `simulate_trade` at entry, byte-identical trade lists. Guarded by the determinism suite (`1acb7eb`, `PYTHONHASHSEED=0`) plus a new fixture test that runs a non-hook strategy through the post-change code and compares to a stored trade list.

**`exit_hook=True`:** entries are appended to `open_book` (list of trade dicts + captured `signal_params`) instead of being walked. On every OOS date, **before** `generate_signals` runs for that date, each open trade is advanced by that date's bar, in this order:

1. **Intra-bar bracket** on the ticker's H/L — the same predicates and `OPENCLAW_BT_DOUBLE_TOUCH` priority as `simulate_trade`; exit level → adverse fill (`slippage_bps`, per-ticker `cost_bps_by_ticker` as today).
2. **Hook at the close:** `reason = should_exit(position, close_wide.loc[:current_date], regime_payload, aux)`; on a reason, exit at `close` with adverse slippage, `exit_reason=f'strategy_exit:{reason}'`.
3. **Time:** `holding_days == min(hold_days, max_hold_days)` ⇒ exit at close, `'max_hold'`; no further bar for the ticker ⇒ `'end_of_data'`.

Precedence is fixed: a bar where the stop (or target) and the hook both fire is a bracket exit — the bracket is intra-bar, the hook is a close decision. `daily_marks` accumulate exactly as in `simulate_trade` (mark-to-close on interior bars, exit fill on the exit bar), so `_true_mtm`, tail stats, CVaR and the run/regime persistence need no change. Trades still open when the OOS window ends close at the last close as `'end_of_data'`.

Ordering rationale: exits on bar t are decided before bar t's new entries, mirroring live (`update_pnl` runs in the `signals` step before the `trade` step builds targets). Multiple open trades per (strategy, ticker) remain allowed (today's behaviour; X1's edge-trigger makes re-fires rare).

Persistence: only new `exit_reason` values in `strategy_backtest_trades` — no migration. Captured `signal_params` live in memory for the run (Phase 1 does not persist them per trade).

Cost: one hook call per open trade per day (X1 ≈ 15 open pairs ⇒ negligible). `prices` is passed as the `close_wide.loc[:current_date]` view, never copied.

**Measured (X1 run 3, 2026-08-28):** the engine-side cost is indeed negligible; what dominated was I/O *inside the hook*. `S_coint_pairs_sector_v2.should_exit` re-read the 860k-row single-row-group pair ledger on every call (~154 ms), once per open leg per bar, turning a ~10 min backtest into 38 min. Fixed by caching the approved table per ledger version (one read per `(path, mtime_ns, size)`; 154 → 0.7 ms warm). The standing lesson for any future hook: `should_exit` runs O(open trades × bars) times, so treat every read inside it as being in that inner loop.

## 3. Live — `engine.update_pnl` (signals step, 15:00 ET)

`update_pnl(cur, prices, run_date, *, strategies=None, regime=None)` gains two optional kwargs; `main()` passes the `strategies` list it already built for `run_strategies` (engine.py:1440) and the regime payload. Inside the per-open-signal loop, after the existing stop/target inference and only when `close_reason is None`:

1. `strat = by_id.get(row.strategy_id)`; skip unless `strat is not None and strat.exit_hook`.
2. Build `position` from the row (§1 table; `signal_params` JSONB → dict).
3. `reason = strat.should_exit(position, prices, regime, aux)` with the same close-proxy `prices` panel `update_pnl` already receives (parity with the backtest's `close_wide.loc[:current_date]`).
4. On a reason: `close_reason=f'strategy_exit:{reason}'`, `close_status='closed'`, `realized_pct=unrealized_pct` — the identical UPSERT/UPDATE the stop/target branches use, so the id lands in `newly_closed_ids`.
5. Time stop (§1), evaluated after the hook when the row is still open: `days_held >= min(hold_days, max_hold)` ⇒ `close_reason='max_hold'` (same UPSERT). Order on both sides is bracket → hook → time (§2).

Downstream needs nothing new: the closed row leaves the `APPROVED` carried set at 15:55 ⇒ `orphan_close` ⇒ executor closes at the 15:55 submit with cancel-before-close on the OCO legs. `fire_report_triggers`, `send_report`, `b1_order_source` switch on `close_reason` with `elif` / `IS DISTINCT FROM` chains — unknown reasons fall through; `stale_tracker` / `rolled_continuation` handling untouched. A pairs strategy's two legs are two signals evaluated with identical inputs ⇒ same-day close by construction; a lone surviving leg is already an orphan for `_classify_position_deltas`.

**Kill switch:** `OPENCLAW_EXIT_HOOK_LIVE` — default `'0'`: steps 1–5 are skipped and `update_pnl` is byte-identical to today; `'1'` enables. Read once per run, logged.

Same-day-lane note: `update_pnl` sees the 15:00 close-proxy, not the official close; the backtest sees the official close. This is the existing parity tolerance of the whole signal chain (08-07 spec §0) and is accepted here — the hook's threshold semantics must be robust to it (X1: |z| ≤ 0.5 is a 1.5σ margin from entry).

## 4. Promotion guard (lands in Phase 1)

A strategy whose backtest depends on the hook must not go live on a book that cannot honor it:
- `unified_backtest.run_backtest` writes `metadata.exit_hook: true` into the strategy's manifest entry when `instance.exit_hook` (same manifest-metadata surface as `backtest_universe_cap`, :1045/:1065).
- `promotion_service.js` (candidate→live judgement, `judgeRegimeSleeve` callers) and `auto_approval.js` refuse promotion when `metadata.exit_hook` is true and `OPENCLAW_EXIT_HOOK_LIVE !== '1'`, with an explicit verdict line (`exit_hook_live_disabled`). Activation/eligibility assigners are NOT gated (they mint eligibility, not live-ness).

`force: true` bypasses this guard like every other gate (existing force semantics) — the operator override is unchanged.

**Refinement (Phase 1 plan, 2026-08-28):** `unified_backtest` only READS the manifest today; introducing a manifest write from the backtest would be a new pattern. The guard therefore reads `strategy_backtest_runs.config_json.exit_hook` (written by every run since Phase 1) via `_latestPrimaryRun` instead of manifest `metadata.exit_hook`. Behaviour is the same: the primary run that would be promoted declares whether it relied on the hook.

## 5. Parity and testing

- **Parity test (Phase 2 exit gate):** synthetic two-ticker panel + a fixture hook strategy (deterministic z-like rule); run (a) the open-book simulator and (b) a harness that feeds the same rows through `update_pnl`'s hook branch day by day with a stub cursor (pattern: existing `tests/execution` DB stubs). Assert identical `{(ticker, exit_date, exit_reason)}`. Backtest side authoritative (operator ruling 2026-08-07).
- **Simulator unit tests (Phase 1):** stop and hook on the same bar ⇒ `'stop'`; hook fires at close with adverse slippage; `hold_days` honored and capped by `max_hold_days`; `end_of_data` at window end; raising hook ⇒ held + counted; `exit_hook=False` strategy ⇒ trade list identical to the stored fixture; `exit_hook=True` without override ⇒ class-definition error.
- **X1 hook (Phase 1 consumer)** — `S_coint_pairs_sector_v2.should_exit`: recompute the log-spread z from `signal_params['beta','alpha']` over `Z_WINDOW` on `prices`; `'z_revert'` when `|z| ≤ Z_EXIT (0.5)` or the sign of z flips from entry (`signal_params['z']`); `'pair_decohered'` when the pair is absent from the latest ledger snapshot with `as_of ≤ date` (the existing `_load_approved_pairs` predicate — look-ahead-safe). Tests: reversion day, decoherence via a later ledger row, look-ahead probe (future ledger row ignored), missing leg ⇒ `None`.
- **Live unit test (Phase 2):** `update_pnl` with `OPENCLAW_EXIT_HOOK_LIVE=1`, one open row for a hook strategy ⇒ `signal_pnl.close_reason='strategy_exit:z_revert'`, `newly_closed_ids` contains the id; flag `'0'` ⇒ no call.

## 6. Phasing

| phase | deliverables | done when |
|---|---|---|
| 1 — backtest | §1, §2, §4, X1 hook, tests; re-run X1 (`x1-backtest-3`, no `--max-hold-days` pin — `hold_days` now honored) | tests green; determinism fixture identical; X1 run persisted with `strategy_exit:*` exits |
| 2 — live | §3 behind `OPENCLAW_EXIT_HOOK_LIVE`, health-digest counter, parity test, live unit test | flag flipped to `1` after ONE paper day on which a hook strategy's `strategy_exit:*` closes reconcile at the broker (`alpaca_reconcile`). **STATUS 2026-08-28: code LANDED `656c9aa..d23f77e`, flag OFF (`OPENCLAW_EXIT_HOOK_LIVE=0`)** — parity test 4/4 passed, live replay `AGREEMENT 11/11` (23/26 disagreements = `max_hold` hold-cap config mismatch, 21 vs 30; 3/26 = unexplained early `strategy_exit:*` divergence). Flip BLOCKED pending the 3-exit diagnosis; see `docs/specs/2026-08-28-per-bar-exit-hook-phase2-spec.md` §4 for the full record and runbook. |

Phase 1 cost budget, measured: the open-book stepper itself is negligible against `simulate_trade`; the whole of run 3's 4× slowdown was hook-side ledger I/O (§2), removed by the ledger cache. Phase 2 should budget the live mirror the same way — one `should_exit` per open signal per run is cheap only if the hook's own reads are.

Out of scope (YAGNI): partial exits/resizing, hook-driven re-entries, intraday evaluation, options legs, persisting `signal_params` per backtest trade.

## 7. Assumptions recorded (operator may override)

1. Exit fill = the evaluation bar's close with adverse slippage (mirrors `same_close`). Next-open fills are not modelled.
2. Bracket beats hook on the same bar (intra-bar vs close).
3. Hook error ⇒ hold, never exit.
4. Hook exits are excluded from the stop cooldown.
5. `exit_hook` is a class attribute (fleet loops skip non-hook strategies without reflection).
6. Live time stop (`'max_hold'`) ships with the hook flag, not separately.
