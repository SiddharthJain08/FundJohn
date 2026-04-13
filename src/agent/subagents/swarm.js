'use strict';

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const { v4: uuidv4 } = require('uuid');
const { setSubagentStatus, getSubagentStatus, recordProviderRateLimit, checkProviderReady } = require('../../database/redis');
const { checkpoints, verdictCache } = require('../../database/postgres');
const tokenDb = require('../../database/tokens');
const { verifySubagentOutput, appendUnverifiedBanner } = require('../../security/verification');
const pipelineState = require('../../database/pipeline-state');
const types = require('./types');
const batch            = require('../../budget/batch');
const dataRequester    = require('../data-requester');
const pipelineActivity = require('../middleware/pipeline-activity');
const tokenBudget      = require('../middleware/token-budget');
const { deploymentGateMiddleware, DeploymentGateError } = require('../middleware/deployment-gate');

const CLAUDE_BIN = process.env.CLAUDE_BIN || '/usr/local/bin/claude-bin';
const CLAUDE_UID = parseInt(process.env.CLAUDE_UID || '1001', 10);
const CLAUDE_GID = parseInt(process.env.CLAUDE_GID || '1001', 10);
const CLAUDE_HOME = process.env.CLAUDE_HOME || '/home/claudebot';

/**
 * Initialize and run a single subagent.
 * useBatch: true — use Anthropic Batch API (50% cheaper, no tool calls, scheduled only).
 *   Only valid for BATCH_ELIGIBLE_TYPES. equity-analyst is always synchronous (veto authority).
 * Returns a promise that resolves with { subagentId, type, output, duration }.
 */
