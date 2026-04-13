#!/usr/bin/env node
'use strict';

/**
 * Layer 6: Diligence Orchestrator
 *
 * Interface: accepts a ticker symbol → loads agent definitions from agents/registry.json →
 * reads PROMPT.md + SOUL.md per agent → spawns 5 sub-agents (Layer 4) in parallel,
 * each with access to MCP servers (Layer 3) via .mcp.json → collects all outputs →
 * merges into a 12-section diligence memo (Layer 7) saved to output/memos/.
 *
 * Progress lines (stdout): BOTJOHN_PROGRESS:{json}
 * Verdict line (stdout):   BOTJOHN_VERDICT:{PROCEED|REVIEW|KILL}
 *
 * Usage: node orchestrator.js <TICKER> [--output-dir /path/to/output]
 */

const { spawn }    = require('child_process');
const fs           = require('fs');
const path         = require('path');
const { randomUUID } = require('crypto');

const agentBus    = require('../agents/channels/agent-bus');
const tokenBudget = require('./token-budget');

// ── Load .env (API keys for agents) ──────────────────────────────────────────
(function loadEnv() {
  const envFile = path.join(__dirname, '..', '.env');
  try {
    const lines = fs.readFileSync(envFile, 'utf8').split('\n');
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || trimmed.startsWith('#')) continue;
      const eq = trimmed.indexOf('=');
      if (eq === -1) continue;
      const key = trimmed.slice(0, eq).trim();
      const val = trimmed.slice(eq + 1).trim();
      if (val && !process.env[key]) process.env[key] = val;
    }
  } catch { /* .env optional — env vars may be set externally */ }
})();

// ── Config ────────────────────────────────────────────────────────────────────
const CLAUDE_BIN     = process.env.CLAUDE_BIN  || '/usr/local/bin/claude-bin';
const CLAUDE_UID     = parseInt(process.env.CLAUDE_UID  || '1001', 10);
const CLAUDE_GID     = parseInt(process.env.CLAUDE_GID  || '1001', 10);
const CLAUDE_HOME    = process.env.CLAUDE_HOME || '/home/claudebot';
const WORKDIR        = process.env.OPENCLAW_DIR || '/root/openclaw';
const GLOBAL_TIMEOUT = parseInt(process.env.CLAUDE_TIMEOUT_MS || '600000', 10);

const cliArgs    = process.argv.slice(2);
const tickerArg  = cliArgs.find(a => !a.startsWith('--'));
const outFlag    = cliArgs.indexOf('--output-dir');
const OUTPUT_DIR = outFlag !== -1 ? cliArgs[outFlag + 1] : path.join(WORKDIR, 'output', 'memos');
const LOG_DIR    = path.join(WORKDIR, 'johnbot', 'logs');
const RUNS_DIR   = path.join(WORKDIR, 'johnbot', 'logs', 'runs');
const LOG_FILE   = path.join(LOG_DIR, 'orchestrator.log');
const STATUS_FILE = path.join(WORKDIR, 'output', 'orchestrator-status.json');

if (!tickerArg) {
  console.error('Usage: node orchestrator.js <TICKER> [--output-dir /path]');
  process.exit(1);
}

const TICKER = tickerArg.toUpperCase();
const RUN_ID = randomUUID().slice(0, 8);

// ── Logger ────────────────────────────────────────────────────────────────────
function log(msg) {
  const line = `[${new Date().toISOString()}] [${TICKER}] [${RUN_ID}] ${msg}\n`;
  process.stderr.write(line); // stderr so stdout stays clean for memo + progress markers
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.appendFileSync(LOG_FILE, line);
  } catch { /* non-fatal */ }
}

// ── Progress reporter (stdout) ────────────────────────────────────────────────
// The bot parses lines matching BOTJOHN_PROGRESS:{...} for real-time Discord updates.
function progress(event, data = {}) {
  const payload = JSON.stringify({ event, runId: RUN_ID, ticker: TICKER, ts: Date.now(), ...data });
  process.stdout.write(`BOTJOHN_PROGRESS:${payload}\n`);
}

// ── Run log (per-run JSON file) ───────────────────────────────────────────────
let runLog = {
  runId:     RUN_ID,
  ticker:    TICKER,
  startTime: null,
  endTime:   null,
  verdict:   null,
  memoFile:  null,
  agents:    {},
};

