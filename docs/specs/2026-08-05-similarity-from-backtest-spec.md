# Spec — Strategy similarity from BACKTEST data, + dead-path removal

**Status:** DESIGN. Shadow build first, no cutover.
**Author context:** operator directive 2026-08-05. Build to be executed as a shadow.
**Grounded against the tree at commit `d8bb59d`.** Every path, signature, flag and
table below was verified live on 2026-08-05, not recalled.

---

## 0. Why

`S_adj` — the **sole live conviction gate** — takes its strategy-correlation
input from a matrix built on **90 days of live signals**. Measured coverage:

| regime | strategies in matrix | fold_map | block_map |
|---|---|---|---|
| LOW_VOL | 62 | 2 | 15 |
| TRANSITIONING | 54 | 4 | 11 |
| **HIGH_VOL** | **13** | 0 | 0 |
| **CRISIS** | **0** | 0 | 0 |

Pairs absent from the matrix fall back to `SPARSE_DEFAULT = 0.05`
(`src/execution/orthogonalization.py:15`, applied at `:104`) — i.e. treated as
**near-independent**. Understating ρ *inflates* the tangency combination Sharpe,
so `S_adj` is overstated and more tickers clear the `min_corr_cum_sharpe` floor.

**The bias is anti-conservative in precisely the two regimes where strategies
co-move most.** In CRISIS every pair defaults; in HIGH_VOL most do.

Available instead — `strategy_backtest_trades` (23.2M rows) joined to
`strategy_backtest_runs WHERE primary_window`:

| regime | backtest trades | strategies |
|---|---|---|
| TRANSITIONING | 1,675,436 | 184 |
| LOW_VOL | 1,153,777 | 182 |
| HIGH_VOL | 634,537 | 179 |
| **CRISIS** | **227,913** | **168** |

Span 2016-04-11 → 2026-07-22 (10y) vs live 2026-04-10 → 2026-08-04 (~4mo).

---

## 1. Current wiring (verified)

### 1.1 Producer
`src/execution/strategy_similarity.py`

| symbol | line | note |
|---|---|---|
| `REGIME_STATES` | 18 | `('LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS')` |
| `DEFAULT_WINDOW_DAYS` | 19 | `90` |
| `_cofiring_sets_by_regime(window_days)` | 164 | reads **`execution_signals`** → `{regime:{sid:{(iso_week,ticker,dir)}}}` |
| `_returns_by_regime(window_days)` | 189 | reads **`strategy_daily_returns`** → `{regime:{sid:{date:ret}}}` |
| `similarity_for_regime(regime_state, window_days, ...)` | 214 | blends the two |
| `rebuild(trigger, window_days, verbose) -> dict` | 239 | writes all tables |
| `load_groups(regime_state) -> dict` | 316 | `{fold_map, rep_map, block_map, matrix}` |
| CLI | 370-373 | `--rebuild --trigger --window-days --verbose` |

Writes: `strategy_similarity_matrix`, `strategy_fold_groups`,
`strategy_factor_blocks`, `strategy_fold_audit`.

### 1.2 Consumer — the load-bearing path
`src/execution/regime_blended_sizer.py`

```
:1388   _ortho_groups = _ss.load_groups(regime_state)        # UNCONDITIONAL
:1393   if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_FOLD'):   # fold/block application — OFF
:~1442  _sim = (_ortho_groups or {}).get('matrix') or {}     # INDEPENDENT of the flags above
:1472   gate_net_sharpe, _size_adj, ... = _corr_adjusted_maps(ticker_meta, _cw_gate, _cw_size, _sim)
:282    _corr_adjusted_maps(...)  ->  orthogonalization.tangency_net_sharpe   (OPENCLAW_TANGENCY_SADJ != '0')
        -> S_adj  -> gated vs _resolve_min_corr_cum_sharpe(params)  (:227)
```