async function init({ type, ticker, workspace, threadId, prompt, notify, mode, force, useBatch = false }) {

  // ── DEPLOYMENT GATE — must be first, before any token consumption ──────────
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
  // ───────────────────────────────────────────────────────────────────────────

  const subagentId = uuidv4();
  const typeDef = types.getType(type);
  if (!typeDef) throw new Error(`Unknown subagent type: ${type}`);

  // Inject token budget as prompt instruction (enforced via model instruction, not CLI flag)
  const tokenBudgetNote = typeDef.maxTokens
    ? `\n\nOPERATING CONSTRAINT: Your entire response must be under ${typeDef.maxTokens} tokens. Be direct and structured — no prose padding.`
    : '';
  const fullPrompt = types.buildPrompt(type, ticker, workspace, prompt) + tokenBudgetNote;
  const workDir = workspace || process.env.OPENCLAW_DIR || '/root/openclaw';

  // Merge default + per-type session pruning config for middleware
  const typesConfig  = require('../config/subagent-types.json');
  const pruningConfig = { ...(typesConfig.defaults?.sessionPruning || {}), ...(typeDef.sessionPruning || {}) };

  // Check provider rate limit state before spawning
  const providerReady = await checkProviderReady('anthropic').catch(() => ({ ready: true, waitMs: 0 }));
  if (!providerReady.ready) {
    console.warn(`[swarm] Anthropic rate limited — waiting ${Math.round(providerReady.waitMs / 1000)}s before ${type}`);
    if (notify) notify(`⏳ Rate limited — waiting ${Math.round(providerReady.waitMs / 1000)}s before ${type} for ${ticker}`);
    await new Promise(r => setTimeout(r, providerReady.waitMs));
  }

  // Batch path — 50% cheaper, no tool calls, scheduled runs only
  const batchEligible = useBatch && batch.BATCH_ELIGIBLE_TYPES.has(type) && !batch.BATCH_NEVER_TYPES.has(type);
  if (batchEligible) {
    return _initBatch({ subagentId, type, ticker, workspace, threadId, prompt: fullPrompt, notify, typeDef });
  }

  // Register as active so the strategist yields when we're running
  if (type !== 'strategist') {
    await pipelineActivity.registerAgentActive(workspace, type, threadId).catch(() => {});
  }

  console.log(`[swarm] Starting ${type} subagent ${subagentId} for ${ticker}`);
  // botjohn is conversational — don't announce start/complete, just send his response
  if (notify && type !== 'botjohn') notify(`⚙️ ${capitalize(type)} subagent started for ${ticker}`);

  await setSubagentStatus(subagentId, {
    id: subagentId, type, ticker, status: 'running',
    startedAt: Date.now(), threadId,
  });

  const checkpointId = await checkpoints.save(threadId, type, ticker, { prompt, workspace }).catch(() => null);
  const startTime = Date.now();

  return new Promise((resolve, reject) => {
    const model      = typeDef.model        || 'claude-sonnet-4-6';
    const effort     = typeDef.effortLevel  || 'medium';
    const maxBudget  = typeDef.maxBudgetUsd ?? null;

    const args = [
      '--dangerously-skip-permissions',
      '-p', fullPrompt,
      '--output-format', 'json',
      '--model', model,
      '--effort', effort,
    ];

    // Hard cap per subagent — prevents a runaway agent from burning the budget
    if (maxBudget != null) args.push('--max-budget-usd', String(maxBudget));

    const child = spawn(CLAUDE_BIN, args, {
      uid: CLAUDE_UID,
      gid: CLAUDE_GID,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        HOME: CLAUDE_HOME,
        CLAUDE_HOME,
        TICKER: ticker,
        WORKSPACE: workspace,
        PRUNING_CONFIG: JSON.stringify(pruningConfig),
      },
      cwd: workDir,
    });

    let stdout = '';
    let stderr = '';

    child.stdout.on('data', (d) => { stdout += d.toString(); });
    child.stderr.on('data', (d) => { stderr += d.toString(); });

    // Timeout disabled during testing — set SUBAGENT_TIMEOUT_S env var to re-enable
    const timeoutSec = process.env.SUBAGENT_TIMEOUT_S ? parseInt(process.env.SUBAGENT_TIMEOUT_S, 10) : null;
    const timeout = timeoutSec
      ? setTimeout(() => {
          child.kill('SIGTERM');
          reject(new Error(`${type} subagent timed out after ${timeoutSec}s`));
        }, timeoutSec * 1000)
      : null;

    child.on('exit', async (code) => {
      if (timeout) clearTimeout(timeout);
      const durationMs = Date.now() - startTime;
      const duration   = Math.round(durationMs / 1000);

      // Parse JSON output — extract result text and cost
      let output = stdout;
      let costUsd = null;
      let numTurns = null;
      try {
        const parsed = JSON.parse(stdout);
        output   = parsed.result ?? parsed.message ?? stdout;
        costUsd  = parsed.cost_usd ?? parsed.total_cost_usd ?? null;
        numTurns = parsed.num_turns ?? null;
      } catch { /* text fallback */ }

      await setSubagentStatus(subagentId, {
        id: subagentId, type, ticker, status: code === 0 ? 'complete' : 'error',
        startedAt: startTime, duration, threadId, costUsd,
      });

      // Record cost to DB if task tracking is active
      if (threadId) {
        await tokenDb.recordSubagent(threadId, subagentId, type, ticker, model, costUsd, durationMs, numTurns).catch(() => null);
      }

      if (checkpointId) await checkpoints.complete(checkpointId).catch(() => null);

      // Deregister from pipeline activity tracker (always, even on error)
      if (type !== 'strategist') {
        await pipelineActivity.registerAgentDone(workspace, type, threadId).catch(() => {});
      }

      // Record token usage for budget monitoring
      try {
        const parsed = JSON.parse(stdout);
        const usage  = parsed.usage || {};
        const tokIn  = usage.input_tokens || usage.cache_creation_input_tokens || 0;
        const tokOut = usage.output_tokens || 0;
        if (tokIn + tokOut > 0) {
          await tokenBudget.recordUsage(workspace, type, tokIn, tokOut).catch(() => {});
        }
      } catch { /* stdout not JSON or no usage field */ }

      if (code !== 0) {
        // Detect 429 rate limit in stderr — update provider state for all subsequent agents
        const rateLimitMatch = stderr.match(/429|rate.?limit|retry.?after[:\s]*(\d+)/i);
        if (rateLimitMatch) {
          const retryAfter = parseInt(rateLimitMatch[1] || '30', 10);
          await recordProviderRateLimit('anthropic', retryAfter).catch(() => null);
          console.warn(`[swarm] Rate limit detected for ${type} — recorded ${retryAfter}s cooldown`);
        }
        console.error(`[swarm] ${type} subagent ${subagentId} exited with code ${code}`);
        if (notify) notify(`⚠️ ${capitalize(type)} subagent error for ${ticker} (exit ${code})`);
        const errDetail = stderr.slice(0, 300) || stdout.slice(0, 300) || '(no output)';
        reject(new Error(`${type} exited with code ${code}: ${errDetail}`));
        return;
      }

      const costStr = costUsd != null ? ` | $${costUsd.toFixed(4)}` : '';
      console.log(`[swarm] ${type} subagent ${subagentId} complete in ${duration}s${costStr}`);

      // Output verification — marks UNVERIFIED in DB, never blocks pipeline
      const verification = await verifySubagentOutput(type, output, { ticker, subagentId }).catch(() => ({ verified: true, skipped: true }));
      const verifiedStr = verification.skipped ? '' : verification.verified ? ' ✓' : ' ⚠UNVERIFIED';
      if (notify && type !== 'botjohn') notify(`✅ ${capitalize(type)} complete for ${ticker} [${duration}s${costStr}${verifiedStr}]`);

      resolve({ subagentId, type, ticker, output, duration, costUsd, numTurns, verification });
    });
  });
}