function saveRunLog() {
  try {
    fs.mkdirSync(RUNS_DIR, { recursive: true });
    const date    = new Date().toISOString().slice(0, 10);
    const logFile = path.join(RUNS_DIR, `${TICKER}-${date}-${RUN_ID}.json`);
    fs.writeFileSync(logFile, JSON.stringify(runLog, null, 2));
  } catch { /* non-fatal */ }
}

// ── Status file ───────────────────────────────────────────────────────────────
let statusObj = {
  ticker:    TICKER,
  runId:     RUN_ID,
  startTime: null,
  phase:     'idle',
  agents:    {},
  verdict:   'pending',
  memoFile:  null,
};

function writeStatus() {
  try {
    fs.mkdirSync(path.dirname(STATUS_FILE), { recursive: true });
    fs.writeFileSync(STATUS_FILE, JSON.stringify(statusObj, null, 2));
  } catch { /* non-fatal */ }
}

// ── Registry loader ───────────────────────────────────────────────────────────
function loadRegistry() {
  const registryPath = path.join(WORKDIR, 'agents', 'registry.json');
  try {
    return JSON.parse(fs.readFileSync(registryPath, 'utf8'));
  } catch (err) {
    log(`WARN registry load failed: ${err.message} — falling back to empty agent list`);
    return { agents: [], mcpPreambles: {} };
  }
}

/**
 * Build the full prompt for an agent:
 *   1. Read agents/<id>/PROMPT.md
 *   2. Optionally prepend SOUL.md behavioral context
 *   3. Inject the MCP preamble for this agent's data sources
 *   4. Replace {{TICKER}} placeholders
 */
function buildAgentPrompt(agentDef, registry) {
  const agentDir = path.join(WORKDIR, agentDef.dir);

  // Read PROMPT.md
  let prompt;
  try {
    prompt = fs.readFileSync(path.join(agentDir, 'PROMPT.md'), 'utf8');
  } catch {
    log(`WARN agent=${agentDef.id} PROMPT.md not found — using fallback`);
    prompt = `Analyze ${TICKER} for the ${agentDef.name} section of a diligence memo. Output structured markdown.`;
  }

  // Inject template substitutions: ticker + API keys
  const FMP_KEY = process.env.FMP_API_KEY || '';
  const AV_KEY  = process.env.ALPHA_VANTAGE_API_KEY || '';
  prompt = prompt
    .replace(/\{\{TICKER\}\}/g, TICKER)
    .replace(/\{\{FMP_KEY\}\}/g, FMP_KEY)
    .replace(/\{\{AV_KEY\}\}/g, AV_KEY)
    .replace(/\$\{FMP_API_KEY\}/g, FMP_KEY)
    .replace(/\$\{ALPHA_VANTAGE_API_KEY\}/g, AV_KEY);

  // Prepend SOUL.md behavioral rules (optional — helps with model alignment)
  let soul = '';
  try {
    soul = fs.readFileSync(path.join(agentDir, 'SOUL.md'), 'utf8');
    soul = `# Behavioral Rules for this Session\n${soul}\n\n---\n\n# Your Task\n\n`;
  } catch { /* soul file optional */ }

  return soul + prompt;
}

