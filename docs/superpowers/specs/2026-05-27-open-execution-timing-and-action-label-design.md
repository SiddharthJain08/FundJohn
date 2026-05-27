# Close-Execution (close[t]-proxy) + Action-Label Clarity — Design

**Date:** 2026-05-27
**Status:** Design (final) → writing-plans
**Scope:** Make live execution mirror the backtests as closely as physically possible: generate signals from a **~3 PM `close[t]` price-proxy snapshot** (not prior-day EOD) and **execute market orders into the 4 PM close** — so both the *decision data* and the *fill price* approximate `close[t]`, exactly as `unified_backtest` does (decide from `close[t]`, fill at `close[t]`). Add a dashboard-only SOD price refresh, formalize the collect-free redeploy, **remove the min-signal-count guard** (a legitimately empty signal set → full liquidation is correct), and fix the misleading `direction` label. Pipeline-scheduling + signal-engine + executor + sizer-output change. No strategy-logic change; master parquet stays append-only.

> **Design history:** drafts iterated open-exec → close-exec(EOD) → here. The deciding facts: (1) backtests fill at `close[t]` using `close[t]` data (§3); (2) the system currently feeds signals **prior-day EOD** (`close[t−1]`) — confirmed: GLW had no 2026-05-27 row, only 4/454 tickers had a today-row, and `collector.js:461` *"appends yesterday's slice."* Mirroring the backtest therefore requires feeding signals a **same-day `close[t]` proxy** (a ~3 PM snapshot) and filling at the close. That is this design.

---

## 1. Background — what triggered this

GLW (prior day's top long) "dropped at the open" on 2026-05-27. Investigation: GLW opened flat ($196.00 vs $196.17), fell ~4% intraday, recovered to $189.33. Equity curve: +$569 at the 9:30 open → −$4,066 in the 9:30–9:45 bar → day-low −$5,449 at ~10:30 ET (when the 10:00 cycle traded into the dip) → recovered to −$3,600. Broad market flat (SPY −0.2%); the −3.6% was concentration-driven (GLW 27% NAV the prior day; sizer has no per-ticker cap by design — concentration **out of scope**).

The actionable defect surfaced by this: **live execution is misaligned with the convention every strategy was validated under.** The backtest decides and fills at `close[t]`; live decides on `close[t−1]` and fills the next mid-morning at a different price. This design closes that gap.

Second observation (Part 2): the 05-27 handoff showed GLW `direction: "short"` — actually a `close_only` long *reduction* (`target_usd=+$13,307`), executed correctly. `"direction":"short"` is just the sell-order sign (`regime_blended_sizer.py:528`), which the executor reads as buy/sell (`alpaca_executor.py:481,1205`). Load-bearing for order side, misleading as a label.

---

## 2. Goal / non-goals

**Goal:** signals computed from a `close[t]` proxy (~3 PM snapshot) and **filled at the 4 PM close**, mirroring `unified_backtest`'s decide-and-fill-at-`close[t]` as closely as physically possible (the irreducible gap is ~3 PM proxy vs 4 PM true close — minutes of drift, vs today's ~18-hour, wrong-price lag). Plus: dashboard SOD refresh, collect-free redeploy formalized, min-signal guard removed, human-readable `action` label.

**Non-goals:** no strategy-logic change; no per-name cap (deferred); no shorting/downside-capture; no change to `direction`→side semantics (additive label); no master-parquet mutation beyond the existing EOD append; intraday `*/5` HMM keeps its redeploy-trigger role.

---

## 3. Backtest fill convention (the target we mirror)

- **`unified_backtest.py:537-541`** — `entry_price = sig.entry_price if >0 else ticker_bars.loc[current_date,'close']`: entry = **`close[t]`**, signal from data through `close[t]`; exits walk from t+1.
- **`quick_backtest.py`** — close-to-close returns model.
- **`regime_blended_backtest.py`** — reads backfilled `strategy_regime_backtests` (same close-based fills).

