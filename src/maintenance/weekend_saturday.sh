#!/usr/bin/env bash
#
# Saturday 08:00 ET — Strategy Adjustment pipeline.
# Sequenced: review -> critique -> position-recs -> backtest-coupling (applies
# persist their candidate run as the canonical backtest) -> proposal auto-apply
# -> eligibility refresh -> candidate tuner -> weekly weights -> panel
# rebuild/verify -> universe-recs. All steps WARN-and-continue.
# The weekly FULL backtest refresh is RETIRED (operator directive 2026-07-14):
# canonical metrics update when — and only when — an adjustment applies.
set -uo pipefail
cd /root/openclaw
export PYTHONPATH=src
LOG="/var/log/openclaw/weekend_saturday_$(date -u +%Y%m%d_%H%M%S).log"
mkdir -p "$(dirname "$LOG")"
DRY="${1:-}"   # pass --dry-run to skip live writes where supported
# NOTE: $DRY (--dry-run) is forwarded ONLY to the coupling step (step 4 below).
# The mastermind modes (review/critique/position-recs), eligibility refresh,
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
# 2026-07-14 (full-auto): accepts ANY strict Sharpe improvement (was >= +0.10),
# also bracket-couples low-confidence 'noted' recs, and re-anchors broker stops
# on open positions for coupling-approved stop changes.
python3 -m execution.backtest_coupled_recs $DRY 2>&1 | tee -a "$LOG" || step "WARN coupling rc=$?"

step "4b proposal auto-apply (confidence > 0.8 applies; rest noted — operator directive 2026-07-14)"
# Size/eligibility proposals from tonight's review: strictly > 0.8 confidence
# auto-approves through the same set_params path as a dashboard click
# (source='auto-approval'); lower/missing confidence parks as 'noted' for the
# dashboard + next-Saturday re-evaluation. Runs before weights (step 6) so new
# scalars/eligibility flow into this weekend's rebuild. Skips $DRY runs.
if [ -z "$DRY" ]; then
  python3 -m strategies.proposal_manager --auto-apply-batch 2>&1 | tee -a "$LOG" || step "WARN auto-apply rc=$?"
else
  step "4b skipped (dry-run)"
fi

step "5/8 eligibility refresh (fleet re-backtest RETIRED — operator directive 2026-07-14)"
# The weekly full re-backtest (refresh_backtests.sh, 6h) is retired: canonical
# strategy_backtest_runs rows are now maintained by the coupling step itself —
# an APPLIED candidate backtest is committed as the new primary_window row, so
# metrics refresh exactly when an adjustment is determined helpful and never
# otherwise. refresh_backtests.sh remains on disk as a manual tool only.
# eligibility_assigner is kept (cheap, DB-only): it re-derives eligible_regimes
# from the newest per-regime rows, including those the coupling step just wrote.
python3 -m backtest.eligibility_assigner --all 2>&1 | tee -a "$LOG" \
  || step "WARN eligibility_assigner rc=$? — continuing on last-known eligibility"

step "5b candidate tuner (Sharpe-seeking auto-apply; operator directive 2026-07-14)"
# Runs the code-review APPLY path on CANDIDATES only (applyAndValidate
# hard-refuses live): Opus proposes a corrected implementation, an ephemeral
# re-backtest measures it, and the fix is kept ONLY if Sharpe does not
# regress (byte-revert otherwise). Runs on last-known backtests. This is
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
