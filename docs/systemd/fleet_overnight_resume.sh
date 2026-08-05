#!/usr/bin/env bash
# Nightly (Mon-Fri post-close) DURABLE fleet re-backtest continuation, until the
# canonical reaches UNIFORM. Installed as openclaw-fleet-overnight-resume.{service,
# timer}; box is Etc/UTC so all times below are UTC.
#
# Bounding: the hard wall-clock stop is the SERVICE's RuntimeMaxSec (systemd
# cgroup-wide SIGTERM->SIGKILL — kills the in-flight python child too, which a bare
# `timeout` can orphan). --deadline just stops NEW spawns near the wall.
# --per-timeout 14400 (240m) gives the slow-sim strategies their full shot (this is
# the overnight window they were deferred to).
#
# COMPUTE ONLY: writes canonical; NO weights rebuild, NO activation, NO Oxford.
# The masked actuators + OPENCLAW_ACTIVATION_ASSIGNER/AUTO_DEMOTE=0 stay as-is —
# reaching CANONICAL UNIFORM does NOT auto-actuate; the OWED post-fleet sequence is
# operator-gated. On reaching 0 outstanding this self-disables its own timer.
set -u
cd /root/openclaw || exit 1
LOG=/root/openclaw/logs/fleet_overnight_resume.log

# Guard: never run two drivers at once. Matching on `pgrep -x node` (process NAME
# is exactly node) then grepping the cmdline is load-bearing: a bare `pgrep -f`
# also matches any SHELL whose command line merely CONTAINS the driver's filename
# — e.g. an operator/agent session running `pgrep -f refresh_backtests_resumable.js`
# to check on it. That self-match makes the nightly run skip SILENTLY (observed
# 2026-07-30 06:45Z). A shell is never named `node`, so this form cannot self-match.
if pgrep -a -x node 2>/dev/null | grep -q 'refresh_backtests_resumable\.js'; then
  echo "[overnight $(date -u +%FT%TZ)] a fleet driver is already running — skipping this trigger" >> "$LOG"
  exit 0
fi

# No-new-spawn deadline = 10:30 UTC. Fires tonight after the 21:30 start, so
# 'today 10:30' is in the past -> roll to tomorrow (13h of spawning).
#
# WAS 06:15, justified as "before the ~07:30 premarket scan" — WRONG, and it cost
# ~5h of window every night. `openclaw-premarket-scan-0730.timer` is NAMED in ET
# but SCHEDULED in UTC: it fires at 11:30Z, not 07:30Z. (Same trap as the
# `openclaw-sunday-*` units, which fire on Saturday.) Measured 2026-08-03, every
# unit between 06:15Z and 11:30Z: amcheck 2s, refresh-universe-sizes 34s,
# regime-live-pnl 1s, phase2d-nightly 7s, afterhours-tp-premarket 1s.
#
# ⚠ ONE of them is NOT a single run: openclaw-afterhours-stop-monitor is
# `Mon..Fri 04..19:00/10:00 America/New_York` = every 10 MINUTES from 08:00Z, so
# the extension newly overlaps ~19 invocations (~4.4min CPU total). It runs
# afterhours_tp.py --monitor, which protects LIVE open positions, so it must
# never be starved. It is not: fleet children run Nice=19 (monitor wins CPU) and
# are marked oom_score_adj=1000 by fleet_oom_victim_exec.sh (kernel kills the
# BACKTEST, never the monitor). Overlap is deliberate and bounded, not free.
#
# The rest are also WRITE-DISJOINT from the fleet: they write strategy_universe_sizes,
# strategy_signal_overlap, mastermind_proposal_outcomes and
# strategy_regime_live_pnl_rollup, none of which is referenced anywhere under
# src/backtest/. First real boundary is edgar-8k at 11:15Z.
#
# ⚠ This is HALF of a two-part change. The service's RuntimeMaxSec is the real
# bound; it was 32400 (9h -> hard stop 06:30Z), which would have made this edit
# INERT. Raised to 48600 (13.5h -> hard stop 11:00Z) in the unit file. If you
# ever move this deadline again, move RuntimeMaxSec with it or nothing changes.
#
# Friday's run lands on SATURDAY morning: 06:00Z options-eligibility is 17s (and
# already overlapped the old 06:30Z stop), and Sat 06:30-11:00Z is otherwise
# empty — sunday-research-ingest is 12:00Z, an hour after the new stop.
DL=$(date -u -d 'today 10:30' +%s); NOW=$(date -u +%s)
(( DL <= NOW )) && DL=$(date -u -d 'tomorrow 10:30' +%s)
DL_STR=$(date -u -d "@${DL}" +%Y-%m-%dT%H:%M)

echo "[overnight $(date -u +%FT%TZ)] start (pid $$); --deadline ${DL_STR}Z --per-timeout 14400" >> "$LOG"
node scripts/refresh_backtests_resumable.js --resume \
  --deadline "${DL_STR}" --per-timeout 14400 --mem-floor 4500 --mem-wait 3600 >> "$LOG" 2>&1
rc=$?

OUT=$(grep -oE '[0-9]+ strategies still outstanding' "$LOG" | tail -1 | grep -oE '^[0-9]+')
echo "[overnight $(date -u +%FT%TZ)] exit rc=$rc; outstanding=${OUT:-unknown}" >> "$LOG"

