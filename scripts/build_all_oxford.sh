#!/usr/bin/env bash
# Backtest every oxf_* strategy. SEQUENTIAL (2-core/8GB/no-swap box, OOM history):
# one subprocess per strategy so RSS frees between runs. nice -n 19, per-strategy
# timeout, continue-on-failure, resumable/idempotent (each run demotes prior
# primary_window rows for that strategy). Logs to $LOG.
# Invocation note: the engine does Path(filepath).relative_to(ROOT), so
# --strategy-file MUST be an ABSOLUTE path, and the module needs PYTHONPATH=src.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
export PYTHONPATH=src
export POSTGRES_URI=$(grep -m1 '^POSTGRES_URI=' .env | cut -d= -f2-)
LOG="${1:-/tmp/oxford_backtest.log}"
TIMEOUT_S="${OXF_TIMEOUT_S:-2700}"   # 45 min/strategy watchdog
: > "$LOG"
ok=0; fail=0; n=0
strats=$(ls "$ROOT"/src/strategies/implementations/oxf_*.py | sort)
total=$(echo "$strats" | wc -l | tr -d ' ')
echo "[$(date -u +%H:%M:%S)] backtesting $total oxf strategies (timeout ${TIMEOUT_S}s each)" | tee -a "$LOG"
for f in $strats; do
  s=$(basename "$f" .py); n=$((n+1))
  # Resumable: skip strategies that already have a primary_window run (unless OXF_FORCE=1).
  if [ "${OXF_FORCE:-0}" != "1" ] && python3 -c "
import os,sys,psycopg2
c=psycopg2.connect(os.environ['POSTGRES_URI']);cur=c.cursor()
cur.execute('SELECT 1 FROM strategy_backtest_runs WHERE primary_window AND strategy_id=%s LIMIT 1',(sys.argv[1],))
sys.exit(0 if cur.fetchone() else 1)" "$s" 2>/dev/null; then
    echo "[$(date -u +%H:%M:%S)] ($n/$total) $s SKIP (already has primary_window run)" | tee -a "$LOG"
    ok=$((ok+1)); continue
  fi
  echo "[$(date -u +%H:%M:%S)] ($n/$total) $s ..." | tee -a "$LOG"
  if nice -n 19 timeout "$TIMEOUT_S" python3 -m backtest.unified_backtest \
        --strategy-file "$f" >>"$LOG" 2>&1; then
    line=$(grep "wrote run_id=" "$LOG" | tail -1)
    echo "[$(date -u +%H:%M:%S)] ($n/$total) $s OK  ${line#*wrote }" | tee -a "$LOG"
    ok=$((ok+1))
  else
    rc=$?
    echo "[$(date -u +%H:%M:%S)] ($n/$total) $s FAILED rc=$rc $([ $rc -eq 124 ] && echo TIMEOUT)" | tee -a "$LOG"
    fail=$((fail+1))
  fi
done
echo "[$(date -u +%H:%M:%S)] DONE: $ok ok, $fail failed of $total" | tee -a "$LOG"
