/**
 * Startup probe: on johnbot boot, check whether the previous daily-cycle
 * thread is mid-cycle. If yes, resume from the last checkpoint.
 *
 * Gate: OPENCLAW_LANGGRAPH_ORCHESTRATOR=1 — legacy orchestrator owns
 * recovery via its Redis checkpoint when the flag is off.
 *
 * Window: today's runDate (UTC), plus yesterday's if it's before 14:00 UTC
 * (~9am ET) to cover overnight crashes.
 */
'use strict';

const { listThreadState, resumeDailyCycle } = require('./daily-cycle');

function _candidateRunDates(now = new Date()) {
  const today = now.toISOString().slice(0, 10);
  if (now.getUTCHours() < 14) {
    const y = new Date(now); y.setUTCDate(y.getUTCDate() - 1);
    return [today, y.toISOString().slice(0, 10)];
  }
  return [today];
}

async function recoverInflight() {
  if (process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR !== '1') {
    return { recovered: false, skipped: true, reason: 'flag_off' };
  }
  for (const runDate of _candidateRunDates()) {
    let snap;
    try {
      snap = await listThreadState(runDate);
    } catch (e) {
      console.warn(`[recover_inflight] listThreadState ${runDate} failed: ${e.message}`);
      continue;
    }
    if (snap && snap.next && snap.next.length > 0) {
      console.log(`[recover_inflight] resuming daily-cycle:${runDate} at ${snap.next.join(',')}`);
      const out = await resumeDailyCycle(runDate);
      return { recovered: true, runDate, nextSteps: snap.next, result: out };
    }
  }
  return { recovered: false };
}

module.exports = { recoverInflight, _candidateRunDates };