/**
 * Batch API execution path — submits prompt directly to Anthropic batch API.
 * No tool calls, no claude-bin spawn. 50% cheaper for scheduled synthesis tasks.
 * Returns same shape as init() so callers are transparent to the execution mode.
 */
async function _initBatch({ subagentId, type, ticker, workspace, threadId, prompt, notify, typeDef }) {
  const model     = typeDef.model || 'claude-sonnet-4-6';
  const maxTokens = typeDef.maxTokens || 2000;
  const startTime = Date.now();

  console.log(`[swarm] Starting ${type} subagent ${subagentId} for ${ticker} [BATCH MODE]`);
  if (notify) notify(`⚙️ ${capitalize(type)} subagent started for ${ticker} [batch]`);

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

  if (notify) notify(`⏳ ${capitalize(type)} queued in batch ${batchId} for ${ticker} — polling…`);

  const results = await batch.poll(batchId);
  const myResult = results.find(r => r.custom_id === subagentId);

  const durationMs = Date.now() - startTime;
  const duration   = Math.round(durationMs / 1000);

  if (!myResult || myResult.result?.type === 'errored') {
    const errMsg = myResult?.result?.error?.message || 'Batch result missing or errored';
    await setSubagentStatus(subagentId, { id: subagentId, type, ticker, status: 'error', startedAt: startTime, duration, threadId });
    if (notify) notify(`⚠️ ${capitalize(type)} batch error for ${ticker}: ${errMsg}`);
    throw new Error(`${type} batch failed: ${errMsg}`);
  }

  // Extract text from batch response
  const content = myResult.result?.message?.content || [];
  const output  = content.map(b => (b.type === 'text' ? b.text : '')).join('').trim();

  // Estimate cost from usage (batch = 50% of standard)
  const usage   = myResult.result?.message?.usage || {};
  const inputM  = (usage.input_tokens  || 0) / 1e6;
  const outputM = (usage.output_tokens || 0) / 1e6;
  const costUsd = (inputM * 1.5 + outputM * 7.5); // batch pricing

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
  if (notify) notify(`✅ ${capitalize(type)} [batch] complete for ${ticker} [${duration}s${costStr}${verifiedStr}]`);

  return { subagentId, type, ticker, output, duration, costUsd, numTurns: null, verification, batchMode: true };
}

/**
 * Run multiple subagents in parallel with 2s stagger between launches.
 * Prevents burst patterns that trigger provider-level rate limits.
 */
async function parallel(configs) {
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  return Promise.all(configs.map((cfg, idx) =>
    idx === 0 ? init(cfg) : sleep(idx * 2000).then(() => init(cfg))
  ));
}

/**
 * Run the full trade pipeline sequentially with circuit breaker.
 * data-prep → [VALIDATE] → compute || equity-analyst (parallel) → report-builder
 *
 * scheduled: true — enables batch API for eligible stages (research, data-prep, compute, report-builder).
 *   equity-analyst is ALWAYS synchronous (veto authority requires real-time flow).
 */
async function runTradePipeline(ticker, workspace, taskDir, threadId, notify, runId = null, { scheduled = false } = {}) {
  console.log(`[swarm] Trade pipeline starting for ${ticker}${scheduled ? ' [scheduled/batch-eligible]' : ''}`);

  // 1. data-prep
  await pipelineState.advanceStage(runId, 'data-prep').catch(() => null);
  const dataPrepResult = await init({
    type: 'data-prep', ticker, workspace, threadId, notify, useBatch: scheduled,
  });

  // 2. Circuit breaker: validate
  await pipelineState.advanceStage(runId, 'validation', 'data-prep', dataPrepResult.output).catch(() => null);
  const manifestPath = path.join(taskDir, 'data', 'DATA_MANIFEST.json');
  if (!fs.existsSync(manifestPath)) {
    const err = `DATA_MANIFEST.json not found in ${taskDir}/data/`;
    if (notify) notify(`⛔ DATA VALIDATION FAILED for ${ticker}: ${err}`);
    await pipelineState.failRun(runId, err).catch(() => null);
    return { status: 'ABORTED', reason: 'data_validation_failed', errors: [err] };
  }

  // 3. compute + equity-analyst in parallel — 2s stagger to avoid burst rate limits
  // equity-analyst: NEVER batch (veto authority requires synchronous flow)
  // compute: batch-eligible for scheduled runs
  let computeResult, analystResult;
  try {
    await pipelineState.advanceStage(runId, 'compute+equity-analyst').catch(() => null);
    [computeResult, analystResult] = await Promise.all([
      init({ type: 'compute', ticker, workspace, threadId, notify, useBatch: scheduled }),
      new Promise(r => setTimeout(r, 2000)).then(() => init({ type: 'equity-analyst', ticker, workspace, threadId, notify, useBatch: false })),
    ]);
  } catch (err) {
    if (notify) notify(`⛔ Pipeline error for ${ticker}: ${err.message}`);
    await pipelineState.failRun(runId, err.message).catch(() => null);
    return { status: 'ABORTED', reason: err.message };
  }

  // 4. report-builder — batch-eligible for scheduled runs (pure synthesis, no tool calls needed)
  await pipelineState.advanceStage(runId, 'report-builder',
    'compute+equity-analyst', { compute: computeResult.output, analyst: analystResult.output }
  ).catch(() => null);
  const reportResult = await init({ type: 'report-builder', ticker, workspace, threadId, notify, useBatch: scheduled });

  // If report-builder was unverified, append warning banner to the report file
  if (reportResult.verification && !reportResult.verification.verified) {
    const reportPath = path.join(workspace, 'results', `${ticker}-report.md`);
    appendUnverifiedBanner(reportPath);
    if (notify) notify(`⚠️ report-builder output UNVERIFIED for ${ticker} — banner appended to report`);
  }

  return { status: 'COMPLETE', dataPrepResult, computeResult, analystResult, reportResult };
}

