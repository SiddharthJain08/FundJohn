'use strict';
const { spawn } = require('child_process');
const fs   = require('fs');
const path = require('path');
const { v4: uuidv4 } = require('uuid');
const { setSubagentStatus, getSubagentStatus, recordProviderRateLimit, checkProviderReady } = require('../../database/redis');
const { checkpoints, verdictCache } = require('../../database/postgres');
const tokenDb = require('../../database/tokens');
const { verifySubagentOutput } = require('../../security/verification');
const types = require('./types');
const batch            = require('../../budget/batch');
const pipelineActivity = require('../middleware/pipeline-activity');
const tokenBudget      = require('../middleware/token-budget');
const { deploymentGateMiddleware, DeploymentGateError } = require('../middleware/deployment-gate');

const CLAUDE_BIN  = process.env.CLAUDE_BIN  || '/usr/local/bin/claude-bin';
const CLAUDE_UID  = parseInt(process.env.CLAUDE_UID  || '1001', 10);
const CLAUDE_GID  = parseInt(process.env.CLAUDE_GID  || '1001', 10);
const CLAUDE_HOME = process.env.CLAUDE_HOME || '/home/claudebot';

/**
 * Initialize and run a single subagent.
 * Returns a promise that resolves with { subagentId, type, output, duration }.
 */
