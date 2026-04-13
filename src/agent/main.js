'use strict';

/**
 * BotJohn — Main PTC Agent Entrypoint
 * Handles complex tasks via the full subagent swarm with workspace management.
 */

const fs = require('fs');
const path = require('path');
require('dotenv').config({ path: path.join(__dirname, '../../.env') });

const swarm = require('./subagents/swarm');
const { verdictCache: pgVerdictCache } = require('../database/postgres');
const { initRateLimitBuckets } = require('../database/redis');
const workspaceManager = require('../workspace/manager');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';

/**
 * Run a full diligence analysis for a ticker.
 * This is the primary PTC-mode entry point called by the Discord bot.
 *
 * @param {Object} opts
 * @param {string} opts.ticker
 * @param {string} opts.workspaceId
 * @param {string} opts.threadId
 * @param {Function} opts.notify — async function to post progress to Discord
 * @returns {Promise<Object>} — pipeline result
 */
async function runDiligence({ ticker, workspaceId, threadId, notify, scheduled = false }) {
  const workspace = await workspaceManager.getOrCreate(workspaceId);

  // Check verdict cache — skip if fresh results exist
  const cached = await pgVerdictCache.getFresh(ticker, 'diligence').catch(() => null);
  if (cached) {
    const staleDays = Math.round((new Date(cached.stale_after) - new Date()) / 86400000);
    if (notify) await notify(`📋 Using cached diligence for ${ticker} (fresh for ${staleDays} more days) — verdict: **${cached.verdict}** (${cached.score})`);
    return { cached: true, result: cached };
  }

  // Initialize rate limit buckets from workspace preferences
  const prefsPath = path.join(workspace, '.agents', 'user', 'preferences.json');
  if (fs.existsSync(prefsPath)) {
    const prefs = JSON.parse(fs.readFileSync(prefsPath, 'utf8'));
    await initRateLimitBuckets(prefs).catch((err) => console.warn('[main] Redis unavailable, rate limits not initialized:', err.message));
  }

  return swarm.runDiligence(ticker, workspace, threadId, notify, { scheduled });
}

/**
 * Run a trade pipeline for a ticker (assumes diligence already complete).
 */
async function runTrade({ ticker, workspaceId, threadId, notify }) {
  const workspace = await workspaceManager.getOrCreate(workspaceId);
  const taskDir = path.join(workspace, 'work', `${ticker}-diligence`);

  // Check portfolio staleness
  const portfolioPath = path.join(workspace, '.agents', 'user', 'portfolio.json');
  if (fs.existsSync(portfolioPath)) {
    const portfolio = JSON.parse(fs.readFileSync(portfolioPath, 'utf8'));
    const lastVerified = new Date(portfolio.last_verified_at);
    const hoursStale = (Date.now() - lastVerified.getTime()) / 3600000;
    if (hoursStale > 24) {
      const warning = `⚠️ PORTFOLIO STATE STALE — ${Math.round(hoursStale)} hours. Update portfolio.json before trade execution.`;
      if (notify) await notify(warning);
      console.warn(`[main] ${warning}`);
    }
  }

  return swarm.runTradePipeline(ticker, workspace, taskDir, threadId, notify);
}

/**
 * Handle a general PTC-mode task (not diligence or trade).
 * Spawns appropriate subagents based on task description.
 */
async function runTask({ task, ticker, workspaceId, threadId, notify }) {
  const workspace = await workspaceManager.getOrCreate(workspaceId);

  // All general PM requests go to BotJohn directly — he is the master agent
  // with full project access, orchestrator authority, and file write permissions.
  // PM_TASK mode bypasses the deployment gate (BotJohn is always permitted).
  return swarm.init({
    type:      'botjohn',
    ticker,
    workspace: workspace || '/root/openclaw',
    threadId,
    prompt:    task,
    notify,
    mode:      'PM_TASK',
  });
}

module.exports = { runDiligence, runTrade, runTask };
