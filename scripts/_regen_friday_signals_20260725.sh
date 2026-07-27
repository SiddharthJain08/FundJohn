#!/bin/bash
cd /root/openclaw || exit 1
LOG=/root/openclaw/logs/regen_friday_signals_20260725.log
echo "=== REGEN START $(date -u +%FT%TZ) runDate=2026-07-24 ===" >> "$LOG"
PYTHONPATH=src node -e "
require('dotenv').config({path:'.env'});
require('./src/agent/graphs/daily-cycle').runDailyCycleGraph({
  runDate:'2026-07-24',
  reason:'manual-regen-post-canonical',
  requestedSteps:['collect','sentiment','signals','option_hedge']
}).then(o=>{console.log('done',JSON.stringify(o));process.exit(0)})
 .catch(e=>{console.error('FAIL',e && e.message, e && e.stack);process.exit(1)})
" >> "$LOG" 2>&1
echo "=== REGEN rc=$? END $(date -u +%FT%TZ) ===" >> "$LOG"
