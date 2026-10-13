# Session handoff — QuantDinger follow-on program (written 2026-10-13 00:4x UTC)

Purpose: let another Claude Code session take over the controller role for the FundJohn/OpenClaw
follow-on program without the prior conversation. Everything below is current as of the timestamp.
The live rolling handoff (`/root/openclaw/.remember/remember.md`, gitignored) is overwritten each
session and is the FIRST thing to read; this document is the durable, committed version.

## 0. Standing rules (operator-set; never relax without the operator)

- **NEVER DELETE from the master DB** (`data/master/*.parquet`, canonical Postgres tables): append-only; deprecation = flag.
- **Never `git reset --hard`**; never `git stash` bare (shared stash). Production = working tree `/root/openclaw` on `main`; timers pick up the tree on their next run, so never leave it half-edited across a timer boundary.
- **Never source or `cat` `.env`.** Transient units get it via `--property=EnvironmentFile=/root/openclaw/.env`; for manual DB access extract one key: `grep '^POSTGRES_URI=' .env | cut -d= -f2-` (there is no `psql` on PATH — use `python3` + `psycopg2`).
- **User-scope johnbot only**: `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot.service`. NEVER start the system-scope unit. Restart johnbot AND `fundjohn-dashboard` (:7870) when served code changes; johnbot also when `.env` gains a flag its subprocesses need.
- **CPU rule (operator 09-17)**: ONE subagent at a time; every test `nice -n 19`, single file, ≤2 runs per file; never the full suite; NO agent/test activity 11:30–13:30Z or 19:00–20:45Z on weekdays.
- **Memory**: 8 GB, no swap. Never load whole `prices.parquet`/`options_eod.parquet`. Heavy one-offs run as `systemd-run` units with `MemoryMax` and `OOMScoreAdjust=1000`. `python3 -m backtest.unified_backtest` under systemd-run NEEDS `--setenv=PYTHONPATH=/root/openclaw/src`.
- **Manifest**: never edit `src/strategies/manifest.json` / `strategy_signatures*.json` on branches; manifest changes only via operator scripts under the manifest lock.
- **Process guards**: anchored `pgrep -f '^(/usr/bin/)?(python3|node) .*(…)'` — unanchored pgrep matches itself.
- **Verify raw values before calling anything an anomaly** (several false alarms were corrected this program).
- **Commit trailer**: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Docs/ledger commits straight to `main` are approved; code goes through the SDD process (brief → sonnet implementer in a git worktree → opus review → fix round → scoped re-review → `merge --no-ff` → restart services when served code/migrations change → ledger + handoff).
- **Harness limitation**: the auto-mode classifier refuses edits to `.env` and `/etc/systemd` from the controller unless the operator explicitly says to run them; otherwise hand them over as `!`-prefixed commands.
- `benchmark_max_nav_frac` (SPY cap, 1.0) and `OPENCLAW_BT_UNIVERSE_FILTER_REF` are operator-decision flags.

## 1. Live state (all verified 2026-10-12/13)

