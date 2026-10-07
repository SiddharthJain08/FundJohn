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

## Preconditions (check, do not skip)

1. Phase 2 opt-in applied (the 21 strategies in `phase2-optin-audit.json` now point at `stocks_liquid`); this branch merged to `main`
   by the operator (`unified_backtest.py` change must be live before the first epoch run).
2. No other epoch in flight (ruling R1, never stack epochs): `systemctl is-active openclaw-fleet-overnight-resume.service 'fleet-*'` all inactive,
   and `python3 scripts/target_mode_flip_gate.py` is settled.
3. **`.env` must not define `OPENCLAW_BT_UNIVERSE_FILTER_REF`** — `EnvironmentFile=` wins over `Environment=`, so a stale `=0` there silently
   defeats the whole epoch. `install` warns if it finds the name (names only; values are never printed).
4. Other backtest writers that would drop un-flagged rows into the middle of the epoch: `openclaw-backtest-refresh.timer` (Sat 06:00 UTC,
   `src/maintenance/refresh_backtests.sh`, no flag) and the Saturday weekend maintenance. Check `systemctl list-timers 'openclaw-backtest-refresh*'`;
   if it would fire during the epoch, hold it (`systemctl stop` the timer; re-enable at the end). A flag-less run stamps `universe_bound_source='none'` and
   shows up in the gate as lagging ("ref failed open") — it is visible, not silent, but it wastes a slot.
5. `/root/fleet_overnight_resume.sh` has `ALLOW_ACTUATION=0` (verified 2026-10-07): reaching uniform does NOT auto-run `run_universe_shrink --adopt --reassign --force`.
   Leave it at 0.

## Ordered steps (UTC; the box is Etc/UTC)

| # | Command | Expected duration | Notes |
|---|---|---|---|
| 1 | `scripts/epoch_universe_parity.sh checkpoint` | seconds | Copies `data/.refresh_backtests.done{,.failed}` to `.pre-universe-parity-<YYYYMMDD>`; refuses to overwrite. The tag date is the epoch start the gate/report read (00:00 UTC of that day), so take the checkpoint the day the epoch starts. |
| 2 | `scripts/epoch_universe_parity.sh artifact` (read the plan) then `... artifact --run` | minutes (estimate — not measured; MemoryMax 3500M, Nice 19) | Transient-unit build of `data/universe_tier_membership_shrink-<YYYYMMDD>.parquet` (`--start 2016-03-01 --end <today>`); refuses to overwrite. `_bounded_resolver` prefers the newest `shrink-*` file, so the corrected Aug/Sep month-ends and the `stocks_*` tiers are seen by BOTH the 9 capped strategies and the new fallback. Sanity: the parquet must contain the tiers `stocks_sp500, stocks_r1000, stocks_r3000, stocks_liquid` (an artifact that predates them makes the fallback fail open with a WARNING naming the tier). |
| 3 | `scripts/epoch_universe_parity.sh rotate` then `... rotate --run` | seconds | Moves the two live ledgers to `.rotated-<tag>` (only if byte-identical to the checkpoint) so the driver sees `done=0` (as the 09-06 rotation did). The checkpoint copies stay. |
| 4 | `scripts/epoch_universe_parity.sh install --deadline 2026-10-xxT10:30 --on-calendar "2026-10-xx 08:05:00 UTC"` then the same with `--run` | seconds | Writes the nightly drop-in `docs/systemd/openclaw-fleet-overnight-resume.service.d/universe-parity.conf` (copied to `/etc/systemd/system/...`) and `fleet-universe-parity-epoch-<date>.service` (+ `.timer` with `--on-calendar`); daemon-reload; **does not start or enable anything** — it prints the start command. Pick the deadline like the 09-13 unit (Sunday 08:05Z start, `--deadline` Monday 10:30Z, default RuntimeMaxSec 99000 s = 27.5 h). |
| 5 | `systemctl start fleet-universe-parity-epoch-<date>.timer` (or `.service`) | weekend window ~26 h | The Mon-Fri 21:30 nightly (`openclaw-fleet-overnight-resume`) continues with the drop-in until uniform. Serial cost at the last epoch's average is ~128 strategies x ~25 min ≈ 53 h, so uniform is expected ≈ Wed after a Sat/Sun start (measure; the 21 `stocks_liquid` strategies and the ~95 now-bounded ones should run FASTER on smaller universes). |
| 6 | `scripts/epoch_universe_parity.sh status` (daily) | seconds | Runs the gate: `OK` / `NOT_YET` + detail (below). |
| 7 | When the gate prints OK (or NOT_YET with a short list of exceptions the operator accepts): `scripts/epoch_universe_parity.sh report` | seconds | Writes `docs/superpowers/plans/universe-parity-report-<date>.{csv,md}` (never overwrites). |
| 8 | **OPERATOR REVIEW CHECKPOINT** — read the report. | — | See below. Nothing in steps 1–7 changes weights or activation. |
| 9 | Only after sign-off: `python3 scripts/run_universe_shrink.py --adopt --reassign --force` -> weights rebuild -> floor recheck -> `activation_assigner --all` (preview first) -> re-enable `openclaw-weekly-strategy-weights.timer` / `OPENCLAW_ACTIVATION_ASSIGNER`/`AUTO_DEMOTE` as in the standing owed-sequence. | per the standing runbooks | Operator-gated; the nightly script stays `ALLOW_ACTUATION=0`. |
| 10 | Close-out: `scripts/epoch_universe_parity.sh uninstall` then `--run`. | seconds | Removes the drop-in + epoch unit(s). If the operator wants the fallback permanent (so ad-hoc/candidate backtests also use it), set `OPENCLAW_BT_UNIVERSE_FILTER_REF=1` in `.env` BEFORE removing the drop-in (a later change; the code default is deliberately still OFF). |

### The gate (`scripts/universe_parity_gate.py`; step 6)

Epoch start = `--since YYYY-MM-DD[THH:MM]` or the date of the newest `.refresh_backtests.done.pre-universe-parity-<date>` checkpoint file.
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

* Before step 5: `scripts/epoch_universe_parity.sh uninstall --run`; the live ledgers are in `.rotated-<tag>` — `mv` them back (or `uninstall --run --restore-ledgers`,
  which restores from the checkpoint only if no live ledger exists). The new artifact file is additive and harmless to leave (the capped strategies pick it up on their
  next run either way).
* Mid-epoch: `systemctl stop fleet-universe-parity-epoch-<date>.service`, `uninstall --run`. Rows already written stay (append-only history; the canonical row per
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