🔴 **The single most important fact in this spec:** fold/block
*orthogonalisation is retired* (`OPENCLAW_STRATEGY_FOLD=0`), but the **matrix is
still consumed** for `S_adj` at `:~1442`, independently of that flag. Retiring
orthogonalisation did **not** retire the matrix.

### 1.3 Current trigger — ONE, and it is the wrong cadence
`src/agent/curators/weekly_live_sharpe.js:94-95`
```js
console.log('rebuilding strategy similarity (orthogonalization)…');
execSync(`... python3 -m execution.strategy_similarity --rebuild --trigger=weekly_cron --verbose`)
```
Invoked only by `openclaw-weekly-strategy-weights.timer` (`Mon *-*-* 00:00
America/New_York`, `Persistent=true`). Nothing rebuilds similarity when
strategies are **created** or **adjusted** at the weekend — see §4.

---

## 2. The three correlation objects — DO NOT CONFLATE

| module | keyed by | purpose | status |
|---|---|---|---|
| `src/execution/asset_correlation.py` | **ticker** | asset de-gross cluster cap; `price_return_corr(tickers, window=63)`, Pearson on daily price returns | **LIVE** — `asset_corr_cap_enabled=1`, `thr=0.6`, `cap_pct=0.20` in `pipeline_config` |
| `src/execution/strategy_similarity.py` | **strategy** | feeds `S_adj` | **LIVE** — the subject of this spec |
| `src/execution/correlation_matrix.py` | ticker | Phase 2H per-regime ticker matrices | ⛔ **ORPHANED** — see §5 |

`correlation_matrix.py` is **not** what performs asset de-gross, and its
`CRISIS_CORRELATION_PRIOR = 0.7` stress prior **protects nothing** because
nothing imports it.

---

## 3. Build — backtest-sourced similarity (SHADOW FIRST)

### 3.1 Principle: replace ONE leg, not both

`similarity_for_regime` blends two signals. Keep them separate:

* **PnL-correlation leg → move to backtest.** This is the leg starved of data.
* **Co-firing overlap leg → keep live where it exists.** Real production
  co-firing (same ticker, same ISO week, same direction) is genuinely *observed*
  behaviour. Backtest co-firing is a proxy and carries the §3.4 confounds.
* Where live overlap is absent (CRISIS, most of HIGH_VOL), fall back to
  backtest-derived overlap rather than to `SPARSE_DEFAULT`.

### 3.2 New functions (mirror the existing seam exactly)

Add to `strategy_similarity.py`, same shapes as the live siblings so the call
sites are interchangeable:

```python
BACKTEST_SOURCE = os.environ.get('OPENCLAW_SIMILARITY_SOURCE', 'live')  # 'live' | 'backtest' | 'shadow'

def _returns_by_regime_backtest(as_of=None) -> dict[str, dict[str, dict[str, float]]]:
    """{regime: {strategy_id: {date_str: return}}} from strategy_backtest_trades.

    Source: strategy_backtest_trades t JOIN strategy_backtest_runs r
            ON r.run_id = t.run_id AND r.primary_window
    Bucket a trade's pnl_pct onto its EXIT date (realisation), grouped by
    t.entry_regime.  See §3.4(2) on why entry_regime is not the holding period.
    """

def _cofiring_sets_by_regime_backtest(as_of=None) -> dict[str, dict[str, set]]:
    """{regime: {strategy_id: {(iso_week, ticker, direction_int)}}} — same tuple
    shape as the live version, from t.entry_date / t.ticker / t.direction."""
```

`similarity_for_regime()` gains `source: str = 'live'` and dispatches. **No
change to `load_groups()` or to any consumer** — the sizer stays untouched for
the shadow.

### 3.3 Shadow output — the comparison IS the deliverable

Add `--shadow` to the CLI. It must **write nothing to the live tables**. Emit,
per regime:

1. `n_strategies`, `n_pairs` live vs backtest.
2. Pair-level ρ diff **restricted to pairs that currently carry weight** (join
   `strategy_weights_by_regime` for the current run) — median, p25/p75, and the
   count where `|Δρ| > 0.2`.
