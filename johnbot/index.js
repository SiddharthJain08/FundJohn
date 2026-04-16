'use strict';

const { Client, GatewayIntentBits, EmbedBuilder, AttachmentBuilder, ChannelType } = require('discord.js');
const { spawn, execFile } = require('child_process');
const fs   = require('fs');
const path = require('path');

const OperatorFeed    = require('../agents/channels/operator-feed');
const TradeFeed       = require('../agents/channels/trade-feed');
const TokenFeed       = require('../agents/channels/token-feed');
const tokenBudget     = require('../scripts/token-budget');
const discordRelay    = require('../agents/channels/discord-relay');
const { setupServer } = require('./discord-setup');
const channelMap      = require('./channel-map');

// ── Config ────────────────────────────────────────────────────────────────────
const BOT_TOKEN    = process.env.DISCORD_TOKEN;
const CLAUDE_BIN   = process.env.CLAUDE_BIN      || '/usr/local/bin/claude-bin';
const CLAUDE_UID   = parseInt(process.env.CLAUDE_UID   || '1001', 10);
const CLAUDE_GID   = parseInt(process.env.CLAUDE_GID   || '1001', 10);
const CLAUDE_HOME  = process.env.CLAUDE_HOME     || '/home/claudebot';
const MAX_TIMEOUT  = parseInt(process.env.CLAUDE_TIMEOUT_MS || '600000', 10);

// Model selection — haiku for fast skill commands, sonnet for complex/general
const MODEL_FAST   = 'claude-haiku-4-5-20251001';   // ~3-8s responses
const MODEL_FULL   = 'claude-sonnet-4-6';            // default for general prompts

// Working dirs
const ROOT_DIR     = process.env.JOHN_WORKDIR    || '/root';         // general prompts
const OPENCLAW_DIR = process.env.OPENCLAW_DIR    || '/root/openclaw'; // hedge fund commands
const OUTPUT_DIR   = path.join(OPENCLAW_DIR, 'output', 'memos');
const STATUS_FILE  = path.join(OPENCLAW_DIR, 'output', 'orchestrator-status.json');

const PREFIX       = '!john';

// ── State ─────────────────────────────────────────────────────────────────────
const activeAgents = new Map();  // id → { ticker, command, startTime, pid }
let   agentIdSeq   = 0;
const taskQueue    = [];
let   lastCompleted = null;

// Operator feed — multi-channel routing via channelMap (initialized in clientReady)
const feed       = new OperatorFeed();
const tradeFeed  = new TradeFeed();
const tokenFeed  = new TokenFeed();
discordRelay.setFeed(feed);

// Direct agent address map — maps @name → { dir, model }
const DIRECT_AGENTS = {
  botjohn:{label:"BotJohn"},
  datajohn:{label:"DataJohn"},
  researchjohn:{label:"ResearchJohn"},
  tradejohn:{label:"TradeJohn"},
};

// ── Discord client ────────────────────────────────────────────────────────────
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages,
    GatewayIntentBits.DirectMessageTyping,
  ],
  partials: ['CHANNEL', 'MESSAGE'],
});

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Split text into ≤1990-char chunks, breaking on newlines. */
function splitMessage(text, maxLen = 1990) {
  const chunks = [];
  while (text.length > 0) {
    if (text.length <= maxLen) { chunks.push(text); break; }
    let at = text.lastIndexOf('\n', maxLen);
    if (at <= 0) at = maxLen;
    chunks.push(text.slice(0, at));
    text = text.slice(at).replace(/^\n/, '');
  }
  return chunks;
}

/** Keep typing indicator alive. Returns stopper fn. */
function startTyping(channel) {
  channel.sendTyping().catch(() => {});
  const t = setInterval(() => channel.sendTyping().catch(() => {}), 8000);
  return () => clearInterval(t);
}

/** Ensure output directory exists. */
function ensureOutputDir() {
  fs.mkdirSync(OUTPUT_DIR, { recursive: true });
}

/** Save text to a timestamped file, return the path. */
function saveMemo(label, content) {
  ensureOutputDir();
  const ts   = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  const name = `${label}-${ts}.md`;
  const file = path.join(OUTPUT_DIR, name);
  fs.writeFileSync(file, content, 'utf8');
  return { file, name };
}

/**
 * Send response to Discord.
 * - Short responses (≤ 1990 chars): plain reply
 * - Medium responses (1990–8000 chars): split into chunks or embeds
 * - Long responses (> 8000 chars): send as markdown file attachment
 */
async function sendResponse(message, text, label = 'output') {
  // Long output → file attachment
  if (text.length > 8000) {
    const { file, name } = saveMemo(label, text);
    const attachment = new AttachmentBuilder(Buffer.from(text, 'utf8'), { name });
    await message.reply({
      content: `📎 Output is ${text.length.toLocaleString()} chars — sending as file.`,
      files: [attachment],
      allowedMentions: { repliedUser: false },
    });
    return;
  }

  const chunks = splitMessage(text);

  if (chunks.length === 1) {
    await message.reply({ content: chunks[0], allowedMentions: { repliedUser: false } });
    return;
  }
  if (chunks.length === 2) {
    await message.reply({ content: chunks[0], allowedMentions: { repliedUser: false } });
    await message.channel.send(chunks[1]);
    return;
  }

  await message.reply({ content: `*(${chunks.length} parts)*`, allowedMentions: { repliedUser: false } });
  for (let i = 0; i < chunks.length; i++) {
    await message.channel.send({
      embeds: [
        new EmbedBuilder()
          .setDescription(chunks[i])
          .setFooter({ text: `Part ${i + 1} / ${chunks.length}` })
          .setColor(0x5865f2),
      ],
    });
  }
}

