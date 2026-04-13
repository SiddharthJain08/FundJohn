'use strict';

/**
 * token-budget.js — Token Budget Manager
 *
 * File-based IPC: state lives in output/session/state.json so both the
 * parent process (index.js) and worker child processes (orchestrator,
 * trade-pipeline, pipeline-runner) share the same budget view.
 *
 * Controller methods (called from index.js):
 *   startSession(opts), endSession(), halt(reason), resume(),
 *   setSpeed(multiplier), getStatus()
 *
 * Worker methods (called from orchestrator, trade-pipeline):
 *   checkpoint(agentId)   — waits if halted, throws if session expired
 *   recordUsage(agentId, model, promptChars, outputChars)
 *   isSessionActive()
 */

const fs   = require('fs');
const path = require('path');

const WORKDIR      = process.env.OPENCLAW_DIR || '/root/openclaw';
const SESSION_DIR  = path.join(WORKDIR, 'output', 'session');
const STATE_FILE   = path.join(SESSION_DIR, 'state.json');
const HALT_FILE    = path.join(SESSION_DIR, 'halt');
const SPEED_FILE   = path.join(SESSION_DIR, 'speed');

// ── Token cost estimates (USD per million tokens) ──────────────────────────
const TOKEN_COSTS = {
  'claude-haiku-4-5-20251001': { input: 0.80,  output: 4.00  },
  'claude-haiku-4-5':          { input: 0.80,  output: 4.00  },
  'claude-sonnet-4-6':         { input: 3.00,  output: 15.00 },
  'claude-sonnet-4-5':         { input: 3.00,  output: 15.00 },
  'claude-opus-4-6':           { input: 15.00, output: 75.00 },
  'default':                   { input: 3.00,  output: 15.00 },
};

// Rate limits (tokens per minute) — used for throttle delay calculations
const RATE_LIMITS_TPM = {
  'claude-haiku-4-5-20251001': 200000,
  'claude-haiku-4-5':          200000,
  'claude-sonnet-4-6':          40000,
  'claude-sonnet-4-5':          40000,
  'default':                    40000,
};

// 1 token ≈ 4 characters (English text estimate)
const CHARS_PER_TOKEN = 4;

// ── File helpers ───────────────────────────────────────────────────────────
function ensureDir() {
  try { fs.mkdirSync(SESSION_DIR, { recursive: true }); } catch { /* ok */ }
}

function readState() {
  try { return JSON.parse(fs.readFileSync(STATE_FILE, 'utf8')); } catch { return null; }
}