3. **Signed mean Δρ on live-traded pairs.** This is the decision datum:
   *if backtest ρ is systematically LOWER, the change makes de-gross less
   conservative* and must not ship as-is.
4. Recomputed `S_adj` for the last N sizer cycles under both matrices, and the
   resulting ticker keep/drop set vs the `min_corr_cum_sharpe` floor (1.35
   LOW_VOL — read live, never hardcode).
5. Coverage delta: pairs currently taking `SPARSE_DEFAULT=0.05` that gain a real
   ρ, split by regime.

### 3.4 Three confounds the shadow MUST test

1. **Post-shrink universes differ per strategy.** 50 tier changes were adopted
   2026-08-05, so backtest co-firing reflects each strategy's *chosen* tier —
   two strategies can look uncorrelated because they were simulated on different
   symbol sets, not because they are orthogonal. Live co-firing lacked this
   confound. Mitigation: compute overlap on the **intersection** of the two
   strategies' universes, and report the intersection size alongside ρ.
2. **`entry_regime` is the ENTRY stamp, not the holding period.** A long-horizon
   trade entered in LOW_VOL may realise PnL across a regime change. This is a
   different segmentation from `strategy_daily_returns.regime_state` and will
   move pairs between buckets. Mitigation: report, per regime, the share of
   trades whose `exit_date` falls in a different regime; if material, consider
   attributing daily PnL to the regime *of each day* rather than of entry.
3. **Look-ahead contamination propagates into sizing.** `S_vp_macd` was found
   look-ahead-contaminated once. If any primary run carries that, its
   correlations are wrong and would now feed the conviction gate. Mitigation:
   flag pairs with `|ρ| > 0.9` and any strategy whose backtest Sharpe is a
   distribution outlier, for manual review before cutover.

### 3.5 Cutover gate (all must hold)

- [ ] Signed mean Δρ on live-traded pairs is **≥ 0** (not systematically looser)
      in LOW_VOL and TRANSITIONING, or the loosening is explained and accepted.
- [ ] `SPARSE_DEFAULT` reliance drops materially in HIGH_VOL and CRISIS.
- [ ] `S_adj` keep/drop deltas reviewed against the live floor.
- [ ] §3.4 confounds measured and non-disqualifying.
- [ ] Cutover happens **outside a trading day** — not with the 15:00Z sizer
      pending, and not stacked on another book change.

Cutover = flip `OPENCLAW_SIMILARITY_SOURCE=backtest`. Keep `live` selectable for
one epoch as a rollback.

---

## 4. Wiring — similarity must regenerate on strategy CHANGE, not just weekly

**Requirement:** strategy *adjustments* and strategy *creation* both invalidate
the matrix, so both weekend processes must regenerate it.

### 4.1 Live weekend units (verified)

| unit | fires | ExecStart |
|---|---|---|
| `openclaw-sunday-research-ingest` | Sat 12:00Z | `run_mastermind.js --mode saturday-brain --phase ingest` |
| `openclaw-sunday-research-code` | Sat 18:00Z | `saturday_brain_finisher.js --mark-run-complete --tier-a-cap 6` |
| `openclaw-sunday-code-review` | Sat 22:00Z | `mastermind_code_review.js --state candidate …` |
| `openclaw-weekend-maintenance-sun` | Sun 00:00Z | `run_maintenance.js --mode weekend-sun` |
| `openclaw-weekend-maintenance-sat` | Mon 00:00Z | `run_maintenance.js --mode weekend-sat` |

⚠ The `openclaw-sunday-*` units fire on **SATURDAY** (names kept through the
research-Sat/actuator-Sun swap). Never infer schedule from a unit name here.

### 4.2 Insertion points

