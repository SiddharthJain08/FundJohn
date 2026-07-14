#!/usr/bin/env bash
#
# Saturday 08:00 ET — Strategy Adjustment + Backtest Refresh pipeline.
# Sequenced: review -> critique -> position-recs -> backtest-coupling ->
# full backtest refresh -> weekly weights -> panel rebuild/verify -> universe-recs.
# Steps 1-4 WARN-and-continue; step 5 (backtest refresh) failing aborts 6-7.
set -uo pipefail
cd /root/openclaw
export PYTHONPATH=src
LOG="/var/log/openclaw/weekend_saturday_$(date -u +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
DRY="${1:-}"   # pass --dry-run to skip live writes where supported
# NOTE: $DRY (--dry-run) is forwarded ONLY to the coupling step (step 4 below).
# The mastermind modes (review/critique/position-recs), backtest refresh,
# weekly weights, panel rebuild, and universe-recs all still run LIVE — they
# do not consume $DRY. Use a no-trading day if you need a fully dry run.

step() { echo "[weekend_saturday] $(date -u +%FT%TZ) >>> $*" | tee -a "$LOG"; }

step "1/8 comprehensive-review"
node src/agent/curators/run_mastermind.js --mode comprehensive-review 2>&1 | tee -a "$LOG" || step "WARN review rc=$?"

step "2/8 critique"
node src/agent/curators/run_mastermind.js --mode critique 2>&1 | tee -a "$LOG" || step "WARN critique rc=$?"

step "3/8 position-recs"
node src/agent/curators/run_mastermind.js --mode position-recs 2>&1 | tee -a "$LOG" || step "WARN position-recs rc=$?"

step "4/8 backtest-coupling"
python3 -m execution.backtest_coupled_recs $DRY 2>&1 | tee -a "$LOG" || step "WARN coupling rc=$?"

step "5/8 full backtest refresh (internally time-bounded; WARN-and-continue)"
bash src/maintenance/refresh_backtests.sh 2>&1 | tee -a "$LOG"
BT_RC=${PIPESTATUS[0]}
# DECOUPLED (W1 reconcile 2026-06-28): step 5 is internally bounded to 6h, so a
# slow/incomplete refresh no longer aborts the pipeline. Steps 6-8 (weights/
# panels/universe) now run on last-known backtests instead of being skipped —
# the prior hard `exit "$BT_RC"` was the root cause of weekly weights/panels
# repeatedly needing manual rebuilds.
[ "$BT_RC" -ne 0 ] && step "WARN backtest refresh rc=$BT_RC — continuing to weights/panels on last-known backtests"

step "5b candidate tuner (Sharpe-seeking auto-apply; operator directive 2026-07-14)"
# Runs the code-review APPLY path on CANDIDATES only (applyAndValidate
# hard-refuses live): Opus proposes a corrected implementation, an ephemeral
# re-backtest measures it, and the fix is kept ONLY if Sharpe does not
# regress (byte-revert otherwise). Uses the fresh step-5 backtests. This is
# the alteration half of the 3-weekend candidate lifecycle — Sunday
# investigates + re-offers the gate, the reaper ejects after 3 missed
# weekends. --recent-days 22 covers the full lifecycle; the low-trade union
# pulls in every <30-trade candidate regardless of age. Capped (2-core box).
node src/agent/curators/mastermind_code_review.js \
  --state candidate --recent-days 22 --include-low-trade \
  --apply --limit 12 \
  --out logs/code_review_candidate_tuner_saturday.md 2>&1 | tee -a "$LOG" \
  || step "WARN candidate-tuner rc=$?"

step "6/8 weekly strategy weights"
node src/agent/curators/weekly_live_sharpe.js 2>&1 | tee -a "$LOG" || step "WARN weights rc=$?"

step "7/8 panel rebuild + verify"
python3 -m backtest.backtest_panel --rebuild 2>&1 | tee -a "$LOG" || step "WARN panel rebuild rc=$?"
python3 - <<'PY' 2>&1 | tee -a "$LOG"
import os, psycopg2
c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor()
cur.execute("""SELECT COUNT(*) FROM strategy_backtest_panel p
               JOIN (SELECT DISTINCT ON (strategy_id) strategy_id, run_at FROM strategy_backtest_runs
                     WHERE primary_window=TRUE ORDER BY strategy_id, run_at DESC) r
                 ON r.strategy_id=p.strategy_id
              WHERE p.computed_at < r.run_at""")
stale=cur.fetchone()[0]
print(f'[weekend_saturday] panel staleness check: {stale} panels older than their run')
PY

step "8/8 universe-ladder sentinel (SP-7 Phase B — replaced legacy universe-recs 2026-06-06)"
nice -n 19 python3 scripts/check_ladder_saturday.py 2>&1 | tee -a "$LOG" || step "WARN ladder-sentinel rc=$?"

# No-silent-failure (W1 reconcile 2026-06-28): steps 6-8 ran above regardless,
# but if step 5's bounded refresh was incomplete (BT_RC!=0, e.g. rc=124 = 6h cap
# hit) some strategies' backtests are STALE and the weights/panels just rebuilt
# used last-known data. Fail the unit so OnFailure alerts #botjohn-log instead of
# the staleness being silent. Clears permanently via the per-strategy refresh redesign.
if [ "${BT_RC:-0}" -ne 0 ]; then
  step "INCOMPLETE: step-5 backtest refresh rc=$BT_RC (rc=124 = 6h cap hit). Weights/panels ran on last-known backtests; some are STALE. log=$LOG"
  exit "$BT_RC"
fi
step "DONE log=$LOG"