| Item | State |
|---|---|
| Account breaker (`src/execution/account_breaker.py`, spec C1 Amend. 2/2a/2b) | **ARMED** since 10-07 02:59Z (`.env` `OPENCLAW_ACCOUNT_BREAKER=1`); 4 clean armed days through Mon 10-12 (dd −1.1 %, daily −0.8 %, recon 0, rebased 0). DD_LIMIT −10 %, DAILY_LIMIT −3 %. Log `logs/account_breaker_<date>.log`, 5-min ticks. |
| M3 lazy aux loading | LIVE (`OPENCLAW_AUX_LAZY=1`); signals step 50–68 s. |
| Promotion gate | bench-aware (spec 2026-09-25 §9 Amend. 2; kill switch `OPENCLAW_PROMOTION_BENCH_GATE=0`). |
| Activation assigner | bench-aware; Mon 10-12 threshold 0.9378 (LOW_VOL); newly dormant S_covariance_shrinkage_minvar, S_diversity_weighted_price_generated, S_micro_alpha_elastic_net. |
| Universe security-type track | Phase 1 (`security_type`, mig 163), rank regression fix (`OPENCLAW_RANK_COMMON_ONLY`), history repair, Phase 2 opt-in (21 strategies → `stocks_liquid`; S_52wk_low_capitulation_reversal since auto-demoted), Phase 3 parity epoch 20261007 EXECUTED + CLOSED. |
| Backtest universe parity | **PERMANENT**: `OPENCLAW_BT_UNIVERSE_FILTER_REF=1` at the end of `.env` (backup `.env.pre-universe-parity-permanent-20261012`). Fleet drop-in, one-shot weekend unit/timer removed 10-13 00:24Z. Fleet ledgers `data/.refresh_backtests.done{,.failed}` stay rotated (checkpoint `*.pre-universe-parity-20261007`). |
| Epoch gate | OK: live 106 = 92 bound + 14 static-by-design (no `universe_filter_ref`) + 0 lagging (uniform since 10-13 02:00Z). G2 n=103 median ΔSharpe +0.010, positive 50 vs 48. Report `docs/superpowers/plans/universe-parity-report-20261013.{csv,md}` (supersedes 20261011). |
| Beta budget / rule C | LIVE; SPY clamped at `benchmark_max_nav_frac=1.0`×NAV (~$95k); alpha (~$15k) rides on margin. Cap draft `docs/specs/2026-10-07-beta-budget-cap-amendment-draft.md` recommends keeping 1.00. |
| Daily cycle | 11-step base (`collect → coverage_warm → signals → …`); `coverage_warm` added 10-12 by the maintenance agent, committed 10-13 (see §3.1). |
| Regime | LOW_VOL since 10-09 19:15Z (one-tick TRANSITIONING flicker 10-12 19:45Z, no redeploy). |

Recent `main` SHAs: breaker Tasks 2–4 `0c4d3011`/`e428f53c`/`93085923`; promotion gate `cf3b9b4a`; security-type Phase 1 `1dafd5e3`; rank fix `d2756f53`; Phase 2 script `985d9475`; Phase 3 tooling `16594805`; cap draft `a8fd54ff`; epoch install `0df09344`; research `34470687` (Sat 10-10); epoch report `c206be0d`; close-out `b9bdf5b3`; maintenance fix commit `8d114f2f` (10-13).

## 2. In flight at hand-over

