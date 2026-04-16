'use strict';
/**
 * FundJohn / OpenClaw — Main Agent Entrypoint
 * 3-agent quant hedge fund system: DataPipeline → ResearchJohn → TradeJohn → BotJohn
 */
const fs   = require('fs');
const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '../../.env') });
const swarm = require('./subagents/swarm');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';

/**
 * Run a full FundJohn cycle.
 * Orchestrates: DataPipeline (hardcoded) → ResearchJohn → TradeJohn → BotJohn approval.
 *
 * @param {Object} opts
 * @param {string}   opts.cycleDate       — ISO date string (e.g. '2026-04-15')
 * @param {Object}   opts.portfolioState  — current portfolio state (NAV, positions, etc.)
 * @param {Array}    opts.strategyList    — live/paper strategies from manifest.json
 * @param {string}   [opts.threadId]      — Discord thread ID for status updates
 * @param {Function} [opts.notify]        — async fn to post progress to Discord
 * @returns {Promise<Object>}             — cycle result with all agent outputs
 */
async function runCycle({ cycleDate, portfolioState, strategyList, threadId, notify }) {
  // Ensure output directories exist
  ['output/memos', 'output/reports', 'output/signals'].forEach(dir => {
    fs.mkdirSync(path.join(OPENCLAW_DIR, dir), { recursive: true });
  });

  const memoDir    = path.join(OPENCLAW_DIR, 'output', 'memos');
  const reportPath = path.join(OPENCLAW_DIR, 'output', 'reports', `${cycleDate}_research.md`);

  return swarm.runCycle({ cycleDate, portfolioState, strategyList, memoDir, reportPath, threadId, notify });
}

/**
 * Handle a general PM task (Discord operator command).
 * Routes directly to BotJohn.
 *
 * @param {Object}   opts
 * @param {string}   opts.task       — operator task/command text
 * @param {string}   [opts.ticker]   — ticker context if relevant
 * @param {string}   [opts.threadId] — Discord thread ID
 * @param {Function} [opts.notify]   — Discord notify function
 * @returns {Promise<Object>}
 */
async function runTask({ task, ticker, threadId, notify }) {
  return swarm.init({
    type:      'botjohn',
    ticker:    ticker || 'N/A',
    workspace: OPENCLAW_DIR,
    threadId,
    prompt:    task,
    notify,
    mode:      'PM_TASK',
  });
}

module.exports = { runCycle, runTask };