// ── Claude runner ─────────────────────────────────────────────────────────────
function runClaude(prompt, workdir, agentMeta = {}) {
  return new Promise((resolve, reject) => {
    let stdout = '';
    let stderr = '';

    const id    = ++agentIdSeq;
    const start = Date.now();
    const model = agentMeta.model || MODEL_FULL;

    const proc = spawn(
      CLAUDE_BIN,
      ['--dangerously-skip-permissions', '--model', model, '-p', prompt],
      {
        cwd: workdir,
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

    activeAgents.set(id, {
      id,
      command: agentMeta.command || prompt.slice(0, 60),
      ticker:  agentMeta.ticker  || '—',
      startTime: start,
      pid: proc.pid,
    });

    proc.stdout.on('data', (d) => { stdout += d.toString(); });
    proc.stderr.on('data', (d) => { stderr += d.toString(); });

    const killer = setTimeout(() => {
      proc.kill('SIGTERM');
      activeAgents.delete(id);
      reject(new Error(`Timed out after ${MAX_TIMEOUT / 1000}s`));
    }, MAX_TIMEOUT);

    proc.on('close', (code) => {
      clearTimeout(killer);
      activeAgents.delete(id);
      lastCompleted = {
        command:  agentMeta.command || prompt.slice(0, 60),
        ticker:   agentMeta.ticker  || '—',
        elapsed:  ((Date.now() - start) / 1000).toFixed(1) + 's',
        success:  code === 0 || !!stdout.trim(),
        at:       new Date().toISOString(),
      };

      if (code === 0 || stdout.trim()) {
        resolve(stdout.trim() || '*(no output)*');
      } else {
        reject(new Error(stderr.trim() || `Claude exited with code ${code}`));
      }
    });

    proc.on('error', (err) => {
      clearTimeout(killer);
      activeAgents.delete(id);
      reject(err);
    });
  });
}

// ── Orchestrator runner ───────────────────────────────────────────────────────
/**
 * Runs the orchestrator for a ticker.
 * Parses BOTJOHN_PROGRESS:{json} lines from stdout for real-time feed updates.
 * Returns the full stdout (memo + BOTJOHN_VERDICT: line).
 */
function runOrchestrator(ticker) {
  return new Promise((resolve, reject) => {
    // Memo accumulates everything EXCEPT BOTJOHN_PROGRESS lines
    let memo    = '';
    let stderr  = '';
    let lineBuf = '';

    const id    = ++agentIdSeq;
    const start = Date.now();

    const proc = spawn(
      'node',
      [path.join(OPENCLAW_DIR, 'scripts', 'orchestrator.js'), ticker],
      {
        cwd: OPENCLAW_DIR,
        uid: CLAUDE_UID,
        gid: CLAUDE_GID,
        env: {
          ...process.env,
          HOME:         CLAUDE_HOME,
          USER:         'claudebot',
          LOGNAME:      'claudebot',
          CLAUDE_BIN,
          CLAUDE_UID:   String(CLAUDE_UID),
          CLAUDE_GID:   String(CLAUDE_GID),
          CLAUDE_HOME,
          OPENCLAW_DIR,
          CLAUDE_TIMEOUT_MS: String(MAX_TIMEOUT),
          SUDO_USER:    undefined,
          SUDO_UID:     undefined,
          SUDO_GID:     undefined,
          SUDO_COMMAND: undefined,
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      }
    );

    activeAgents.set(id, { id, command: `orchestrator ${ticker}`, ticker, startTime: start, pid: proc.pid });

    // Parse stdout line by line — separate BOTJOHN_PROGRESS: markers from memo content
    proc.stdout.on('data', (chunk) => {
      lineBuf += chunk.toString();
      const lines = lineBuf.split('\n');
      lineBuf = lines.pop(); // last partial line stays in buffer

      for (const line of lines) {
        if (line.startsWith('BOTJOHN_PROGRESS:')) {
          // Real-time progress update — parse and forward to feed
          try {
            const payload = JSON.parse(line.slice('BOTJOHN_PROGRESS:'.length));
            handleOrchestratorProgress(payload);
          } catch { /* malformed progress line — ignore */ }
        } else {
          // Regular memo content or BOTJOHN_VERDICT line
          memo += line + '\n';
        }
      }
    });

    proc.stderr.on('data', (d) => { stderr += d.toString(); });

    const killer = setTimeout(() => {
      proc.kill('SIGTERM');
      activeAgents.delete(id);
      reject(new Error(`Orchestrator timed out after ${MAX_TIMEOUT / 1000}s`));
    }, MAX_TIMEOUT);

    proc.on('close', (code) => {
      clearTimeout(killer);
      activeAgents.delete(id);

      // Flush any remaining buffer
      if (lineBuf) memo += lineBuf;

      lastCompleted = {
        command: `orchestrator ${ticker}`,
        ticker,
        elapsed: ((Date.now() - start) / 1000).toFixed(1) + 's',
        success: code === 0 || !!memo.trim(),
        at:      new Date().toISOString(),
      };

      if (code === 0 || memo.trim()) {
        resolve(memo.trim() || '*(no output)*');
      } else {
        reject(new Error(stderr.trim() || `Orchestrator exited with code ${code}`));
      }
    });

    proc.on('error', reject);
  });
}

// ── Trade pipeline runner (Quant → Risk → Timing) ────────────────────────────
/**
 * Runs trade-pipeline.js for a ticker + optional memo path.
 * Parses TRADE_PROGRESS:{json} lines for real-time feed updates.
 * Returns the final trade report (stdout minus progress lines).
 */
function runCycle(ticker, memoPath = null) {
  return new Promise((resolve, reject) => {
    let report  = '';
    let stderr  = '';
    let lineBuf = '';

    const id    = ++agentIdSeq;
    const start = Date.now();
    const args  = [path.join(OPENCLAW_DIR, 'scripts', 'trade-pipeline.js'), ticker];
    if (memoPath) args.push('--memo', memoPath);

    const proc = spawn('node', args, {
      cwd: OPENCLAW_DIR,
      uid: CLAUDE_UID,
      gid: CLAUDE_GID,
      env: {
        ...process.env,
        HOME:           CLAUDE_HOME,
        USER:           'claudebot',
        LOGNAME:        'claudebot',
        CLAUDE_BIN,
        CLAUDE_UID:     String(CLAUDE_UID),
        CLAUDE_GID:     String(CLAUDE_GID),
        CLAUDE_HOME,
        OPENCLAW_DIR,
        TRADE_TIMEOUT_MS: '300000',
        SUDO_USER:      undefined,
        SUDO_UID:       undefined,
        SUDO_GID:       undefined,
        SUDO_COMMAND:   undefined,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    activeAgents.set(id, { id, command: `trade-pipeline ${ticker}`, ticker, startTime: start, pid: proc.pid });

    proc.stdout.on('data', (chunk) => {
      lineBuf += chunk.toString();
      const lines = lineBuf.split('\n');
      lineBuf = lines.pop();

      for (const line of lines) {
        if (line.startsWith('TRADE_PROGRESS:')) {
          try {
            const payload = JSON.parse(line.slice('TRADE_PROGRESS:'.length));
            handleTradePipelineProgress(payload);
          } catch { /* malformed */ }
        } else {
          report += line + '\n';
        }
      }
    });

    proc.stderr.on('data', d => { stderr += d.toString(); });

    const killer = setTimeout(() => {
      proc.kill('SIGTERM');
      activeAgents.delete(id);
      reject(new Error(`Trade pipeline timed out after ${MAX_TIMEOUT / 1000}s`));
    }, MAX_TIMEOUT);

    proc.on('close', (code) => {
      clearTimeout(killer);
      activeAgents.delete(id);
      if (lineBuf) report += lineBuf;

      lastCompleted = {
        command: `trade-pipeline ${ticker}`,
        ticker,
        elapsed: ((Date.now() - start) / 1000).toFixed(1) + 's',
        success: code === 0 || !!report.trim(),
        at:      new Date().toISOString(),
      };

      if (code === 0 || report.trim()) {
        resolve(report.trim() || '*(no output)*');
      } else {
        reject(new Error(stderr.trim() || `Trade pipeline exited with code ${code}`));
      }
    });

    proc.on('error', reject);
  });
}

// ── Trade pipeline progress handler ──────────────────────────────────────────
function handleTradePipelineProgress(payload) {
  const { event, ticker, elapsed, file } = payload;

  switch (event) {
    case 'PIPELINE_START':
      tradeFeed.pipelineStart(ticker, payload.memoPath || '');
      break;

    case 'QUANT_COMPLETE':
      tradeFeed.quantComplete(
        ticker,
        payload.recommendation || '—',
        payload.evRatio        || '—',
        payload.sizePct        || '—',
        payload.negativeEV     || false,
        elapsed,
      );
      break;

    case 'RISK_COMPLETE':
      tradeFeed.riskComplete(
        ticker,
        payload.decision  || '—',
        payload.riskScore || '—',
        payload.blocked   || false,
        payload.reduced   || false,
        elapsed,
      );
      break;

    case 'TIMING_COMPLETE':
      tradeFeed.timingComplete(
        ticker,
        payload.signal         || '—',
        payload.earningsWarning || false,
        elapsed,
      );
      break;

    case 'FINAL_REPORT':
      tradeFeed.finalReport(ticker, payload.verdict || '—', file || null, elapsed);
      break;

    case 'PIPELINE_ERROR':
      tradeFeed.pipelineError(ticker, payload.error || 'Unknown error');
      break;

    default: break;
  }
}

// ── Portfolio formatter ───────────────────────────────────────────────────────
function formatPortfolioState(state) {
  const date       = new Date().toISOString().slice(0, 10);
  const positions  = state.positions || [];
  const exposure   = state.exposure  || {};
  const sectors    = state.sectors   || {};
  const risk       = state.risk_metrics || {};
  const cash       = state.cash_balance ?? state.total_equity ?? 0;
  const currency   = state.currency || 'USD';

  let out = `## PORTFOLIO SUMMARY — ${date}\n\n`;
  out    += `**Total Equity:** $${(state.total_equity || cash).toLocaleString()} ${currency}\n`;
  out    += `**Cash Balance:** $${cash.toLocaleString()} ${currency}\n\n`;

  if (positions.length === 0) {
    out += `*No open positions.*\n\n`;
  } else {
    out += `### Holdings\n| Ticker | Direction | Entry | Size % | Sector |\n|--------|-----------|-------|--------|--------|\n`;
    for (const p of positions) {
      out += `| ${p.ticker} | ${p.direction || 'LONG'} | $${p.entry_price || '—'} | ${p.size_pct || '—'}% | ${p.sector || '—'} |\n`;
    }
    out += '\n';
  }

  out += `### Exposure\n`;
  out += `  Long: ${exposure.long_pct ?? 0}% | Short: ${exposure.short_pct ?? 0}% | Net: ${exposure.net_pct ?? 0}% | Cash: ${exposure.cash_pct ?? 100}%\n\n`;

  if (Object.keys(sectors).length > 0) {
    out += `### Sector Breakdown\n`;
    for (const [sec, pct] of Object.entries(sectors)) {
      out += `  ${sec}: ${pct}%\n`;
    }
    out += '\n';
  }

  out += `### Risk Metrics\n`;
  out += `  Max Single Position:  ${risk.max_single_position_pct ?? 0}%\n`;
  out += `  Max Sector:           ${risk.max_sector_pct ?? 0}%\n`;
  out += `  Portfolio Heat:       ${risk.portfolio_heat_pct ?? 0}%\n`;
  out += `  Open P&L:             $${(risk.total_open_pnl ?? 0).toLocaleString()}\n`;

  return out;
}

// ── Universe manager ──────────────────────────────────────────────────────────
const UNIVERSE_FILE = path.join(OPENCLAW_DIR, 'output', 'universe.json');

function loadUniverse() {
  try { return JSON.parse(fs.readFileSync(UNIVERSE_FILE, 'utf8')); }
  catch { return { sectors: { Technology: { tickers: [] }, Healthcare: { tickers: [] }, Industrials: { tickers: [] }, Consumer: { tickers: [] } } }; }
}

function saveUniverse(u) {
  u.last_updated = new Date().toISOString().slice(0, 10);
  fs.writeFileSync(UNIVERSE_FILE, JSON.stringify(u, null, 2));
}

function resolveRunTickers(raw) {
  const u = loadUniverse();
  if (!raw || raw === '' || raw.toUpperCase() === 'ALL') {
    return Object.values(u.sectors).flatMap(s => s.tickers || []);
  }
  // Match sector name
  for (const [name, data] of Object.entries(u.sectors)) {
    if (name.toLowerCase() === raw.toLowerCase()) return data.tickers || [];
  }
  // Otherwise treat as comma-separated tickers
  return raw.split(',').map(t => t.trim().toUpperCase()).filter(Boolean);
}

// ── Watchlist manager ─────────────────────────────────────────────────────────
const WATCHLIST_FILE = path.join(OPENCLAW_DIR, 'output', 'portfolio', 'watchlist.json');

function loadWatchlist() {
  try { return JSON.parse(fs.readFileSync(WATCHLIST_FILE, 'utf8')); } catch { return { tickers: [] }; }
}

function saveWatchlist(wl) {
  fs.writeFileSync(WATCHLIST_FILE, JSON.stringify(wl, null, 2));
}

// ── Pipeline runner (background process for /run) ─────────────────────────────
let _runnerProc = null;

function startPipelineRunner(tickers, hours, intervalMin, maxCostUSD, { preScreen = false } = {}) {
  if (_runnerProc && !_runnerProc.killed) {
    _runnerProc.kill('SIGTERM');
  }

  const args = [
    path.join(OPENCLAW_DIR, 'scripts', 'pipeline-runner.js'),
    '--tickers', tickers.join(','),
    '--hours',   String(hours),
    '--interval', String(intervalMin),
  ];
  if (maxCostUSD) args.push('--max-cost', String(maxCostUSD));
  if (preScreen)  args.push('--pre-screen');

  const proc = spawn('node', args, {
    cwd: OPENCLAW_DIR,
    uid: CLAUDE_UID,
    gid: CLAUDE_GID,
    env: {
      ...process.env,
      HOME:         CLAUDE_HOME,
      USER:         'claudebot',
      LOGNAME:      'claudebot',
      CLAUDE_BIN,
      OPENCLAW_DIR,
      SUDO_USER:    undefined,
      SUDO_UID:     undefined,
      SUDO_GID:     undefined,
      SUDO_COMMAND: undefined,
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  _runnerProc = proc;
  let lineBuf = '';

  proc.stdout.on('data', (chunk) => {
    lineBuf += chunk.toString();
    const lines = lineBuf.split('\n');
    lineBuf = lines.pop();
    for (const line of lines) {
      if (line.startsWith('RUNNER_PROGRESS:')) {
        try { handleRunnerProgress(JSON.parse(line.slice('RUNNER_PROGRESS:'.length))); } catch { /* ignore */ }
      } else if (line.startsWith('BOTJOHN_PROGRESS:')) {
        try { handleOrchestratorProgress(JSON.parse(line.slice('BOTJOHN_PROGRESS:'.length))); } catch { /* ignore */ }
      } else if (line.startsWith('TRADE_PROGRESS:')) {
        try { handleTradePipelineProgress(JSON.parse(line.slice('TRADE_PROGRESS:'.length))); } catch { /* ignore */ }
      }
    }
  });

  proc.stderr.on('data', d => { console.error('[runner]', d.toString().trim()); });

  proc.on('close', (code) => {
    _runnerProc = null;
    console.log(`[runner] Pipeline runner exited (code=${code})`);
    const logCh = channelMap.getChannel(client, 'botjohn-log');
    if (logCh) {
      const status = tokenBudget.getStatus();
      const cost = status ? ` | Est. cost: $${status.estimatedCostUSD}` : '';
      logCh.send({ content: `🧮 Pipeline runner completed${cost}` }).catch(() => {});
    }
  });

  return proc;
}

function handleRunnerProgress(payload) {
  const { event, tickers, ticker, scanNum, timeRemainingMs, elapsed } = payload;

  // Helper: post to a named channel (non-blocking, errors suppressed)
  function post(channelKey, content) {
    const ch = channelMap.getChannel(client, channelKey);
    if (ch) ch.send({ content }).catch(() => {});
  }

  switch (event) {

    // ── Session-level ──────────────────────────────────────────────────────
    case 'RUNNER_START':
      tokenFeed.sessionStart(tickers || [], payload.durationHours, payload.intervalMin, payload.maxCostUSD);
      post('botjohn-log', `🧮 **Full pipeline session started** | Tickers: \`${(tickers||[]).join(', ')}\` | Duration: **${payload.durationHours}h** | Interval: ${payload.intervalMin}m${payload.maxCostUSD ? ` | Cap: $${payload.maxCostUSD}` : ''}`);
      break;

    case 'RUNNER_COMPLETE': {
      const status = tokenBudget.getStatus();
      tokenFeed.sessionEnd(
        payload.scansCompleted || 0,
        status?.totalSpawns   || 0,
        status?.totalTokens   || 0,
        status?.estimatedCostUSD || '0.0000',
      );
      post('botjohn-log', `✅ **Pipeline session ended** | ${payload.scansCompleted} scan(s) complete | Est. cost: $${status?.estimatedCostUSD || '0.00'}`);
      break;
    }

    case 'RUNNER_HALTED':
      tokenFeed.operatorHalt(payload.reason || 'unknown');
      break;

    case 'RUNNER_ERROR':
      post('alerts', `🔥 **Pipeline runner error**: ${payload.error}`);
      break;

    // ── Scan-level ─────────────────────────────────────────────────────────
    case 'SCAN_START': {
      tokenFeed.scanStart(scanNum, tickers || [], timeRemainingMs || 0);
      const h = Math.floor((timeRemainingMs||0) / 3_600_000);
      const m = Math.floor(((timeRemainingMs||0) % 3_600_000) / 60_000);
      post('botjohn-log', `🔄 **Scan #${scanNum} starting** | ${(tickers||[]).join(', ')} | ${h}h ${m}m remaining`);
      break;
    }

    case 'SCAN_COMPLETE': {
      tokenFeed.scanComplete(scanNum, tickers || [], elapsed);
      const summary = (payload.summary || [])
        .map(r => `**${r.ticker}**: ${r.verdict || '—'} → ${r.signal || '—'}`)
        .join(' | ');
      post('botjohn-log', `✅ **Scan #${scanNum} complete** | ${summary}`);
      break;
    }

    case 'SCAN_WAITING':
      post('botjohn-log', `⏳ **Scan #${scanNum} done** — next scan in **${payload.nextScanMin}m**`);
      break;

    case 'SCAN_PARTIAL':
      post('botjohn-log', `⚠️ **Scan #${scanNum} interrupted by budget halt**`);
      break;

    // ── Ticker-level: Diligence ────────────────────────────────────────────
    case 'TICKER_START':
      post('research-feed', `🔬 **[${payload.tickerIdx}/${payload.totalTickers}] Starting full pipeline — ${ticker}** (Scan #${scanNum})`);
      break;

    case 'DILIGENCE_START':
      post('research-feed', `📋 **Diligence running — ${ticker}** | Spawning 5 research agents...`);
      break;

    case 'DILIGENCE_COMPLETE':
      post('research-feed', `✅ **Diligence complete — ${ticker}** | Memo saved: \`${payload.memoFile || '—'}\``);
      break;

    case 'DILIGENCE_SKIPPED':
      post('research-feed', `⏩ **Diligence skipped — ${ticker}** | Existing memo is ${payload.ageHours}h old (threshold: 12h)`);
      break;

    case 'DILIGENCE_ERROR':
      post('research-feed', `⚠️ **Diligence error — ${ticker}**: ${payload.error}`);
      break;

    case 'VERDICT_READ': {
      const icon = payload.verdict === 'PROCEED' ? '✅' : payload.verdict === 'KILL' ? '🛑' : '⚠️';
      post('research-feed', `${icon} **Verdict — ${ticker}**: **${payload.verdict}**`);
      break;
    }

    // ── Ticker-level: Scenario Lab ─────────────────────────────────────────
    case 'SCENARIO_START':
      post('scenario-lab', `🔬 **Scenario lab running — ${ticker}** | Building base/bull/bear worktrees...`);
      break;

    case 'SCENARIO_COMPLETE':
      post('scenario-lab', `✅ **Scenario lab complete — ${ticker}** | Comparison saved`);
      break;

    case 'SCENARIO_SKIPPED':
      post('scenario-lab', `⏩ **Scenario skipped — ${ticker}** | Existing comparison is ${payload.ageHours}h old`);
      break;

    case 'SCENARIO_ERROR':
      post('scenario-lab', `⚠️ **Scenario error — ${ticker}** (non-fatal): ${payload.error}`);
      break;

    // ── Ticker-level: Trade Pipeline ───────────────────────────────────────
    case 'TRADE_START':
      post('agent-chat', `📐 **Trade pipeline starting — ${ticker}** | Quant → Risk → Timing`);
      break;

    case 'TRADE_COMPLETE': {
      const sig  = payload.signal;
      const icon = sig === 'GO' ? '✅' : sig === 'BLOCKED' ? '🛑' : '⏳';
      post('agent-chat', `${icon} **Trade pipeline complete — ${ticker}** | Signal: **${sig}**`);
      break;
    }

    case 'TRADE_SKIPPED':
      post('agent-chat', `🛑 **Trade pipeline skipped — ${ticker}** (${payload.reason})`);
      break;

    case 'TRADE_ERROR':
      post('agent-chat', `⚠️ **Trade pipeline error — ${ticker}**: ${payload.error}`);
      break;

    // ── Ticker-level: GO signal alert ──────────────────────────────────────
    case 'TICKER_SIGNAL_GO':
      tokenFeed.tickerSignal(ticker, 'GO', scanNum);
      post('alerts', `🎯 **GO SIGNAL — ${ticker}** (Scan #${scanNum}) | Check #trade-reports for the full report`);
      break;

    case 'BUDGET_HALT_TICKER':
      post('alerts', `🛑 **Budget halt — stopped before ${ticker}**: ${payload.reason}`);
      break;

    case 'TICKER_COMPLETE': {
      const elapsedMin = elapsed ? `${(elapsed / 60_000).toFixed(1)}m` : '—';
      post('botjohn-log', `🏁 **${ticker}** done | verdict=${payload.verdict} signal=${payload.signal} elapsed=${elapsedMin}`);
      break;
    }

    default: break;
  }
}

// Budget alert poller — checks for threshold files written by token-budget.js workers
function startBudgetAlertPoller() {
  const alertPcts = [75, 90];
  setInterval(() => {
    if (!tokenBudget.isSessionActive()) return;
    for (const pct of alertPcts) {
      const alertFile = path.join(tokenBudget.SESSION_DIR, `alert-${pct}.json`);
      try {
        const data = JSON.parse(fs.readFileSync(alertFile, 'utf8'));
        fs.unlinkSync(alertFile); // consume it
        const state = tokenBudget.getStatus();
        tokenFeed.budgetAlert(pct, data.cost || 0, state?.maxCostUSD || 0);
        // At 90% auto-throttle to slow
        if (pct >= 90) {
          tokenBudget.setSpeed(0.5);
          tokenFeed.speedChange(0.5, 'SLOW');
        }
      } catch { /* file doesn't exist — no alert */ }
    }
  }, 15_000); // check every 15s
}

// ── Progress event handler ────────────────────────────────────────────────────
const AGENT_EMOJI = { bull: '🐂', bear: '🐻', mgmt: '📋', filing: '📄', revenue: '📊' };

function handleOrchestratorProgress(payload) {
  const { event, ticker, agentId, agentName, elapsed, error, verdict, agentCount, outputPreview } = payload;
  const emoji = AGENT_EMOJI[agentId] || '';

  switch (event) {
    case 'RUN_START':
      feed.runStarted(ticker, agentCount, payload.runId);
      break;

    case 'AGENT_COMPLETE': {
      feed.agentComplete(ticker, `${emoji} ${agentName}`, emoji, elapsed, error);
      // Post agent output preview to #research-feed
      if (outputPreview && !error) {
        const resCh = channelMap.getChannel(client, 'research-feed');
        if (resCh) {
          const preview = outputPreview.length > 1500 ? outputPreview.slice(0, 1500) + '\n*(truncated — full output in memo)*' : outputPreview;
          resCh.send({ content: `${emoji} **${agentName}** — ${ticker} (${(elapsed/1000).toFixed(0)}s)\n\`\`\`markdown\n${preview}\n\`\`\`` }).catch(() => {});
        }
      }
      break;
    }

    case 'AGENT_TIMEOUT':
      feed.agentTimeout(ticker, `${emoji} ${agentName}`);
      break;

    case 'AGENT_ERROR':
      feed.agentComplete(ticker, `${emoji} ${agentName}`, emoji, elapsed, true);
      break;

    case 'RUN_COMPLETE': {
      feed.runComplete(ticker, verdict, payload.elapsed, payload.memoFile);
      // Log completion to #botjohn-log
      const logCh = channelMap.getChannel(client, 'botjohn-log');
      if (logCh) {
        const icon = verdict === 'PROCEED' ? '✅' : verdict === 'KILL' ? '🛑' : '⚠️';
        logCh.send({ content: `\`${new Date().toISOString()}\` ${icon} Diligence complete — **${ticker}** — verdict: **${verdict}** — elapsed: ${(payload.elapsed/1000).toFixed(0)}s` }).catch(() => {});
      }
      break;
    }

    case 'KILL_SIGNAL': {
      const alertCh = channelMap.getChannel(client, 'alerts');
      if (alertCh) {
        alertCh.send({ content: `🛑 **KILL SIGNAL — ${ticker}**\nAgent: ${agentName} | ${payload.signal}\n${payload.evidence ? `> ${payload.evidence.slice(0, 300)}` : ''}` }).catch(() => {});
      }
      break;
    }

    case 'RUN_ERROR':
      feed.runError(ticker, payload.error || 'Unknown error');
      break;

    default:
      break;
  }
}

// ── Scenario runner ───────────────────────────────────────────────────────────
function runScenario(ticker) {
  return new Promise((resolve, reject) => {
    let stdout = '';
    let stderr = '';

    const id    = ++agentIdSeq;
    const start = Date.now();
    const script = path.join(OPENCLAW_DIR, 'scripts', 'scenario.sh');

    const proc = spawn('bash', [script, ticker], {
      cwd: OPENCLAW_DIR,
      env: {
        ...process.env,
        HOME:         '/root',
        CLAUDE_BIN,
        CLAUDE_UID:   String(CLAUDE_UID),
        CLAUDE_HOME,
        OPENCLAW_DIR,
        CLAUDE_TIMEOUT_MS: String(MAX_TIMEOUT),
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    activeAgents.set(id, { id, command: `scenario ${ticker}`, ticker, startTime: start, pid: proc.pid });

    proc.stdout.on('data', (d) => { stdout += d.toString(); });
    proc.stderr.on('data', (d) => { stderr += d.toString(); });

    const killer = setTimeout(() => {
      proc.kill('SIGTERM');
      activeAgents.delete(id);
      reject(new Error(`Scenario timed out after ${MAX_TIMEOUT / 1000}s`));
    }, MAX_TIMEOUT);

    proc.on('close', (code) => {
      clearTimeout(killer);
      activeAgents.delete(id);
      lastCompleted = { command: `scenario ${ticker}`, ticker, elapsed: ((Date.now() - start) / 1000).toFixed(1) + 's', success: code === 0, at: new Date().toISOString() };

      const out = stdout.trim() || stderr.trim();
      if (code === 0 || stdout.trim()) {
        resolve(out || '*(no output)*');
      } else {
        reject(new Error(stderr.trim() || `Scenario script exited with code ${code}`));
      }
    });

    proc.on('error', reject);
  });
}

// ── Direct agent handler ──────────────────────────────────────────────────────

/**
 * Parse @agentname from the start of a string.
 * Accepts: @bull, @bull AAPL, @screener, etc.
 * Returns { agentName, query } or null.
 */
function parseDirectAgent(text) {
  const match = text.trim().match(/^@(bull|bear|mgmt|filing|revenue|screener|sizer|timer|risk|reporter)(?:\s+(.+))?$/si);
  if (!match) return null;
  return { agentName: match[1].toLowerCase(), query: (match[2] || '').trim() };
}

/**
 * Run a direct agent query and return the response.
 * Loads IDENTITY.md + SOUL.md as preamble, appends user query.
 */
async function runDirectAgent(agentName, query) {
  const agentDef = DIRECT_AGENTS[agentName];
  if (!agentDef) return `Unknown agent: ${agentName}`;

  const agentDir = path.join(OPENCLAW_DIR, agentDef.dir);

  let preamble = '';
  try { preamble += fs.readFileSync(path.join(agentDir, 'IDENTITY.md'), 'utf8') + '\n\n'; } catch {}
  try { preamble += fs.readFileSync(path.join(agentDir, 'SOUL.md'), 'utf8')    + '\n\n'; } catch {}

  const userQuery = query
    ? `User query: ${query}\n\nRespond concisely using your role and expertise. No preamble.`
    : `Describe your current role, what data you need to work with, and what commands activate you. Be brief.`;

  const fullPrompt = preamble + '---\n\n' + userQuery;
  return runClaude(fullPrompt, OPENCLAW_DIR, { command: `@${agentName}`, ticker: '—', model: agentDef.model });
}

// ── Command parser ────────────────────────────────────────────────────────────

/**
 * Detect if a message is a hedge fund slash command.
 * Recognized: /comps, /earnings-delta, /filing-diff, /mgmt-scorecard,
 *             /diligence-checklist, /screen, /status, /scenario, /diligence
 */
const HEDGE_COMMANDS = new Set([
  '/comps', '/earnings-delta', '/filing-diff', '/mgmt-scorecard',
  '/diligence-checklist', '/screen', '/status', '/scenario', '/diligence',
  // Quant trading desk commands (desk-controller pipeline)
  '/trade-scan', '/trade-report', '/signal', '/risk', '/exit',
  // Skill-builder trade pipeline (Quant → Risk → Timing)
  '/trade', '/portfolio', '/watchlist',
  // Full pipeline runner + token monitor
  '/run', '/universe', '/token-status', '/token-halt', '/token-resume', '/token-speed',
  // System
  '/restart',
]);

function parseCommand(text) {
  const trimmed = text.trim();
  if (!trimmed.startsWith('/')) return null;

  const [cmdPart, ...rest] = trimmed.split(/\s+/);
  const cmd = cmdPart.toLowerCase();

  if (!HEDGE_COMMANDS.has(cmd)) return null;
  return { cmd, args: rest.join(' ') };
}

/**
 * Load a .claude/commands/<name>.md template, strip frontmatter,
 * and replace $ARGUMENTS with the user's args.
 * Returns null if the file doesn't exist.
 */
function expandCommand(cmdName, args) {
  const fileName = cmdName.slice(1) + '.md'; // '/comps' → 'comps.md'
  const filePath = path.join(OPENCLAW_DIR, '.claude', 'commands', fileName);
  try {
    let content = fs.readFileSync(filePath, 'utf8');
    // Strip YAML frontmatter (--- ... ---\n)
    content = content.replace(/^---[\s\S]*?---\s*\n/, '');
    return content.replace(/\$ARGUMENTS/g, args);
  } catch {
    return null;
  }
}

// ── Status handler ────────────────────────────────────────────────────────────
function buildStatusMessage() {
  const now   = Date.now();
  const lines = [];

  lines.push('**BotJohn Status**');
  lines.push(`Active agents: ${activeAgents.size} | Queue depth: ${taskQueue.length}`);

  if (activeAgents.size > 0) {
    lines.push('\n**Running:**');
    for (const [, a] of activeAgents) {
      const elapsed = ((now - a.startTime) / 1000).toFixed(0);
      lines.push(`• \`${a.command}\` (ticker: ${a.ticker}) — ${elapsed}s elapsed`);
    }
  }

  if (lastCompleted) {
    lines.push('\n**Last completed:**');
    lines.push(`• \`${lastCompleted.command}\` (${lastCompleted.ticker}) — ${lastCompleted.elapsed} — ${lastCompleted.success ? '✅' : '⚠️'} — ${lastCompleted.at}`);
  }

  // Pull orchestrator sub-agent detail from the shared status file
  try {
    const statusPath = path.join(OPENCLAW_DIR, 'output', 'orchestrator-status.json');
    const orch = JSON.parse(fs.readFileSync(statusPath, 'utf8'));
    if (orch.ticker && orch.phase) {
      lines.push(`\n**Orchestrator (${orch.ticker}):** phase=${orch.phase} verdict=${orch.verdict}`);
      const agentEntries = Object.entries(orch.agents || {});
      if (agentEntries.length) {
        for (const [id, a] of agentEntries) {
          const elapsedStr = a.elapsed ? `${(a.elapsed / 1000).toFixed(1)}s` : '—';
          lines.push(`  • ${id}: ${a.status} (${elapsedStr})`);
        }
      }
    }
  } catch { /* no status file yet */ }

  // Signal ledger summary
  try {
    const ledgerSummary = null||();
    if (ledgerSummary.total > 0) {
      lines.push(`\n**Signal Ledger:** ${ledgerSummary.pending} pending | ${ledgerSummary.executed} executed | ${ledgerSummary.rejected_by_risk} vetoed by Risk | ${ledgerSummary.expired} expired | ${ledgerSummary.total} total`);
      const active = null||();
      if (active.length > 0) {
        lines.push('**Pending signals:**');
        active.slice(0, 5).forEach(s => lines.push(`  • \`${s.signal_id}\` — ${s.ticker} — ${s.signal_type} — strength ${s.signal_strength}`));
        if (active.length > 5) lines.push(`  *(+${active.length - 5} more — run \`!john /trade-report\`)*`);
      }
    }
  } catch { /* ledger not yet initialized */ }

  lines.push(`\nUptime: ${(process.uptime() / 60).toFixed(1)}min | Working dir: ${OPENCLAW_DIR}`);
  return lines.join('\n');
}

// ── Message handler ───────────────────────────────────────────────────────────
client.on('messageCreate', async (message) => {
  if (message.author.bot) return;

  const isDM          = message.channel.type === ChannelType.DM;
  const isMentioned   = message.mentions.has(client.user);
  const hasPrefix     = message.content.toLowerCase().startsWith(PREFIX);
  const isAgentChat   = channelMap.getId('agent-chat') === message.channelId;

  // In #agent-chat, @agentname messages are handled without !john prefix
  if (isAgentChat && !isDM && !isMentioned && !hasPrefix) {
    const directParsed = parseDirectAgent(message.content.trim());
    if (directParsed) {
      const stopTyping = startTyping(message.channel);
      try {
        const response = await runDirectAgent(directParsed.agentName, directParsed.query);
        stopTyping();
        // Post response back to #agent-chat
        await sendResponse(message, `**@${directParsed.agentName}** responds:\n\n${response}`, 'agent-response');
      } catch (err) {
        stopTyping();
        await message.reply({ content: `⚠️ ${err.message}`, allowedMentions: { repliedUser: false } });
      }
      return;
    }
    // Non-agent messages in #agent-chat are ignored unless they have the prefix
    if (!hasPrefix && !isMentioned) return;
  } else if (!isDM && !isMentioned && !hasPrefix) {
    return;
  }

  // Extract prompt text
  let raw = message.content;
  if (!isDM) {
    if (isMentioned) {
      raw = raw.replace(/<@!?\d+>/g, '').trim();
    } else {
      raw = raw.slice(PREFIX.length).trim();
    }
  }

  if (!raw) {
    await message.reply(
      'Ready. Commands: `!john /diligence AAPL` | `!john /trade-scan` | `!john /status`\n' +
      'Direct agents: `!john @bull AAPL` | In #agent-chat: `@screener scan MSFT`'
    );
    return;
  }

  // Handle !john @agentname routing (works in any channel)
  const directParsed = parseDirectAgent(raw);
  if (directParsed) {
    const stopTyping = startTyping(message.channel);
    try {
      const response = await runDirectAgent(directParsed.agentName, directParsed.query);
      stopTyping();

      // Post to #agent-chat if the request came from elsewhere
      const agentChatCh = channelMap.getChannel(client, 'agent-chat');
      if (agentChatCh && agentChatCh.id !== message.channelId) {
        await sendResponse(message, `Routed to <#${agentChatCh.id}>.`, 'notice');
        const content = `**@${directParsed.agentName}** responds to \`${message.author.username}\`:\n\n${response}`;
        if (content.length > 8000) {
          const { AttachmentBuilder: AB } = require('discord.js');
          await agentChatCh.send({ content: `📎 @${directParsed.agentName} response (long)`, files: [new AttachmentBuilder(Buffer.from(response, 'utf8'), { name: `${directParsed.agentName}-response.md` })] });
        } else {
          const chunks = splitMessage(content);
          for (const chunk of chunks) await agentChatCh.send({ content: chunk });
        }
      } else {
        await sendResponse(message, `**@${directParsed.agentName}** responds:\n\n${response}`, 'agent-response');
      }
    } catch (err) {
      stopTyping();
      await message.reply({ content: `⚠️ ${err.message}`, allowedMentions: { repliedUser: false } });
    }
    return;
  }

  const parsed = parseCommand(raw);
  const author = message.author.tag;
  const guild  = isDM ? 'DM' : message.guild?.name;

  console.log(`[${new Date().toISOString()}] [${author}] [${guild}] ${raw.slice(0, 120)}`);

  // ── /status ──────────────────────────────────────────────────────────────
  if (parsed?.cmd === '/status') {
    await message.reply({ content: buildStatusMessage(), allowedMentions: { repliedUser: false } });
    return;
  }

  // ── /restart ─────────────────────────────────────────────────────────────
  if (parsed?.cmd === '/restart') {
    await message.reply({ content: '🔄 Restarting BotJohn...', allowedMentions: { repliedUser: false } });
    // Delay slightly so reply is sent before process dies
    setTimeout(() => process.exit(0), 500);
    return;
  }

  const stopTyping = startTyping(message.channel);

  try {
    // ── /scenario <TICKER> ───────────────────────────────────────────────────
    if (parsed?.cmd === '/scenario') {
      const ticker = parsed.args.trim().toUpperCase();
      if (!ticker || !/^[A-Z]{1,5}$/.test(ticker)) {
        stopTyping();
        await message.reply('Usage: `!john /scenario <TICKER>` — e.g. `!john /scenario AAPL`');
        return;
      }

      await message.reply({ content: `🔬 Starting scenario lab for **${ticker}** (base/bull/bear worktrees)...`, allowedMentions: { repliedUser: false } });

      const output = await runScenario(ticker);
      stopTyping();

      // Send scenario comparison file if it exists
      const compareFiles = fs.readdirSync(path.join(OPENCLAW_DIR, 'output', 'memos'))
        .filter(f => f.startsWith(`${ticker}-scenario-comparison`))
        .sort()
        .reverse();

      if (compareFiles.length > 0) {
        const compareFile    = path.join(OPENCLAW_DIR, 'output', 'memos', compareFiles[0]);
        const compareContent = fs.readFileSync(compareFile, 'utf8');

        // If a diligence memo exists, append the scenario comparison as Section 11
        try {
          const diligenceFiles = fs.readdirSync(path.join(OPENCLAW_DIR, 'output', 'memos'))
            .filter(f => f.startsWith(`${ticker}-diligence-`))
            .sort().reverse();
          if (diligenceFiles.length > 0) {
            const diligencePath = path.join(OPENCLAW_DIR, 'output', 'memos', diligenceFiles[0]);
            let diligenceMemo   = fs.readFileSync(diligencePath, 'utf8');
            const scenarioMarker = '## 11. Scenario Comparison';
            if (diligenceMemo.includes(scenarioMarker)) {
              // Replace the existing placeholder section
              diligenceMemo = diligenceMemo.replace(
                /## 11\. Scenario Comparison[\s\S]*?(?=\n## 12\.|$)/,
                `${scenarioMarker}\n\n${compareContent}\n\n`
              );
            } else {
              diligenceMemo += `\n\n${scenarioMarker}\n\n${compareContent}\n`;
            }
            fs.writeFileSync(diligencePath, diligenceMemo, 'utf8');
          }
        } catch { /* non-fatal — still send the comparison */ }

        const attachment = new AttachmentBuilder(Buffer.from(compareContent, 'utf8'), { name: compareFiles[0] });
        await message.reply({ content: `✅ Scenario lab complete for **${ticker}**.`, files: [attachment], allowedMentions: { repliedUser: false } });
      } else {
        await sendResponse(message, output, `${ticker}-scenario`);
      }
      return;
    }

    // ── /diligence <TICKER> (full orchestrator run) ───────────────────────────
    if (parsed?.cmd === '/diligence') {
      const ticker = parsed.args.trim().toUpperCase();
      if (!ticker || !/^[A-Z]{1,5}$/.test(ticker)) {
        stopTyping();
        await message.reply('Usage: `!john /diligence <TICKER>` — e.g. `!john /diligence AAPL`');
        return;
      }

      // Wire the operator feed to this channel for real-time notifications
      feed.setChannel(message.channel);

      // Use discordRelay for per-channel concurrency control
      const raw = await discordRelay.handleDiligenceRequest(message, ticker, (t) => runOrchestrator(t));
      stopTyping();

      // Strip the BOTJOHN_VERDICT: marker line
      const verdictMatch = raw.match(/\nBOTJOHN_VERDICT:(PROCEED|REVIEW|KILL)\s*$/);
      const verdict      = verdictMatch ? verdictMatch[1] : 'REVIEW';
      const memo         = verdictMatch ? raw.slice(0, verdictMatch.index) : raw;

      // Find the saved memo file
      let memoFile = null;
      try {
        const files = fs.readdirSync(OUTPUT_DIR)
          .filter(f => f.startsWith(`${ticker}-diligence-`))
          .sort().reverse();
        if (files.length) memoFile = path.join(OUTPUT_DIR, files[0]);
      } catch { /* ignore */ }

      // Full memo as file attachment — post to #diligence-memos and reply in current channel
      const memoContent  = memoFile ? fs.readFileSync(memoFile, 'utf8') : memo;
      const ts           = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const fileName     = `${ticker}-diligence-${ts}.md`;
      const attachment   = new AttachmentBuilder(Buffer.from(memoContent, 'utf8'), { name: fileName });

      // Post to #diligence-memos
      const memosCh = channelMap.getChannel(client, 'diligence-memos');
      if (memosCh && memosCh.id !== message.channelId) {
        const verdictIcon = verdict === 'PROCEED' ? '✅' : verdict === 'KILL' ? '🛑' : '⚠️';
        await memosCh.send({
          content: `${verdictIcon} **${ticker}** — VERDICT: **${verdict}**`,
          files:   [new AttachmentBuilder(Buffer.from(memoContent, 'utf8'), { name: fileName })],
        });
      }

      // Reply in the command channel
      await message.channel.send({
        content: `📎 Memo saved — ${(memoContent.length / 1024).toFixed(1)} KB${memosCh ? ` | Posted to <#${memosCh.id}>` : ''}`,
        files:   [attachment],
        allowedMentions: { repliedUser: false },
      });
      return;
    }

    // ── /trade-scan — full quant desk pipeline ───────────────────────────────
    if (parsed?.cmd === '/trade-scan') {
      const ticker = parsed.args.trim().toUpperCase() || null;

      feed.setChannel(message.channel);
      await message.reply({
        content: `📡 Quant Trading Desk starting${ticker ? ` — focused on **${ticker}**` : ' — scanning all PROCEED names'}...`,
        allowedMentions: { repliedUser: false },
      });

      let reportOutput;
      try {
        reportOutput = await console.warn("[DEPRECATED] deskController removed  use /cycle"); (async()=>{})(message.channel);
      } catch (err) {
        stopTyping();
        await message.reply({ content: `⚠️ Trade scan error: ${err.message}`, allowedMentions: { repliedUser: false } });
        return;
      }
      stopTyping();

      await sendResponse(message, reportOutput, 'trade-scan');
      return;
    }

    // ── /trade-report — portfolio summary ────────────────────────────────────
    if (parsed?.cmd === '/trade-report') {
      feed.setChannel(message.channel);
      await message.reply({ content: `📊 Generating portfolio report...`, allowedMentions: { repliedUser: false } });

      let result;
      try {
        result = await console.warn("[DEPRECATED] deskController removed  use /cycle"); (async()=>{})(message.channel);
      } catch (err) {
        stopTyping();
        await message.reply({ content: `⚠️ Report error: ${err.message}`, allowedMentions: { repliedUser: false } });
        return;
      }
      stopTyping();

      // Portfolio reports are always sent as file attachments
      const ts         = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const fileName   = `portfolio-report-${ts}.md`;
      const attachment = new AttachmentBuilder(Buffer.from(result.output, 'utf8'), { name: fileName });
      await message.channel.send({
        content: `📎 Portfolio report attached (${(result.output.length / 1024).toFixed(1)} KB)`,
        files:   [attachment],
        allowedMentions: { repliedUser: false },
      });
      return;
    }

    // ── /signal {ID} {ACTION} — operator acts on a signal ───────────────────
    if (parsed?.cmd === '/signal') {
      const parts    = parsed.args.trim().split(/\s+/);
      const signalId = parts[0];
      const action   = parts[1]?.toUpperCase();

      if (!signalId || !['BUY', 'PASS'].includes(action)) {
        stopTyping();
        await message.reply({
          content: 'Usage: `!john /signal SIG-AAPL-20260405-001 BUY` or `...PASS`',
          allowedMentions: { repliedUser: false },
        });
        return;
      }

      const updated = null||(signalId, action);
      stopTyping();

      if (!updated) {
        await message.reply({ content: `⚠️ Signal \`${signalId}\` not found in ledger.`, allowedMentions: { repliedUser: false } });
      } else {
        const icon = action === 'BUY' ? '✅' : '⏭️';
        await message.reply({
          content: `${icon} Signal \`${signalId}\` marked as **${action === 'BUY' ? 'EXECUTED' : 'PASSED'}**.`,
          allowedMentions: { repliedUser: false },
        });
      }
      return;
    }

    // ── /risk — standalone portfolio risk assessment ──────────────────────────
    if (parsed?.cmd === '/risk') {
      feed.setChannel(message.channel);
      await message.reply({ content: `🛡️ Running portfolio risk assessment...`, allowedMentions: { repliedUser: false } });

      let result;
      try {
        result = await console.warn("[DEPRECATED] deskController removed  use /cycle"); (async()=>{})(message.channel);
      } catch (err) {
        stopTyping();
        await message.reply({ content: `⚠️ Risk error: ${err.message}`, allowedMentions: { repliedUser: false } });
        return;
      }
      stopTyping();
      await sendResponse(message, result.output, 'risk-report');
      return;
    }

    // ── /exit {TICKER} — exit analysis for a position ────────────────────────
    if (parsed?.cmd === '/exit') {
      const ticker = parsed.args.trim().toUpperCase();
      if (!ticker || !/^[A-Z]{1,5}$/.test(ticker)) {
        stopTyping();
        await message.reply('Usage: `!john /exit AAPL`', { allowedMentions: { repliedUser: false } });
        return;
      }

      feed.setChannel(message.channel);
      await message.reply({
        content: `🚨 Running exit analysis for **${ticker}**...`,
        allowedMentions: { repliedUser: false },
      });

      let result;
      try {
        result = await console.warn("[DEPRECATED] deskController removed  use /cycle"); (async()=>{})(ticker, message.channel);
      } catch (err) {
        stopTyping();
        await message.reply({ content: `⚠️ Exit analysis error: ${err.message}`, allowedMentions: { repliedUser: false } });
        return;
      }
      stopTyping();

      const ts         = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const fileName   = `${ticker}-exit-${ts}.md`;
      const attachment = new AttachmentBuilder(Buffer.from(result.output, 'utf8'), { name: fileName });
      await message.channel.send({
        content: `🚨 **EXIT ALERT — ${ticker}** — report attached`,
        files:   [attachment],
        allowedMentions: { repliedUser: false },
      });
      return;
    }

    // ── /trade <TICKER> — full skill-builder trade pipeline ─────────────────────
    if (parsed?.cmd === '/trade') {
      const ticker = parsed.args.trim().toUpperCase();
      if (!ticker || !/^[A-Z]{1,5}$/.test(ticker)) {
        stopTyping();
        await message.reply({ content: 'Usage: `!john /trade AAPL`', allowedMentions: { repliedUser: false } });
        return;
      }

      // Find latest diligence memo
      let memoPath = null;
      try {
        const memoFiles = fs.readdirSync(OUTPUT_DIR)
          .filter(f => f.startsWith(`${ticker}-diligence-`) && f.endsWith('.md'))
          .sort().reverse();
        if (memoFiles.length) memoPath = path.join(OUTPUT_DIR, memoFiles[0]);
      } catch { /* ignore */ }

      if (!memoPath) {
        stopTyping();
        await message.reply({
          content: `⚠️ No diligence memo found for **${ticker}**. Run \`!john /diligence ${ticker}\` first, then re-run \`/trade\`.`,
          allowedMentions: { repliedUser: false },
        });
        return;
      }

      tradeFeed.setChannelMap(channelMap, client);
      await message.reply({
        content: `📐 Trade pipeline starting for **${ticker}** (Quant → Risk → Timing)...`,
        allowedMentions: { repliedUser: false },
      });

      let tradeOutput;
      try {
        tradeOutput = await runCycle(ticker, memoPath);
      } catch (err) {
        stopTyping();
        await message.reply({ content: `⚠️ Trade pipeline error: ${err.message}`, allowedMentions: { repliedUser: false } });
        return;
      }
      stopTyping();

      // Parse verdict and strip TRADE_VERDICT: marker
      const verdictMatch  = tradeOutput.match(/\nTRADE_VERDICT:(GO|WAIT|PASS|BLOCKED)\s*$/);
      const tradeVerdict  = verdictMatch ? verdictMatch[1] : '—';
      const reportContent = verdictMatch ? tradeOutput.slice(0, verdictMatch.index) : tradeOutput;

      // Post report to #trade-reports and reply in command channel
      const ts         = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
      const fileName   = `${ticker}-trade-${ts}.md`;
      const attachment = new AttachmentBuilder(Buffer.from(reportContent, 'utf8'), { name: fileName });

      const tradeReportsCh = channelMap.getChannel(client, 'trade-reports');
      if (tradeReportsCh && tradeReportsCh.id !== message.channelId) {
        const icon = tradeVerdict === 'GO' ? '✅' : tradeVerdict === 'BLOCKED' ? '🛑' : '⏳';
        await tradeReportsCh.send({
          content: `${icon} **${ticker}** — Trade signal: **${tradeVerdict}**`,
          files:   [new AttachmentBuilder(Buffer.from(reportContent, 'utf8'), { name: fileName })],
        });
      }

      await message.channel.send({
        content: `📎 Trade report — **${ticker}** | Signal: **${tradeVerdict}**${tradeReportsCh ? ` | Posted to <#${tradeReportsCh.id}>` : ''}`,
        files:   [attachment],
        allowedMentions: { repliedUser: false },
      });
      return;
    }

    // ── /portfolio — display portfolio state ──────────────────────────────────
    if (parsed?.cmd === '/portfolio') {
      const stateFile = path.join(OPENCLAW_DIR, 'output', 'portfolio', 'state.json');
      let state;
      try {
        state = JSON.parse(fs.readFileSync(stateFile, 'utf8'));
      } catch {
        stopTyping();
        await message.reply({
          content: '⚠️ Portfolio state file not found. Create `output/portfolio/state.json` to track positions.',
          allowedMentions: { repliedUser: false },
        });
        return;
      }
      stopTyping();
      const summary = formatPortfolioState(state);
      await sendResponse(message, summary, 'portfolio');
      return;
    }

    // ── /watchlist [add|remove|scan] [TICKER] ─────────────────────────────────
    if (parsed?.cmd === '/watchlist') {
      const parts  = parsed.args.trim().split(/\s+/);
      const subCmd = parts[0]?.toLowerCase() || 'show';
      const ticker = parts[1]?.toUpperCase() || '';
      const wl     = loadWatchlist();

      if (subCmd === 'add') {
        if (!ticker || !/^[A-Z]{1,5}$/.test(ticker)) {
          stopTyping();
          await message.reply({ content: 'Usage: `!john /watchlist add AAPL`', allowedMentions: { repliedUser: false } });
          return;
        }
        if (wl.tickers.find(t => t.symbol === ticker)) {
          stopTyping();
          await message.reply({ content: `⚠️ **${ticker}** is already on the watchlist.`, allowedMentions: { repliedUser: false } });
          return;
        }
        // Find latest memo and trade report
        let memoPath = null, tradePath = null;
        try {
          const mf = fs.readdirSync(OUTPUT_DIR).filter(f => f.startsWith(`${ticker}-diligence-`)).sort().reverse();
          if (mf.length) memoPath = path.join(OUTPUT_DIR, mf[0]);
        } catch { /* ignore */ }
        try {
          const tf = fs.readdirSync(path.join(OPENCLAW_DIR, 'output', 'trades')).filter(f => f.startsWith(`${ticker}-`) && f.endsWith('-final.md')).sort().reverse();
          if (tf.length) tradePath = path.join(OPENCLAW_DIR, 'output', 'trades', tf[0]);
        } catch { /* ignore */ }

        if (!memoPath) {
          stopTyping();
          await message.reply({ content: `⚠️ No diligence memo for **${ticker}**. Run \`/diligence ${ticker}\` first.`, allowedMentions: { repliedUser: false } });
          return;
        }
        wl.tickers.push({
          symbol: ticker,
          added:  new Date().toISOString().slice(0, 10),
          direction:    'LONG',
          entry_zone:   [],
          last_signal:  'UNKNOWN',
          last_checked: new Date().toISOString(),
          memo_path:    memoPath,
          trade_path:   tradePath,
        });
        saveWatchlist(wl);
        stopTyping();
        await message.reply({ content: `✅ **${ticker}** added to watchlist.`, allowedMentions: { repliedUser: false } });
        return;
      }

      if (subCmd === 'remove') {
        if (!ticker) {
          stopTyping();
          await message.reply({ content: 'Usage: `!john /watchlist remove AAPL`', allowedMentions: { repliedUser: false } });
          return;
        }
        const before = wl.tickers.length;
        wl.tickers = wl.tickers.filter(t => t.symbol !== ticker);
        saveWatchlist(wl);
        stopTyping();
        await message.reply({
          content: before !== wl.tickers.length ? `✅ **${ticker}** removed from watchlist.` : `⚠️ **${ticker}** was not on the watchlist.`,
          allowedMentions: { repliedUser: false },
        });
        return;
      }

      if (subCmd === 'scan') {
        if (wl.tickers.length === 0) {
          stopTyping();
          await message.reply({ content: '⚠️ Watchlist is empty. Add tickers with `!john /watchlist add AAPL`.', allowedMentions: { repliedUser: false } });
          return;
        }
        tradeFeed.setChannelMap(channelMap, client);
        await message.reply({
          content: `🎯 Scanning ${wl.tickers.length} watchlist ticker(s) for fresh entry signals...`,
          allowedMentions: { repliedUser: false },
        });
        // Re-run trade pipeline on each watchlist ticker sequentially
        for (const entry of wl.tickers) {
          try {
            const raw      = await runCycle(entry.symbol, entry.memo_path || null);
            const vmatch   = raw.match(/\nTRADE_VERDICT:(GO|WAIT|PASS|BLOCKED)\s*$/);
            const newSig   = vmatch ? vmatch[1] : 'UNKNOWN';
            const oldSig   = entry.last_signal;
            entry.last_signal   = newSig;
            entry.last_checked  = new Date().toISOString();
            if (newSig === 'GO' && oldSig !== 'GO') {
              const alertCh = channelMap.getChannel(client, 'alerts');
              if (alertCh) await alertCh.send({ content: `🎯 **WATCHLIST ALERT — ${entry.symbol}** signal changed to **GO** — check #entry-timing for details` });
            }
          } catch (err) {
            console.error(`[watchlist scan] ${entry.symbol} error:`, err.message);
          }
        }
        saveWatchlist(wl);
        stopTyping();
        const summary = wl.tickers.map(t => `**${t.symbol}**: ${t.last_signal}`).join(' | ');
        await message.reply({ content: `✅ Scan complete — ${summary}`, allowedMentions: { repliedUser: false } });
        return;
      }

      // Default: show watchlist
      stopTyping();
      if (wl.tickers.length === 0) {
        await message.reply({ content: '📋 Watchlist is empty. Add a ticker: `!john /watchlist add AAPL`', allowedMentions: { repliedUser: false } });
      } else {
        let out = `## 📋 Watchlist\n| Ticker | Signal | Checked | Added |\n|--------|--------|---------|-------|\n`;
        for (const t of wl.tickers) {
          const checked = t.last_checked ? new Date(t.last_checked).toISOString().slice(0, 10) : '—';
          out += `| ${t.symbol} | ${t.last_signal} | ${checked} | ${t.added} |\n`;
        }
        await sendResponse(message, out, 'watchlist');
      }
      return;
    }

    // ── /universe [show | add TICKER Sector | remove TICKER] ─────────────────
    if (parsed?.cmd === '/universe') {
      const parts  = parsed.args.trim().split(/\s+/);
      const subCmd = parts[0]?.toLowerCase() || 'show';
      const u      = loadUniverse();
      const sectors = Object.keys(u.sectors);

      if (subCmd === 'add') {
        const ticker = parts[1]?.toUpperCase();
        const sector = parts.slice(2).join(' ') || '';
        const matchedSector = sectors.find(s => s.toLowerCase() === sector.toLowerCase()) || sectors[0];

        if (!ticker || !/^[A-Z]{1,6}$/.test(ticker)) {
          stopTyping();
          await message.reply({ content: `Usage: \`!john /universe add AAPL Technology\`\nSectors: ${sectors.join(', ')}`, allowedMentions: { repliedUser: false } });
          return;
        }
        // Check if already exists
        const existing = sectors.find(s => (u.sectors[s].tickers || []).includes(ticker));
        if (existing) {
          stopTyping();
          await message.reply({ content: `⚠️ **${ticker}** already in universe (${existing}).`, allowedMentions: { repliedUser: false } });
          return;
        }
        if (!u.sectors[matchedSector].tickers) u.sectors[matchedSector].tickers = [];
        u.sectors[matchedSector].tickers.push(ticker);
        saveUniverse(u);
        stopTyping();
        await message.reply({ content: `✅ **${ticker}** added to **${matchedSector}**.\nTotal universe: ${sectors.flatMap(s => u.sectors[s].tickers || []).length} tickers.`, allowedMentions: { repliedUser: false } });
        return;
      }

      if (subCmd === 'remove') {
        const ticker = parts[1]?.toUpperCase();
        if (!ticker) {
          stopTyping();
          await message.reply({ content: 'Usage: `!john /universe remove AAPL`', allowedMentions: { repliedUser: false } });
          return;
        }
        let found = false;
        for (const s of sectors) {
          const before = (u.sectors[s].tickers || []).length;
          u.sectors[s].tickers = (u.sectors[s].tickers || []).filter(t => t !== ticker);
          if (u.sectors[s].tickers.length !== before) found = true;
        }
        if (found) saveUniverse(u);
        stopTyping();
        await message.reply({ content: found ? `✅ **${ticker}** removed from universe.` : `⚠️ **${ticker}** not found in universe.`, allowedMentions: { repliedUser: false } });
        return;
      }

      // Default: show
      stopTyping();
      let out = `## 🌐 Coverage Universe\n\n`;
      for (const [sectorName, sectorData] of Object.entries(u.sectors)) {
        const tickers = (sectorData.tickers || []);
        out += `**${sectorName}** (${tickers.length}): ${tickers.length ? tickers.join(', ') : '*empty*'}\n`;
        if (sectorData.description) out += `  *${sectorData.description}*\n`;
      }
      const total = sectors.flatMap(s => u.sectors[s].tickers || []).length;
      out += `\n**Total: ${total} ticker(s)**\n`;
      out += `\nAdd: \`!john /universe add AAPL Technology\`\nRemove: \`!john /universe remove AAPL\``;
      await sendResponse(message, out, 'universe');
      return;
    }

    // ── /run [hours] [all | SectorName | TICKER,...] [--interval N] [--max-cost N] ──
    if (parsed?.cmd === '/run') {
      const parts      = parsed.args.trim().split(/\s+/);
      const hours      = parseFloat(parts[0]) || 1;

      // Second arg can be: "all", sector name, or comma-separated tickers
      const tickerSpec = parts.slice(1).filter(p => !p.startsWith('--')).join(',') || 'all';
      const tickers    = resolveRunTickers(tickerSpec);

      if (tickers.length === 0) {
        stopTyping();
        const u = loadUniverse();
        const sectorList = Object.entries(u.sectors)
          .map(([n, d]) => `${n} (${(d.tickers||[]).length})`)
          .join(', ');
        await message.reply({
          content: `⚠️ No tickers found for \`${tickerSpec}\`.\n\nPopulate the universe first: \`!john /universe add AAPL Technology\`\nOr specify tickers: \`!john /run 4 AAPL MSFT NVDA\`\nSectors: ${sectorList}`,
          allowedMentions: { repliedUser: false },
        });
        return;
      }

      // Parse optional flags
      const intervalIdx = parts.indexOf('--interval');
      const intervalMin = intervalIdx !== -1 ? parseInt(parts[intervalIdx + 1], 10) : 30;
      const costIdx     = parts.indexOf('--max-cost');
      const maxCostUSD  = costIdx !== -1 ? parseFloat(parts[costIdx + 1]) : null;
      const preScreen   = parts.includes('--screen');

      // Start budget session
      tokenBudget.startSession({
        durationHours: hours,
        maxCostUSD,
        tickers,
        intervalMin,
      });

      // Wire feeds
      tradeFeed.setChannelMap(channelMap, client);
      tokenFeed.setChannelMap(channelMap, client);

      // Start background runner — pass resolved tickers explicitly
      startPipelineRunner(tickers, hours, intervalMin, maxCostUSD, { preScreen });

      stopTyping();
      const costStr    = maxCostUSD ? ` | Cost cap: **$${maxCostUSD}**` : '';
      const screenStr  = preScreen ? ' | Pre-screen: **ON** (only passing tickers enter diligence)' : '';
      const tickerStr  = tickers.length > 6 ? `${tickers.slice(0, 6).join(', ')} +${tickers.length - 6} more` : tickers.join(', ');
      await message.reply({
        content: [
          `🧮 **Full pipeline activated**`,
          `Tickers: **${tickerStr}** (${tickers.length} total)`,
          `Duration: **${hours}h** | Interval: **${intervalMin}m**${costStr}${screenStr}`,
          ``,
          `Token Monitor is active. Live updates will appear in each channel as agents complete.`,
          `Controls: \`/token-status\` \`/token-halt\` \`/token-resume\` \`/token-speed slow|normal|fast\``,
        ].join('\n'),
        allowedMentions: { repliedUser: false },
      });
      return;
    }

    // ── /token-status ─────────────────────────────────────────────────────────
    if (parsed?.cmd === '/token-status') {
      stopTyping();
      const status = tokenBudget.getStatus();
      await sendResponse(message, tokenBudget.formatStatus(status), 'token-status');
      return;
    }

    // ── /token-halt [reason] ──────────────────────────────────────────────────
    if (parsed?.cmd === '/token-halt') {
      const reason = parsed.args.trim() || 'operator halt';
      tokenBudget.halt(reason);
      tokenFeed.setChannelMap(channelMap, client);
      tokenFeed.operatorHalt(reason);
      stopTyping();
      await message.reply({
        content: `🛑 **Pipeline halted** — ${reason}\nUse \`!john /token-resume\` to continue.`,
        allowedMentions: { repliedUser: false },
      });
      return;
    }

    // ── /token-resume ─────────────────────────────────────────────────────────
    if (parsed?.cmd === '/token-resume') {
      tokenBudget.resume();
      tokenFeed.setChannelMap(channelMap, client);
      tokenFeed.operatorResume();
      stopTyping();
      await message.reply({
        content: '▶ **Pipeline resumed** — agents will continue spawning.',
        allowedMentions: { repliedUser: false },
      });
      return;
    }

    // ── /token-speed [slow|normal|fast|0.5|1.0|2.0] ──────────────────────────
    if (parsed?.cmd === '/token-speed') {
      const arg = parsed.args.trim().toLowerCase();
      const presets = { slow: 0.5, normal: 1.0, fast: 2.0, 'very slow': 0.25, 'very-slow': 0.25 };
      const multiplier = presets[arg] ?? parseFloat(arg);
      if (isNaN(multiplier) || multiplier <= 0) {
        stopTyping();
        await message.reply({
          content: 'Usage: `!john /token-speed slow|normal|fast` or a multiplier like `0.5`',
          allowedMentions: { repliedUser: false },
        });
        return;
      }
      const actual = tokenBudget.setSpeed(multiplier);
      const label  = actual >= 2.0 ? 'FAST' : actual >= 1.0 ? 'NORMAL' : actual >= 0.5 ? 'SLOW' : 'VERY SLOW';
      tokenFeed.setChannelMap(channelMap, client);
      tokenFeed.speedChange(actual, label);
      stopTyping();
      await message.reply({
        content: `🧮 Pipeline speed set to **${label}** (${actual}x multiplier)`,
        allowedMentions: { repliedUser: false },
      });
      return;
    }

    // ── /screen [sector] [--min-score N] [--extra-args "..."] ────────────────
    if (parsed?.cmd === '/screen') {
      const rawArgs   = parsed.args.trim();
      // Parse sector — first non-flag word, or "sector=X" key=value, or "all"
      const sectorKV  = rawArgs.match(/sector=(\S+)/i)?.[1];
      const sectorPos = rawArgs.split(/\s+/).find(p => !p.startsWith('--') && !p.includes('='));
      const sector    = sectorKV || sectorPos || 'all';
      const minScore  = rawArgs.match(/--min-score\s+([\d.]+)/)?.[1] || '0';
      const extraArgsMatch = rawArgs.match(/--extra-args\s+"([^"]+)"/)?.[1] || '';

      const sectorLabel = sector === 'all' ? 'all sectors' : sector;
      await message.reply({
        content: `📡 Running quantitative screen for **${sectorLabel}**... Fetching live data from Yahoo Finance + EDGAR (may take a few minutes).`,
        allowedMentions: { repliedUser: false },
      });

      const screenerOutput = await new Promise((resolve, reject) => {
        let stdout = '', stderr = '', lineBuf = '';
        const args = [
          path.join(OPENCLAW_DIR, 'scripts', 'screener.js'),
          '--sector', sector,
          '--min-score', minScore,
        ];
        if (extraArgsMatch) args.push('--extra-args', extraArgsMatch);

        const proc = spawn('node', args, {
          cwd: OPENCLAW_DIR,
          uid: CLAUDE_UID,
          gid: CLAUDE_GID,
          env: { ...process.env, HOME: CLAUDE_HOME, USER: 'claudebot', LOGNAME: 'claudebot', CLAUDE_BIN, OPENCLAW_DIR, SUDO_USER: undefined, SUDO_UID: undefined, SUDO_GID: undefined, SUDO_COMMAND: undefined },
          stdio: ['ignore', 'pipe', 'pipe'],
        });
        proc.stdout.on('data', (chunk) => {
          lineBuf += chunk.toString();
          const lines = lineBuf.split('\n');
          lineBuf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith('SCREENER_RESULT:')) stdout += line + '\n';
          }
        });
        proc.stderr.on('data', d => { stderr += d.toString(); });
        proc.on('close', (code) => {
          if (lineBuf && !lineBuf.startsWith('SCREENER_RESULT:')) stdout += lineBuf;
          if (code === 0 || stdout.trim()) resolve(stdout.trim());
          else reject(new Error(stderr.trim().slice(0, 300) || `screener exit ${code}`));
        });
        proc.on('error', reject);
      });

      stopTyping();

      // Load saved results for summary
      let passingTickers = [];
      let scanned = 0;
      try {
        const res = JSON.parse(fs.readFileSync(path.join(OPENCLAW_DIR, 'output', 'screen-results.json'), 'utf8'));
        passingTickers = res.tickers || [];
        scanned = res.scanned || 0;
      } catch { /* no results file */ }

      const summary = `📡 **Screen complete** — ${scanned} scanned | **${passingTickers.length} passed**: ${passingTickers.length ? passingTickers.join(', ') : 'none'}\n\nTo run diligence on passing tickers: \`!john /run 4 ${passingTickers.join(',')} --screen\``;
      await sendResponse(message, screenerOutput || summary, 'screen-results');
      if (passingTickers.length) {
        await message.reply({ content: summary, allowedMentions: { repliedUser: false } });
      }
      return;
    }

    // ── All other hedge fund commands → expand template, run via Claude Code ──
    if (parsed) {
      const ticker = parsed.args.trim().split(/\s+/)[0]?.toUpperCase() || '—';
      const fullPrompt = expandCommand(parsed.cmd, parsed.args) ?? raw;
      // Skill commands use haiku — faster responses for structured outputs
      const output = await runClaude(fullPrompt, OPENCLAW_DIR, { command: parsed.cmd, ticker, model: MODEL_FAST });
      stopTyping();
      await sendResponse(message, output, ticker !== '—' ? `${ticker}-${parsed.cmd.slice(1)}` : 'output');
      return;
    }

    // ── General prompt → Claude Code in root workdir ──────────────────────────
    const output = await runClaude(raw, ROOT_DIR, { command: 'general', ticker: '—' });
    stopTyping();
    await sendResponse(message, output, 'response');

  } catch (err) {
    stopTyping();
    console.error('Error:', err.message);
    await message.reply({ content: `⚠️ ${err.message}`, allowedMentions: { repliedUser: false } });
  }
});

// ── Auth sync — copy root credentials to claudebot on every startup ──────────
function syncClaudeAuth() {
  const srcCreds = '/root/.claude/.credentials.json';
  const dstCreds = `${CLAUDE_HOME}/.claude/.credentials.json`;
  const srcSettings = '/root/.claude/settings.local.json';
  const dstSettings = `${CLAUDE_HOME}/.claude/settings.local.json`;
  try {
    if (fs.existsSync(srcCreds)) {
      fs.copyFileSync(srcCreds, dstCreds);
      fs.chownSync(dstCreds, CLAUDE_UID, CLAUDE_GID);
      fs.chmodSync(dstCreds, 0o600);
    }
    if (fs.existsSync(srcSettings)) {
      fs.copyFileSync(srcSettings, dstSettings);
      fs.chownSync(dstSettings, CLAUDE_UID, CLAUDE_GID);
    }
    console.log('   Auth synced     : claudebot credentials refreshed');
  } catch (err) {
    console.warn('   Auth sync warn  :', err.message);
  }
}

// ── Ready ─────────────────────────────────────────────────────────────────────
client.once('clientReady', async () => {
  syncClaudeAuth();
  // Re-sync credentials every 45 minutes — OAuth tokens expire ~1h after root refreshes
  setInterval(syncClaudeAuth, 45 * 60 * 1000);
  console.log(`✅ BotJohn v2 online as ${client.user.tag}`);
  console.log(`   Root workdir    : ${ROOT_DIR}`);
  console.log(`   OpenClaw dir    : ${OPENCLAW_DIR}`);
  console.log(`   Claude bin      : ${CLAUDE_BIN}`);
  console.log(`   Claude user     : claudebot (uid=${CLAUDE_UID})`);
  console.log(`   Timeout         : ${MAX_TIMEOUT / 1000}s`);
  console.log(`   Output dir      : ${OUTPUT_DIR}`);
  client.user.setActivity('!john /run | /diligence | /trade | /status', { type: 4 });

  // Ensure required output directories exist
  for (const d of ['session', 'portfolio', 'trades', 'memos'].map(n => path.join(OPENCLAW_DIR, 'output', n))) {
    try { fs.mkdirSync(d, { recursive: true }); } catch { /* ok */ }
  }

  // ── Discord server setup ────────────────────────────────────────────────────
  const guild = client.guilds.cache.first();
  if (guild) {
    try {
      await setupServer(client, guild);
      channelMap.reload();
      // Wire multi-channel routing into the operator feed
      feed.setChannelMap(channelMap, client);
      // Wire channel map into trade feed
      tradeFeed.setChannelMap(channelMap, client);
      // Wire channel map into token feed + start budget alert poller
      tokenFeed.setChannelMap(channelMap, client);
      startBudgetAlertPoller();
      // Wire channel map into desk-controller for agent-to-channel posting
      console.log(`   Channel setup   : complete (${Object.keys(channelMap.getAll()).length} channels mapped)`);

      // Post startup notice to #botjohn-log
      const logCh = channelMap.getChannel(client, 'botjohn-log');
      if (logCh) {
        await logCh.send({ content: `\`${new Date().toISOString()}\` 🦞 BotJohn v2 online as **${client.user.tag}** | claudebot uid=${CLAUDE_UID} | claude-bin ready` });
      }
    } catch (err) {
      console.warn(`   Channel setup   : failed — ${err.message} (grant Administrator to the bot role)`);
    }
  }
});

client.on('error', (err) => console.error('Discord error:', err));
process.on('unhandledRejection', (err) => console.error('Unhandled rejection:', err));

client.login(BOT_TOKEN);
