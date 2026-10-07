# Backtest universe parity epoch — operator runbook (Phase 3, 2026-10-06)

Spec: `docs/specs/2026-10-04-universe-security-type-spec.md` §3. Brief:
`.superpowers/sdd/2026-10-04-universe-security-type/phase3-epoch-brief.md`.
Model: the 2026-09-10 target-geometry epoch (checkpoint → drop-in + weekend unit → read-only gate → flip).

**Goal.** Every fleet backtest bounds its universe by the strategy's OWN manifest `universe_filter_ref`
(`OPENCLAW_BT_UNIVERSE_FILTER_REF=1`, set on the FLEET units only; the code default stays OFF; the live engine is
untouched), the fleet is re-gated once, and weights/activation are rebuilt only after the operator has read the
before/after report.

**Tooling (all in this branch, nothing started or installed by it).**
`scripts/epoch_universe_parity.sh` (subcommands `checkpoint | rotate | artifact | install | status | report | uninstall`),
`scripts/universe_parity_gate.py` (read-only), `scripts/universe_parity_report.py` (read-only).
`unified_backtest.config_json` now carries `universe_bound_source` (`cap` | `filter_ref` | `none` | `explicit`),
`universe_filter_ref_tier`, `universe_bound_tier`, `universe_filter_ref_unresolved`.

Every mutating subcommand prints its plan and does nothing until `--run` is added (`checkpoint`, `status`, `report`
execute directly; `--dry-run` prints them instead). The script never sources or cats `.env`.

## Preconditions (step 0 — check, do not skip)

1. Phase 2 opt-in applied (the 21 strategies in `phase2-optin-audit.json` now point at `stocks_liquid`); this branch merged to `main` by the operator
   (the `unified_backtest.py` change must be live before the first epoch run).
2. **Rank flags re-derived BEFORE the artifact build** (the artifact reads them): `scripts/rederive_rank_flags.py --apply` has been applied (passes 2026-10-05 and
   2026-10-07). Verify read-only: `python3 scripts/rederive_rank_flags.py --from 2026-08-24` (dry-run, no `--apply`) must report `rows_to_update=0`.
3. No other epoch in flight (ruling R1): `systemctl is-active openclaw-fleet-overnight-resume.service 'fleet-*'` all inactive, and
   `python3 scripts/target_mode_flip_gate.py` is settled. **`openclaw-fleet-overnight-resume.timer` is ENABLED (Mon-Fri 21:30Z -> 10:30Z): do steps 1-4 in ONE sitting
   between 11:00Z and 21:00Z, after `systemctl is-active openclaw-fleet-overnight-resume.service` prints `inactive`.**
4. **`.env` must not define `OPENCLAW_BT_UNIVERSE_FILTER_REF`** — `EnvironmentFile=` wins over `Environment=`, so a stale `=0` there silently defeats the epoch.
   `install` warns if it finds the name (names only; values never printed).
5. Other backtest writers. Verify both report `disabled` (they are today): `systemctl is-enabled openclaw-backtest-refresh.timer openclaw-strategy-backtest-refresh.timer`.
   If either is enabled, `systemctl disable --now` it (a plain `stop` does not survive a reboot) and re-enable at close-out.
   * `openclaw-backtest-refresh` (Sat 06:00Z) runs `src/maintenance/refresh_backtests.sh` = `unified_backtest --all-live` (unflagged) THEN `eligibility_assigner --all` — the latter is REAL actuation.
   * `openclaw-strategy-backtest-refresh` (Sun 06:00 America/New_York) runs `scripts/backfill_regime_backtests.py --states live`.
   * Unflagged ad-hoc writers cannot be disabled: the dashboard per-strategy re-backtest, the staging approver and the weekend research finisher. Their mid-epoch rows
     appear in the gate as lagging `source=none` ("ref failed open") — visible, not silent — and the strategy's next fleet run supersedes them.
6. `/root/fleet_overnight_resume.sh` has `ALLOW_ACTUATION=0` (verified 2026-10-07); it stays 0. Reaching uniform does NOT auto-run `run_universe_shrink --adopt --reassign --force`.

## Ordered steps (UTC; the box is Etc/UTC)

