#!/usr/bin/env bash
set -e
SCRIPT="$(dirname "$0")/../src/maintenance/weekend_saturday.sh"
bash -n "$SCRIPT"
grep -q 'mode comprehensive-review' "$SCRIPT"
grep -q 'mode critique' "$SCRIPT"
grep -q 'mode position-recs' "$SCRIPT"
grep -q 'execution.backtest_coupled_recs' "$SCRIPT"
grep -q 'refresh_backtests.sh' "$SCRIPT"
grep -q 'weekly_live_sharpe.js' "$SCRIPT"
grep -q 'backtest_panel --rebuild' "$SCRIPT"
grep -q 'mode universe-recs' "$SCRIPT"
awk '/backtest_coupled_recs/{c=NR} /refresh_backtests.sh/{r=NR} END{exit !(c<r)}' "$SCRIPT"
echo "OK test_weekend_saturday_dryrun"