/**
 * Run a full diligence run: research + data-prep in parallel, then trade pipeline.
 * Creates a durable pipeline_state record; gates verdict cache on full completion.
 *
 * scheduled: true — operator-not-present run (daily cron). Enables batch API for
 *   BATCH_ELIGIBLE_TYPES (research, data-prep, compute, report-builder).
 *   equity-analyst is always synchronous.
 */
async function runDiligence(ticker, workspace, threadId, notify, { scheduled = false } = {}) {
  const taskDir = path.join(workspace, 'work', `${ticker}-diligence`);
  fs.mkdirSync(path.join(taskDir, 'data'), { recursive: true });
  fs.mkdirSync(path.join(taskDir, 'charts'), { recursive: true });

  // Create durable run record before spawning anything
  const budgetMode = await require('../../database/redis').getClient().get('budget:mode').catch(() => 'GREEN') || 'GREEN';
  const runId = await pipelineState.startRun({ skillName: 'diligence', ticker, budgetMode }).catch(() => null);

  if (notify) notify(`🦞 Diligence started for ${ticker} — spawning research + data-prep in parallel`);
  await pipelineState.advanceStage(runId, 'research').catch(() => null);

  // Phase 1: parallel research — batch-eligible for scheduled runs
  const [researchResult] = await Promise.all([
    init({ type: 'research', ticker, workspace, threadId, notify, useBatch: scheduled }),
  ]);

  // Fulfill any DATA_REQUEST blocks emitted by the research agent
  const dataRequestResult = await dataRequester.fulfill(
    researchResult.output, taskDir, notify
  ).catch(err => {
    console.warn(`[swarm] data-requester error: ${err.message}`);
    return { fulfilled: [], pending: [], errors: [], reportSection: '' };
  });

  if (dataRequestResult.fulfilled.length > 0 || dataRequestResult.pending.length > 0) {
    console.log(`[swarm] Data requests: ${dataRequestResult.fulfilled.length} fulfilled, ${dataRequestResult.pending.length} pending`);
  }

  await pipelineState.advanceStage(runId, 'data-prep', 'research', researchResult.output).catch(() => null);

  // Phase 2: trade pipeline — passes runId + scheduled flag for stage tracking + batch routing
  const pipelineResult = await runTradePipeline(ticker, workspace, taskDir, threadId, notify, runId, { scheduled });

  // Verdict cache only written after full successful pipeline + all verifications passed
  let verdictWritten = false;
  if (pipelineResult.status === 'COMPLETE') {
    const allVerified = [
      pipelineResult.dataPrepResult?.verification,
      pipelineResult.computeResult?.verification,
      pipelineResult.analystResult?.verification,
      pipelineResult.reportResult?.verification,
    ].every(v => !v || v.verified || v.skipped);

    if (allVerified) {
      // Verdict cache write happens in report-builder — just flag it here
      verdictWritten = true;
    } else {
      console.warn(`[swarm] Verdict cache NOT written for ${ticker} — one or more stages unverified`);
      if (notify) notify(`⚠️ Verdict cache skipped for ${ticker} — unverified stage output. Re-run to refresh.`);
    }
    await pipelineState.completeRun(runId, { verdictWritten }).catch(() => null);
  }

  return { runId, researchResult, pipelineResult, verdictWritten, dataRequestResult };
}

/**
 * Update a running subagent with a new steering message.
 * Pushes message into Redis steering queue — agent sees it on next LLM call.
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
    type: subagent_type,
    ticker,
    workspace: state.workspace,
    threadId: checkpoint.thread_id,
    prompt: extraPrompt,
  });
}

function capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

module.exports = { init, parallel, runTradePipeline, runDiligence, update, resume };