Steps 0-4 happen in ONE sitting, 11:00Z-21:00Z, nightly service inactive. **Once the drop-in is installed AND the ledgers are rotated (step 4), the next 21:30Z nightly
starts the flagged epoch by itself** (done=0 + `OPENCLAW_BT_UNIVERSE_FILTER_REF=1`); the weekend unit only adds a window. `rotate` therefore REFUSES unless the drop-in is
installed under `/etc/systemd/system` (`--force-order` overrides for tests/emergencies).

| # | Command | Expected duration | Notes |
|---|---|---|---|
| 0 | Preconditions above | minutes | rank flags `rows_to_update=0`; nightly service inactive; both refresh timers disabled; `.env` has no flag. |
| 1 | `scripts/epoch_universe_parity.sh checkpoint` | seconds | Copies `data/.refresh_backtests.done{,.failed}` to `.pre-universe-parity-<YYYYMMDD>`; refuses to overwrite. |
| 2 | `scripts/epoch_universe_parity.sh artifact` (read the plan) then `... artifact --run` — **wait for completion** | minutes (estimate, not measured; MemoryMax 3500M, Nice 19) | Transient-unit build of `data/universe_tier_membership_shrink-<YYYYMMDD>.parquet` (`--start 2016-03-01 --end <today>`); refuses to overwrite. `_bounded_resolver` prefers the newest `shrink-*` file, so the capped strategies and the new fallback both see the corrected month-ends and `stocks_*` tiers. Sanity: the parquet must contain `stocks_sp500, stocks_r1000, stocks_r3000, stocks_liquid`. |
| 3 | `scripts/epoch_universe_parity.sh install --deadline <YYYY-MM-DDT10:30> --on-calendar "<Sun 08:05:00> UTC"` then the same with `--run` | seconds | Writes the nightly drop-in (`/etc/systemd/system/openclaw-fleet-overnight-resume.service.d/universe-parity.conf`, plus the `docs/systemd` snapshot) and `fleet-universe-parity-epoch-<date>.service` + `.timer`; daemon-reload. **Starts/enables nothing.** Then `systemctl start fleet-universe-parity-epoch-<date>.timer` so the weekend window fires (the timer must be started once; it is not enabled to survive a reboot — re-start it after any reboot). Default RuntimeMaxSec 99000 s (27.5 h). |
| 4 | `scripts/epoch_universe_parity.sh rotate` then `... rotate --run` | seconds | **Refuses unless step 3 `--run` happened.** Moves the live ledgers to `.rotated-<tag>` (only if byte-identical to the checkpoint) so the driver sees `done=0`, and **stamps the rotate time** (ISO UTC) into `data/.refresh_backtests.done.pre-universe-parity-<date>.since`; the gate and report read that file as the default epoch start (override with `--since`). Note the printed time. |
| 5 | Commit the snapshot: `git add docs/systemd/openclaw-fleet-overnight-resume.service.d/universe-parity.conf && git commit` (on `main`, operator) | — | `install --run` wrote it. Remember `install_systemd.sh` installs every drop-in in that directory — remove it from the snapshot at close-out. Pre-existing drift (not ours): the snapshot dir also holds `target-atr-r.conf`, `rf-macro.conf`, `onfailure.conf` while `/etc` holds only `oom-continue.conf`. |
| 6 | `scripts/epoch_universe_parity.sh status --since <rotate time>` (daily) | seconds | Runs the gate: `OK` / `NOT_YET` + detail. **"Uniform" means gate G1 OK** — NOT the nightly log's "outstanding" count: 2 quarantined strategies never reach 0 (and `ALLOW_ACTUATION=0` anyway). |
| 7 | When G1 is OK (G2 OK or accepted): `scripts/epoch_universe_parity.sh report --since <rotate time>` | seconds | Writes `docs/superpowers/plans/universe-parity-report-<date>.{csv,md}` (never overwrites). |
| 8 | **OPERATOR REVIEW CHECKPOINT** — read the report. | — | Nothing in steps 0-7 changes weights or activation. |
| 9 | Only after sign-off: `python3 scripts/run_universe_shrink.py --adopt --reassign --force` -> weights rebuild -> floor recheck -> `activation_assigner --all` (preview first) -> re-enable `openclaw-weekly-strategy-weights.timer` / `OPENCLAW_ACTIVATION_ASSIGNER`/`AUTO_DEMOTE` per the standing owed-sequence. | per standing runbooks | Operator-gated. |
| 10 | Close-out: `scripts/epoch_universe_parity.sh uninstall --date <epoch YYYYMMDD> --run [--restore-ledgers]` | seconds | `uninstall` never defaults the date to today: without `--date` it globs `fleet-universe-parity-epoch-*` under `/etc/systemd/system` and the `pre-universe-parity-*` checkpoint under `data/` and refuses if more than one epoch matches. Also stop the weekend unit/timer if running; re-enable any refresh timer disabled in step 0. To keep the fallback permanent for ad-hoc/candidate backtests, set `OPENCLAW_BT_UNIVERSE_FILTER_REF=1` in `.env` BEFORE removing the drop-in (a later change; the code default stays OFF). |