async function init({ type, ticker, workspace, threadId, prompt, notify, mode, force, useBatch = false }) {
  // ── DEPLOYMENT GATE ─────────────────────────────────────────────────────────
  let effectiveMode;
  try {
    effectiveMode = deploymentGateMiddleware(type, mode, prompt);
  } catch (err) {
    if (err instanceof DeploymentGateError) {
      console.log(`[DeploymentGate] BLOCKED ${type}/${mode}: ${err.message.split('\n')[0]}`);
      if (typeof notify === 'function') {
        await notify(
          `⛔ **Agent blocked** — ${err.message}\n\nThe agent layer is only active for DEPLOY and REPORT tasks.`
        ).catch(() => {});
      }
      return { blocked: true, reason: err.message };
    }
    throw err;
  }
  // ────────────────────────────────────────────────────────────────────────────

  const subagentId = uuidv4();
  const typeDef = types.getType(type);
  if (!typeDef) throw new Error(`Unknown subagent type: ${type}`);

  // Inject token budget as prompt instruction
  const tokenBudgetNote = typeDef.maxTokens
    ? `\n\nOPERATING CONSTRAINT: Your entire response must be under ${typeDef.maxTokens} tokens. Be direct and structured — no prose padding.`
    : '';
  const fullPrompt = types.buildPrompt(type, ticker, workspace, prompt) + tokenBudgetNote;
  const workDir    = workspace || process.env.OPENCLAW_DIR || '/root/openclaw';

  // Merge default + per-type session pruning config
  const typesConfig   = require('../config/subagent-types.json');
  const pruningConfig = { ...(typesConfig.defaults?.sessionPruning || {}), ...(typeDef.sessionPruning || {}) };

  // Check provider rate limit state before spawning
  const providerReady = await checkProviderReady('anthropic').catch(() => ({ ready: true, waitMs: 0 }));
  if (!providerReady.ready) {
    console.warn(`[swarm] Anthropic rate limited — waiting ${Math.round(providerReady.waitMs / 1000)}s before ${type}`);
    if (notify) notify(`⏳ Rate limited — waiting ${Math.round(providerReady.waitMs / 1000)}s before ${type}`);
    await new Promise(r => setTimeout(r, providerReady.waitMs));
  }

  // Batch path — 50% cheaper, no tool calls, scheduled runs only
  const batchEligible = useBatch && batch.BATCH_ELIGIBLE_TYPES.has(type) && !batch.BATCH_NEVER_TYPES.has(type);
  if (batchEligible) {
    return _initBatch({ subagentId, type, ticker, workspace, threadId, prompt: fullPrompt, notify, typeDef });
  }

  // Register as active
  await pipelineActivity.registerAgentActive(workspace, type, threadId).catch(() => {});

  console.log(`[swarm] Starting ${type} subagent ${subagentId}`);
  if (notify && type !== 'botjohn') notify(`⚙️ ${capitalize(type)} started`);

  await setSubagentStatus(subagentId, {
    id: subagentId, type, ticker, status: 'running',
    startedAt: Date.now(), threadId,
  });

  const checkpointId = await checkpoints.save(threadId, type, ticker, { prompt, workspace }).catch(() => null);
  const startTime    = Date.now();

  return new Promise((resolve, reject) => {
    const model     = typeDef.model       || 'claude-sonnet-4-6';
    const effort    = typeDef.effortLevel || 'medium';
    const maxBudget = typeDef.maxBudgetUsd ?? null;

    const args = [
      '--dangerously-skip-permissions',
      '-p', fullPrompt,
      '--output-format', 'json',
      '--model', model,
      '--effort', effort,
    ];
    if (maxBudget != null) args.push('--max-budget-usd', String(maxBudget));

    const child = spawn(CLAUDE_BIN, args, {
      uid: CLAUDE_UID,
      gid: CLAUDE_GID,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        HOME: CLAUDE_HOME,
        CLAUDE_HOME,
        TICKER:         ticker,
        WORKSPACE:      workspace,
        PRUNING_CONFIG: JSON.stringify(pruningConfig),
      },
      cwd: workDir,
    });

    let stdout = '';
    let stderr = '';
    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });

    const timeoutSec = process.env.SUBAGENT_TIMEOUT_S ? parseInt(process.env.SUBAGENT_TIMEOUT_S, 10) : null;
    const timeout    = timeoutSec
      ? setTimeout(() => {
          child.kill('SIGTERM');
          reject(new Error(`${type} subagent timed out after ${timeoutSec}s`));
        }, timeoutSec * 1000)
      : null;

    child.on('exit', async (code) => {
      if (timeout) clearTimeout(timeout);
      const durationMs = Date.now() - startTime;
      const duration   = Math.round(durationMs / 1000);

      let output  = stdout;
      let costUsd = null;
      let numTurns = null;
      try {
        const parsed = JSON.parse(stdout);
        output    = parsed.result ?? parsed.message ?? stdout;
        costUsd   = parsed.cost_usd ?? parsed.total_cost_usd ?? null;
        numTurns  = parsed.num_turns ?? null;
      } catch { /* text fallback */ }

      await setSubagentStatus(subagentId, {
        id: subagentId, type, ticker, status: code === 0 ? 'complete' : 'error',
        startedAt: startTime, duration, threadId, costUsd,
      });

      if (threadId) {
        await tokenDb.recordSubagent(threadId, subagentId, type, ticker, model, costUsd, durationMs, numTurns).catch(() => null);
      }
      if (checkpointId) await checkpoints.complete(checkpointId).catch(() => null);
      await pipelineActivity.registerAgentDone(workspace, type, threadId).catch(() => {});

      // Record token usage
      try {
        const parsed = JSON.parse(stdout);
        const usage  = parsed.usage || {};
        const tokIn  = usage.input_tokens  || usage.cache_creation_input_tokens || 0;
        const tokOut = usage.output_tokens || 0;
        if (tokIn + tokOut > 0) {
          await tokenBudget.recordUsage(workspace, type, tokIn, tokOut).catch(() => {});
        }
      } catch { /* ignore */ }

      if (code !== 0) {
        const rateLimitMatch = stderr.match(/429|rate.?limit|retry.?after[:\s]*(\d+)/i);
        if (rateLimitMatch) {
          const retryAfter = parseInt(rateLimitMatch[1] || '30', 10);
          await recordProviderRateLimit('anthropic', retryAfter).catch(() => null);
          console.warn(`[swarm] Rate limit detected for ${type} — recorded ${retryAfter}s cooldown`);
        }
        console.error(`[swarm] ${type} subagent ${subagentId} exited with code ${code}`);
        if (notify) notify(`⚠️ ${capitalize(type)} error (exit ${code})`);
        const errDetail = stderr.slice(0, 300) || stdout.slice(0, 300) || '(no output)';
        reject(new Error(`${type} exited with code ${code}: ${errDetail}`));
        return;
      }

      const costStr = costUsd != null ? ` | $${costUsd.toFixed(4)}` : '';
      console.log(`[swarm] ${type} subagent ${subagentId} complete in ${duration}s${costStr}`);

      const verification = await verifySubagentOutput(type, output, { ticker, subagentId }).catch(() => ({ verified: true, skipped: true }));
      const verifiedStr  = verification.skipped ? '' : verification.verified ? ' ✓' : ' ⚠UNVERIFIED';
      if (notify && type !== 'botjohn') notify(`✅ ${capitalize(type)} complete [${duration}s${costStr}${verifiedStr}]`);

      resolve({ subagentId, type, ticker, output, duration, costUsd, numTurns, verification });
    });
  });
}

