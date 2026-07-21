# Universe Ladder Selection Campaign — Implementation Plan (2026-07-21)

Handoff plan for re-selecting every strategy's trading universe from the corrected
full-universe re-backtest. Authored by BotJohn (Opus) for the follow-on (fable) session.
Operator directives are in **bold**. Cross-refs: memory `project_universe_ladder_selection_campaign`,
`project_fleet_rebacktest_and_trade_factor`; `/root/.learnings/ERRORS.md` ERR-20260721-002.

---

## 0. Goal

The fleet re-backtest runs every strategy on the **full ~12,536-ticker static universe**
(the CLI `unified_backtest --strategy-id` never wires the resolver → `run_backtest`'s
`resolver=None` → `bar_universe = static_universe`). A full-universe backtest is a
**superset**: any sub-universe's metrics come from **filtering the stored per-trade rows**,
no re-run. Re-select each strategy's `universe_filter_ref` from these trades.

**Operator rules:**
1. **Shrink the stored trades down a ladder — do NOT re-run per tier.** (The current
   recommender re-runs; this campaign replaces that.)
2. **Prefer the LARGEST universe; move to a smaller tier only if it raises Sharpe by
   ≥ 0.1** ("give preference to larger universes if Sharpe is maintained; start largest,
   shrink downward"). ⚠️ The existing selector codes the OPPOSITE (parsimony) — flip it.
3. **On any shrink, `max_dd ≤ class_ceiling` AND `trades ≥ 100` must be MAINTAINED for
   every regime in which they were met in the full-universe backtest.** A smaller tier may
   not be chosen if it breaks a regime's qualification the full universe achieved.
4. **Same rule in the research/strategy-creation pipeline** (always start largest, shrink).
5. **The dashboard must show the CHOSEN-universe metrics**, not the full-universe numbers.
6. **Backfill historical index membership / cap / ADV** (metadata only spans 2021+).

---

## 1. Existing machinery (reuse)

| Piece | Location | Role |
|---|---|---|
| Per-trade rows | table `strategy_backtest_trades` | `run_id, ticker, direction, entry_date, exit_date, pnl_pct, holding_days, entry_regime, …` — the shrink input |
| Metrics from a trade list | `src/backtest/unified_backtest.py::aggregate_metrics(trades)` + `_portfolio_daily_returns(trades)` | Sharpe / DD / trade_count from a list of trade dicts |
| Tier selector | `src/backtest/universe_ladder_selection.py::select_tier(metrics_by_tier)` | `LADDER_TIERS=('sp500','tier_r1000','tier_r3000','tier_liquid')`, `DELTA_SHARPE=0.10`, `MIN_TRADES=30`. **Currently parsimony (narrowest-first) — must flip + add the DD/trades constraint.** |
| Recs I/O | `src/backtest/universe_ladder_recs.py` | `insert_recommendation`, `build_rationale`, discord format/post (keep) |
| Orchestrator | `src/agent/curators/universe_recommender.js` | **Currently RE-RUNS the grid (`spawnSync … --resolver-override <candidate>`, ~line 203). Replace the per-tier re-run with the shrink path.** |
| Adopt | `src/strategies/lifecycle_universe_adoption.py::adopt_universe_recommendation(rec_id)` | atomically writes `manifest.strategies[sid].metadata.universe_filter_ref` |
| Tier predicates | `src/strategies/universe_default.py` | `sp500, tier_r1000, tier_r3000, tier_liquid, no_otc, …` — all read `TickerMetadata` (`in_sp500, in_r1000, in_r3000, market_cap, adv_usd_20d, …`) |
| Resolver | `src/strategies/universe_resolver.py::UniverseResolver`; factory `src/execution/live_universe.py::build_resolver()` | point-in-time `resolve(strategy_id, as_of)`; predicate = manifest `universe_filter_ref` else `DEFAULT_UNIVERSE_FILTER = sp500` |
| Qualification rule | `src/backtest/regime_qualification.py::qualifies_regime(sharpe, trade_count, max_dd_pct)` + `class_thresholds` | Sharpe>0 AND trades≥100 AND DD≤ceiling (equity/etp 20, option 30, crypto 70) |
| Metadata source | table `ticker_metadata_snapshots` | **only 2021-01-31 → 2026** (107 monthly snapshots) — the backfill blocker |
| Timer | `openclaw-universe-recs.timer` | currently **disabled** |

---

## 2. The shrink mechanism (core — this is what replaces the re-run)

For one strategy's latest `primary_window=TRUE` full-universe run (its `run_id`):

1. **Load its trades:** `SELECT ticker, entry_date, exit_date, pnl_pct, holding_days, entry_regime FROM strategy_backtest_trades WHERE run_id = :run_id`.
2. **For each ladder tier T and each regime r:** keep trades where `entry_regime = r` AND the
   trade's `ticker` satisfies tier T's predicate **at `entry_date`** (point-in-time). Then
   `m = aggregate_metrics(kept_trades)` → `{sharpe, max_dd_pct, total_trades, …}`.
   - Point-in-time membership = evaluate the tier predicate against `ticker_metadata_snapshots`
     as-of `entry_date` (reuse `UniverseResolver`/`_db_adapters.PostgresMetadataDB.fetch_metadata_as_of`).
     Cache metadata per as_of/month (predicate is cheap once metadata is in hand).
   - Nesting means membership is monotone (sp500 ⊆ tier_r1000 ⊆ tier_r3000 ⊆ tier_liquid ⊆ full),
     so you can bucket each trade once to its **narrowest** tier and roll up.
3. **Build `metrics_by_tier` and per-(tier,regime) `{sharpe, max_dd_pct, total_trades}`.**
4. Feed to the revised `select_tier` (§3). Emit a `strategy_universe_recommendations` row via
   `insert_recommendation`; adopt via `adopt_universe_recommendation`.

Everything needed is already persisted — no backtest re-run. **Blocker:** step 2's point-in-time
membership needs historical metadata (§5 W2); until then only 2021+ trades classify correctly.

---

## 3. `select_tier` changes (W1)

Current (`universe_ladder_selection.py`): starts at **narrowest** eligible, broadens iff
`broader.sharpe − winner.sharpe ≥ DELTA_SHARPE` (parsimony). Two changes:

**(a) Flip to largest-first.** Iterate broadest→narrowest; seed `winner = broadest eligible`;
a **narrower** tier displaces iff `narrower.sharpe − winner.sharpe ≥ DELTA_SHARPE − 1e-9`
(keep the IEEE754 epsilon guard). Net: pick the LARGEST tier no smaller tier beats by ≥0.1.

**(b) Per-regime maintain-constraint.** New input: per-(tier,regime) DD + trades. Compute
`R_full` = regimes where the largest/full universe met `qualifies_regime` (DD≤ceiling AND
trades≥100 — Sharpe>0 is the activation gate, keep it too). A narrower tier is a **valid
displacer only if** for every `r ∈ R_full` it still has `trades ≥ 100` AND `max_dd_pct ≤ ceiling`.
If a narrower tier would drop any `r ∈ R_full` below the bar, it is **disqualified from
displacing** (stay on the larger tier). `class_ceiling` from `regime_qualification.class_thresholds(instrument_class)`.

Keep `DELTA_SHARPE=0.10`, `MIN_TRADES=30` (eligibility floor). Return verdict + comparisons +
the maintained-regime set for the audit/rationale. **Add unit tests** for: largest-first on a
tie (→ largest), a smaller tier winning by ≥0.1 (→ smaller), and a smaller tier disqualified
by dropping a regime below 100 trades / over DD (→ stays larger). Mirror the existing test file.

---

## 4. Work items (ordered)

- **W1 — Flip `select_tier` + add the maintain-constraint** (`universe_ladder_selection.py` + tests).
  Self-contained, no data dependency. Do first.
- **W2 — Backfill historical metadata** into `ticker_metadata_snapshots` to 2016: point-in-time
  `in_sp500 / in_r1000 / in_r3000` (index membership from a provider) + `market_cap` + `adv_usd_20d`
  (derivable from prices+shares). Prerequisite for accurate pre-2021 point-in-time tier membership.
  Operator: "relatively easy." Verify by re-running the §5 resolver check for a 2018 as_of → non-empty.
- **W3 — Build the shrink orchestrator** (§2): new module (or rewrite the grid section of
  `universe_recommender.js`) that reads `strategy_backtest_trades`, buckets trades to tiers
  point-in-time per regime, `aggregate_metrics`, calls the revised `select_tier`, writes a
  recommendation, and (auto-)adopts. **Remove the per-tier `spawnSync` re-run.** Run over the
  corrected fleet trades once the re-backtest is uniform.
- **W4 — Dashboard shows chosen-universe metrics.** Find the dashboard strategy-metrics source
  (`src/channels/api/server.js` / the portfolio + strategy tiles) and point Sharpe/DD/trades at
  the SELECTED tier's shrunk metrics, not the full-universe run.
- **W5 — Research/creation-pipeline parity.** Wherever a new strategy's universe is assigned
  (research pipeline / `auto_backtest` / lifecycle adoption), apply the same start-largest-shrink
  rule off its full-universe backtest instead of a fixed default.
- **W6 — Full-universe casualties (S_ivol).** S_ivol OOMs on the full 12.5k panel (see below),
  so it can't produce full-universe trades to shrink. Give such strategies a bounded backtest
  ceiling (start at `tier_liquid`, not the raw 12.5k static universe) so they complete, then
  shrink from there. Lift its `backtest_quarantine` once it completes on the bounded universe.

---

## 5. Blockers & gotchas

- **Metadata history (W2 blocker).** `ticker_metadata_snapshots` = 2021-01-31 → 2026. For
  `as_of < 2021` the point-in-time query returns 0 rows → 0-ticker tier. Verified live:
  `build_resolver().resolve('S_ivol_mispricing_asymmetry', date(2018,6,15))` → **0 tickers**;
  `date(2023,6,15)` → 476. So pre-2021 tier classification is wrong until W2 lands.
- **`DEFAULT_UNIVERSE_FILTER = sp500`** (`universe_default.py:15`) — a filterless strategy resolves
  to sp500, NOT the full universe. Full-universe backtest happens only because the CLI passes
  `resolver=None` (static universe). This is intended raw material — do NOT "fix" the CLI to wire
  the resolver (that would re-introduce re-runs and the empty-pre-2021 problem).
- **S_ivol OOM (W6).** Vectorized (240min→8.8ms, `284c04f`) + memory-opt (dropped full-panel copy,
  `6dcb876`), both exactly-equivalent + tested, BUT the full-universe backtest still OOM-kills at
  ~4.3 GB — the cost is the **engine's full 12,536-ticker panel baseline + per-bar `prices[tickers]`
  subset**, not `generate_signals`. 8 GB no-swap box shared w/ ~3.3 GB live services can't hold it.
  Fix is the bounded backtest universe (W6), not more `generate_signals` tuning. Quarantined.
- **Point-in-time correctness.** Filtering trades by CURRENT membership = survivorship bias
  (drops delisted names). Use as-of-`entry_date` membership (needs W2). Nesting lets you bucket a
  trade once to its narrowest tier.
- **Universe is per-strategy, eligibility is per-regime.** One `universe_filter_ref` per strategy;
  it then drives per-regime `strategy_regime_params.eligible` (via the activation assigner). The
  selection picks ONE tier; the per-regime constraint (§3b) guards that choice.
- **Coupling to the eligibility system.** After a universe changes, the strategy's per-regime
  eligibility should be re-derived (activation assigner off the SHRUNK metrics). Sequence universe
  selection BEFORE the post-fleet activation rebuild, or re-run the assigner after adoption.

---

## 6. Verification

- W1: unit tests (largest-first tie, ≥0.1 displacement, regime-maintain disqualification).
- W2: `build_resolver().resolve(<strat>, date(2018,…))` → non-empty & plausible (~sp500 size).
- W3: for a strategy, shrink-derived tier metrics ≈ a one-off real backtest on that tier
  (spot-check a couple strategies to validate the shrink equals a re-run within tolerance).
- W4: dashboard Sharpe/DD/trades for a re-universed strategy match the chosen tier's shrunk metrics.
- End-to-end: pick 2–3 strategies, run the full shrink→select→adopt, confirm `universe_filter_ref`
  written and the live resolver returns the new universe.

---

## 7. Session artifacts (already shipped)

- Fleet re-backtest: full-universe, ~102/145 done, running nightly
  (`openclaw-fleet-overnight-resume.timer`, Mon-Fri 21:30 UTC). Produces `strategy_backtest_trades`.
- Eligibility authority unified: DB `strategy_regime_params` is the SOLE live gate; `should_run`
  bypassed live (`0e8b9ee`); manifest `eligible_regimes` = records-only. ERR-20260721-002.
- Coskewness inf-guard fixed + lifted (`298ee9d`/`75c4000`). S_ivol vectorized + memory-opt +
  quarantined (`284c04f`/`6dcb876`). 8 missing `strategy_regime_params` cells seeded FALSE.
- Probes (read-only, scratchpad): `gate_probe.py`, `gate_probe2.py`, `resolver_test.py`.
```