### Fleet size and retry behaviour

117 runnable strategies (105 live + 10 candidate + 4 staging − 2 quarantined), ordered live first (alphabetical) then candidates/staging. Only `.refresh_backtests.done` skips
a strategy: failures and OOMs are logged to `.done.failed` and retried at every nightly. Serial cost at the last epoch's ~25 min average is ~49 h; the bounded universes should
run faster than the old full panel (measure).

### Schedule (controller follows this)

* **Wed 2026-10-07 ~14:00Z** — steps 0–4 in one sitting (the nightly service must be `inactive`; 21:30Z nightly is the first epoch run, with the flag).
* **Nightlies Wed 10-07, Thu 10-08, Fri 10-09** (21:30Z -> 10:30Z) run the flagged epoch from the drop-in.
* **Saturday 10-10** — do NOT start anything: Sat 12:00Z through ~00:00Z belongs to the research chain (sunday-research split swapped onto Saturday). The Friday nightly
  ends 10:30Z Saturday, clear of it.
* **Weekend window unit Sun 2026-10-11 08:05Z -> Mon 10-12 10:30Z** (`--deadline 2026-10-12T10:30`; created in step 3 with `--on-calendar "2026-10-11 08:05:00 UTC"`).
* **Expected uniform (G1 OK) ≈ Mon 2026-10-12**; then `report --since <rotate time>` -> operator review -> activation apply (step 9).

### The gate (`scripts/universe_parity_gate.py`; step 6)

Epoch start = `--since YYYY-MM-DD[THH:MM]`, else the rotate time stamped in `.refresh_backtests.done.pre-universe-parity-<date>.since`, else the date of the newest checkpoint file.
* **G1 uniformity** — each manifest-`live` strategy's LATEST primary run (run_at >= epoch start) has `config_json.universe_bound_source` in
  `cap`/`filter_ref`; or the strategy has neither `universe_filter_ref` nor `backtest_universe_cap`, in which case `none` is accepted and listed separately
  ("static by design"). Lagging = no key (pre-epoch code), older than the epoch start, `explicit`, or `none` with a manifest ref (the ref failed open).
  At most `--max-lagging` (default 3) exceptions.
* **G2 sanity** — pre-epoch row = the latest row of the same `window_kind`, primary or not, dated before the epoch start (the fleet demotes the previous row
  to `primary_window=false` on every new run, so baselines are non-primary — same lesson as the 09-17 atr_r G2 fix). Median Δ `total_sharpe` >= `--min-median-dsharpe`
  (−0.10) and count(Sharpe>0 after) >= `--min-positive-frac` (0.90) x count before; needs >= `--min-pairs` (20).
* NOT_YET on G2 is not a failure: it means the operator reads the numbers (report) and decides.

### The review (step 8)

Per live strategy the report lists pre vs first post-epoch run: universe size, bound source/tier, total Sharpe/trades, per-regime Sharpe, and the
per-regime activation verdict under the bench rule for both runs (computed with `activation_assigner._judge`, strict/no hysteresis band, the bench vector
from `load_bench_sharpe`; the CURRENT live eligibility is shown beside it), plus an implication line (would be DEACTIVATED / ACTIVATED in regime X, goes
DORMANT, Sharpe turned non-positive, ref failed open). Sorted by |Δ Sharpe|. Look for: strategies that lose all regimes (they drop out of the sizer on the next
assigner run), universes that collapsed (`post_universe_size`), and the 9 capped strategies (pure artifact effect).