/**
 * Batch API execution path — 50% cheaper, no tool calls, scheduled runs only.
 */
async function _initBatch({ subagentId, type, ticker, workspace, threadId, prompt, notify, typeDef }) {
  const model     = typeDef.model || 'claude-sonnet-4-6';
  const maxTokens = typeDef.maxTokens || 2000;
  const startTime = Date.now();

  console.log(`[swarm] Starting ${type} subagent ${subagentId} [BATCH MODE]`);
  if (notify) notify(`⚙️ ${capitalize(type)} started [batch]`);

  await setSubagentStatus(subagentId, {
    id: subagentId, type, ticker, status: 'running',
    startedAt: startTime, threadId, batchMode: true,
  });

  let batchId;
  try {
    batchId = await batch.submit([{
      customId: subagentId,
      model,
      maxTokens,
      messages: [{ role: 'user', content: prompt }],
    }]);
  } catch (err) {
    await setSubagentStatus(subagentId, { id: subagentId, type, ticker, status: 'error', startedAt: startTime, threadId });
    throw err;
  }

  if (notify) notify(`⏳ ${capitalize(type)} queued in batch ${batchId} — polling…`);

  const results  = await batch.poll(batchId);
  const myResult = results.find(r => r.custom_id === subagentId);
  const durationMs = Date.now() - startTime;
  const duration   = Math.round(durationMs / 1000);

  if (!myResult || myResult.result?.type === 'errored') {
    const errMsg = myResult?.result?.error?.message || 'Batch result missing or errored';
    await setSubagentStatus(subagentId, { id: subagentId, type, ticker, status: 'error', startedAt: startTime, duration, threadId });
    if (notify) notify(`⚠️ ${capitalize(type)} batch error: ${errMsg}`);
    throw new Error(`${type} batch failed: ${errMsg}`);
  }

  const content = myResult.result?.message?.content || [];
  const output  = content.map(b => (b.type === 'text' ? b.text : '')).join('').trim();

  const usage   = myResult.result?.message?.usage || {};
  const inputM  = (usage.input_tokens  || 0) / 1e6;
  const outputM = (usage.output_tokens || 0) / 1e6;
  const costUsd = (inputM * 1.5 + outputM * 7.5);

  await setSubagentStatus(subagentId, {
    id: subagentId, type, ticker, status: 'complete',
    startedAt: startTime, duration, threadId, costUsd,
  });

  if (threadId) {
    await tokenDb.recordSubagent(threadId, subagentId, type, ticker, model, costUsd, durationMs, null).catch(() => null);
  }

  const verification = await verifySubagentOutput(type, output, { ticker, subagentId }).catch(() => ({ verified: true, skipped: true }));
  const verifiedStr  = verification.skipped ? '' : verification.verified ? ' ✓' : ' ⚠UNVERIFIED';
  const costStr      = costUsd ? ` | $${costUsd.toFixed(4)}` : '';
  console.log(`[swarm] ${type} batch ${subagentId} complete in ${duration}s${costStr}${verifiedStr}`);
  if (notify) notify(`✅ ${capitalize(type)} [batch] complete [${duration}s${costStr}${verifiedStr}]`);

  return { subagentId, type, ticker, output, duration, costUsd, numTurns: null, verification, batchMode: true };
}

/**
 * Run multiple subagents in parallel with 2s stagger.
 */
async function parallel(configs) {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  return Promise.all(configs.map((cfg, idx) =>
    idx === 0 ? init(cfg) : sleep(idx * 2000).then(() => init(cfg))
  ));
}