- Nothing running. The pairs re-run LANDED 10-13 02:00Z (run `18edc6c6`, Sharpe 0.10 → 0.19, 4 GB peak, 1 h 42 min) and is checkpointed in `data/.refresh_backtests.done`; gate = 92 bound / 14 static / 0 lagging; report regenerated as `universe-parity-report-20261013.{csv,md}`.
- Nightly queue left: `S_low_volatility_us_gk63` (candidate; OOM-killed at 3.4 GB each night — the fleet's `MEM_FLOOR_MB=5000` spawn gate does not cap the child) and `S_first_candle_ema12_breakout` (manifest repointed 10-13 → should now run). Quarantined by design: TR04/TR06.
- WATCH tonight 10-13: first real `coverage_warm` run in the 20:15Z collect cycle (`logs/daily_cycle_steps_2026-10-13.log`), nightly 21:30Z.

## 3. Owed operator decisions

1. ~~Maintenance agent's uncommitted fix~~ — DECIDED 10-13: keep + commit (done). WATCH the first premarket cycles (`logs/daily_cycle_steps_2026-10-13.log`): a `coverage_warm` line, then `signals` must run; a 600 s timeout or OOM of the rebuild would abort the cycle.
2. **Parity report review** — `universe-parity-report-20261011.md` is the deliverable of the universe track. Worst movers S_HV20_iv_dispersion_reversion −6.34, S_HV8 −1.44, S_fama_french −1.01; best S_options_flow +3.52, S_HV19 +3.43, low_volatility_us +2.32 (−2.47 → −0.15, the cash-ETF artefact).
3. **SPY cap** — keep 1.00 (draft `a8fd54ff`); regime-tiered ceiling is the documented alternative.
4. **Noisy nightly candidate**: `S_low_volatility_us_gk63` (OOM-killed at 3.4 GB nightly) — quarantine flag via the operator manifest script, or run it once in a quiet window like pairs. (`S_first_candle_ema12_breakout` was fixed 10-13: the maintenance agent repointed `canonical_file` to the existing `_tv1.py`; committed.) NOTE: the daily maintenance agent (`run_maintenance.js --mode daily`, 16:00Z) edits the live tree but does NOT commit — check `git -C /root/openclaw status` after each run and commit or revert its changes deliberately.
5. **Tier-1 vs tier-2 bench vector drift** (assigner logs "fail-safe tier 2 vector") — decide whether the registry sleeve run should be re-stamped.

## 4. Engineering backlog (from this program; each needs an SDD brief)

- Activation hold / sleeve-last ordering for fleet epochs (LRN-20261011-001 in `/root/.learnings/LEARNINGS.md`): re-running `S_beta_spy` mid-epoch tripped the daily `activation` trigger.
- Wire `prices_30m` into the backtest aux loader so `S_TR04_zarattini_intraday_spy` / `S_TR06_baltussen_eod_reversal` leave `backtest_quarantine`; until then `/root/fleet_overnight_resume.sh` logs "NOT actuating … canonical cannot be uniform" and its post-uniform `run_universe_shrink --adopt --reassign --force` branch never fires.
- Min-trades parity on the promotion gate (`src/lib/promotion_service.js`) vs the assigner's `min_trades=100`.
- Late-fill residual exact fix (`created_at` on lot rows; `src/execution/account_breaker.py` late-fill detector).
- Secondary-line misses for class-letter suffix lines (`src/strategies/universe_meta.py::security_types_from_profiles`).
- `scripts/rederive_rank_flags.py` reproducibility-guard cosmetic.
- 14 live strategies with no `universe_filter_ref` (listed by `epoch_universe_parity.sh status` as static_by_design) + ~6 more non-live.
- `universe_resolution_audit` table never populated (`src/strategies/universe_resolver.py` audit_writer path).
- Silent-strategy system check (`src/system_checks/`), post-merge opus review of M1 (signals-memory ledger), options-surface flip gate recalibration, dead `--throttle` flag, Saturday-night memory measurement.
- Hardening idea for §3.1: make `coverage_warm` non-fatal in the orchestrator (today any rc≠0 aborts the cycle; see `pipeline_orchestrator.py` "Step … failed — aborting pipeline").

## 5. Older open operator actions (pre-dating this window)

23 OnFailure drop-ins (`docs/systemd/*/onfailure.conf` snapshot drift vs `/etc`); macro-events two-pass; IC-screen shadow weekend 2; `scripts/measure_gap_fill_impact.py`; Stream A T8 epoch; 13 orphan `strategy_regime_params` rows (the "live but has no eligible regimes" list printed by the weekly weights run: oxf_price_momentum, oxf_welles_wilder_breakout, S_sector_trend_overlay, S_btc_kelly_powerlaw_vol_decay, S_covariance_shrinkage_minvar, S_crypto_time_series_momentum, S_diversity_weighted_price_generated, …); `S_coint_pairs_sector_v2` absent from the manifest; staging failure `S_quadratic_risk_tangency_mvo`; 'alpaca-snapshot' EOD data-quality question. Runbook with the owed decisions: `docs/superpowers/plans/2026-08-24-five-repo-adoptions.md` (bottom).

## 6. Daily watch (what the controller checks each weekday)

```bash
# breaker (expect armed, recon=0, rebased=0, breach=0, no WARN/ERROR)
grep -cE 'ERROR|WARN' logs/account_breaker_$(date -u +%F).log; grep -E 'armed|halted=1|breach=1' logs/account_breaker_$(date -u +%F).log | tail -n 2
# cycle (19:00Z same-day compute; 19:55Z execute; 20:15Z collect)
grep -nE 'step=|bench_sizing|clamped|ALPACA_EXEC\] Done|bench_realized|coverage_warm' logs/daily_cycle_steps_$(date -u +%F).log | tail -n 20
# fleet nightly (Mon–Fri 21:30Z → ≤10:30Z)
grep -E '\[rebt\] (ok|FAIL|DONE|fleet=)' logs/fleet_overnight_resume.log | tail -n 8
# parity gate / report
bash scripts/epoch_universe_parity.sh status
# weekly weights (Mon 04:00Z) — journal
journalctl --since "<mon> 03:59" --until "<mon> 04:05" -o short-iso --no-pager | grep -E 'activation_assigner\]|strategy_weights'
```

Calendar: premarket 11:30Z/13:00Z; breaker epoch 13:30Z; same-day compute 19:00Z; execute 19:55Z; EOD collect 20:15Z; nightly fleet 21:30Z; weekly weights Mon 04:00Z; Saturday research chain 12:00Z/18:00Z/22:00Z; weekend-maintenance Sun+Mon 00:00Z; BotJohn daily maintenance 16:00Z (OOM-killed 10-12 at 4.4 GB — check it tomorrow).

## 7. Where things live

- Specs: `docs/specs/2026-09-12-quantdinger-adoptions-spec.md` (C1 breaker, AS BUILT), `2026-09-25-activation-bench-relative-spec.md` (§9), `2026-10-04-universe-security-type-spec.md` (Phases 1–3), `2026-10-07-beta-budget-cap-amendment-draft.md`.
- Runbook + execution log of the epoch: `docs/superpowers/plans/2026-10-06-universe-parity-epoch.md`.
- SDD ledgers (gitignored, on disk): `/root/openclaw/.superpowers/sdd/{2026-09-28-breaker-alpha-pnl,2026-09-28-signals-memory,2026-10-04-promotion-bench-gate,2026-10-04-universe-security-type}/progress.md` (+ briefs, diagnostics, backups e.g. `rank_flags_backup_pre_rederive_*.csv.gz`).
- Learnings: `/root/.learnings/LEARNINGS.md` (LRN-20261004-001/002/003, LRN-20261011-001).
- Controller memory: `/root/.claude/projects/-root-openclaw/memory/` (`qd-wave2-status.md`, `cpu-single-agent-rule.md`).
- Changelog: `docs/archive/changelog.md` (newest first; load-bearing rules stay in `CLAUDE.md`).
- Scripts added this program: `scripts/epoch_universe_parity.sh`, `universe_parity_gate.py`, `universe_parity_report.py`, `build_tier_membership.py`, `refresh_fmp_profiles.py`, `report_fund_exposure.py`, `rederive_rank_flags.py`, `apply_stock_universe_optin.py`, `account_breaker.py --rebase TICKER [--realized-adjust X] [--apply]`.

## 8. Lessons that cost time (don't repeat)

- Warm `data/cache/coverage_index_counts.parquet` BEFORE building the tier artifact after a collect (`CoverageIndex.from_parquet` under MemoryMax 5200M, 20 s); the build then peaks ~290 MB instead of OOM at 3.4 GB. The new `coverage_warm` step now does this daily.
- Fleet children are the designated OOM victims (`oom_score_adj 1000`); a slow strategy dies whenever anything else spikes (a `claude` session invoked the 22:04Z kill on 10-12). Re-run long ones in a quiet window as transient units.
- A live→live lifecycle history event resets the 30-day auto-demote grace; provenance belongs in manifest metadata.
- Rotating fleet ledgers before installing an epoch drop-in lets a nightly finish unflagged; the script now refuses.
- Implementer hand-backs can come back as placeholders — verify the branch directly (git log/diff + run the single test file yourself).
