'use strict';

/**
 * desk-controller.js — Quantitative Trading Desk orchestration layer (Layer 8).
 *
 * Runs the 5 trader agents SEQUENTIALLY (each depends on prior output):
 *   Screener → Sizer → Timer → Risk → Reporter
 *
 * Entry points:
 *   runTradeScan(channel)            — full pipeline on PROCEED names
 *   runTradeReport(channel)          — Reporter only, portfolio summary
 *   runExitAnalysis(ticker, channel) — exit pipeline for a specific position
 *   runRiskStandalone(channel)       — Risk only, current portfolio assessment
 */

const { spawn }  = require('child_process');
const fs         = require('fs');
const path       = require('path');

const ledger     = require('./signal-ledger');
const OperatorFeed = require('../channels/operator-feed');

// Lazily loaded to avoid circular deps — set via setDiscordClient()
let _discordClient  = null;
let _channelMap     = null;

/** Call this from index.js after channel setup completes. */
function setDiscordClient(client, channelMap) {
  _discordClient = client;
  _channelMap    = channelMap;
}

/** Post content to a named channel (from channel-map). Falls back silently. */
async function postToChannel(key, content, attachment = null) {
  if (!_channelMap || !_discordClient) return;
  const ch = _channelMap.getChannel(_discordClient, key);
  if (!ch) return;
  try {
    if (attachment) {
      await ch.send({ content, files: [attachment] });
    } else {
      // Split if too long for a single message
      if (content.length <= 1990) {
        await ch.send({ content });
      } else {
        // Send as attachment
        const { AttachmentBuilder } = require('discord.js');
        const buf = Buffer.from(content, 'utf8');
        await ch.send({ content: `*(output too long — see attachment)*`, files: [new AttachmentBuilder(buf, { name: 'output.md' })] });
      }
    }
  } catch (err) {
    console.error(`[desk] postToChannel(${key}) failed:`, err.message);
  }
}

// ── Config ────────────────────────────────────────────────────────────────────
const CLAUDE_BIN   = process.env.CLAUDE_BIN     || '/usr/local/bin/claude-bin';
const CLAUDE_UID   = parseInt(process.env.CLAUDE_UID   || '1001', 10);
const CLAUDE_GID   = parseInt(process.env.CLAUDE_GID   || '1001', 10);
const CLAUDE_HOME  = process.env.CLAUDE_HOME    || '/home/claudebot';
const WORKDIR      = process.env.OPENCLAW_DIR   || '/root/openclaw';
const OUTPUT_DIR   = path.join(WORKDIR, 'output', 'trades');
const MEMOS_DIR    = path.join(WORKDIR, 'output', 'memos');
const PORTFOLIO    = path.join(WORKDIR, 'output', 'portfolio.json');

// Desk agents use Sonnet — they need reasoning, not speed
const DESK_MODEL   = process.env.DESK_MODEL || 'claude-sonnet-4-6';
const AGENT_TIMEOUT = 180_000;   // 3 min per agent
const DESK_TIMEOUT  = 900_000;   // 15 min total

// ── File helpers ──────────────────────────────────────────────────────────────

function loadPortfolio() {
  try {
    if (!fs.existsSync(PORTFOLIO)) return { portfolio_value: 1_000_000, positions: [], last_updated: null };
    return JSON.parse(fs.readFileSync(PORTFOLIO, 'utf8'));
  } catch {
    return { portfolio_value: 1_000_000, positions: [], last_updated: null };
  }
}

/**
 * Scan output/memos/ for the most recent diligence memo per ticker.
 * Returns array of { ticker, memoPath, verdict, date } for PROCEED names.
 */
function findProceedNames() {
  const proceed = [];
  try {
    const files = fs.readdirSync(MEMOS_DIR)
      .filter(f => f.match(/^[A-Z]{1,5}-diligence-/))
      .sort().reverse(); // most recent first

    const seen = new Set();
    for (const file of files) {
      const match = file.match(/^([A-Z]{1,5})-diligence-/);
      if (!match || seen.has(match[1])) continue;
      const ticker = match[1];
      seen.add(ticker);

      const content = fs.readFileSync(path.join(MEMOS_DIR, file), 'utf8');
      if (/VERDICT:\s*PROCEED/i.test(content)) {
        // Extract a short memo summary (first 3000 chars, or up to end of section 5)
        const summary = content.slice(0, 3000);
        proceed.push({ ticker, memoPath: path.join(MEMOS_DIR, file), summary, date: file.slice(ticker.length + 11, ticker.length + 21) });
      }
    }
  } catch { /* memos dir may be empty */ }
  return proceed;
}

