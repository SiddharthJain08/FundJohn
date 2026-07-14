#!/usr/bin/env bash
set -e
SCRIPT="$(dirname "$0")/../src/maintenance/weekend_saturday.sh"
bash -n "$SCRIPT"
grep -q 'mode comprehensive-review' "$SCRIPT"
grep -q 'mode critique' "$SCRIPT"
grep -q 'mode position-recs' "$SCRIPT"
grep -q 'execution.backtest_coupled_recs' "$SCRIPT"
grep -q 'weekly_live_sharpe.js' "$SCRIPT"
grep -q 'backtest_panel --rebuild' "$SCRIPT"
# Step 8 = SP-7 ladder sentinel (replaced legacy universe-recs 2026-06-06).
grep -q 'check_ladder_saturday' "$SCRIPT"
# Step 4b = Saturday confidence auto-apply (2026-07-14 full-auto directive).
grep -q 'proposal_manager --auto-apply-batch' "$SCRIPT"
# Step 5 = eligibility refresh only; the weekly FULL backtest refresh is
# RETIRED (2026-07-14 operator directive) — applied coupling candidates are
# committed as the canonical runs instead. The script must NOT invoke
# refresh_backtests.sh (mentions in comments are fine).
grep -q 'backtest.eligibility_assigner --all' "$SCRIPT"
! grep -Eq '^[^#]*refresh_backtests\.sh' "$SCRIPT"
# Coupling (step 4) must precede the eligibility refresh (step 5) so the
# assigner sees the per-regime rows the coupling applies just committed.
awk '/backtest_coupled_recs/{c=NR} /eligibility_assigner/{e=NR} END{exit !(c<e)}' "$SCRIPT"
# Auto-apply must land between coupling (step 4) and the weights rebuild (step 6)
# so fresh scalars/eligibility flow into the same weekend's weights.
awk '/backtest_coupled_recs/{c=NR} /auto-apply-batch/{a=NR} /weekly_live_sharpe.js/{w=NR} END{exit !(c<a && a<w)}' "$SCRIPT"
echo "OK test_weekend_saturday_dryrun"