**A. After strategy creation/promotion — `saturday_brain_finisher.js`.**
Phase 9 auto-approval (`auto_approval.js`, called from the finisher) is where new
strategies reach `candidate`/`live`. A new strategy has **no** matrix row, so
every pair against it takes `SPARSE_DEFAULT` until the next Monday cron —
up to 2 days of inflated `S_adj`. Add a similarity rebuild as the **last** step
of the finisher, gated non-fatal (same posture as the existing
daily_returns/similarity steps in `weekly_live_sharpe.js`), and **only if the run
actually changed lifecycle state** (skip on a no-op run).

**B. After strategy adjustment — `run_maintenance.js --mode weekend-sat`.**
This is the actuator lane where weights/universes/eligibility move. Rebuild
similarity **after** any universe adoption or activation change, i.e. at the end
of the mode.

**C. Ordering constraint — non-negotiable.**
`strategy_similarity` derives its strategy set from `strategy_weights`
(2026-07-25 finding: before the post-activation weights rebuild, a strategy with
no weight row had no ρ and fell back to `SPARSE_DEFAULT`). Therefore:

```
universe shrink (--adopt --reassign --force)
  -> activation assigner
    -> weights rebuild
      -> similarity rebuild        <-- ALWAYS LAST
```

Any new trigger must sit **after** the weights rebuild in its lane. `--force` is
mandatory on the shrink; see §5.4.

**D. Idempotency + concurrency.** The rebuild must be safe to run twice (the
2026-08-05 catch-up proved the assigner is idempotent; similarity must be too),
and must not run concurrently with itself — take an advisory lock or reuse the
`is_current` flip pattern already in `strategy_similarity.rebuild()`.

---

## 5. Dead-path removal

### 5.1 DELETE — `correlation_matrix.py` and its tests

Verified: no production import anywhere. Only references are
`tests/execution/test_correlation_matrix.py`,
`tests/execution/test_correlation_matrix_per_regime.py`, and two comments
(`orthogonalization.py:15`, `strategy_similarity.py:4,7`).

Delete:
- `src/execution/correlation_matrix.py`
- `tests/execution/test_correlation_matrix.py`
- `tests/execution/test_correlation_matrix_per_regime.py`

Update the two comments to stop referring to a deleted module. **Note in the
commit that `CRISIS_CORRELATION_PRIOR=0.7` dies with it** — it was never wired,
so nothing is lost, but the record should say so explicitly to prevent someone
"restoring" a safety net that never existed.

### 5.2 ~~DELETE — orphan env var~~ — CORRECTION: it does not exist

`OPENCLAW_STRATEGY_BLOCK` is **not in `.env` and not in the code**. An earlier
draft listed it as "present but empty" — that was a misread of an empty `grep`
result (no match) as an empty value. Nothing to delete; `.env` untouched.

**Method note, since this is the failure mode this whole section is about:**
`grep -m1 "^KEY=" .env | cut -d= -f2-` prints an empty string BOTH when the key
is absent and when it is set-but-empty. Distinguish with
`grep -c "^KEY=" .env` (0 = absent) before claiming anything about a flag.

### 5.3 RETIRE — fold/block orthogonalisation (operator-confirmed)

`OPENCLAW_STRATEGY_FOLD=0`; superseded by the corr-adjusted cum-Sharpe process.
Once the backtest matrix ships, decide whether to remove:
- the `if _ortho_groups and _ortho_enabled('OPENCLAW_STRATEGY_FOLD')` branch in
  `regime_blended_sizer.py:1393`;
- `fold_map` / `rep_map` / `block_map` production in `strategy_similarity.rebuild()`;
- tables `strategy_fold_groups`, `strategy_factor_blocks`, `strategy_fold_audit`.

⚠ **Do NOT remove `matrix` production** — it is what feeds `S_adj` (§1.2).
⚠ **Do NOT drop the tables in the same change as the code.** Stop writing them,
leave them in place for one epoch, then drop. Master-data rule: prefer a flag
over a `DELETE`.

### 5.4 FIX (already shipped, recorded here for completeness)