## Rollback

* Before the first nightly after step 4: `scripts/epoch_universe_parity.sh uninstall --date <YYYYMMDD> --run`; the live ledgers are in `.rotated-<tag>` — `mv` them back (or `uninstall --date <YYYYMMDD> --run --restore-ledgers`,
  which restores from the checkpoint only if no live ledger exists). The new artifact file is additive and harmless to leave (the capped strategies pick it up on their
  next run either way).
* Mid-epoch: `systemctl stop fleet-universe-parity-epoch-<date>.service`, `uninstall --date <YYYYMMDD> --run`. Rows already written stay (append-only history; the canonical row per
  strategy is whatever ran last). To resume the OLD universe semantics for the not-yet-rerun strategies simply do nothing — they are still on the pre-epoch rows.
* Nothing in this runbook touches weights, activation, the manifest, or the live engine, so there is nothing live to roll back before step 9.

## Known caveats

* **The 9 explicitly capped strategies re-epoch too**: they bind via `backtest_universe_cap`, but the artifact is new (corrected month-ends + `stocks_*`),
  so their numbers move even though their cap did not. They record `universe_bound_source='cap'`.
* **The 21 Phase-2 strategies** now bind to `stocks_liquid` (security-type aware) — their universe shrinks relative to the old `tier_liquid`; expect the largest
  Δ there. They require the `stocks_liquid` tier to be present in the artifact (step 2 check).
* **Strategies with no `universe_filter_ref` stay on the static universe** (`universe_bound_source='none'`) — by design; listed separately by the gate.
* A tier absent from the artifact, or a missing artifact, FAILS OPEN to the static universe with a `WARNING ... tier '<x>' not found` line in the backtest log;
  the gate flags it as "ref failed open".
* Caller-supplied resolvers (grid cells / coupling overrides) record `universe_bound_source='explicit'`; the fleet never passes one.
* The report judges activation strictly (prior eligibility unknown => no hysteresis band) — near-threshold cells can differ from what the assigner would
  keep under hysteresis; the `current_eligible` column is the live truth.
* `primary_window`: `run_backtest` always writes the new run `primary_window=TRUE` and demotes the strategy's previous rows, so every post-epoch row is primary
  until superseded and every pre-epoch baseline is non-primary.

### Execution log — 2026-10-07 (controller)
- 20:44Z checkpoint `pre-universe-parity-20261007` (done 135 lines / failed 47). Preconditions all green (nightly service inactive; both refresh
  timers disabled; `.env` lacks the flag; `rederive_rank_flags` dry-run `rows_to_update=0`).
- 20:44Z `artifact --run` was OOM-KILLED at 3.4 GB in 14 s: the 20:19Z EOD collect had appended to `prices.parquet`, which invalidates
  `data/cache/coverage_index_counts.parquet` (freshness key = mtime+size), and the rebuild reads 19M (ticker, date) rows into pandas.
  **Lesson for future epochs:** warm the coverage cache FIRST under a larger cap, then build:
  `systemd-run --wait --pipe --collect --property=EnvironmentFile=/root/openclaw/.env --property=Nice=19 --property=MemoryMax=5200M
   --setenv=PYTHONPATH=/root/openclaw/src python3 -c "from src.strategies.coverage_index import CoverageIndex;
   CoverageIndex.from_parquet('data/master/prices.parquet')"` (20 s). The artifact build then peaks at ~290 MB (1 min 29 s).
- 20:47Z artifact `universe_tier_membership_shrink-20261007.parquet` built: 8 tiers (4 ladder + 4 stocks_*), 2016-03-31 → 2026-10-07, 1,024 rows.
- 20:47Z `install --run` (drop-in on openclaw-fleet-overnight-resume + weekend unit/timer, deadline 2026-10-12T10:30, OnCalendar Sun 2026-10-11 08:05Z);
  `systemctl start fleet-universe-parity-epoch-20261007.timer` (next Sun 08:05Z); the nightly unit shows `OPENCLAW_BT_UNIVERSE_FILTER_REF=1`.
- 20:47Z `rotate --run`; epoch start stamped `2026-10-07T20:47`. The 21:30Z nightly starts the flagged re-gate.