No engine uses open or next-day fills. **Mirror = decide from `close[t]`, fill at `close[t]`.** Since live cannot observe the 4 PM close and trade it simultaneously, the closest physically achievable approximation is: **decide from a ~3 PM snapshot (`close[t]` proxy), fill market-into-the-close (~3:55 PM).** The residual is ~1 hour of intraday drift between the proxy and the true close — negligible for the long-lookback strategies that dominate, and far smaller than the current scheme's full-session, wrong-price lag.

Regime: backtests blend **daily** `historical_regimes`. Live uses the **intraday HMM** (operator decision §5) — at ~3 PM it is fresh and is itself a `close[t]`-proxy regime, consistent with the snapshot philosophy; typically identical to the daily label. Accepted minor live(intraday)/backtest(daily) basis difference.

---

## 4. Part 1 — Close-execution with a `close[t]`-proxy snapshot

Live orchestrator = LangGraph daily cycle (`OPENCLAW_LANGGRAPH_ORCHESTRATOR=1`; `src/agent/graphs/daily-cycle.js`, `STEPS_IN_ORDER = [collect, sentiment, signals, ic_gate, handoff, trade, alpaca, reconcile, report, (pyportfolioopt_shadow), health]`). Split into afternoon compute + close execute:

| Time (ET) | Phase | Steps | Notes |
|-----------|-------|-------|-------|
| **~9:35** | **SOD refresh (new)** | opening-price ingest | **Dashboard only.** Does not feed signals. |
| **~3:10 PM** | **Compute** | `collect → sentiment → signals → ic_gate → handoff → trade` | **`signals` runs against a ~3 PM `close[t]`-proxy snapshot** (§4.1). Produces `output/handoffs/<date>_sized.json`. No execution. |
| **~3:55 PM** | **Execute** | `alpaca → reconcile → report → pyportfolioopt_shadow → health` (every step after `trade`) | **Market orders into the close.** Lead set by `OPENCLAW_CLOSE_EXEC_LEAD_SEC` (default 300 → ~3:55). |
| **after 4 PM** | **EOD refresh (kept)** | existing daily price ingest | Writes the real complete `close[t]` to `prices.parquet` (append-only master) — what tomorrow's history uses. |

### 4.1 The `close[t]`-proxy snapshot (the core new mechanism)