/**
 * Load SOUL.md + DESK.md as behavioral preamble for a desk agent.
 * Returns concatenated string or empty string if files missing.
 */
function loadAgentContext(agentId) {
  const agentDir  = path.join(WORKDIR, 'agents', 'quant', agentId);
  const deskPath  = path.join(WORKDIR, 'agents', 'quant', 'DESK.md');
  let context = '';
  try { context += `# Trading Desk Rules\n${fs.readFileSync(deskPath, 'utf8')}\n\n---\n\n`; } catch {}
  try { context += `# Your Behavioral Rules\n${fs.readFileSync(path.join(agentDir, 'SOUL.md'), 'utf8')}\n\n---\n\n`; } catch {}
  return context;
}

/**
 * Load PROMPT.md for a desk agent and substitute template variables.
 */
function buildPrompt(agentId, vars = {}) {
  const promptPath = path.join(WORKDIR, 'agents', 'quant', agentId, 'PROMPT.md');
  let prompt = '';
  try { prompt = fs.readFileSync(promptPath, 'utf8'); } catch {
    return `You are the ${agentId} agent. Analyze the provided context and produce structured output. Date: ${vars.DATE || new Date().toISOString().slice(0, 10)}`;
  }

  // Substitute all {{VARIABLE}} placeholders
  for (const [key, value] of Object.entries(vars)) {
    prompt = prompt.replace(new RegExp(`\\{\\{${key}\\}\\}`, 'g'), value || '(not provided)');
  }
  return prompt;
}

// ── Agent runner ──────────────────────────────────────────────────────────────

function runAgent(agentId, fullPrompt) {
  return new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
    const start = Date.now();

    const proc = spawn(
      CLAUDE_BIN,
      ['--dangerously-skip-permissions', '--model', DESK_MODEL, '-p', fullPrompt],
      {
        cwd: WORKDIR,  // .mcp.json available
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
      resolve({ output: `⚠️ ${agentId} timed out after ${AGENT_TIMEOUT / 1000}s`, elapsed: Date.now() - start, error: true });
    }, AGENT_TIMEOUT);

    proc.on('close', (code) => {
      clearTimeout(killer);
      const elapsed = Date.now() - start;
      const success = code === 0 || !!stdout.trim();
      resolve({ output: success ? stdout.trim() : `⚠️ ${agentId} error (exit ${code}): ${stderr.trim()}`, elapsed, error: !success });
    });

    proc.on('error', (err) => {
      clearTimeout(killer);
      resolve({ output: `⚠️ ${agentId} spawn error: ${err.message}`, elapsed: Date.now() - start, error: true });
    });
  });
}

// ── Signal parsers ────────────────────────────────────────────────────────────

/** Extract tickers with active signals from screener output. */
function parseSignalTickers(screenerOutput) {
  const tickers = [];
  const re = /^TRADE SIGNAL — ([A-Z]{1,5})/gm;
  let m;
  while ((m = re.exec(screenerOutput)) !== null) tickers.push(m[1]);
  return [...new Set(tickers)];
}

/** Check if output contains a Risk rejection for a specific ticker. */
function isRiskRejected(riskOutput, ticker) {
  return /\[RISK VETO\].*?{ticker}|\[RISK VETO\]/i.test(riskOutput) &&
    new RegExp(`RISK ASSESSMENT — ${ticker}[\\s\\S]*?Risk Verdict:\\s*REJECTED`, 'i').test(riskOutput);
}

/** Check for special signal markers in any output string. */
function extractMarkers(text) {
  return {
    tradeAlert:   /\[TRADE ALERT\]/i.test(text),
    exitAlert:    /\[EXIT ALERT\]/i.test(text),
    riskVeto:     /\[RISK VETO\]/i.test(text),
    urgentEntry:  /\[URGENT ENTRY\]/i.test(text),
    elevatedRisk: /\[ELEVATED RISK\]/i.test(text),
    sizeReject:   /\[SIZE REJECT\]/i.test(text),
  };
}

// ── Main pipelines ────────────────────────────────────────────────────────────

/**
 * Full trade scan pipeline: Screener → Sizer → Timer → Risk → Reporter.
 * @param {import('discord.js').TextChannel} channel
 * @returns {Promise<string>} final reporter output
 */