`run_universe_shrink.py` silently skips a strategy when a recommendation exists
for the same `(strategy_id, candidate_set_id)` — no date/run comparison. The
nightly was calling it without `--force` (fixed in `d8bb59d`). **Any new
automated caller must pass `--force`.** Measured: without it,
`adopted=4 skipped_existing=132`; with it, `adopted=46 skipped_existing=0`, and
the assigner's verdict flipped from 77 deactivations / 35 newly-dormant to 10 / 2.
Note `--dry-run` **ignores** the skip, so a dry-run preview does not predict what
an un-forced adopt does.

### 5.5 DO **NOT** remove — and why "= 0" does not mean "stale"

🔴 **`0` is not a synonym for "off" in this codebase.** Three distinct
categories share the value `0`, and only one is deletable. The test that
separates them is **"does anything consume it?"**, never the value.

| category | what `0` means | examples | evidence | action |
|---|---|---|---|---|
| **Mode selector** | `0` **IS the live setting** — it selects the running mode | `OPENCLAW_EOD_SIGNAL_REGISTER` (67 consumers), `OPENCLAW_CLOSE_EXEC_LIVE` (19) | `engine.py:124-132`: *"target_date=T+1 (gate ON) vs same-day target_date=T (gate OFF — the close-exec/same-day semantics)"*. We run same-day, so `0` is correct and load-bearing. `doctor.py:434` fails if both are `1` — mutually exclusive flows. | **LEAVE.** Changing these changes execution semantics. |
| **Revert switch** | off by a live risk DECISION; the flag is the documented rollback path | `OPENCLAW_STRATEGY_CADENCE_STOP_NORM` (8 consumers), `OPENCLAW_REDEPLOY_EXTENDED_HOURS` (7) | `bracket_stacking.py:103`: *"DISABLED IN PRODUCTION since 2026-07-31 … Kept because the flag is the revert path, but understand what turning it back ON does."* Anti-churn (`21a406f`, stops widen ×3). `REDEPLOY_EXTENDED_HOURS` off after measured after-hours slippage. | **LEAVE.** Deleting removes the rollback. |
| **Genuinely dead** | no consumer anywhere | `OPENCLAW_STRATEGY_BLOCK` (**0 refs in `src/`**), `correlation_matrix.py` (**0 imports**) | grep across `src/ scripts/ tests/` | **DELETE** (§5.1, §5.2). |

⇒ Of the five `OPENCLAW_*` flags currently at `0`, **exactly one**
(`OPENCLAW_STRATEGY_BLOCK`) is stale. Verify with:
```bash
grep -rn "$FLAG" --include=*.py --include=*.js --include=*.sh src/ scripts/ tests/ | wc -l
```
A count of 0 is the only evidence that justifies deletion.

The 15 disabled timers (`openclaw-saturday-brain`, `openclaw-mastermind-corpus`,
`openclaw-position-recs`, `openclaw-strategy-review`, `openclaw-paper-expansion`,
`openclaw-weekend-saturday`, `openclaw-weekend-sunday`, …) are **superseded by
the sunday-research split**, not stale artefacts — the underlying modes still
exist on `run_mastermind.js --mode {corpus|comprehensive-review|position-recs|paper-expansion}`
for manual runs. Removing their units removes that capability. Leave disabled;
do not delete.

---

## 6. Risks

| risk | mitigation |
|---|---|
| Backtest ρ systematically lower ⇒ looser de-gross | §3.3(3) signed Δρ is a hard cutover gate |
| Universe mismatch fakes orthogonality | §3.4(1) intersection-only overlap + report intersection size |
| Regime mis-bucketing from `entry_regime` | §3.4(2) measure cross-regime exits first |
| Look-ahead contamination reaches sizing | §3.4(3) outlier review before cutover |
| Cutover on a live day | §3.5 — outside trading hours, not stacked on another book change |
| Deleting a live safety switch | §5.5 explicit keep-list |

## 7. Out of scope

Rewiring `asset_correlation.py` (ticker de-gross) — separate concern, currently
healthy. Any change to `min_corr_cum_sharpe` floors. Re-enabling fold/block.
