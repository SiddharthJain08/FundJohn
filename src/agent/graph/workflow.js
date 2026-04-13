'use strict';

/**
 * Agent Workflow Graph — Plan → Validate → Execute → Report
 *
 * This module defines the high-level workflow BotJohn follows for complex tasks.
 * It is NOT LangGraph. It's a simple state machine with 4 stages.
 */

const swarm = require('../subagents/swarm');
const { verdictCache } = require('../../database/postgres');
const { isOperatorOnline } = require('../../database/redis');

const STAGES = ['plan', 'validate', 'execute', 'report'];

/**
 * Run the full workflow for a diligence or trade task.
 * @param {Object} context — { ticker, type, workspaceId, threadId, workspace, notify, prefs }
 */
async function run(context) {
  const { ticker, type, workspace, threadId, notify, prefs } = context;
  const state = { stage: 'plan', ticker, type, workspace, threadId, results: {} };

  // Stage 1: Plan
  state.stage = 'plan';
  const plan = await plan(state, prefs);
  if (notify) await notify(`📋 Plan: ${plan.summary}`);

  // Stage 2: Validate (check cache, check staleness)
  state.stage = 'validate';
  const validation = await validate(state, plan, prefs);
  if (validation.canSkip) {
    if (notify) await notify(`♻️ Using cached results for ${ticker} — skipping subagent spawn`);
    return { cached: true, cached_result: validation.cached };
  }

  // Stage 3: Execute
  state.stage = 'execute';
  let result;
  if (type === 'diligence') {
    result = await swarm.runDiligence(ticker, workspace, threadId, notify);
  } else if (type === 'trade') {
    const taskDir = require('path').join(workspace, 'work', `${ticker}-diligence`);
    result = await swarm.runTradePipeline(ticker, workspace, taskDir, threadId, notify);
  } else {
    result = await swarm.init({ type: 'research', ticker, workspace, threadId, notify });
  }
  state.results = result;

  // Stage 4: Report (handled by report-builder subagent — nothing to do here)
  state.stage = 'report';
  return { cached: false, result, stage: 'complete' };
}

async function plan(state, prefs) {
  const { ticker, type } = state;
  const staleness = prefs?.staleness_windows || {};

  const summary = type === 'diligence'
    ? `Diligence ${ticker}: spawn research + data-prep → validate → compute + equity-analyst → report-builder`
    : `Trade ${ticker}: data-prep → validate → compute + equity-analyst → report-builder`;

  return { summary, type, ticker, staleness };
}

async function validate(state, plan, prefs) {
  const { ticker, type } = state;

  // Check verdict cache
  const cached = await verdictCache.getFresh(ticker, type).catch(() => null);
  if (cached) {
    return { canSkip: true, cached };
  }

  return { canSkip: false };
}

/**
 * Daily close routine: run the zero-token execution engine after market hours.
 * Called by a cron or Discord /engine-run command.
 * @param {string} workspaceId
 * @param {Function} [notify] — optional Discord notify callback
 */
async function runDailyClose(workspaceId = 'default', notify = null) {
    const runner = require('../../execution/runner');
    try {
        if (notify) await notify('Running execution engine...');
        const result = await runner.runDailyClose(workspaceId);
        if (notify) await notify(runner.formatEngineReport(result));
        return result;
    } catch (err) {
        if (notify) await notify(`Execution engine error: ${err.message}`);
        throw err;
    }
}

module.exports = { run, runDailyClose, STAGES };