function writeState(state) {
  ensureDir();
  fs.writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

function isHalted() {
  try { fs.accessSync(HALT_FILE); return true; } catch { return false; }
}

function getSpeed() {
  try { return parseFloat(fs.readFileSync(SPEED_FILE, 'utf8').trim()) || 1.0; } catch { return 1.0; }
}

// ── Cost estimation ─────────────────────────────────────────────────────────
function estimateCost(model, inputTokens, outputTokens) {
  const rates = TOKEN_COSTS[model] || TOKEN_COSTS['default'];
  return (inputTokens * rates.input + outputTokens * rates.output) / 1_000_000;
}

function charsToTokens(chars) {
  return Math.ceil(chars / CHARS_PER_TOKEN);
}

// ── Controller API (index.js) ───────────────────────────────────────────────

/**
 * Start a new budget session.
 * @param {object} opts
 * @param {number} opts.durationHours   — how long to run (wall clock)
 * @param {number} [opts.maxCostUSD]    — dollar cap (default: no cap)
 * @param {number[]} [opts.alertPct]    — alert at these cost % thresholds
 * @param {number} [opts.haltPct]       — auto-halt at this cost % of cap
 * @param {string[]} [opts.tickers]     — tickers queued for the runner
 * @param {number} [opts.intervalMin]   — runner re-scan interval (minutes)
 */
function startSession(opts = {}) {
  ensureDir();

  // Clear any leftover halt/speed files
  try { fs.unlinkSync(HALT_FILE); } catch { /* ok */ }
  try { fs.writeFileSync(SPEED_FILE, '1.0'); } catch { /* ok */ }

  const state = {
    active:          true,
    startTime:       new Date().toISOString(),
    durationMs:      (opts.durationHours || 1) * 3_600_000,
    endTime:         new Date(Date.now() + (opts.durationHours || 1) * 3_600_000).toISOString(),
    maxCostUSD:      opts.maxCostUSD || null,
    alertPct:        opts.alertPct   || [75, 90],
    haltPct:         opts.haltPct    || 95,
    tickers:         opts.tickers    || [],
    intervalMin:     opts.intervalMin || 30,
    speedMultiplier: 1.0,
    halted:          false,
    haltReason:      null,
    alertsFired:     [],
    usage: {
      total: { spawns: 0, inputTokens: 0, outputTokens: 0, estimatedCostUSD: 0 },
      byAgent: {},
    },
    runnerPid: null,
  };

  writeState(state);
  return state;
}

function endSession() {
  try { fs.unlinkSync(HALT_FILE); } catch { /* ok */ }
  try { fs.unlinkSync(SPEED_FILE); } catch { /* ok */ }
  const state = readState();
  if (state) {
    state.active = false;
    state.endedAt = new Date().toISOString();
    writeState(state);
  }
}

function halt(reason = 'operator halt') {
  ensureDir();
  fs.writeFileSync(HALT_FILE, reason);
  const state = readState();
  if (state) {
    state.halted     = true;
    state.haltReason = reason;
    writeState(state);
  }
}

function resume() {
  try { fs.unlinkSync(HALT_FILE); } catch { /* ok */ }
  const state = readState();
  if (state) {
    state.halted     = false;
    state.haltReason = null;
    writeState(state);
  }
}

/**
 * @param {number} multiplier  0.25=very slow, 0.5=slow, 1.0=normal, 2.0=fast
 */
function setSpeed(multiplier) {
  const clamped = Math.max(0.1, Math.min(3.0, multiplier));
  ensureDir();
  fs.writeFileSync(SPEED_FILE, String(clamped));
  const state = readState();
  if (state) {
    state.speedMultiplier = clamped;
    writeState(state);
  }
  return clamped;
}

function getStatus() {
  const state = readState();
  if (!state || !state.active) return null;

  const now       = Date.now();
  const startMs   = new Date(state.startTime).getTime();
  const elapsed   = now - startMs;
  const remaining = Math.max(0, state.durationMs - elapsed);
  const pctTime   = Math.min(100, (elapsed / state.durationMs) * 100);

  const cost      = state.usage.total.estimatedCostUSD;
  const maxCost   = state.maxCostUSD;
  const pctCost   = maxCost ? Math.min(100, (cost / maxCost) * 100) : null;

  const byAgent   = state.usage.byAgent || {};
  const agentRows = Object.entries(byAgent)
    .sort((a, b) => b[1].estimatedCostUSD - a[1].estimatedCostUSD)
    .map(([id, u]) => ({
      id,
      runs:     u.runs || 0,
      tokens:   (u.inputTokens || 0) + (u.outputTokens || 0),
      costUSD:  (u.estimatedCostUSD || 0).toFixed(4),
    }));

  return {
    active:          state.active,
    halted:          state.halted,
    haltReason:      state.haltReason,
    speedMultiplier: getSpeed(),
    startTime:       state.startTime,
    endTime:         state.endTime,
    elapsedMs:       elapsed,
    remainingMs:     remaining,
    pctTimeUsed:     pctTime.toFixed(1),
    totalSpawns:     state.usage.total.spawns,
    totalTokens:     (state.usage.total.inputTokens || 0) + (state.usage.total.outputTokens || 0),
    estimatedCostUSD: cost.toFixed(4),
    maxCostUSD:      maxCost,
    pctCostUsed:     pctCost !== null ? pctCost.toFixed(1) : null,
    byAgent:         agentRows,
    tickers:         state.tickers || [],
  };
}

// ── Worker API (orchestrator, trade-pipeline) ───────────────────────────────

/**
 * Returns true if a budget session is currently active.
 */
function isSessionActive() {
  const state = readState();
  return !!(state && state.active);
}

/**
 * Checkpoint before spawning an agent.
 * - If halted: polls until resumed or session expires
 * - If session time expired: throws
 * - Applies speed throttle delay
 * @param {string} agentId
 * @param {string} [model]
 */
async function checkpoint(agentId, model = 'default') {
  if (!isSessionActive()) return; // no active session — passthrough

  // Wait if halted
  while (isHalted()) {
    const state = readState();
    if (!state || !state.active) throw new Error('Budget session ended while halted');
    await new Promise(r => setTimeout(r, 5000)); // poll every 5s
  }

  // Check session time
  const state = readState();
  if (!state || !state.active) return;

  const elapsed   = Date.now() - new Date(state.startTime).getTime();
  const remaining = state.durationMs - elapsed;

  if (remaining <= 0) {
    halt('Session time expired');
    throw new Error(`Budget session expired (${(state.durationMs / 3_600_000).toFixed(1)}h limit reached)`);
  }

  // Check cost cap
  if (state.maxCostUSD) {
    const cost    = state.usage.total.estimatedCostUSD || 0;
    const pctUsed = (cost / state.maxCostUSD) * 100;
    if (pctUsed >= (state.haltPct || 95)) {
      halt(`Cost cap reached: $${cost.toFixed(2)} of $${state.maxCostUSD}`);
      throw new Error(`Budget cost cap reached ($${cost.toFixed(2)} / $${state.maxCostUSD})`);
    }
  }

  // Speed throttle — slow mode adds inter-spawn delay
  const speed = getSpeed();
  if (speed < 1.0) {
    const delay = Math.round((1 / speed - 1) * 4000); // up to ~36s at 0.1x
    if (delay > 0) await new Promise(r => setTimeout(r, delay));
  }
}

/**
 * Record token usage after an agent completes.
 * @param {string} agentId
 * @param {string} model
 * @param {number} promptChars    — input prompt character count (used for estimation)
 * @param {number} outputChars    — output character count
 */
function recordUsage(agentId, model, promptChars, outputChars) {
  if (!isSessionActive()) return;

  const inputTokens  = charsToTokens(promptChars);
  const outputTokens = charsToTokens(outputChars);
  const cost         = estimateCost(model, inputTokens, outputTokens);

  // Atomic-ish update: read → modify → write
  const state = readState();
  if (!state || !state.active) return;

  // Total
  state.usage.total.spawns           = (state.usage.total.spawns || 0) + 1;
  state.usage.total.inputTokens      = (state.usage.total.inputTokens || 0) + inputTokens;
  state.usage.total.outputTokens     = (state.usage.total.outputTokens || 0) + outputTokens;
  state.usage.total.estimatedCostUSD = (state.usage.total.estimatedCostUSD || 0) + cost;

  // By agent
  if (!state.usage.byAgent[agentId]) {
    state.usage.byAgent[agentId] = { runs: 0, inputTokens: 0, outputTokens: 0, estimatedCostUSD: 0, model };
  }
  state.usage.byAgent[agentId].runs++;
  state.usage.byAgent[agentId].inputTokens      += inputTokens;
  state.usage.byAgent[agentId].outputTokens     += outputTokens;
  state.usage.byAgent[agentId].estimatedCostUSD += cost;

  writeState(state);

  // Check alert thresholds
  if (state.maxCostUSD) {
    const pctCost   = (state.usage.total.estimatedCostUSD / state.maxCostUSD) * 100;
    const alertsFired = state.alertsFired || [];
    const alertThresholds = Array.isArray(state.alertPct) ? state.alertPct : (state.alertPct ? [state.alertPct] : [75, 90]);
    for (const threshold of alertThresholds) {
      if (pctCost >= threshold && !alertsFired.includes(threshold)) {
        alertsFired.push(threshold);
        // Signal via a separate alert file that index.js can poll
        try {
          fs.writeFileSync(
            path.join(SESSION_DIR, `alert-${threshold}.json`),
            JSON.stringify({ threshold, cost: state.usage.total.estimatedCostUSD, ts: Date.now() })
          );
        } catch { /* non-fatal */ }
      }
    }
    state.alertsFired = alertsFired;
    writeState(state);
  }
}

// ── Format helpers ──────────────────────────────────────────────────────────

function formatDuration(ms) {
  if (ms < 0) return '0s';
  const h = Math.floor(ms / 3_600_000);
  const m = Math.floor((ms % 3_600_000) / 60_000);
  const s = Math.floor((ms % 60_000) / 1000);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatStatus(status) {
  if (!status) return '⚪ No active budget session.';

  const speedLabel = status.speedMultiplier >= 2.0 ? 'FAST' :
                     status.speedMultiplier >= 1.0 ? 'NORMAL' :
                     status.speedMultiplier >= 0.5 ? 'SLOW' : 'VERY SLOW';

  const stateIcon = status.halted ? '🛑 HALTED' : `▶ RUNNING (${speedLabel})`;

  let out = `## 📊 Token Budget Status\n\n`;
  out += `**State:** ${stateIcon}`;
  if (status.halted && status.haltReason) out += ` — ${status.haltReason}`;
  out += '\n\n';

  out += `**Time:** ${formatDuration(status.elapsedMs)} elapsed | ${formatDuration(status.remainingMs)} remaining (${status.pctTimeUsed}% used)\n`;
  out += `**Session end:** ${new Date(status.endTime).toUTCString()}\n\n`;

  out += `**Tokens:** ${status.totalTokens.toLocaleString()} total (${status.totalSpawns} agent spawns)\n`;
  out += `**Estimated cost:** $${status.estimatedCostUSD}`;
  if (status.maxCostUSD) {
    out += ` of $${status.maxCostUSD} cap (${status.pctCostUsed}% used)`;
  }
  out += '\n\n';

  if (status.byAgent.length > 0) {
    out += `**Usage by agent:**\n`;
    out += `| Agent | Runs | Tokens | Cost |\n|-------|------|--------|------|\n`;
    for (const a of status.byAgent) {
      out += `| ${a.id} | ${a.runs} | ${a.tokens.toLocaleString()} | $${a.costUSD} |\n`;
    }
    out += '\n';
  }

  const tickerStr = Array.isArray(status.tickers)
    ? status.tickers.join(', ')
    : String(status.tickers || '');
  if (tickerStr) {
    out += `**Queued tickers:** ${tickerStr}\n`;
  }

  return out;
}

module.exports = {
  // Controller
  startSession,
  endSession,
  halt,
  resume,
  setSpeed,
  getStatus,
  formatStatus,
  // Worker
  isSessionActive,
  checkpoint,
  recordUsage,
  // Internals exposed for testing/monitor
  STATE_FILE,
  HALT_FILE,
  SPEED_FILE,
  SESSION_DIR,
};