async function runTradeScan(channel) {
  const feed      = new OperatorFeed(channel);
  const portfolio = loadPortfolio();
  const proceed   = findProceedNames();
  const date      = new Date().toISOString().slice(0, 10);

  fs.mkdirSync(OUTPUT_DIR, { recursive: true });

  if (proceed.length === 0) {
    const msg = 'No PROCEED-rated names found in output/memos/. Run `/diligence` on a ticker first.';
    if (channel) await channel.send({ content: `📡 ${msg}` }).catch(() => {});
    return msg;
  }

  feed._enqueue({ type: 'DESK_START', content: `📡 Trade scan starting — ${proceed.length} PROCEED name(s): ${proceed.map(p => p.ticker).join(', ')}` });

  const proceedTickers  = proceed.map(p => p.ticker).join(', ');
  const memoSummaries   = proceed.map(p => `### ${p.ticker}\n${p.summary}`).join('\n\n');
  const portfolioJson   = JSON.stringify(portfolio, null, 2);

  // ── Step 1: Screener ────────────────────────────────────────────────────────
  await postToChannel('agent-chat', `📡 **Screener** starting scan of \`${proceed.map(p => p.ticker).join(' | ')}\` — looking for R/R ≥ 2.0 entries...`);
  const screenerCtx    = loadAgentContext('screener');
  const screenerPrompt = buildPrompt('screener', {
    DATE:             date,
    PORTFOLIO_JSON:   portfolioJson,
    PROCEED_TICKERS:  proceedTickers,
    MEMO_SUMMARIES:   memoSummaries,
  });
  const screener = await runAgent('screener', screenerCtx + screenerPrompt);
  // Post Screener output to #trade-signals
  await postToChannel('trade-signals', `📡 **Screener output** (${(screener.elapsed / 1000).toFixed(0)}s)\n\`\`\`\n${screener.output.slice(0, 1800)}\n\`\`\``);

  if (screener.error || !/^TRADE SIGNAL/m.test(screener.output)) {
    const msg = screener.error
      ? `Screener error: ${screener.output}`
      : 'Screener found no actionable signals. No trades today.';
    await postToChannel('agent-chat', `📡 **Screener** — ${msg}`);
    feed._enqueue({ type: 'DESK_COMPLETE', content: `📊 Trade scan complete — 0 signals. ${msg}` });
    return msg;
  }

  const signalCount = (screener.output.match(/^TRADE SIGNAL/gm) || []).length;
  await postToChannel('agent-chat', `📡 **Screener → Sizer**: Found **${signalCount}** signal(s). Passing to Sizer for position sizing...`);

  // ── Step 2: Sizer ───────────────────────────────────────────────────────────
  const sizerCtx    = loadAgentContext('sizer');
  const sizerPrompt = buildPrompt('sizer', {
    DATE:             date,
    PORTFOLIO_JSON:   portfolioJson,
    PORTFOLIO_VALUE:  String(portfolio.portfolio_value || 1_000_000),
    SCREENER_OUTPUT:  screener.output,
  });
  const sizer = await runAgent('sizer', sizerCtx + sizerPrompt);
  await postToChannel('position-sizing', `⚖️ **Sizer output** (${(sizer.elapsed / 1000).toFixed(0)}s)\n\`\`\`\n${sizer.output.slice(0, 1800)}\n\`\`\``);
  const sizeRejects = (sizer.output.match(/\[SIZE REJECT\]/g) || []).length;
  await postToChannel('agent-chat', `⚖️ **Sizer → Timer**: Sized ${signalCount - sizeRejects}/${signalCount} position(s)${sizeRejects ? ` (${sizeRejects} rejected — limit breach)` : ''}. Passing to Timer for entry timing...`);

  // ── Step 3: Timer ───────────────────────────────────────────────────────────
  const timerCtx    = loadAgentContext('timer');
  const timerPrompt = buildPrompt('timer', {
    DATE:            date,
    SCREENER_OUTPUT: screener.output,
    SIZER_OUTPUT:    sizer.output,
  });
  const timer = await runAgent('timer', timerCtx + timerPrompt);
  await postToChannel('entry-timing', `⏱️ **Timer output** (${(timer.elapsed / 1000).toFixed(0)}s)\n\`\`\`\n${timer.output.slice(0, 1800)}\n\`\`\``);
  const urgentCount = (timer.output.match(/\[URGENT ENTRY\]/g) || []).length;
  await postToChannel('agent-chat', `⏱️ **Timer → Risk**: Timing assessed${urgentCount ? ` — ⚠️ ${urgentCount} URGENT ENTRY signal(s)` : ''}. Passing to Risk for final approval...`);

  // ── Step 4: Risk ────────────────────────────────────────────────────────────
  const riskCtx    = loadAgentContext('risk');
  const riskPrompt = buildPrompt('risk', {
    DATE:            date,
    PORTFOLIO_JSON:  portfolioJson,
    SCREENER_OUTPUT: screener.output,
    SIZER_OUTPUT:    sizer.output,
    TIMER_OUTPUT:    timer.output,
    STANDALONE:      'false',
  });
  const risk = await runAgent('risk', riskCtx + riskPrompt);
  await postToChannel('risk-desk', `🛡️ **Risk assessment** (${(risk.elapsed / 1000).toFixed(0)}s)\n\`\`\`\n${risk.output.slice(0, 1800)}\n\`\`\``);
  const riskMarkers = extractMarkers(risk.output);
  const vetoes      = (risk.output.match(/\[RISK VETO\]/g) || []).length;
  const approved    = signalCount - vetoes;
  await postToChannel('agent-chat', `🛡️ **Risk → Reporter**: ${approved} approved | ${vetoes} vetoed${riskMarkers.elevatedRisk ? ' | ⚠️ ELEVATED RISK' : ''}. Passing to Reporter...`);
  if (riskMarkers.riskVeto) {
    await postToChannel('alerts', `🛡️ **RISK VETO** — ${vetoes} trade(s) rejected. See <#${_channelMap?.getId('risk-desk')}> for details.`);
  }

  // ── Step 5: Reporter ────────────────────────────────────────────────────────
  const reporterCtx    = loadAgentContext('reporter');
  const reporterPrompt = buildPrompt('reporter', {
    DATE:            date,
    REPORT_TYPE:     'TRADE_SCAN',
    PORTFOLIO_JSON:  portfolioJson,
    SCREENER_OUTPUT: screener.output,
    SIZER_OUTPUT:    sizer.output,
    TIMER_OUTPUT:    timer.output,
    RISK_OUTPUT:     risk.output,
  });
  const reporter = await runAgent('reporter', reporterCtx + reporterPrompt);
  const reportMarkers = extractMarkers(reporter.output);

  // ── Log signals to ledger ───────────────────────────────────────────────────
  const signalTickers = parseSignalTickers(screener.output);
  for (const ticker of signalTickers) {
    const isVetoed = isRiskRejected(risk.output, ticker);
    ledger.addSignal({
      ticker,
      signal_type:     'ENTRY_LONG',
      screener_output: screener.output,
      sizer_output:    sizer.output,
      timer_output:    timer.output,
      risk_output:     risk.output,
      risk_verdict:    isVetoed ? 'REJECTED' : 'APPROVED',
      reporter_output: reporter.output,
      status:          isVetoed ? 'REJECTED_BY_RISK' : 'PENDING_OPERATOR',
    });
  }

  // ── Save report to file ─────────────────────────────────────────────────────
  const ts         = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const reportFile = path.join(OUTPUT_DIR, `trade-scan-${ts}.md`);
  const fullReport = `# Trade Scan Report — ${date}\n\n## Screener Output\n${screener.output}\n\n---\n\n## Sizer Output\n${sizer.output}\n\n---\n\n## Timer Output\n${timer.output}\n\n---\n\n## Risk Assessment\n${risk.output}\n\n---\n\n## Trade Reports\n${reporter.output}`;
  fs.writeFileSync(reportFile, fullReport, 'utf8');

  // ── Post final reports to Discord channels ──────────────────────────────────
  await postToChannel('trade-reports', reporter.output.slice(0, 1900));
  if (reportMarkers.tradeAlert || reportMarkers.urgentEntry) {
    await postToChannel('alerts', `📡 **TRADE ALERT** — high conviction signal(s) found. See <#${_channelMap?.getId('trade-reports')}> for full reports.`);
  }
  await postToChannel('agent-chat', `📊 **Reporter** — trade scan complete. ${approved} report(s) sent to <#${_channelMap?.getId('trade-reports')}>.`);
  feed._enqueue({ type: 'DESK_COMPLETE', content: `📊 Trade scan complete — ${signalCount} signal(s) | ${approved} approved | ${vetoes} rejected by Risk` });

  return reporter.output;
}

