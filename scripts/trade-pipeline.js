#!/usr/bin/env node
'use strict';

/**
 * scripts/trade-pipeline.js — Layer 8: Quant Trade Pipeline
 *
 * Runs sequentially after the Orchestrator produces a diligence memo:
 *   Quant → Risk → Timing → Final Report
 *
 * Progress lines (stdout): TRADE_PROGRESS:{json}
 * Verdict line (stdout):   TRADE_VERDICT:{GO|WAIT|PASS|BLOCKED}
 *
 * Usage: node trade-pipeline.js <TICKER> [--memo /path/to/memo.md]
 */

const { spawn } = require('child_process');
const fs        = require('fs');
const path      = require('path');
const tokenBudget = require('./token-budget');

// ── Config ────────────────────────────────────────────────────────────────────
const CLAUDE_BIN    = process.env.CLAUDE_BIN    || '/usr/local/bin/claude-bin';
const CLAUDE_UID    = parseInt(process.env.CLAUDE_UID    || '1001', 10);
const CLAUDE_GID    = parseInt(process.env.CLAUDE_GID    || '1001', 10);
const CLAUDE_HOME   = process.env.CLAUDE_HOME   || '/home/claudebot';
const WORKDIR       = process.env.OPENCLAW_DIR  || '/root/openclaw';
const MEMOS_DIR     = path.join(WORKDIR, 'output', 'memos');
const TRADES_DIR    = path.join(WORKDIR, 'output', 'trades');
const STATE_FILE    = path.join(WORKDIR, 'output', 'portfolio', 'state.json');
const LEGACY_FILE   = path.join(WORKDIR, 'output', 'portfolio.json');
const AGENT_TIMEOUT = parseInt(process.env.TRADE_TIMEOUT_MS || '300000', 10);
const AGENT_MODEL   = 'claude-sonnet-4-6';

// ── CLI args ──────────────────────────────────────────────────────────────────
const cliArgs   = process.argv.slice(2);
const tickerArg = cliArgs.find(a => !a.startsWith('--'));
const memoFlag  = cliArgs.indexOf('--memo');
const memoArg   = memoFlag !== -1 ? cliArgs[memoFlag + 1] : null;

if (!tickerArg) {
  console.error('Usage: node trade-pipeline.js <TICKER> [--memo /path/to/memo.md]');
  process.exit(1);
}

const TICKER = tickerArg.toUpperCase();

// ── Helpers ───────────────────────────────────────────────────────────────────
function progress(event, data = {}) {
  process.stdout.write(`TRADE_PROGRESS:${JSON.stringify({ event, ticker: TICKER, ts: Date.now(), ...data })}\n`);
}

function log(msg) {
  process.stderr.write(`[${new Date().toISOString()}] [TRADE] [${TICKER}] ${msg}\n`);
}

function findLatestMemo(ticker) {
  try {
    const files = fs.readdirSync(MEMOS_DIR)
      .filter(f => f.startsWith(`${ticker}-diligence-`) && f.endsWith('.md'))
      .sort().reverse();
    return files.length ? path.join(MEMOS_DIR, files[0]) : null;
  } catch { return null; }
}

function loadPortfolio() {
  for (const p of [STATE_FILE, LEGACY_FILE]) {
    try { return fs.readFileSync(p, 'utf8'); } catch { /* try next */ }
  }
  return JSON.stringify({ positions: [], cash_pct: 100, message: 'No portfolio state file found' }, null, 2);
}

function buildAgentPrompt(agentDir, context) {
  const dir = path.join(WORKDIR, agentDir);
  let prompt = '';

  // Load SOUL.md as behavioral preamble
  try {
    const soul = fs.readFileSync(path.join(dir, 'SOUL.md'), 'utf8');
    prompt += `# Behavioral Rules for this Session\n${soul}\n\n---\n\n# Your Task\n\n`;
  } catch { /* optional */ }

  // Load PROMPT.md and replace all {{PLACEHOLDER}} tokens
  try {
    let task = fs.readFileSync(path.join(dir, 'PROMPT.md'), 'utf8');
    for (const [key, val] of Object.entries(context)) {
      task = task.replace(new RegExp(`\\{\\{${key}\\}\\}`, 'g'), val || '');
    }
    prompt += task;
  } catch {
    prompt += `Analyze ${TICKER} and produce your structured output. Follow SOUL.md rules exactly.`;
  }
  return prompt;
}

