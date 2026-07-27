#!/bin/bash
# Regen Monday (2026-07-27 target) signals AFTER the Calmar-gate reactivation +
# slider-0.5 eligibility rebuild, so newly-activated strategies (momentum_12_1,
# S25_dual_momentum_v2, S_insider_drawdown_confirmation) emit for Monday
# instead of waiting for Tuesday. Mirrors _regen_friday_signals_20260725.sh but
# signals+option_hedge only — collect/sentiment data is unchanged since the
# Friday-close run. Newest signal per (strategy,ticker) supersedes stale rows.
cd /root/openclaw || exit 1
LOG=/root/openclaw/logs/regen_monday_signals_20260727.log
echo "=== REGEN START $(date -u +%FT%TZ) runDate=2026-07-24 (signals,option_hedge) ===" >> "$LOG"
PYTHONPATH=src node -e "
require('dotenv').config({path:'.env'});
require('./src/agent/graphs/daily-cycle').runDailyCycleGraph({
  runDate:'2026-07-24',
  reason:'manual-regen-post-calmar-gate-reactivation',
  requestedSteps:['signals','option_hedge']
}).then(o=>{console.log('done',JSON.stringify(o));process.exit(0)})
 .catch(e=>{console.error('FAIL',e && e.message, e && e.stack);process.exit(1)})
" >> "$LOG" 2>&1
echo "=== REGEN rc=$? END $(date -u +%FT%TZ) ===" >> "$LOG"