// ── Sub-agent runner (Layer 4 → Layer 3) ──────────────────────────────────────
async function runAgent(agentDef, prompt) {
  // Token budget checkpoint — waits if halted, throws if session expired
  try {
    await tokenBudget.checkpoint(agentDef.id, agentDef.model || 'claude-haiku-4-5-20251001');
  } catch (budgetErr) {
    log(`BUDGET HALT agent=${agentDef.id} reason="${budgetErr.message}"`);
    progress('BUDGET_HALT', { agentId: agentDef.id, reason: budgetErr.message });
    return { id: agentDef.id, name: agentDef.name, output: `⚠️ Budget halt: ${budgetErr.message}`, elapsed: 0, error: true };
  }

  return new Promise((resolve) => {
    const start = Date.now();
    let stdout = '';
    let stderr = '';

    const agentTimeout = agentDef.timeout || 300000;
    const agentModel   = agentDef.model   || 'claude-haiku-4-5-20251001';

    log(`START agent=${agentDef.id} name="${agentDef.name}" model=${agentModel}`);
    statusObj.agents[agentDef.id] = { status: 'running', startTime: new Date().toISOString(), endTime: null, elapsed: null };
    writeStatus();

    runLog.agents[agentDef.id] = { status: 'running', startTime: new Date().toISOString() };
    agentBus.agentStarted(RUN_ID, agentDef.id, agentDef.name);
    progress('AGENT_START', { agentId: agentDef.id, agentName: agentDef.name, emoji: agentDef.emoji || '' });

    const proc = spawn(
      CLAUDE_BIN,
      ['--dangerously-skip-permissions', '--model', agentModel, '-p', prompt],
      {
        cwd: WORKDIR,       // .mcp.json lives here — Layer 3 MCP servers auto-loaded
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

    proc.stdout.on('data', (d) => { stdout += d.toString(); });
    proc.stderr.on('data', (d) => { stderr += d.toString(); });

    const killer = setTimeout(() => {
      proc.kill('SIGTERM');
      const elapsed = Date.now() - start;
      log(`TIMEOUT agent=${agentDef.id} elapsed=${(elapsed / 1000).toFixed(1)}s`);

      statusObj.agents[agentDef.id] = { status: 'timeout', startTime: statusObj.agents[agentDef.id].startTime, endTime: new Date().toISOString(), elapsed };
      writeStatus();

      runLog.agents[agentDef.id] = { ...runLog.agents[agentDef.id], status: 'timeout', elapsed };
      agentBus.agentFinished(RUN_ID, agentDef.id, agentDef.name, stdout, true, elapsed, true);
      progress('AGENT_TIMEOUT', { agentId: agentDef.id, agentName: agentDef.name, emoji: agentDef.emoji || '', elapsed });

      resolve({ id: agentDef.id, name: agentDef.name, output: `⚠️ Agent timed out after ${agentTimeout / 1000}s`, elapsed, error: true });
    }, agentTimeout);

    proc.on('close', (code) => {
      clearTimeout(killer);
      const elapsed = Date.now() - start;
      const success = code === 0 || !!stdout.trim();
      const status  = success ? 'complete' : 'error';

      log(`END agent=${agentDef.id} status=${status} code=${code} elapsed=${(elapsed / 1000).toFixed(1)}s`);

      statusObj.agents[agentDef.id] = { status, startTime: statusObj.agents[agentDef.id].startTime, endTime: new Date().toISOString(), elapsed };
      writeStatus();

      runLog.agents[agentDef.id] = { ...runLog.agents[agentDef.id], status, elapsed, endTime: new Date().toISOString() };
      agentBus.agentFinished(RUN_ID, agentDef.id, agentDef.name, stdout, !success, elapsed, false);
      // Record token usage for the budget monitor
      tokenBudget.recordUsage(agentDef.id, agentModel, prompt.length, stdout.length);
      // Include first 1200 chars of output so the bot can post a live preview to #research-feed
      const outputPreview = success ? stdout.trim().slice(0, 1200) : '';
      progress('AGENT_COMPLETE', { agentId: agentDef.id, agentName: agentDef.name, emoji: agentDef.emoji || '', elapsed, error: !success, outputPreview });

      if (success) {
        resolve({ id: agentDef.id, name: agentDef.name, output: stdout.trim(), elapsed, error: false });
      } else {
        resolve({ id: agentDef.id, name: agentDef.name, output: `⚠️ Agent error (exit ${code}): ${stderr.trim() || 'no output'}`, elapsed, error: true });
      }
    });

    proc.on('error', (err) => {
      clearTimeout(killer);
      const elapsed = Date.now() - start;
      log(`ERROR agent=${agentDef.id} msg="${err.message}"`);

      statusObj.agents[agentDef.id] = { status: 'error', startTime: statusObj.agents[agentDef.id].startTime, endTime: new Date().toISOString(), elapsed };
      writeStatus();

      runLog.agents[agentDef.id] = { ...runLog.agents[agentDef.id], status: 'error', elapsed };
      agentBus.agentFinished(RUN_ID, agentDef.id, agentDef.name, '', true, elapsed, false);
      progress('AGENT_ERROR', { agentId: agentDef.id, agentName: agentDef.name, emoji: agentDef.emoji || '', elapsed, error: err.message });

      resolve({ id: agentDef.id, name: agentDef.name, output: `⚠️ Spawn error: ${err.message}`, elapsed, error: true });
    });
  });
}

// ── Structured block parser ───────────────────────────────────────────────────
// Parses the new ---AGENT:id--- ... ---END--- format produced by agents.
// Returns { id, ticker, signals, status, kv } where kv is key→value map.
function parseAgentBlock(output) {
  const match = output.match(/---AGENT:(\w+)---\s*([\s\S]*?)---END---/i);
  if (!match) return null;

  const id   = match[1].toLowerCase();
  const body = match[2];
  const kv   = {};

  for (const line of body.split('\n')) {
    const m = line.match(/^([A-Z_0-9]+):\s*(.*)/);
    if (m) {
      // Multi-line values (GUIDANCE_TABLE, CHANGES, DRIVERS) accumulate under same key
      const key = m[1];
      const val = m[2].trim();
      if (kv[key] !== undefined) {
        kv[key] += '\n' + line;
      } else {
        kv[key] = val;
      }
    } else if (line.startsWith('  ') && Object.keys(kv).length > 0) {
      // Indented continuation line — append to last key
      const lastKey = Object.keys(kv).at(-1);
      if (lastKey) kv[lastKey] += '\n' + line;
    }
  }

  // Header fields
  const tickerM  = output.match(/---TICKER:([A-Z^]+)---/i);
  const signalsM = output.match(/---SIGNALS:(.*?)---/i);
  const statusM  = output.match(/---STATUS:(\w+)---/i);

  return {
    id,
    ticker:  tickerM?.[1]  || '',
    signals: (signalsM?.[1] || '').split(',').map(s => s.trim()).filter(Boolean),
    status:  statusM?.[1]  || 'unknown',
    kv,
    raw: output,
  };
}

// ── Deterministic checklist evaluation ──────────────────────────────────────
// Evaluates 6 checklist items from parsed agent data without an LLM call.
function evaluateChecklist(byId) {
  const bull    = byId.bull?.parsed?.kv    || {};
  const bear    = byId.bear?.parsed?.kv    || {};
  const mgmt    = byId.mgmt?.parsed?.kv    || {};
  const filing  = byId.filing?.parsed?.kv  || {};
  const revenue = byId.revenue?.parsed?.kv || {};

  // Parse numeric values safely
  const num = (str) => parseFloat((str || '').replace(/[^0-9.-]/g, '')) || null;
  const pct = (str) => { const n = num(str); return n !== null && n > 1 ? n : n !== null ? n * 100 : null; };

  const checks = [
    {
      name: 'EV/NTM Revenue',
      // Deferred to bull agent — look for UPSIDE_MULTIPLE or pass if bull says MED/HIGH probability
      result: (() => {
        const prob = (bull.PROBABILITY || '').toUpperCase();
        if (prob === 'HIGH') return 'PASS';
        if (prob === 'LOW')  return 'FAIL';
        return 'REVIEW';
      })(),
      data: bull.UPSIDE_MULTIPLE || '—',
    },
    {
      name: 'Revenue Growth >10%',
      result: (() => {
        const r = (revenue.CHECKLIST_2_GROWTH || '').toUpperCase();
        if (r.startsWith('PASS')) return 'PASS';
        if (r.startsWith('FAIL')) return 'FAIL';
        return 'REVIEW';
      })(),
      data: revenue.CHECKLIST_2_GROWTH || '—',
    },
    {
      name: 'Gross Margin >40%',
      result: (() => {
        const r = (revenue.CHECKLIST_3_MARGIN || '').toUpperCase();
        if (r.startsWith('PASS')) return 'PASS';
        if (r.startsWith('FAIL')) return 'FAIL';
        return 'REVIEW';
      })(),
      data: revenue.CHECKLIST_3_MARGIN || '—',
    },
    {
      name: 'Insider Selling <$10M',
      result: (() => {
        const signals = [
          ...(byId.bear?.parsed?.signals  || []),
          ...(byId.filing?.parsed?.signals || []),
        ];
        if (signals.some(s => s.includes('insider_selling'))) return 'FAIL';
        const net = filing.INSIDER_XREF || '';
        if (/\$[0-9]+[MB].*sell/i.test(net)) return 'REVIEW';
        return 'PASS';
      })(),
      data: filing.INSIDER_XREF || bear.KILL_CRITERIA_CHECK?.match(/insider.*\n?(.*)/i)?.[1] || '—',
    },
    {
      name: 'No Restatements',
      result: (() => {
        const signals = byId.filing?.parsed?.signals || [];
        if (signals.some(s => s.includes('restatement') || s.includes('going_concern'))) return 'FAIL';
        const net = (filing.NET_ASSESSMENT || '').toUpperCase();
        if (net === 'RED') return 'FAIL';
        if (net === 'YELLOW') return 'REVIEW';
        if (net === 'GREEN') return 'PASS';
        return 'REVIEW';
      })(),
      data: `Filing net assessment: ${filing.NET_ASSESSMENT || '—'}`,
    },
    {
      name: 'Customer Concentration <25%',
      result: (() => {
        const r = (revenue.CHECKLIST_6_CONCENTRATION || '').toUpperCase();
        if (r.startsWith('PASS')) return 'PASS';
        if (r.startsWith('FAIL')) return 'FAIL';
        return 'REVIEW';
      })(),
      data: revenue.CHECKLIST_6_CONCENTRATION || `Top customer: ${revenue.TOP_1_PCT || '—'}`,
    },
  ];

  const failures = checks.filter(c => c.result === 'FAIL').length;
  const reviews  = checks.filter(c => c.result === 'REVIEW').length;

  // Collect all signals from all agents
  const allSignals = Object.values(byId)
    .flatMap(a => a.parsed?.signals || [])
    .filter(Boolean);

  // Kill overrides: any hard kill signal → KILL regardless of score
  const hasHardKill = allSignals.some(s =>
    s.startsWith('kill_') ||
    s.includes('going_concern') ||
    s.includes('insider_selling')
  );

  let verdict;
  if (hasHardKill || failures >= 3) {
    verdict = 'KILL';
  } else if (failures >= 1 || reviews >= 2) {
    verdict = 'REVIEW';
  } else {
    verdict = 'PROCEED';
  }

  return { checks, failures, reviews, verdict, allSignals };
}

// ── Verdict extraction ────────────────────────────────────────────────────────
function extractVerdict(results) {
  const byId = {};
  results.forEach(r => {
    byId[r.id] = { ...r, parsed: parseAgentBlock(r.output) };
  });

  // Use deterministic checklist when structured blocks are present
  const hasStructured = Object.values(byId).some(r => r.parsed !== null);
  if (hasStructured) {
    const { verdict } = evaluateChecklist(byId);
    return verdict;
  }

  // Legacy fallback: regex scan for kill signals in raw markdown output
  const allText   = results.map(r => r.output).join('\n');
  const filingOut = byId.filing?.output || '';
  const bearOut   = byId.bear?.output   || '';

  const hasKill = /going concern|KILL SIGNAL|⚠️.*KILL|\[red\]|verdict.*kill/i.test(filingOut + bearOut);
  if (hasKill) return 'KILL';

  const errorCount = results.filter(r => r.error).length;
  if (errorCount >= 3) return 'REVIEW';

  if (/\[yellow\]|low credibility|mixed|MGMT SIGNAL/i.test(allText)) return 'REVIEW';

  return 'REVIEW';
}

// ── Scenario comparison lookup ────────────────────────────────────────────────
function findScenarioComparison(ticker) {
  try {
    const files = fs.readdirSync(OUTPUT_DIR)
      .filter(f => f.startsWith(`${ticker}-scenario-comparison`))
      .sort().reverse();
    if (!files.length) return null;
    return fs.readFileSync(path.join(OUTPUT_DIR, files[0]), 'utf8');
  } catch {
    return null;
  }
}

// ── Memo assembly (Layer 7) ───────────────────────────────────────────────────
// Direct concatenation — no LLM summarization, no template substitution.
// Agent outputs are inserted verbatim under section headers.
function assembleMemo(ticker, results, startTime, verdict) {
  const date    = new Date().toISOString().slice(0, 10);
  const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);

  const byId = {};
  results.forEach(r => {
    byId[r.id] = { ...r, parsed: parseAgentBlock(r.output) };
  });

  // Deterministic checklist evaluation
  const { checks, failures, reviews, allSignals } = evaluateChecklist(byId);
  const checklistScore = `${checks.filter(c => c.result === 'PASS').length}/6`;
  const checklistTable = checks.map((c, i) =>
    `| ${i + 1} | ${c.name} | ${c.result} | ${c.data.split('\n')[0].slice(0, 60)} |`
  ).join('\n');

  const agentLogRows = results
    .map(r => `| ${r.name} | ${r.error ? '⚠️ Error' : '✅ Complete'} | ${(r.elapsed / 1000).toFixed(1)}s |`)
    .join('\n');

  const scenarioComparison = findScenarioComparison(ticker);

  const signalList = allSignals.length > 0
    ? allSignals.map(s => `- \`${s}\``).join('\n')
    : '- none';

  const verdictReasoning = verdict === 'KILL'
    ? `Kill signals fired: ${allSignals.filter(s => s.startsWith('kill_')).join(', ') || 'checklist failures ≥ 3'}`
    : verdict === 'REVIEW'
    ? `${failures} fail(s), ${reviews} review(s) — resolve flagged sections before entering position`
    : 'All 6 checklist items pass. No kill signals. Proceed with position sizing.';

  return `DILIGENCE MEMO — ${ticker}
Date: ${date} | Run: ${RUN_ID} | Elapsed: ${elapsed}s

=== VERDICT ===
${verdict} (${checklistScore} passed) — ${verdictReasoning}

=== CHECKLIST ===
| # | Item | Result | Data |
|---|------|--------|------|
${checklistTable}

=== SIGNALS ===
${signalList}

=== BULL CASE ===
${byId.bull?.output || '⚠️ Agent did not complete'}

=== BEAR CASE ===
${byId.bear?.output || '⚠️ Agent did not complete'}

=== MANAGEMENT ===
${byId.mgmt?.output || '⚠️ Agent did not complete'}

=== FILINGS ===
${byId.filing?.output || '⚠️ Agent did not complete'}

=== REVENUE ===
${byId.revenue?.output || '⚠️ Agent did not complete'}

=== SCENARIO ===
${scenarioComparison || `No scenario comparison. Run \`!john /scenario ${ticker}\` to generate.`}

=== AGENT LOG ===
| Agent | Status | Elapsed |
|-------|--------|---------|
${agentLogRows}

Generated by BotJohn Orchestrator — scripts/orchestrator.js
`;
}

// ── Main ──────────────────────────────────────────────────────────────────────
async function main() {
  const startTime = Date.now();

  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  fs.mkdirSync(LOG_DIR,    { recursive: true });
  fs.mkdirSync(RUNS_DIR,   { recursive: true });

  statusObj.startTime = new Date().toISOString();
  statusObj.phase     = 'running';
  runLog.startTime    = statusObj.startTime;
  writeStatus();

  // Load agent definitions from registry
  const registry = loadRegistry();
  // Filter to research agents only — trade_agents (pipeline:'trade') run via trade-pipeline.js
  const agentDefs = (registry.agents || []).filter(a => a.pipeline !== 'trade');

  if (agentDefs.length === 0) {
    log('FATAL no agents found in registry.json');
    process.exit(1);
  }

  log(`ORCHESTRATOR START ticker=${TICKER} runId=${RUN_ID} agents=${agentDefs.length}`);
  agentBus.registerRun(RUN_ID, TICKER, agentDefs.map(a => a.id));
  progress('RUN_START', { agentCount: agentDefs.length, agentIds: agentDefs.map(a => a.id) });

  // Build prompts for all agents
  const agentJobs = agentDefs.map(def => ({
    def,
    prompt: buildAgentPrompt(def, registry),
  }));

  // Run all agents in parallel (Layer 4), each with access to MCP (Layer 3)
  const results = await Promise.all(agentJobs.map(({ def, prompt }) => {
    log(`SPAWN agent=${def.id}`);
    return runAgent(def, prompt);
  }));

  results.forEach(r => {
    log(`RESULT agent=${r.id} error=${r.error} elapsed=${(r.elapsed / 1000).toFixed(1)}s`);
  });

  // Determine verdict
  const verdict = extractVerdict(results);
  log(`VERDICT ticker=${TICKER} verdict=${verdict}`);
  progress('RUN_VERDICT', { verdict });

  // Assemble memo
  const memo    = assembleMemo(TICKER, results, startTime, verdict);
  const ts      = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const outFile = path.join(OUTPUT_DIR, `${TICKER}-diligence-${ts}.md`);

  fs.writeFileSync(outFile, memo, 'utf8');
  log(`MEMO_SAVED path=${outFile} size=${memo.length}`);

  // Update final status
  const totalMs = Date.now() - startTime;
  statusObj.phase   = 'complete';
  statusObj.verdict = verdict;
  statusObj.memoFile = outFile;
  writeStatus();

  runLog.endTime  = new Date().toISOString();
  runLog.verdict  = verdict;
  runLog.memoFile = outFile;
  saveRunLog();

  log(`ORCHESTRATOR END ticker=${TICKER} elapsed=${(totalMs / 1000).toFixed(1)}s memoFile=${outFile}`);
  progress('RUN_COMPLETE', { verdict, elapsed: totalMs, memoFile: outFile });

  // Output memo + verdict marker to stdout for the bot
  process.stdout.write(memo);
  process.stdout.write(`\nBOTJOHN_VERDICT:${verdict}\n`);
}

main().catch(err => {
  log(`FATAL error="${err.message}"`);
  statusObj.phase = 'error';
  writeStatus();
  agentBus.runError(RUN_ID, TICKER, err);
  progress('RUN_ERROR', { error: err.message });
  console.error('Orchestrator fatal error:', err);
  process.exit(1);
});