# Independent quarantine check (2026-08-02). Belt-and-braces with the driver's
# own accounting fix: quarantined strategies used to be filtered out of
# allStrategies(), so they vanished from the outstanding count and OUT could
# read 0 while they sat on the OLD methodology — silently arming the actuation
# below. The driver now counts them, but this branch does REAL actuation
# (--adopt commits universe-ladder verdicts, --reassign re-derives per-regime
# sleeve eligibility, and the timer disables itself), and none of it is covered
# by the masked OPENCLAW_ACTIVATION_ASSIGNER / AUTO_DEMOTE guards. So verify
# against the manifest directly rather than trusting a grepped log line.
QUAR=$(python3 -c "
import json
m=json.load(open('/root/openclaw/src/strategies/manifest.json'))
q=[k for k,v in (m.get('strategies') or {}).items()
   if v.get('state') in ('live','candidate','staging') and v.get('backtest_quarantine')]
print(','.join(sorted(q)))
" 2>/dev/null)
if [[ -n "$QUAR" ]]; then
  echo "[overnight $(date -u +%FT%TZ)] NOT actuating: ${QUAR//,/, } still quarantined — canonical cannot be uniform. Fix + re-backtest, or clear the flag deliberately." >> "$LOG"
  OUT=""   # neutralise the uniform branch; leave the nightly timer enabled
fi

# ── OPERATOR ACTUATION GATE (2026-08-03) ────────────────────────────────────
# Set to 1 to restore the original behaviour (self-disable timer + run the
# universe shrink automatically on reaching UNIFORM). ONE line to re-enable.
#
# Why it exists: until today the QUAR check above was the de-facto gate — it
# blanked OUT whenever anything was quarantined, and S_pairs always was. That
# made the actuation branch unreachable in practice. Clearing the S_pairs
# quarantine (db0560f, so the unattended nightly can retest it) removed that
# accidental protection, and the fleet is now close enough to finish (27
# runnable, ~8.8h of work vs a 13.5h window) that UNIFORM is reachable on a
# single unattended run. The post-uniform sequence is OPERATOR-GATED
# (directive 2026-07-27), so it must not fire because a quarantine flag came
# off for an unrelated reason. Compute is unaffected — only actuation is held.
ALLOW_ACTUATION=0

if [[ "$OUT" == "0" && "$ALLOW_ACTUATION" != "1" ]]; then
  echo "[overnight $(date -u +%FT%TZ)] ✅ CANONICAL UNIFORM REACHED — NOT actuating (ALLOW_ACTUATION=0)." >> "$LOG"
  echo "[overnight $(date -u +%FT%TZ)]    Timer left ENABLED (subsequent runs are no-ops at 0 outstanding)." >> "$LOG"
  echo "[overnight $(date -u +%FT%TZ)]    OWED, operator-gated: run_universe_shrink --adopt --reassign -> weights rebuild -> floor recheck -> activation -> re-enable openclaw-weekly-strategy-weights.timer + OPENCLAW_ACTIVATION_ASSIGNER/AUTO_DEMOTE=1." >> "$LOG"
  OUT=""   # fall through without actuating
fi

if [[ "$OUT" == "0" ]]; then
  echo "[overnight $(date -u +%FT%TZ)] CANONICAL UNIFORM — self-disabling nightly timer." >> "$LOG"
  systemctl disable --now openclaw-fleet-overnight-resume.timer >> "$LOG" 2>&1
  # Honest-cost epoch (operator directive 2026-07-27): universe selection is
  # PART of the re-gate — the cost model changed, so every strategy's tier
  # choice must be re-derived from the fresh honest-cost trades. Shrink the
  # full-universe primary runs down the tier ladder, prefer-largest select,
  # ADOPT change verdicts, and re-derive per-regime eligibility from the
  # chosen tier's sleeves (--reassign). Runs once, niced, post-uniform.
  #
  # 🔴 --force IS LOAD-BEARING, ADDED 2026-08-05 AFTER THIS BIT US.
  # run_universe_shrink skips a strategy outright when a recommendation already
  # exists for the same (strategy_id, candidate_set_id) — the check has NO date
  # or run comparison, so a rec from a PREVIOUS epoch blocks re-derivation
  # against the new backtests. It is a crash-RESUME path, not a correctness one.
  # Running the real sequence by hand on 08-05:
  #     without --force:  changes=4  adopted=4  skipped_existing=132   (recs dated 07-25)
  #     with    --force:  changes=46 adopted=46 skipped_existing=0
  # and the activation assigner's verdict flipped with it — on the STALE
  # universes it deactivated 77 cells and made 35 strategies dormant; on the
  # re-derived ones, 10 and 2. Nothing errored either way: a clean run on wrong
  # inputs. Since this branch only fires post-uniform (i.e. every strategy has a
  # NEW primary run by definition), re-derivation is always what we want here.
  # NOTE `--dry-run` ignores the skip, so a dry-run preview does NOT match what
  # an un-forced adopt actually does — that mismatch is how the bug hid.
  echo "[overnight $(date -u +%FT%TZ)] UNIFORM -> universe selection: run_universe_shrink --adopt --reassign --force" >> "$LOG"
  nice -n 19 python3 scripts/run_universe_shrink.py --adopt --reassign --force >> "$LOG" 2>&1
  src=$?
  echo "[overnight $(date -u +%FT%TZ)] universe selection rc=$src. OWED actuation (weights rebuild -> floor recheck -> activation; then re-enable openclaw-weekly-strategy-weights.timer + OPENCLAW_ACTIVATION_ASSIGNER/AUTO_DEMOTE=1 in .env) is OPERATOR-GATED." >> "$LOG"
fi