/**
 * Portfolio report: Reporter only with current holdings + active signals.
 */
async function runTradeReport(channel) {
  const feed      = new OperatorFeed(channel);
  const portfolio = loadPortfolio();
  const date      = new Date().toISOString().slice(0, 10);

  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  feed._enqueue({ type: 'DESK_START', content: `📊 Generating portfolio report...` });

  const activeSignals = ledger.getActiveSignals();
  const reporterCtx   = loadAgentContext('reporter');
  const reporterPrompt = buildPrompt('reporter', {
    DATE:            date,
    REPORT_TYPE:     'PORTFOLIO',
    PORTFOLIO_JSON:  JSON.stringify(portfolio, null, 2),
    SCREENER_OUTPUT: activeSignals.length > 0 ? activeSignals.map(s => s.screener_output).join('\n') : '(no active signals)',
    SIZER_OUTPUT:    activeSignals.length > 0 ? activeSignals.map(s => s.sizer_output).join('\n') : '(no active signals)',
    TIMER_OUTPUT:    '(not applicable for portfolio report)',
    RISK_OUTPUT:     activeSignals.length > 0 ? activeSignals.map(s => s.risk_output).join('\n') : '(no active signals)',
  });

  const reporter   = await runAgent('reporter', reporterCtx + reporterPrompt);
  const ts         = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const reportFile = path.join(OUTPUT_DIR, `portfolio-${ts}.md`);
  fs.writeFileSync(reportFile, reporter.output, 'utf8');

  feed._enqueue({ type: 'DESK_COMPLETE', content: `📊 Portfolio report complete (${(reporter.elapsed / 1000).toFixed(0)}s)` });
  return { output: reporter.output, file: reportFile };
}