- **New util** `src/ingestion/close_proxy_snapshot.py`: `fetch_close_proxy(universe, asof_date) -> dict[ticker→price]` via `alpaca data latest-trades`/`multi-snapshots` (equity/ETP) + crypto endpoint for crypto tickers. Returns the latest (~3 PM) price per ticker.
- **Engine injection** in `engine.py:load_prices`: when `OPENCLAW_CLOSE_PROXY_SNAPSHOT=1` (default ON for the live close-exec compute), after building the wide close panel, **append a transient `date[t]` row** populated from the snapshot, then proceed. Strategies then compute signals with `iloc[-1]` = today's `close[t]` proxy — matching the backtest's `close[t]` decision.
- **Master untouched:** the snapshot row is in-memory only; `prices.parquet` is **not** written here. The post-4 PM EOD refresh writes the real `close[t]` (append-only invariant preserved).
- **Failure handling (replaces the min-signal guard's safety role):** if the snapshot fetch fails or returns empty, the `signals` step **raises/aborts** — the cycle stops, no handoff is written, positions are preserved. A *failed* fetch must never degrade to an empty signal set. Per-ticker missing snapshot → that ticker simply has no `date[t]` value (NaN); strategies already tolerate missing data (carry the prior close).
- **Redeploys keep EOD, no snapshot, no collect** (§7): redeploys are off-backtest intraday risk actions; the proxy is for the daily backtest-mirroring cycle only.

### 4.2 Regime — intraday HMM (already wired)

The intraday `*/5 9-19` HMM (`run_intraday_market_state.py:292-318`) writes the same `regime_latest.json` + `market_regime` the engine/sizer read. At ~3 PM these are fresh from the intraday model, so the compute **reads the intraday regime with no rewiring**. Daily HMM unchanged (9 AM seed + `historical_regimes` for backtests). Dependency: `*/5` HMM healthy by 3 PM (existing `doctor.regime_freshness`).

### 4.3 Cron changes (`src/engine/cron-schedule.js`, `timezone:'America/New_York'`)
- Add SOD refresh `'35 9 * * 1-5'` (dashboard ingest).
- Replace single `'0 10 * * 1-5'` with **`'10 15 * * 1-5'`** (compute) + **`'55 15 * * 1-5'`** (execute).
- EOD refresh timer + intraday `*/5 9-19` HMM unchanged.

---

## 5. Decisions locked (operator, 2026-05-27)

- **Signal data = ~3 PM `close[t]` proxy snapshot** (not prior EOD). The closest mirror of the backtest's `close[t]` decision.
- **Execute into the 4 PM close** (market orders; lead default 300 s, knob to tighten). MOC/OPG unavailable (removed 2026-05-19; ~7% paper fill).
- **Regime = intraday HMM** (fresh mid-RTH; ~`close[t]` proxy regime; typically == daily).
- **Refreshes:** keep EOD; add SOD (dashboard-only).
- **Redeploy:** confirmed collect-free; formalize (§7); stays on EOD.
- **Min-signal-count guard: REMOVED.** A legitimately empty signal set → full liquidation is **correct** (strategies going to cash). Safety against *false* empties is handled by step-error-abort (§4.1), not a count heuristic.
- **Sentiment staleness: ACCEPT** (`OPENCLAW_CONFIRMER_SENTIMENT=1`; at ~3 PM sentiment is *more* complete than at the open).
- **Rollout: flip live** after a one-shot pre-flip parity diff (§10).

---

## 6. Signal parity / fidelity

Mirrors the backtest on both axes now: **decision data** ≈ `close[t]` (3 PM proxy) and **fill** ≈ `close[t]` (close-into). The only residual is the ~1 h proxy/close drift (immaterial for long-lookback strategies; and *materially better* than EOD for short-horizon ones like `S_extreme_intraday_reversal_nasdaq`, which keys off the most recent daily return — the proxy gives it today's move, EOD would give yesterday's). Empirical gate before flip: §10.

---

## 7. Redeploy: collect-free (formalized) + SOD-for-redeploy (declined)

`redeploy_pipeline.py:158` already runs `REDEPLOY_STEPS='signals,handoff,trade,alpaca,reconcile'` — **no `collect`**, because the prior EOD is already filled. **Formalize + regression-test** so `collect` is never reintroduced (it would slow intraday execution with no benefit). No step-list change.

**SOD-for-redeploy: declined** — redeploys are off-backtest intraday risk actions (no backtest convention to mirror); open prices are a basis mismatch vs close-calibrated strategies; sizing already uses live quotes. SOD stays dashboard-only. Redeploys also do **not** take the `close[t]`-proxy snapshot (that's the daily mirroring cycle's job); they keep using the already-filled EOD for speed.

---

## 8. Safety guards

1. **Step-error-abort (replaces the removed count guard's safety role).** Any compute-phase step failure — including a failed `close[t]`-proxy snapshot fetch — **aborts** the cycle: no handoff, positions preserved. Distinguishes *empty-by-design* (signals computed, none qualified → liquidate, correct) from *empty-by-failure* (step crashed → abort, don't liquidate).
2. **Handoff-freshness gate (execute phase).** Execute refuses unless `output/handoffs/<date>_sized.json` exists with `<date>` = today.
3. **Concurrency lock (execute phase).** The intraday `*/5` HMM ticks in the execute window; the execute phase sets a short-TTL lock `execute:close:inflight:{date}` (TTL ~5 min); `redeploy_pipeline.py` checks it and defers, so a transition can't race the close-execute.

Retained: empty-handoff short-circuit (`regime_blended_sizer_live.py:212`), regime stale-gate (`ENGINE_REGIME_FAIL_HOURS=80`), `no market_regime` abort (`:244`). **Removed:** min-signal-count gate (per decision).

---

## 9. Part 2 — Action-label clarity (additive)

Leave `direction` (long/short) untouched (executor reads it as buy/sell: `alpaca_executor.py:481,740,1205`; passthrough in `trade_handoff_builder.py`). **Add** an `action` field to each sized order, derived from `current_usd`/`target_usd`/`close_only`/kind:

| Condition | `action` |
|-----------|----------|
| new, target >0 / <0 | `open_long` / `open_short` |
| same-sign, \|target\|>\|current\|, long/short | `add_long` / `add_short` |
| same-sign, \|target\|<\|current\|, long/short | `reduce_long` / `reduce_short` |
| opposite sign (flip_open) | `flip_to_long` / `flip_to_short` |
| orphan_close / target 0 | `close_long` / `close_short` |

GLW 05-27 → `reduce_long`. **Surface `action`** (not `direction`) in #trade-reports (`_post_executor_summary`) + dashboard positions/trades. One regression test (eight cases + `direction`/side unchanged).

---

## 10. Rollout (flip-live with one-shot parity gate)

1. Implement in a git worktree, TDD (symlink `data/master`; force `OPENCLAW_DIR`→worktree for real runs; `chown`→`claudebot`).
2. **Pre-flip parity (one trading day):** run the new ~3 PM compute (dry-run, `PIPELINE_DRY_RUN=1`) with the `close[t]`-proxy ON; confirm it produces a healthy signal set and a sane handoff; sanity-diff vs the same day's 10:00 EOD-based handoff (expect *intended* differences — fresher data — not breakage). Abort flip on degenerate output.
3. Flip crons (add SOD `'35 9'`, compute `'10 15'`, execute `'55 15'`, remove `'0 10'`); set `OPENCLAW_CLOSE_PROXY_SNAPSHOT=1`; restart `johnbot`; regenerate integrity manifest on VPS (don't commit it).
4. Watch first live afternoon: 3 PM handoff with proxy-based signals, ~3:55 PM market orders filled into the close, #trade-reports shows `action` labels.

**10:00 cron removed** (replaced by 3:10/3:55 PM pair).

---

## 11. Testing

1. **`close_proxy_snapshot`:** returns latest price per ticker; partial/missing handled; **fetch failure raises** (not empty).
2. **`load_prices` injection:** with flag ON + a fake snapshot, the wide panel gains a `date[t]` row = snapshot prices; flag OFF → byte-identical to today (EOD-only).
3. **Step-error-abort:** a raised snapshot/collect/signals error aborts the cycle → no handoff written; positions untouched.
4. **Empty-by-design liquidation:** signals computed but none qualify → sizer emits orphan-closes (full liquidation) — assert this is NOT blocked (guard removed).
5. **Handoff-freshness gate:** stale/missing handoff → execute skips.
6. **Concurrency lock:** execute holds `execute:close:inflight:{date}`; concurrent transition defers redeploy.
7. **Step-subset runner:** compute runs `collect`…`trade`; execute runs from `alpaca` on.
8. **Redeploy stays collect-free:** assert `REDEPLOY_STEPS` has no `collect`.
9. **`action` derivation:** eight cases; `direction`/executor side unchanged.
10. **SOD refresh:** writes opening prices to the dashboard surface; does not touch the signal-input path.

Regression: executor (bracket, ext-hours, flips, reconcile, DTBP guard) + sizer + redeploy tests stay green.

---

## 12. Config / env

Two gates, **both default OFF**, flipped together at go-live (after §10 parity) so that **merging the code does not change live trading** (inert-until-flip, like `OPENCLAW_LANGGRAPH_ORCHESTRATOR`):
- `OPENCLAW_CLOSE_EXEC_LIVE` — schedules the compute(3:10)/execute(3:55)/SOD crons; OFF keeps the existing 10:00 cron byte-identical.
- `OPENCLAW_CLOSE_PROXY_SNAPSHOT` — injects the ~3 PM `close[t]` proxy into signal generation; OFF → EOD-only (byte-identical / backtest determinism).
- `OPENCLAW_CLOSE_EXEC_LEAD_SEC` — seconds before 16:00 ET to release market orders. **Default `300`.**
- No new secrets. `.env` via `printf >>`.

---

## 13. Out of scope / deferred

- **Per-name concentration cap** — the magnitude amplifier; operator deferred.
- **Downside capture / shorting reversals** — strategy-design question.
- **Tightening `CLOSE_EXEC_LEAD_SEC` toward 0** — revisit if ~3:55 PM fills are reliable.
- **Persisting the proxy snapshot to a history table** — not needed; EOD refresh is the source of truth.
