/**
 * JS twin of the notification + agent-registry helpers in
 * pipeline_orchestrator.py (lines 78-401, 112-145).
 *
 * No new Postgres tables — Python orchestrator never wrote a per-step
 * log table either. Structured per-cycle history lives in
 * langgraph_checkpoints (PostgresSaver) and is queryable via SQL.
 */
'use strict';

const path = require('node:path');
const notifications = require(path.resolve(__dirname, '..', 'channels', 'discord', 'notifications.js'));

// Route map ported verbatim from pipeline_orchestrator.py:78-97.
// Data-pipeline steps surface in #data-alerts; trade-pipeline steps
// surface in #trade-reports; everything else goes to #pipeline-feed.
const STEP_FAILURE_CHANNEL = {
  'collect':                'data-alerts',
  'sentiment':              'data-alerts',
  'signals':                'data-alerts',
  'ic_gate':                'trade-reports',
  'handoff':                'trade-reports',
  'trade':                  'trade-reports',
  'alpaca':                 'trade-reports',
  'reconcile':              'trade-reports',
  'report':                 'trade-reports',
  'pyportfolioopt_shadow':  'pipeline-feed',
  'health':                 'pipeline-feed',
};

// Which agent's identity claims each step in the dashboard registry.
// Ported from pipeline_orchestrator.py:743-758.
const STEP_AGENTS = {
  'collect':                'botjohn',
  'sentiment':              'botjohn',
  'signals':                'botjohn',
  'ic_gate':                'tradebot',
  'handoff':                'tradebot',
  'trade':                  'tradebot',
  'alpaca':                 'tradebot',
  'reconcile':              'tradebot',
  'report':                 'tradebot',
  'pyportfolioopt_shadow':  'botjohn',
  'health':                 'botjohn',
};

async function _safePost(channel, text) {
  try {
    await notifications.post(channel, text);
  } catch (e) {
    console.warn(`[pipeline_logging] post to #${channel} failed: ${e.message}`);
  }
}

async function feedStart(step, runDate, reason) {
  const reasonSuffix = reason && reason !== 'scheduled' ? ` (${reason})` : '';
  await _safePost('pipeline-feed', `▶️ ${step} started for ${runDate}${reasonSuffix}`);
}

async function feedEnd(step, status, runDate, durationMs) {
  const emoji = status === 'ok' ? '✅' : status === 'warn' ? '⚠️' : '❓';
  const secs = (durationMs / 1000).toFixed(1);
  await _safePost('pipeline-feed', `${emoji} ${step} ${status} for ${runDate} (${secs}s)`);
}

async function notifyFailure(step, runDate, rc, stderrTail) {
  const channel = STEP_FAILURE_CHANNEL[step] || 'pipeline-feed';
  const tail = (stderrTail || '').slice(-400);
  await _safePost(channel, `❌ ${step} failed for ${runDate} (rc=${rc})\n\`\`\`\n${tail}\n\`\`\``);
}

async function cycleStart(runDate, reason, runId) {
  await _safePost('pipeline-feed', `🚀 daily cycle started — ${runDate} (${reason}, run=${runId})`);
}

async function cycleEnd(runDate, runId, status, abortedAt) {
  if (status === 'ok') {
    await _safePost('pipeline-feed', `✅ daily cycle completed — ${runDate} (run=${runId})`);
  } else {
    await _safePost('pipeline-feed', `❌ daily cycle aborted at ${abortedAt} — ${runDate} (run=${runId})`);
  }
}

/**
 * Update agent_registry status — mirrors pipeline_orchestrator.py:112
 * set_agent_status(). Best-effort, fail-quiet. Postgres connection
 * uses POSTGRES_URI via the existing pg client.
 */
async function updateAgentStatus(agentId, status, currentTask = null) {
  if (!agentId) return;
  let client;
  try {
    const { Client } = require('pg');
    client = new Client({ connectionString: process.env.POSTGRES_URI });
    await client.connect();
    await client.query(
      'UPDATE agent_registry SET status=$1, current_task=$2, last_seen_at=NOW() WHERE id=$3',
      [status, currentTask, agentId]
    );
  } catch (e) {
    console.warn(`[pipeline_logging] updateAgentStatus(${agentId}, ${status}) failed: ${e.message}`);
  } finally {
    if (client) await client.end().catch(() => {});
  }
}

module.exports = {
  STEP_FAILURE_CHANNEL,
  STEP_AGENTS,
  feedStart,
  feedEnd,
  notifyFailure,
  cycleStart,
  cycleEnd,
  updateAgentStatus,
};