/**
 * Exit analysis: Bear agent context + Timer + Reporter for EXIT report.
 */
async function runExitAnalysis(ticker, channel) {
  const feed      = new OperatorFeed(channel);
  const portfolio = loadPortfolio();
  const date      = new Date().toISOString().slice(0, 10);
  ticker          = ticker.toUpperCase();

  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
  feed._enqueue({ type: 'DESK_START', content: `🚨 Exit analysis starting for **${ticker}**...` });

  // Get most recent signals for this ticker
  const signals    = ledger.getSignalsByTicker(ticker);
  const lastSignal = signals.sort((a, b) => new Date(b.generated) - new Date(a.generated))[0];

  const timerCtx    = loadAgentContext('timer');
  const timerPrompt = buildPrompt('timer', {
    DATE:            date,
    SCREENER_OUTPUT: `EXIT signal for ${ticker}. The operator has requested exit analysis.`,
    SIZER_OUTPUT:    lastSignal ? lastSignal.sizer_output : '(no sizing data available)',
  });
  const timer = await runAgent('timer', timerCtx + timerPrompt);

  const reporterCtx    = loadAgentContext('reporter');
  const reporterPrompt = buildPrompt('reporter', {
    DATE:            date,
    REPORT_TYPE:     'EXIT',
    PORTFOLIO_JSON:  JSON.stringify(portfolio, null, 2),
    SCREENER_OUTPUT: `EXIT signal for ${ticker}`,
    SIZER_OUTPUT:    lastSignal ? lastSignal.sizer_output : '(no sizing data available)',
    TIMER_OUTPUT:    timer.output,
    RISK_OUTPUT:     lastSignal ? lastSignal.risk_output : '(no risk data available)',
  });
  const reporter   = await runAgent('reporter', reporterCtx + reporterPrompt);
  const ts         = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const reportFile = path.join(OUTPUT_DIR, `${ticker}-exit-${ts}.md`);
  fs.writeFileSync(reportFile, reporter.output, 'utf8');

  feed._enqueue({ type: 'EXIT_ALERT', content: `🚨 **EXIT ALERT — ${ticker}** — see exit report` });
  return { output: reporter.output, file: reportFile };
}

/**
 * Standalone risk assessment: Risk agent only on current portfolio.
 */
async function runRiskStandalone(channel) {
  const feed      = new OperatorFeed(channel);
  const portfolio = loadPortfolio();
  const date      = new Date().toISOString().slice(0, 10);

  feed._enqueue({ type: 'DESK_START', content: `🛡️ Running portfolio risk assessment...` });

  const riskCtx    = loadAgentContext('risk');
  const riskPrompt = buildPrompt('risk', {
    DATE:            date,
    PORTFOLIO_JSON:  JSON.stringify(portfolio, null, 2),
    SCREENER_OUTPUT: '(standalone mode — no new trades)',
    SIZER_OUTPUT:    '(standalone mode)',
    TIMER_OUTPUT:    '(standalone mode)',
    STANDALONE:      'true',
  });
  const risk = await runAgent('risk', riskCtx + riskPrompt);

  const ts         = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const reportFile = path.join(OUTPUT_DIR, `risk-report-${ts}.md`);
  fs.writeFileSync(reportFile, risk.output, 'utf8');

  const markers = extractMarkers(risk.output);
  feed._enqueue({ type: 'DESK_COMPLETE', content: `🛡️ Risk assessment complete (${(risk.elapsed / 1000).toFixed(0)}s)${markers.elevatedRisk ? ' — ⚠️ ELEVATED RISK' : ''}` });
  return { output: risk.output, file: reportFile };
}

module.exports = { runTradeScan, runTradeReport, runExitAnalysis, runRiskStandalone, setDiscordClient };