/**
 * Run a full FundJohn cycle.
 * DataPipeline (hardcoded) → ResearchJohn → TradeJohn → BotJohn approval + digest.
 *
 * @param {Object} opts
 * @param {string}   opts.cycleDate      — ISO date string
 * @param {Object}   opts.portfolioState — current portfolio state
 * @param {Array}    opts.strategyList   — live/paper strategies from manifest.json
 * @param {string}   opts.memoDir        — absolute path to output/memos/
 * @param {string}   opts.reportPath     — absolute path for research report output
 * @param {string}   [opts.threadId]     — Discord thread ID
 * @param {Function} [opts.notify]       — Discord notify function
 */
async function runCycle({ cycleDate, portfolioState, strategyList, memoDir, reportPath, threadId, notify }) {
  const workspace   = process.env.OPENCLAW_DIR || '/root/openclaw';
  const signalsPath = path.join(workspace, 'output', 'signals', `${cycleDate}_signals.md`);

    if (notify) notify(`📊 Cycle ${cycleDate} — DataPipeline → ResearchJohn → TradeJohn`);

  // Step 1: Hardcoded data pipeline — strategy execution + memos (no LLM agent)
  if (notify) notify('📈 Running hardcoded data pipeline...');
  const runner = require('../../execution/runner');
  const dataResult = await runner.runDailyClose('default', memoDir);

  // Step 2: ResearchJohn — synthesize memos into research report
  const researchResult = await init({
    type:      'researchjohn',
    ticker:    cycleDate,
    workspace,
    threadId,
    notify,
    mode:      'PM_TASK',
    prompt:    `CYCLE_DATE=${cycleDate}\nMEMO_DIR=${memoDir}`,
  });

  // Step 3: TradeJohn — generate trade signals from research report
  const tradeResult = await init({
    type:      'tradejohn',
    ticker:    cycleDate,
    workspace,
    threadId,
    notify,
    mode:      'PM_TASK',
    prompt:    `CYCLE_DATE=${cycleDate}\nREPORT_PATH=${reportPath}\nPORTFOLIO_STATE=${JSON.stringify(portfolioState || {})}`,
  });

  // Step 4: BotJohn — review signals, approve/veto, post cycle digest to #ops
  const botResult = await init({
    type:      'botjohn',
    ticker:    cycleDate,
    workspace,
    threadId,
    notify,
    mode:      'PM_TASK',
    prompt:    `Cycle review for ${cycleDate}. Read AGENTS.md standing orders. Review trade signals at ${signalsPath}. Approve signals with EV > 0 within 3% NAV limit per SO-4. Auto-veto negative EV. Escalate any strategy with max_drawdown > 20% per SO-5. Post cycle digest to #ops.`,
  });

  return { cycleDate, dataResult, researchResult, tradeResult, botResult };
}

/**
 * Update a running subagent with a steering message.
 */
async function update(subagentId, message) {
  const { pushSteering } = require('../../database/redis');
  const status = await getSubagentStatus(subagentId);
  if (!status || status.status !== 'running') {
    console.warn(`[swarm] Cannot update subagent ${subagentId} — not running`);
    return;
  }
  await pushSteering(subagentId, message);
}

/**
 * Resume a subagent from a checkpoint.
 */
async function resume(checkpointId, additionalContext) {
  const checkpoint = await checkpoints.get(checkpointId);
  if (!checkpoint) throw new Error(`Checkpoint ${checkpointId} not found`);
  const { subagent_type, ticker, state } = checkpoint;
  const extraPrompt = additionalContext
    ? `[RESUME FROM CHECKPOINT]\n\n${additionalContext}\n\nPrevious state:\n${JSON.stringify(state.prompt || '')}`
    : `[RESUME FROM CHECKPOINT]\n\nPrevious state:\n${JSON.stringify(state.prompt || '')}`;
  return init({
    type:      subagent_type,
    ticker,
    workspace: state.workspace,
    threadId:  checkpoint.thread_id,
    prompt:    extraPrompt,
  });
}

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

module.exports = { init, parallel, runCycle, update, resume };
