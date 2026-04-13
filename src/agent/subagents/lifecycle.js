'use strict';

const { v4: uuidv4 } = require('uuid');
const swarm = require('./swarm');
const { checkpoints, query } = require('../../database/postgres');
const { getAllSubagentStatuses } = require('../../database/redis');

/**
 * Subagent lifecycle management — init, update, resume.
 * Wraps swarm.js with checkpoint tracking and status reporting.
 */

/**
 * Start a new subagent with full lifecycle tracking.
 * @returns {{ subagentId: string, promise: Promise }}
 */
function startSubagent(config) {
  const subagentId = uuidv4();
  const promise = swarm.init({ ...config, _subagentId: subagentId });
  return { subagentId, promise };
}

/**
 * Get live status of all running subagents for a thread.
 */
async function getThreadStatus(threadId) {
  const all = await getAllSubagentStatuses();
  const threadAgents = all.filter((s) => s.threadId === threadId);

  return threadAgents.map((s) => ({
    id: s.id,
    type: s.type,
    ticker: s.ticker,
    status: s.status,
    elapsed: s.startedAt ? Math.round((Date.now() - s.startedAt) / 1000) : null,
  }));
}

/**
 * Format status for Discord display.
 */
async function formatStatus(threadId) {
  const statuses = await getThreadStatus(threadId);
  if (statuses.length === 0) return 'No active subagents.';

  const EMOJI = {
    running: '⚙️', complete: '✅', error: '❌', pending: '🕐',
  };

  return statuses.map((s) => {
    const emoji = EMOJI[s.status] || '❓';
    const elapsed = s.elapsed ? ` | ${s.elapsed}s` : '';
    return `${emoji} **${s.type}** [${s.ticker}] — ${s.status}${elapsed}`;
  }).join('\n');
}

/**
 * Resume all incomplete subagents for a thread (on reconnect).
 */
async function resumeIncomplete(threadId) {
  const res = await query(
    `SELECT * FROM checkpoints WHERE thread_id=$1 AND status='running'`,
    [threadId]
  );
  if (!res || !res.rows.length) return [];

  return res.rows.map((cp) => ({
    checkpointId: cp.id,
    type: cp.subagent_type,
    ticker: cp.ticker,
  }));
}

module.exports = { startSubagent, getThreadStatus, formatStatus, resumeIncomplete };