// ── Agent runner ──────────────────────────────────────────────────────────────
async function runAgent(agentDir, prompt) {
  // Token budget checkpoint — waits if halted, throws if session expired
  const agentName = path.basename(agentDir);
  try {
    await tokenBudget.checkpoint(agentName, AGENT_MODEL);
  } catch (budgetErr) {
    log(`BUDGET HALT agent=${agentName} reason="${budgetErr.message}"`);
    progress('BUDGET_HALT', { agent: agentName, reason: budgetErr.message });
    return { output: `⚠️ Budget halt: ${budgetErr.message}`, elapsed: 0, error: true };
  }

  return new Promise((resolve) => {
    const start = Date.now();
    let stdout  = '';
    let stderr  = '';

    log(`START agent=${agentName}`);

    const proc = spawn(
      CLAUDE_BIN,
      ['--dangerously-skip-permissions', '--model', AGENT_MODEL, '-p', prompt],
      {
        cwd: WORKDIR,   // .mcp.json lives here
        uid: CLAUDE_UID,
        gid: CLAUDE_GID,
        env: {
          ...process.env,
          HOME:         CLAUDE_HOME,
          USER:         'claudebot',
          LOGNAME:      'claudebot',
          SUDO_USER:    undefined,
          SUDO_UID:     undefined,
          SUDO_GID:     undefined,
          SUDO_COMMAND: undefined,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      }
    );

    proc.stdout.on('data', d => { stdout += d.toString(); });
    proc.stderr.on('data', d => { stderr += d.toString(); });

    const killer = setTimeout(() => {
      proc.kill('SIGTERM');
      const elapsed = Date.now() - start;
      log(`TIMEOUT agent=${agentName} elapsed=${(elapsed / 1000).toFixed(1)}s`);
      resolve({ output: `⚠️ Agent timed out after ${AGENT_TIMEOUT / 1000}s`, elapsed, error: true });
    }, AGENT_TIMEOUT);

    proc.on('close', (code) => {
      clearTimeout(killer);
      const elapsed = Date.now() - start;
      const success = code === 0 || !!stdout.trim();
      log(`END agent=${agentName} code=${code} elapsed=${(elapsed / 1000).toFixed(1)}s`);
      // Record token usage for the budget monitor
      tokenBudget.recordUsage(agentName, AGENT_MODEL, prompt.length, stdout.length);
      if (success) {
        resolve({ output: stdout.trim(), elapsed, error: false });
      } else {
        resolve({ output: `⚠️ Agent error (exit ${code}): ${stderr.trim() || 'no output'}`, elapsed, error: true });
      }
    });

    proc.on('error', (err) => {
      clearTimeout(killer);
      resolve({ output: `⚠️ Spawn error: ${err.message}`, elapsed: Date.now() - start, error: true });
    });
  });
}

// ── Parse signals ─────────────────────────────────────────────────────────────
function parseSignal(output, pattern) {
  return new RegExp(pattern, 'i').test(output);
}

function extractValue(output, pattern, fallback = '—') {
  const m = output.match(new RegExp(pattern, 'i'));
  return m ? m[1].trim() : fallback;
}

// ── Main pipeline ─────────────────────────────────────────────────────────────
async function main() {
  const startTime = Date.now();
  fs.mkdirSync(TRADES_DIR, { recursive: true });

  // Locate memo
  const memoPath = memoArg || findLatestMemo(TICKER);
  if (!memoPath) {
    const msg = `No diligence memo found for ${TICKER}. Run /diligence first.`;
    progress('PIPELINE_ERROR', { error: msg });
    console.error(msg);
    process.exit(1);
  }
  if (!fs.existsSync(memoPath)) {
    const msg = `Memo file not found: ${memoPath}`;
    progress('PIPELINE_ERROR', { error: msg });
    console.error(msg);
    process.exit(1);
  }

  const memoContent    = fs.readFileSync(memoPath, 'utf8');
  const portfolioState = loadPortfolio();
  const date           = new Date().toISOString().slice(0, 10);

  log(`PIPELINE START memo=${memoPath}`);
  progress('PIPELINE_START', { memoPath: path.basename(memoPath) });

  // ── Step 1: Quant ─────────────────────────────────────────────────────────
  progress('QUANT_START');
  log('STEP 1/3 — Quant');

  const quantPrompt = buildAgentPrompt('agents/quant', {
    TICKER,
    MEMO_CONTENT: memoContent.slice(0, 3500),
    PORTFOLIO_STATE: portfolioState,
  });

  const quantResult = await runAgent('agents/quant', quantPrompt);
  const quantFile   = path.join(TRADES_DIR, `${TICKER}-${date}-quant.md`);
  fs.writeFileSync(quantFile, `# Quant — ${TICKER}\n\n${quantResult.output}`);

  const negativeEV = parseSignal(quantResult.output, '\\[NEGATIVE EV');
  const quantRec   = extractValue(quantResult.output, 'RECOMMENDATION:\\s*(\\w+)');
  const evRatio    = extractValue(quantResult.output, 'EV\\/Risk Ratio:\\s*([\\d.]+)x');
  const quantSize  = extractValue(quantResult.output, 'Max Position Size:\\s*([\\d.]+)%');

  progress('QUANT_COMPLETE', {
    recommendation: quantRec,
    evRatio,
    sizePct:     quantSize,
    negativeEV,
    file:        quantFile,
    elapsed:     quantResult.elapsed,
    error:       quantResult.error,
  });

  // Stop if quant recommends pass
  if (negativeEV || quantRec === 'PASS') {
    log('PIPELINE STOP — Quant PASS (negative EV or explicit PASS)');
    const final = `# Trade Report — ${TICKER}\n\n**Status: STOPPED — Quant recommends PASS**\n\n---\n\n## 📐 Quant\n\n${quantResult.output}\n`;
    const finalFile = path.join(TRADES_DIR, `${TICKER}-${date}-final.md`);
    fs.writeFileSync(finalFile, final);
    progress('FINAL_REPORT', { verdict: 'PASS', file: finalFile, elapsed: Date.now() - startTime });
    process.stdout.write(final);
    process.stdout.write(`\nTRADE_VERDICT:PASS\n`);
    return;
  }

  // ── Step 2: Risk ──────────────────────────────────────────────────────────
  progress('RISK_START');
  log('STEP 2/3 — Risk');

  const riskPrompt = buildAgentPrompt('agents/risk', {
    TICKER,
    PORTFOLIO_STATE: portfolioState,
    QUANT_OUTPUT:    quantResult.output.slice(0, 2500),
  });

  const riskResult = await runAgent('agents/risk', riskPrompt);
  const riskFile   = path.join(TRADES_DIR, `${TICKER}-${date}-risk.md`);
  fs.writeFileSync(riskFile, `# Risk Review — ${TICKER}\n\n${riskResult.output}`);

  const isBlocked    = parseSignal(riskResult.output, '\\[TRADE BLOCKED\\]');
  const isReduced    = parseSignal(riskResult.output, '\\[SIZE REDUCED\\]');
  const riskDecision = extractValue(riskResult.output, 'DECISION:\\s*(\\w+)');
  const riskScore    = extractValue(riskResult.output, 'RISK SCORE:\\s*(\\d+)');

  progress('RISK_COMPLETE', {
    decision:  riskDecision,
    riskScore,
    blocked:   isBlocked,
    reduced:   isReduced,
    file:      riskFile,
    elapsed:   riskResult.elapsed,
    error:     riskResult.error,
  });

  if (isBlocked) {
    log('PIPELINE STOP — Risk agent blocked the trade');
    const final = `# Trade Report — ${TICKER}\n\n**Status: BLOCKED by Risk**\n\n---\n\n## 📐 Quant\n\n${quantResult.output}\n\n---\n\n## 🛡️ Risk\n\n${riskResult.output}\n`;
    const finalFile = path.join(TRADES_DIR, `${TICKER}-${date}-final.md`);
    fs.writeFileSync(finalFile, final);
    progress('FINAL_REPORT', { verdict: 'BLOCKED', file: finalFile, elapsed: Date.now() - startTime, reduced: false, blocked: true });
    process.stdout.write(final);
    process.stdout.write(`\nTRADE_VERDICT:BLOCKED\n`);
    return;
  }

  // ── Step 3: Timing ────────────────────────────────────────────────────────
  progress('TIMING_START');
  log('STEP 3/3 — Timing');

  const timingPrompt = buildAgentPrompt('agents/timing', {
    TICKER,
    QUANT_OUTPUT: quantResult.output.slice(0, 2500),
    RISK_OUTPUT:  riskResult.output.slice(0, 2500),
  });

  const timingResult = await runAgent('agents/timing', timingPrompt);
  const timingFile   = path.join(TRADES_DIR, `${TICKER}-${date}-timing.md`);
  fs.writeFileSync(timingFile, `# Timing Signal — ${TICKER}\n\n${timingResult.output}`);

  const signal          = extractValue(timingResult.output, 'SIGNAL:\\s*(\\w+)');
  const earningsWarning = parseSignal(timingResult.output, '\\[EARNINGS WARNING');

  progress('TIMING_COMPLETE', {
    signal,
    earningsWarning,
    file:    timingFile,
    elapsed: timingResult.elapsed,
    error:   timingResult.error,
  });

  // ── Step 4: Final Report ──────────────────────────────────────────────────
  const totalElapsed = ((Date.now() - startTime) / 1000).toFixed(1);
  const verdict      = signal === 'GO' ? 'GO' : signal === 'WAIT' ? 'WAIT' : 'PASS';

  const finalReport = `# Trade Report — ${TICKER}
*Generated: ${date} | Pipeline elapsed: ${totalElapsed}s*

---

## 📐 Quant — Trade Structure

${quantResult.output}

---

## 🛡️ Risk — Portfolio Risk Review

${riskResult.output}

---

## 🎯 Timing — Entry Signal

${timingResult.output}

---

*OpenClaw Trade Pipeline (Layer 8) — scripts/trade-pipeline.js*
`;

  const finalFile = path.join(TRADES_DIR, `${TICKER}-${date}-final.md`);
  fs.writeFileSync(finalFile, finalReport);
  log(`FINAL_REPORT saved=${finalFile} verdict=${verdict}`);

  progress('FINAL_REPORT', {
    verdict,
    signal,
    blocked: false,
    reduced: isReduced,
    earningsWarning,
    file:    finalFile,
    elapsed: Date.now() - startTime,
    quantRec,
    riskDecision,
  });

  process.stdout.write(finalReport);
  process.stdout.write(`\nTRADE_VERDICT:${verdict}\n`);
}

main().catch(err => {
  log(`FATAL error="${err.message}"`);
  progress('PIPELINE_ERROR', { error: err.message });
  console.error('Trade pipeline fatal error:', err);
  process.exit(1);
});
