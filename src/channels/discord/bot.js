'use strict';

/**
 * John Bot — Discord interface for BotJohn v2 (PTC Architecture)
 * Replaces: johnbot/index.js
 *
 * Routing logic:
 * - Flash commands (/ping, /status, /quote, etc.) → flash.js (instant)
 * - Complex tasks (/diligence, /trade, /screen, etc.) → main.js (PTC mode)
 * - @BotJohn or !john + freeform text → main.js (PTC mode)
 */

require('dotenv').config({ path: require('path').join(__dirname, '../../../.env') });

const { Client, GatewayIntentBits, AttachmentBuilder } = require('discord.js');
const fs   = require('fs');
const path = require('path');

const flash        = require('../../agent/flash');
const main         = require('../../agent/main');
const swarm        = require('../../agent/subagents/swarm');
const relay        = require('./relay');
const notifications = require('./notifications');
const { setupServer, refreshServerMap } = require('./setup');
const agentPersonas = require('./agent-personas');
const { pushSteering, updateOperatorActivity, getAllSubagentStatuses } = require('../../database/redis');
const { workspaces: pgWorkspaces, migrate } = require('../../database/postgres');
const tokenDb = require('../../database/tokens');
const workspaceManager = require('../../workspace/manager');
const { generateToolModules } = require('../../agent/tools/registry');
const { v4: uuidv4 } = require('uuid');
const memoryWriter = require('../../agent/memory/memory-writer');

// ── Config ─────────────────────────────────────────────────────────────────────
const BOT_TOKEN  = process.env.DISCORD_BOT_TOKEN;
const PREFIX     = '!john';
const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';

// Track active PTC runs: threadId → { ticker, command, startTime, channel }
const activeRuns = new Map();

// #data-alerts poster — set at startup when channel is available
let _dataAlertsPost = null;
function getDataAlertsPost() { return _dataAlertsPost; }

// Discord client + guild refs — set at startup, used for on-demand server map refresh
let _discordClient = null;
let _discordGuild  = null;
let _channelMap    = {};
async function triggerMapRefresh() {
  try {
    await refreshServerMap(_discordClient, _discordGuild, _channelMap);
  } catch (e) {
    console.warn('[bot] server map refresh failed:', e.message);
  }
}

// Research orchestrator singleton — shared between /research command and 30s status monitor
let _researchOrch = null;
function getResearchOrch() {
  if (!_researchOrch) _researchOrch = new (require('../../agent/research/research-orchestrator'))();
  return _researchOrch;
}

// ── Discord client ─────────────────────────────────────────────────────────────
const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildMessages,
    GatewayIntentBits.MessageContent,
    GatewayIntentBits.DirectMessages,
  ],
  partials: ['CHANNEL', 'MESSAGE'],
});

// ── Agent pipeline ────────────────────────────────────────────────────────────
// Agent-to-agent communication is DIRECT via pipeline_orchestrator.py:
//   post_memos.py → research_report.py → trade_agent.py  (sequential, in-process)
// Discord channels receive webhook posts for human visibility only.
// bot.js does NOT watch channels to trigger agents — no Discord round-trip.


// ── Message handler ────────────────────────────────────────────────────────────
client.on('messageCreate', async (message) => {
  if (message.author.bot) return;  // agent webhook posts are for human visibility only

  const content = message.content.trim();
  const userId = message.author.id;

  // Track operator activity for veto escalation logic
  await updateOperatorActivity(userId).catch(() => null);

  // Determine if this is a BotJohn command
  const isMention = message.mentions.has(client.user?.id);
  const isPrefix  = content.toLowerCase().startsWith(PREFIX.toLowerCase());
  if (!isMention && !isPrefix) return;

  // Extract command text (strip prefix or mention)
  let cmdText = content;
  if (isPrefix) cmdText = content.slice(PREFIX.length).trim();
  if (isMention) cmdText = content.replace(/<@!?\d+>/g, '').trim();

  // Download any image attachments and append [ATTACH: path] markers so BotJohn can see them
  if (message.attachments.size > 0) {
    const https    = require('https');
    const http     = require('http');
    // Use a claudebot-owned directory so subagent can read without permission issues
    const attachBase = path.join(__dirname, '../../../workspaces/default/tmp');
    fs.mkdirSync(attachBase, { recursive: true });
    const tmpDir   = fs.mkdtempSync(path.join(attachBase, 'discord-'));
    const imageExts = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp']);
    const downloads = [];

    for (const [, att] of message.attachments) {
      const ext = path.extname(att.name || '').toLowerCase();
      if (!imageExts.has(ext) && !att.contentType?.startsWith('image/')) continue;
      const localPath = path.join(tmpDir, att.name || `image${ext || '.png'}`);
      downloads.push(
        new Promise((res, rej) => {
          const dest = fs.createWriteStream(localPath);
          const get  = att.url.startsWith('https') ? https.get : http.get;
          get(att.url, (r) => { r.pipe(dest); dest.on('finish', () => res(localPath)); }).on('error', rej);
        })
      );
    }

    if (downloads.length > 0) {
      try {
        const paths = await Promise.all(downloads);
        // chmod a+r so claudebot (the subagent user) can read root-created files
        for (const p of paths) {
          try { fs.chmodSync(p, 0o644); } catch (_) {}
        }
        try { fs.chmodSync(tmpDir, 0o755); } catch (_) {}
        const markers = paths.map(p => `[ATTACH: ${p}]`).join('\n');
        cmdText = cmdText ? `${cmdText}\n${markers}` : markers;
      } catch (e) {
        console.error('[bot] Failed to download Discord attachments:', e.message);
      }
    }
  }

  if (!cmdText) { await message.reply('🦞 BotJohn online. Try `!john /help`'); return; }

  // Check if a PTC run is active for this thread — inject as steering
  const existingRun = findRunForChannel(message.channelId);
  if (existingRun) {
    await pushSteering(existingRun.threadId, cmdText);
    await message.reply(`⚙️ Steering injected — agent will see your message on next step.`);
    return;
  }

  // Special: /shutdown — handled before flash so we can send a final message
  if (cmdText.toLowerCase().startsWith('/shutdown')) {
    const lower = cmdText.toLowerCase();
    const confirmed = lower.includes('confirm');
    const poweroff  = lower.includes('server');
    if (!confirmed) {
      await message.reply([
        '⚠️ **Confirm shutdown:**',
        '`!john /shutdown confirm` — stop BotJohn (service stays off until manually restarted)',
        '`!john /shutdown server confirm` — stop BotJohn AND poweroff the VPS',
      ].join('\n'));
      return;
    }
    await message.reply(poweroff
      ? '🔴 **Powering off server in 3s...** Goodbye!'
      : '🔴 **Shutting down BotJohn in 3s...** Use `systemctl --user start johnbot.service` to restart.');
    if (collector) collector.pause();
    setTimeout(() => {
      if (poweroff) {
        require('child_process').exec('shutdown -h now', () => {});
      } else {
        process.exit(0);
      }
    }, 3000);
    return;
  }

  // Try flash mode first
  const flashResponse = await flash.dispatch(cmdText, null);
  if (flashResponse !== null) {
    await relay.send(message, flashResponse);
    return;
  }

  // Route to PTC mode
  await handlePtcCommand(cmdText, message, userId);
});

/**
 * Classify a user request for routing purposes.
 * Returns { mode } where mode is DEPLOY/REPORT/RISK_SCAN/STATUS/ADMIN/GENERAL.
 *
 * NOTE: BotJohn is the master PM agent — general messages always go through to
 * main.runTask(). Only deprecated subagent invocation slash-commands are redirected.
 * The hard agent-spawn gate lives in swarm.init (deployment-gate.js), not here.
 */
function classifyRequest(content) {
    const lower = content.toLowerCase();

    if (/\/(deploy|new-strategy|create-strategy|build-strategy)/.test(lower))
        return { mode: 'DEPLOY', agent: 'strategist' };

    if (/\/(strategy-report|performance-report|show-report)/.test(lower))
        return { mode: 'REPORT', agent: 'report-builder' };

    if (/\/risk-scan/.test(lower))
        return { mode: 'RISK_SCAN', agent: 'strategist' };

    // Known slash-commands route to STATUS (handled in switch, no gate needed)
    if (/^\/(signals|strategies|regime|status|portfolio|engine|approve|activate|deactivate|reject|pause|fetch|fill|data|data-status|agents|chart|pipeline|trade|diligence|run|help|sweep|update-profile|cycles|approve-dataset|approve-strategy|strategy-review|engine-status|engine-run|pause-strategy|adjust-strategy|strategy-versions|research|risk-scan|approve-data|veto-data|approve-deprecation|refresh-map)\b/.test(lower.trimStart()))
        return { mode: 'STATUS', agent: null };

    // Everything else — freeform message to BotJohn as master PM agent
    return { mode: 'GENERAL', agent: 'botjohn' };
}

async function handlePtcCommand(cmdText, message, userId) {
  const parts = cmdText.split(/\s+/);
  const cmd   = parts[0].toLowerCase().replace(/^\//, '');
  const args  = parts.slice(1);

  const threadId = uuidv4();
  const channel  = message.channel;
  const workspaceId = 'default'; // TODO: per-user workspaces

  const notify = async (text) => {
    try { await relay.send(message, text, { channelOverride: channel }); } catch {}
  };

  // Register active run
  activeRuns.set(threadId, { threadId, command: cmd, channel: channel.id, startTime: Date.now() });

  // Task type for cost tracking
  const taskType = ['fetch', 'data'].includes(cmd) ? 'data-fetch'
    : cmd === 'diligence' ? 'diligence'
    : cmd === 'trade'     ? 'trade'
    : 'general';

  // Pre-task cost estimate
  const estimate = await tokenDb.estimateCost(taskType, args[0]?.toUpperCase()).catch(() => null);
  if (estimate?.estimated != null) {
    const low  = (estimate.low  * 100).toFixed(2);
    const high = (estimate.high * 100).toFixed(2);
    const avg  = (estimate.estimated * 100).toFixed(2);
    await notify(`💰 Estimated cost: **~$${(estimate.estimated).toFixed(4)}** (¢${low}–¢${high}) | Based on ${estimate.samples} prior runs | ~${Math.round(estimate.avgDurationS / 60)}m`);
  }

  // Start task cost record
  await tokenDb.startTask(threadId, taskType, args[0]?.toUpperCase(), estimate?.estimated).catch(() => null);

  const stopTyping = startTyping(channel);

  try {
    switch (cmd) {
      case 'chart': {
        const ticker = args[0]?.toUpperCase();
        if (!ticker) { await notify('Usage: `!john /chart AAPL`'); break; }
        await notify(`📊 Generating chart for **${ticker}**...`);
        const chartPath = await generateChart(ticker);
        if (!chartPath) { await notify(`No price data for **${ticker}** yet — pipeline still collecting`); break; }
        const attachment = new AttachmentBuilder(chartPath, { name: `${ticker}_price.png` });
        await message.channel.send({ content: `📊 **${ticker}** — 1Y price action`, files: [attachment] });
        fs.unlinkSync(chartPath);
        break;
      }

      case 'fill': {
        // Full historical fill for universe tickers (or specific tickers).
        // By default only fills tickers with no existing price data (new tickers).
        // --force includes all universe tickers regardless of existing coverage.
        //
        // Usage:
        //   !john /fill              — fill all new tickers in universe
        //   !john /fill sp500        — same
        //   !john /fill AAPL NVDA    — specific tickers
        //   !john /fill --force      — fill entire universe (overwrites gaps)

        const storeLib = require('../../pipeline/store');
        const { query: dbQ } = require('../../database/postgres');
        const { Pool: FPool } = require('pg');

        const flagForce  = args.includes('--force');
        const tickerArgs = args.filter(a => !a.startsWith('--') && !/^(sp500|all)$/i.test(a));

        // DataBot owns all fill responses
        const dbPost = (msg) => agentPersonas.post('databot', 'data-alerts', msg).catch(() => {});

        const fullUniverse = await storeLib.getActiveUniverse().catch(() => []);
        if (fullUniverse.length === 0) {
          await dbPost('⚠️ Universe is empty — add tickers to universe_config first.');
          await notify('→ #data-alerts');
          break;
        }

        // Determine candidate tickers
        let candidates;
        if (tickerArgs.length > 0) {
          candidates = tickerArgs.map(t => t.toUpperCase());
        } else {
          candidates = fullUniverse.map(u => u.ticker);
        }

        // Filter to uncovered tickers unless --force
        let fillTickers;
        if (flagForce) {
          fillTickers = candidates;
        } else {
          const coveredSet = await dbQ(
            `SELECT DISTINCT ticker FROM data_coverage WHERE data_type = 'prices'`
          ).then(r => new Set(r.rows.map(row => row.ticker))).catch(() => new Set());

          fillTickers = candidates.filter(t => !coveredSet.has(t));

          if (fillTickers.length === 0) {
            const hint = candidates.length === fullUniverse.length ? 'entire universe' : `${candidates.length} ticker(s)`;
            await dbPost(
              `✅ All ${hint} already have price history — nothing to fill.\n` +
              `Use \`!john /fill --force\` to re-fill all tickers regardless of coverage.`
            );
            await notify('→ #data-alerts');
            break;
          }

          if (fillTickers.length < candidates.length) {
            await dbPost(
              `📋 **${fillTickers.length} new ticker(s)** to fill ` +
              `(${candidates.length - fillTickers.length} already covered — skipping those)`
            );
          }
        }

        // Determine which new tickers have options / fundamentals enabled
        const universeMap = new Map(fullUniverse.map(u => [u.ticker, u]));
        const optTickers  = fillTickers.filter(t => universeMap.get(t)?.has_options);
        const fundTickers = fillTickers.filter(t => universeMap.get(t)?.has_fundamentals);

        const plan = {
          datasets: [
            {
              name: 'prices', tickers: fillTickers,
              lookback_days: 3650, priority: 1, provider: 'polygon',
            },
            optTickers.length > 0 && {
              name: 'options_eod', tickers: optTickers,
              lookback_days: 30, priority: 2, provider: 'polygon',
            },
            fundTickers.length > 0 && {
              name: 'financials', tickers: fundTickers,
              lookback_days: 3650, priority: 3, provider: 'fmp',
            },
          ].filter(Boolean),
          unavailable: [],
          estimated_rows: fillTickers.length * 2520,
        };

        const fillDesc = tickerArgs.length > 0
          ? `Historical fill for ${fillTickers.join(', ')}`
          : `Full universe fill — ${fillTickers.length} new ticker(s) (10yr prices${optTickers.length ? ', options' : ''}${fundTickers.length ? ', fundamentals' : ''})`;

        const fp = new FPool({ connectionString: process.env.POSTGRES_URI });
        const taskRes = await fp.query(
          `INSERT INTO data_tasks (workspace_id, description, status, plan, requested_by)
           VALUES ($1, $2, 'queued', $3, $4) RETURNING id`,
          [workspaceId, fillDesc, JSON.stringify(plan), userId || 'operator']
        );
        await fp.end();
        const taskId = taskRes.rows[0].id;

        // BotJohn hands off, DataBot takes over in #data-alerts
        await notify(`📡 Handed off to DataBot → #data-alerts`);
        await dbPost([
          `📥 **Fill task received** — \`${taskId.slice(0, 8)}\``,
          `Tickers: **${fillTickers.length}** | Est. rows: ~${plan.estimated_rows.toLocaleString()}`,
          `Datasets: ${plan.datasets.map(d => d.name).join(', ')}`,
          `Starting collection now...`,
        ].join('\n'));
        await agentPersonas.setStatus('botjohn', 'busy', `Filling ${fillTickers.length} tickers`);

        const executor = require('../../pipeline/data-task-executor');
        executor.executeTask(taskId, dbPost).catch(err => {
          dbPost(`⚠️ Fill executor error: ${err.message}`).catch(() => null);
        });

        break;
      }

      case 'fetch': {
        const ticker = args[0]?.toUpperCase();
        if (!ticker) { await notify('Usage: `!john /fetch AAPL`'); break; }
        await notify(`📡 Fetching data for **${ticker}** — price action, metrics, technicals...`);
        const result = await swarm.init({ type: 'data-fetch', ticker, workspace: await workspaceManager.getOrCreate(workspaceId), threadId, notify });
        await postFetchResult(message, ticker, result);
        break;
      }

      case 'data': {
        // If first arg looks like a ticker (1-5 uppercase letters), use data-fetch (backward compat)
        const firstArg = args[0] || '';
        if (/^[A-Z]{1,5}(\.[A-Z]{1,2})?$/.test(firstArg) && firstArg.length <= 6) {
          const ticker = firstArg;
          await notify(`📡 Fetching data for **${ticker}** — price action, metrics, technicals...`);
          const result = await swarm.init({ type: 'data-fetch', ticker, workspace: await workspaceManager.getOrCreate(workspaceId), threadId, notify });
          await postFetchResult(message, ticker, result);
          break;
        }

        // Otherwise: natural language data collection request → data agent
        // DataBot owns all responses from here
        const dbPost2 = (msg) => agentPersonas.post('databot', 'data-alerts', msg).catch(() => {});

        const description = args.join(' ').trim();
        if (!description) {
          await notify(
            '📋 **Usage:** `/data {description}`\n' +
            'Examples:\n' +
            '• `/data add 5 years of price history for NVDA and AMD`\n' +
            '• `/data fetch options data for TSLA AAPL MSFT`\n' +
            '• `/data get macro data — GDP CPI rates`\n\n' +
            '_Or use `/fetch AAPL` for a quick single-ticker data pull._'
          );
          break;
        }

        const { Pool: DPool } = require('pg');
        const dp = new DPool({ connectionString: process.env.POSTGRES_URI });
        const taskResult = await dp.query(
          `INSERT INTO data_tasks (workspace_id, description, status, requested_by)
           VALUES ($1, $2, 'queued', $3) RETURNING id`,
          [workspaceId, description, userId || 'operator']
        );
        await dp.end();
        const taskId = taskResult.rows[0].id;

        // BotJohn hands off, DataBot takes over
        await notify(`📡 Handed off to DataBot → #data-alerts`);
        await dbPost2(
          `📋 **Data request received** — \`${taskId.slice(0, 8)}\`\n` +
          `*${description}*\n\n` +
          `Planning collection...`
        );
        await agentPersonas.setStatus('botjohn', 'busy', description.slice(0, 60));

        const workspace = await workspaceManager.getOrCreate(workspaceId);
        try {
          // Phase 1: data-agent (Haiku) writes the collection plan
          await swarm.init({
            type:     'data-agent',
            mode:     'PLAN',
            workspace,
            threadId,
            notify:   dbPost2,
            env: {
              DATA_AGENT_TASK: description,
              DATA_TASK_ID:    taskId,
              WORKSPACE_ID:    workspaceId,
            },
            prompt: `DATA_AGENT_TASK: ${description}\nDATA_TASK_ID: ${taskId}`,
          });

          // Phase 2: executor reads the plan and runs collector functions
          const executor = require('../../pipeline/data-task-executor');
          executor.executeTask(taskId, dbPost2).catch(err => {
            dbPost2(`⚠️ Collection executor error: ${err.message}`).catch(() => null);
          });

        } catch (err) {
          await dbPost2(`⚠️ Data agent error: ${err.message}. Task \`${taskId.slice(0, 8)}\` is queued — collection can be triggered manually.`);
        }
        break;
      }

      case 'data-status': {
        // DataBot reports its own task history
        const dbPost3 = (msg) => agentPersonas.post('databot', 'data-alerts', msg).catch(() => notify(msg));
        const { Pool: DS2 } = require('pg');
        const ds2 = new DS2({ connectionString: process.env.POSTGRES_URI });
        const tasks = await ds2.query(
          `SELECT id, description, status, rows_added, queued_at, completed_at
           FROM data_tasks ORDER BY queued_at DESC LIMIT 10`
        );
        await ds2.end();
        await notify('→ #data-alerts');
        if (tasks.rows.length === 0) { await dbPost3('No data tasks on record.'); break; }
        const lines = tasks.rows.map(t =>
          `• \`${t.id.slice(0, 8)}\` **${t.status}** | +${t.rows_added ?? 0} rows | ${t.description.slice(0, 60)}${t.description.length > 60 ? '…' : ''}`
        );
        await dbPost3(`**📡 Data Tasks (last 10)**\n${lines.join('\n')}`);
        break;
      }

      case 'agents': {
        // Show status board for all OpenClaw agents + any active subagents
        const [agentRows, subagentKeys] = await Promise.all([
          agentPersonas.getAllStatuses(),
          getAllSubagentStatuses().catch(() => []),
        ]);

        const statusIcon = (s) => ({ online: '🟢', busy: '🟡', idle: '🔵', offline: '⚫' }[s] || '⚫');
        const timeAgo = (ts) => {
          if (!ts) return 'never';
          const s = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
          if (s < 60) return `${s}s ago`;
          if (s < 3600) return `${Math.floor(s / 60)}m ago`;
          return `${Math.floor(s / 3600)}h ago`;
        };

        const lines = ['**🤖 OpenClaw Agent Status Board**', ''];

        for (const row of agentRows) {
          const icon = statusIcon(row.status);
          const seen = row.last_seen_at ? `last seen ${timeAgo(row.last_seen_at)}` : 'never active';
          const task = row.current_task ? ` — *${row.current_task.slice(0, 60)}*` : '';
          const channels = (row.channel_keys || []).map(k => `#${k}`).join(', ');
          lines.push(`${icon} **${row.display_name}** \`${row.status}\`${task}`);
          lines.push(`   ${row.description} | Channels: ${channels || 'none'} | ${seen}`);
        }

        // Active subagents from Redis
        const active = subagentKeys.filter(s => s.status === 'running' || s.status === 'active');
        if (active.length > 0) {
          lines.push('');
          lines.push(`**⚙️ Active Subagents (${active.length})**`);
          for (const sa of active.slice(0, 8)) {
            const elapsed = sa.startedAt ? Math.floor((Date.now() - new Date(sa.startedAt).getTime()) / 1000) : 0;
            lines.push(`  🟡 \`${sa.type}\` — ${sa.ticker || 'no ticker'} | ${elapsed}s elapsed | $${(sa.costUsd || 0).toFixed(4)}`);
          }
        } else {
          lines.push('');
          lines.push('*No subagents currently running*');
        }

        await notify(lines.join('\n'));
        break;
      }

      case 'diligence':
      case 'run': {
        const ticker = args[0]?.toUpperCase();
        if (!ticker) { await notify('Usage: `!john /diligence AAPL`'); break; }
        const dilTaskId = memoryWriter.openTask(`Diligence: ${ticker}`, `threadId:${threadId}`);
        agentPersonas.setStatus('researchdesk', 'busy', `Diligence: ${ticker}`).catch(() => {});
        agentPersonas.post('researchdesk', 'research-feed', `🔬 Diligence started for **${ticker}** — spawning subagents`).catch(() => {});
        const result = await main.runTask({ task: `Research ${ticker} strategy`, ticker, workspaceId, threadId, notify });
        await postResult(message, ticker, result, 'diligence');
        // ResearchDesk posts verdict to #strategy-memos
        const verdict = result?.verdict || result?.output?.match(/\b(PROCEED|REVIEW|KILL)\b/)?.[0];
        if (verdict) {
          agentPersonas.post('researchdesk', 'strategy-memos', `🦞 **${ticker}** — **${verdict}** | 📎 memo attached`).catch(() => {});
          memoryWriter.closeTask(dilTaskId, `${verdict} — ${ticker}`);
        } else {
          memoryWriter.closeTask(dilTaskId, `complete — ${ticker}`);
        }
        agentPersonas.setStatus('researchdesk', 'idle', null).catch(() => {});
        break;
      }

      case 'trade': {
        const ticker = args[0]?.toUpperCase();
        if (!ticker) { await notify('Usage: `!john /trade AAPL`'); break; }
        const tradeTaskId = memoryWriter.openTask(`Trade optimization: ${ticker}`, `threadId:${threadId}`);
        agentPersonas.setStatus('tradedesk', 'busy', `Trade: ${ticker}`).catch(() => {});
        agentPersonas.post('tradedesk', 'trade-signals', `📐 Trade pipeline starting for **${ticker}**`).catch(() => {});
        const result = await main.runTask({ task: `Generate trade signal for ${ticker}`, ticker, workspaceId, threadId, notify });
        await postResult(message, ticker, result, 'trade');
        // TradeDesk posts final report to #trade-reports
        const tradeVerdict = result?.verdict || result?.output?.match(/\b(APPROVED|BLOCKED|PENDING REVIEW)\b/)?.[0];
        if (tradeVerdict) {
          const blocked = tradeVerdict === 'BLOCKED';
          agentPersonas.post('tradedesk', 'trade-reports', `${blocked ? '🚫' : '✅'} **${ticker}** — **${tradeVerdict}** | 📎 report attached`).catch(() => {});
          if (blocked) agentPersonas.post('tradedesk', 'trade-reports', `🚫 **BLOCKED trade: ${ticker}** — ${tradeVerdict}`).catch(() => {});
          memoryWriter.closeTask(tradeTaskId, `${tradeVerdict} — ${ticker}`);
        } else {
          memoryWriter.closeTask(tradeTaskId, `complete — ${ticker}`);
        }
        agentPersonas.setStatus('tradedesk', 'idle', null).catch(() => {});
        break;
      }

      case 'pipeline': {
        const sub = args[0]?.toLowerCase();
        if (!collector) { await notify('⚠️ Pipeline module unavailable'); break; }
        if (sub === 'cycles') {
          const flashResult = await flash.dispatch(`cycles ${args[1] || ''}`.trim(), threadId);
          await notify(flashResult || '⚠️ No cycle data');
        } else if (sub === 'pause' || sub === 'off') {
          collector.pause();
          await store.setConfig('collection_enabled', 'false').catch(() => null);
          await notify('⏸️ Pipeline paused — collection halted and persisted off');
        } else if (sub === 'resume' || sub === 'on') {
          collector.resume();
          await store.setConfig('collection_enabled', 'true').catch(() => null);
          await notify('▶️ Pipeline resumed');
        } else {
          // Status
          const stats = collector.getStats();
          const store = require('../../pipeline/store');
          const [cov, cfgRows, apiStats] = await Promise.all([
            store.getCoverageStats().catch(() => null),
            store.getAllConfig().catch(() => []),
            store.getTodayApiStats().catch(() => ({ polygon: 0, fmp: 0, errors: 0, rows: 0 })),
          ]);
          const cfg = Object.fromEntries((cfgRows || []).map(r => [r.key, r.value]));

          const stateIcon = stats.paused ? '⏸️ Paused' : stats.sleeping ? '😴 Sleeping' : '🟢 Running';
          const nextRun = stats.nextRunAt
            ? stats.nextRunAt.toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true, timeZoneName: 'short' })
            : null;

          const fmpPerDay    = parseInt(cfg.fmp_req_per_day || '50000', 10);
          const fmpPerMin    = parseInt(cfg.fmp_req_per_min || '300', 10);
          const polyUnlimited = parseInt(cfg.polygon_req_per_min || '9999', 10) >= 9999;

          // Universe size from DB
          const universeSize = await store.getUniverseTickers().catch(() => []).then(t => t.length);

          const agentRows = await agentPersonas.getAllStatuses().catch(() => []);
          const agentStatusLine = (id, emoji) => {
            const a = agentRows.find(r => r.id === id);
            if (!a) return null;
            const dot = { online: '🟢', busy: '🔴', idle: '🟡', offline: '⚫' }[a.status] || '⚫';
            const task = a.current_task ? ` — ${a.current_task}` : '';
            return `  ${dot} **${a.display_name}**${task}`;
          };

          const lines = [
            `📡 **Pipeline Status** — ${collector.isRunning() ? stateIcon : '🔴 Stopped'}`,
            nextRun ? `Next collection: **${nextRun}**` : '',
            ``,
            `**Agents**`,
            agentStatusLine('botjohn',      '🦞'),
            agentStatusLine('databot',      '📡'),
            agentStatusLine('researchdesk', '🔬'),
            agentStatusLine('tradedesk',    '📈'),
            ``,
            `**Universe** | ${universeSize} tickers active in universe_config`,
            `**Coverage** | prices: ${cov?.price_coverage ?? '?'} tickers | options: ${cov?.options_coverage ?? '?'} | tech: ${cov?.tech_coverage ?? '?'} | fundamentals: ${cov?.fund_coverage ?? '?'}`,
            `**Price history** | ${(cov?.price_rows_total ?? 0).toLocaleString()} rows | ${cov?.price_earliest ?? '?'} → ${cov?.price_latest ?? '?'}`,
            ``,
            `**API calls today** (from DB — survives restarts):`,
            `  Polygon/Massive: **${apiStats.polygon.toLocaleString()} calls** | ${polyUnlimited ? 'unlimited plan' : `${cfg.polygon_req_per_min}/min`}`,
            `  FMP: **${apiStats.fmp.toLocaleString()} calls** | ${fmpPerMin}/min | ${fmpPerDay.toLocaleString()}/day limit`,
            `  Rows written today: **${apiStats.rows.toLocaleString()}** | Errors: ${apiStats.errors}`,
            ``,
            `**Session stats** | snapshots: ${stats.snapshots} | prices: ${stats.prices} | options: ${stats.options} | fundamentals: ${stats.fundamentals}`,
            stats.lastRun ? `Last run: ${new Date(stats.lastRun).toLocaleString('en-US', { timeZone: 'America/New_York', timeZoneName: 'short' })}` : '',
          ].filter(Boolean);
          await notify(lines.join('\n'));
        }
        break;
      }

      case 'git': {
        const sub = args[0]?.toLowerCase();
        if (sub !== 'sync') { await notify('Usage: `!john /git sync`'); break; }

        await notify('🔄 Syncing to GitHub...');
        const { execSync } = require('child_process');
        const GIT_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';

        try {
          // Collect diff stat before staging (unstaged changes)
          let diffStat = '';
          try {
            diffStat = execSync('git diff --stat HEAD', { cwd: GIT_DIR, encoding: 'utf8' }).trim();
          } catch {}

          // Stage everything (respecting .gitignore)
          execSync('git add -A', { cwd: GIT_DIR });

          // Check if there's anything to commit
          const staged = execSync('git diff --cached --stat', { cwd: GIT_DIR, encoding: 'utf8' }).trim();
          if (!staged) {
            await notify('✅ **GitHub in sync** — nothing new to commit.');
            break;
          }

          // Commit with timestamp
          const ts  = new Date().toISOString().replace('T', ' ').slice(0, 16) + ' UTC';
          const msg = `Auto-sync: ${ts}`;
          execSync(`git commit -m "${msg}"`, { cwd: GIT_DIR });

          // Push
          execSync('git push origin main', { cwd: GIT_DIR });

          // Build summary (cap at 1800 chars to stay under Discord limit)
          const lines = staged.split('\n');
          const fileLines = lines.filter(l => l.includes('|') || l.includes('changed'));
          const summary  = fileLines.slice(0, 20).join('\n');
          const truncated = fileLines.length > 20 ? `\n…and ${fileLines.length - 20} more files` : '';

          await notify(
            `✅ **GitHub synced** — \`${msg}\`\n\`\`\`\n${summary}${truncated}\n\`\`\``
          );
        } catch (err) {
          await notify(`⚠️ **Git sync failed**\n\`\`\`\n${err.message.slice(0, 500)}\n\`\`\``);
        }
        break;
      }

      case 'approve': {
        const tradeId = args[0];
        if (!tradeId) { await notify('Usage: `!john /approve {trade_id}`'); break; }
        const { trades } = require('../../database/postgres');
        await trades.updateStatus(tradeId, 'approved');
        await notify(`✅ Trade ${tradeId} approved.`);
        agentPersonas.post('tradedesk', 'trade-reports', `✅ **Trade approved** — ID \`${tradeId}\``).catch(() => {});
        break;
      }

      case 'reject': {
        const tradeId = args[0];
        if (!tradeId) { await notify('Usage: `!john /reject {trade_id}`'); break; }
        const { trades } = require('../../database/postgres');
        await trades.updateStatus(tradeId, 'blocked');
        await notify(`🚫 Trade ${tradeId} rejected.`);
        agentPersonas.post('tradedesk', 'trade-reports', `🚫 **Trade rejected** — ID \`${tradeId}\``).catch(() => {});
        break;
      }

      case 'deploy-strategy':
      case 'new-strategy':
      case 'create-strategy':
      case 'build-strategy': {
        const description = args.join(' ');
        if (!description || description.replace(/--force/g, '').trim().length < 20) {
          await notify(
            '📋 **Usage:** `/deploy-strategy {description}`\n\n' +
            'Describe the strategy in enough detail for the strategist to research and build it.\n\n' +
            '**Examples:**\n' +
            '• `/deploy-strategy momentum strategy using 20-day vs 50-day moving average crossover for SP100`\n' +
            '• `/deploy-strategy value strategy using P/B ratio below 1.5 combined with positive FCF yield`\n' +
            '• `/deploy-strategy options strategy selling covered calls on high-IV names after earnings`'
          );
          break;
        }

        const forceFlag = args.includes('--force');
        const scheduler = require('../agent/graph/strategist-scheduler');
        const check     = await scheduler.canActivate(workspaceId, forceFlag);

        if (!check.allowed && !forceFlag) {
          await notify(
            `⛔ Strategist unavailable:\n${check.summary}\n\n` +
            `Use \`/deploy-strategy ${description} --force\` to override off-hours/budget constraints.`
          );
          break;
        }

        await notify(
          `🔬 **DEPLOY session starting.**\n` +
          `Strategy brief: *${description.replace(' --force', '')}*\n\n` +
          `Budget: ${check.budget?.formatted || 'checking...'}\n` +
          `The strategist will research, backtest, and write the strategy file.\n` +
          `You'll receive a Discord notification when the strategy is ready for review.`
        );

        const workspace = await workspaceManager.getOrCreate(workspaceId);
        await swarm.init({
          type:      'strategist',
          mode:      'EXPLORE',
          workspace: workspace?.path || workspace,
          threadId,
          notify,
          force:     forceFlag,
          prompt: (
            `DEPLOY SESSION. Strategy brief from operator: "${description.replace(' --force', '')}". ` +
            `Research this strategy, backtest it against the master dataset, ` +
            `and if validated, write the Python strategy file and register it. ` +
            `The strategy must read exclusively from the signals cache (master dataset). ` +
            `No external API calls during execution.`
          ),
        });
        break;
      }

      case 'strategy-report': {
        const stratId = args[0];
        if (!stratId) { await notify('Usage: `/strategy-report {strategy_id}`'); break; }
        const workspace = await workspaceManager.getOrCreate(workspaceId);
        await notify(`📊 Generating performance report for strategy \`${stratId}\`...`);
        await swarm.init({
          type:      'report-builder',
          mode:      'STRATEGY_PERFORMANCE',
          workspace: workspace?.path || workspace,
          threadId,
          notify,
          prompt:    `Generate strategy performance report for strategy_id: ${stratId}. Load all signal_pnl records for this strategy and compute win rate, P&L, regime breakdown, and best/worst conditions.`,
        });
        break;
      }

      case 'research': {
        const subcmd = args[0]?.toLowerCase();
        const orch = getResearchOrch();
        const channelNotify = (t) => agentPersonas.post('researchdesk', 'research-feed', t).catch(() => {});

        switch (subcmd) {
          case 'submit': {
            const url = args[1];
            if (!url) { await notify('Usage: `/research submit <url>`'); break; }
            const { candidate_id, message } = await orch.submit({ url, submittedBy: userId });
            await notify(`✅ ${message}`);
            channelNotify?.(`📥 **Paper submitted** by <@${userId}>: ${url}\nID: \`${candidate_id}\``);
            break;
          }
          case 'start': {
            const msg = await orch.start({ notify: (t) => notify(t), channelNotify });
            await notify(msg);
            break;
          }
          case 'pause': {
            await notify(await orch.pause());
            break;
          }
          case 'status': {
            await notify(await orch.getStatus());
            break;
          }
          case 'queue': {
            await notify(await orch.listQueue(10));
            break;
          }
          case 'run-one': {
            // Populate queue from arXiv if empty, then run until 1 strategy promoted, then auto-pause.
            const msg = await orch.runOne({ notify: (t) => notify(t), channelNotify });
            await notify(msg);
            break;
          }
          case 'discover': {
            // Populate the research queue from arXiv without starting processing.
            const days = parseInt(args[1]) || 14;
            await orch.discover({ days, notify: (t) => notify(t), channelNotify });
            await notify(await orch.getStatus());
            break;
          }
          default:
            await notify('Usage: `/research [submit <url> | start | pause | status | queue | run-one | discover]`');
        }
        break;
      }

      case 'approve-data': {
        const reqId = args[0];
        if (!reqId) { await notify('Usage: `/approve-data <request_id>`'); break; }
        try {
          const { Pool } = require('pg');
          const pool = new Pool({ connectionString: process.env.POSTGRES_URI });
          const { rows } = await pool.query(
            `UPDATE data_ingestion_queue SET status='APPROVED', approved_by=$1, approved_at=NOW()
             WHERE request_id=$2 AND status='PENDING'
             RETURNING request_id, column_name`,
            [userId, reqId]
          );
          await pool.end();
          if (rows.length === 0) { await notify(`⚠️ No pending request found for ID \`${reqId}\``); break; }
          const col = rows[0].column_name;
          await notify(`✅ Data column \`${col}\` approved — DataWiringAgent will wire it.`);
          agentPersonas.post('researchdesk', 'research-feed',
            `🔧 **Column approved:** \`${col}\` — DataWiringAgent wiring now...`).catch(() => {});
          triggerMapRefresh().catch(() => {});
          // Fire DataWiringAgent asynchronously
          const orch = getResearchOrch();
          orch._wireColumn(rows[0]).catch((e) => {
            console.error('[bot] DataWiringAgent failed:', e.message);
            agentPersonas.post('researchdesk', 'research-feed',
              `❌ DataWiringAgent failed for \`${col}\`: ${e.message}`).catch(() => {});
          });
        } catch (e) {
          await notify(`❌ approve-data error: ${e.message}`);
        }
        break;
      }

      case 'veto-data': {
        const reqId = args[0];
        if (!reqId) { await notify('Usage: `/veto-data <request_id>`'); break; }
        try {
          const { Pool } = require('pg');
          const pool = new Pool({ connectionString: process.env.POSTGRES_URI });
          const { rows } = await pool.query(
            `UPDATE data_ingestion_queue SET status='VETOED', approved_by=$1, approved_at=NOW()
             WHERE request_id=$2 AND status='PENDING'
             RETURNING column_name`,
            [userId, reqId]
          );
          await pool.end();
          if (rows.length === 0) { await notify(`⚠️ No pending request found for ID \`${reqId}\``); break; }
          await notify(`🚫 Data column \`${rows[0].column_name}\` vetoed.`);
          triggerMapRefresh().catch(() => {});
        } catch (e) {
          await notify(`❌ veto-data error: ${e.message}`);
        }
        break;
      }

      case 'approve-deprecation': {
        const reqId = args[0];
        if (!reqId) { await notify('Usage: `/approve-deprecation <request_id>`'); break; }
        try {
          const { Pool } = require('pg');
          const pool = new Pool({ connectionString: process.env.POSTGRES_URI });
          const { rows } = await pool.query(
            `UPDATE data_deprecation_queue SET status='APPROVED', approved_by=$1, approved_at=NOW()
             WHERE request_id=$2 AND status='PENDING'
             RETURNING request_id, column_name, recommended_action`,
            [userId, reqId]
          );
          await pool.end();
          if (rows.length === 0) { await notify(`⚠️ No pending deprecation found for ID \`${reqId}\``); break; }
          const col = rows[0].column_name;
          await notify(`✅ Deprecation of \`${col}\` approved — DataWiringAgent will remove it.`);
          agentPersonas.post('researchdesk', 'research-feed',
            `🗑️ **Column deprecation approved:** \`${col}\` — DataWiringAgent removing...`).catch(() => {});
          triggerMapRefresh().catch(() => {});
          const orch = getResearchOrch();
          orch._unwireColumn(rows[0]).catch((e) => {
            console.error('[bot] DataWiringAgent (remove) failed:', e.message);
            agentPersonas.post('researchdesk', 'research-feed',
              `❌ DataWiringAgent (remove) failed for \`${col}\`: ${e.message}`).catch(() => {});
          });
        } catch (e) {
          await notify(`❌ approve-deprecation error: ${e.message}`);
        }
        break;
      }

      case 'refresh-map': {
        await notify('🗺️ Refreshing server map...');
        await triggerMapRefresh();
        await notify('✅ Server map refreshed.');
        break;
      }

      default: {
        // Try relay strategist commands first (research, signals, engine-*, strategy-*, etc.)
        const wsCtx = await workspaceManager.getOrCreate(workspaceId);
        const relayCtx = {
          workspace: wsCtx,
          relay:     { reply: notify, userId },
          swarm,
          generateId: () => uuidv4(),
        };
        const relayCmd    = `/${cmd}`;
        const relayArgs   = [null, ...args]; // args[0] unused by relay, args[1..] are actual args
        const wasHandled  = await relay.handleStrategistCommand(relayCmd, relayArgs, relayCtx).catch(() => false);
        if (wasHandled) break;

        // Freeform message → BotJohn PM agent handles it directly.
        const ticker = extractTicker(cmdText);
        const result = await main.runTask({ task: cmdText, ticker, workspaceId, threadId, notify });
        // result.output is the agent's plain-text response from --output-format json
        const text = result?.output || result?.result || result?.message;
        if (text && typeof text === 'string' && text.trim()) {
          await relay.send(message, text.trim());
        } else {
          await postResult(message, ticker || 'task', result, 'general');
        }
      }
    }
  } catch (err) {
    console.error(`[bot] PTC error:`, err);
    await notify(`❌ Error: ${err.message}`);
    await tokenDb.completeTask(threadId, 'failed').catch(() => null);
  } finally {
    stopTyping();
    activeRuns.delete(threadId);
  }

  // Post actual cost on completion
  const taskRecord = await tokenDb.getTaskCost(threadId).catch(() => null);
  if (taskRecord && taskRecord.cost_usd > 0) {
    const actual = Number(taskRecord.cost_usd);
    const est    = taskRecord.est_cost_usd ? Number(taskRecord.est_cost_usd) : null;
    const diff   = est ? ((actual - est) / est * 100).toFixed(0) : null;
    const diffStr = diff ? ` (${diff > 0 ? '+' : ''}${diff}% vs estimate)` : '';
    await notify(`💰 Task cost: **$${actual.toFixed(4)}**${diffStr} | ${taskRecord.num_subagents} subagent(s) | ${Math.round(taskRecord.duration_ms / 1000)}s`);
  }
  await tokenDb.completeTask(threadId).catch(() => null);
}

async function postResult(message, ticker, result, type) {
  if (!result) return;

  // Check for memo file to attach
  const workspace = await workspaceManager.getOrCreate('default').catch(() => null);
  if (workspace) {
    const today = new Date().toISOString().slice(0, 10);
    const memoPath = path.join(workspace, 'results', `${ticker}-${today}-memo.md`);
    if (fs.existsSync(memoPath)) {
      const attachment = new AttachmentBuilder(memoPath, { name: `${ticker}-${today}-memo.md` });
      await message.channel.send({ files: [attachment] }).catch(() => null);
    }
  }
}

async function postFetchResult(message, ticker, result) {
  if (!result) return;
  const workspace = await workspaceManager.getOrCreate('default').catch(() => null);
  if (!workspace) return;

  const dataDir = path.join(workspace, 'work', `${ticker}-data`);
  const chartPath = path.join(dataDir, 'charts', 'price_1y.png');
  const summaryPath = path.join(dataDir, 'FETCH_SUMMARY.json');

  const files = [];
  if (fs.existsSync(chartPath)) files.push(new AttachmentBuilder(chartPath, { name: `${ticker}_price_1y.png` }));

  let summaryText = `📡 **${ticker}** data fetch complete [${result.duration}s]`;
  if (fs.existsSync(summaryPath)) {
    try {
      const summary = JSON.parse(fs.readFileSync(summaryPath, 'utf8'));
      const price = summary.quote?.price ? `$${summary.quote.price}` : '';
      const rsi = summary.rsi ? `RSI: ${summary.rsi}` : '';
      summaryText += `\n${[price, rsi].filter(Boolean).join(' | ')}`;
    } catch {}
  }

  await message.channel.send({ content: summaryText, files }).catch(() =>
    message.channel.send({ content: summaryText })
  );
}

function findRunForChannel(channelId) {
  for (const run of activeRuns.values()) {
    if (run.channel === channelId) return run;
  }
  return null;
}

function startTyping(channel) {
  channel.sendTyping().catch(() => {});
  const t = setInterval(() => channel.sendTyping().catch(() => {}), 8000);
  return () => clearInterval(t);
}

async function generateChart(ticker) {
  const { query: dbQuery } = require('../../database/postgres');
  const res = await dbQuery(
    `SELECT date, close, volume FROM price_data WHERE ticker=$1 ORDER BY date ASC LIMIT 365`,
    [ticker]
  ).catch(() => null);
  if (!res || res.rows.length < 5) return null;

  const dates  = JSON.stringify(res.rows.map(r => r.date.toISOString().slice(0, 10)));
  const closes = JSON.stringify(res.rows.map(r => Number(r.close)));
  const vols   = JSON.stringify(res.rows.map(r => Number(r.volume)));
  const outPath = `/tmp/${ticker}_chart_${Date.now()}.png`;

  const script = `
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np, json

dates  = ${dates}
closes = ${closes}
vols   = ${vols}

xs = range(len(dates))
sma50  = [np.mean(closes[max(0,i-49):i+1]) for i in range(len(closes))]
sma200 = [np.mean(closes[max(0,i-199):i+1]) for i in range(len(closes))]

fig = plt.figure(figsize=(12, 7), facecolor='#0d1117')
gs  = gridspec.GridSpec(2, 1, height_ratios=[3, 1], hspace=0.05)

ax1 = fig.add_subplot(gs[0])
ax1.set_facecolor('#0d1117')
ax1.plot(xs, closes, color='#58a6ff', linewidth=1.5, label='Close')
ax1.plot(xs, sma50,  color='#f0a500', linewidth=1, linestyle='--', label='SMA-50')
ax1.plot(xs, sma200, color='#ff6b6b', linewidth=1, linestyle='--', label='SMA-200')
ax1.fill_between(xs, closes, min(closes), alpha=0.08, color='#58a6ff')
ax1.set_title(f'${ticker} — 1Y Price Action', color='#e6edf3', fontsize=14, pad=10)
ax1.tick_params(colors='#484f58', labelbottom=False)
ax1.spines[:].set_color('#30363d')
ax1.yaxis.label.set_color('#8b949e')
ax1.legend(facecolor='#161b22', edgecolor='#30363d', labelcolor='#e6edf3', fontsize=9)
ax1.grid(True, color='#21262d', linewidth=0.5)

ax2 = fig.add_subplot(gs[1])
ax2.set_facecolor('#0d1117')
colors = ['#3fb950' if i == 0 or closes[i] >= closes[i-1] else '#f85149' for i in range(len(closes))]
ax2.bar(xs, vols, color=colors, alpha=0.7, width=1)
ax2.tick_params(colors='#484f58')
ax2.spines[:].set_color('#30363d')
ax2.grid(True, color='#21262d', linewidth=0.5, axis='y')
tick_step = max(1, len(dates) // 8)
ax2.set_xticks(list(xs)[::tick_step])
ax2.set_xticklabels([dates[i] for i in range(0, len(dates), tick_step)], rotation=30, ha='right', fontsize=8, color='#484f58')

plt.savefig('${outPath}', dpi=150, bbox_inches='tight', facecolor='#0d1117')
plt.close()
print('ok')
`;

  const tmpScript = `/tmp/chart_${Date.now()}.py`;
  fs.writeFileSync(tmpScript, script);
  try {
    const { execSync } = require('child_process');
    execSync(`python3 ${tmpScript}`, { timeout: 30_000 });
    return outPath;
  } catch (err) {
    console.warn('[bot] Chart generation failed:', err.message);
    return null;
  } finally {
    try { fs.unlinkSync(tmpScript); } catch {}
  }
}

function extractTicker(text) {
  const match = text.match(/\b([A-Z]{1,5})\b/);
  return match ? match[1] : null;
}

// ── Credential sync (claudebot) ────────────────────────────────────────────────
function syncClaudeAuth() {
  const src  = '/root/.claude/.credentials.json';
  const dest = '/home/claudebot/.claude/.credentials.json';
  try {
    if (fs.existsSync(src)) {
      fs.mkdirSync('/home/claudebot/.claude', { recursive: true });
      fs.copyFileSync(src, dest);
      fs.chownSync(dest, parseInt(process.env.CLAUDE_UID || '1001'), parseInt(process.env.CLAUDE_GID || '1001'));
    }
  } catch (err) {
    console.warn('[bot] Credential sync failed:', err.message);
  }
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
// Start dashboard server alongside the bot
let dashboardBroadcast = null;
try {
  const server = require('../api/server');
  dashboardBroadcast = server.broadcast;
} catch (err) { console.warn('[bot] Dashboard failed to start:', err.message); }

// ── Pipeline ──────────────────────────────────────────────────────────────────
let collector = null;
try {
  collector = require('../../pipeline/collector');
} catch (err) { console.warn('[bot] Pipeline collector unavailable:', err.message); }

// ── Startup ────────────────────────────────────────────────────────────────────
client.once('ready', async () => {
  console.log(`✅ BotJohn v2 online as ${client.user.tag}`);

  syncClaudeAuth();
  setInterval(syncClaudeAuth, 45 * 60 * 1000);

  // Initialize workspace + tool modules
  try {
    const workspace = await workspaceManager.getOrCreate('default');
    await generateToolModules(workspace);
    console.log(`[bot] Workspace ready: ${workspace}`);
  } catch (err) {
    console.warn('[bot] Workspace init failed:', err.message);
  }

  // Run DB migrations
  await migrate().catch((err) => console.warn('[bot] DB migration skipped:', err.message));

  // Setup Discord server channels + refresh #server-map
  const guild = client.guilds.cache.first();
  _discordClient = client;
  _discordGuild  = guild;
  let channelMap = {};
  if (guild) {
    try {
      channelMap = await setupServer(client, guild);
      _channelMap = channelMap;
    } catch (err) {
      console.warn('[bot] Server setup failed:', err.message);
    }
  }

  // Initialize agent persona webhooks (DataBot, ResearchDesk, etc.)
  try {
    await agentPersonas.initWebhooks(client, channelMap);
    await agentPersonas.setStatus('botjohn', 'online', 'System startup');
    await agentPersonas.setStatus('researchdesk', 'idle', 'Awaiting research task').catch(() => {});
    await agentPersonas.setStatus('tradedesk', 'idle', 'No active trades').catch(() => {});
    // Inject agentPersonas into notifications so trade/research/alert channels work
    notifications.init(client, { agentPersonas });
  } catch (err) {
    console.warn('[bot] Agent persona init failed:', err.message);
  }

  // Research team go-live: set initial status + post announcement to #research-feed
  try {
    const _orch = getResearchOrch();
    const { status: rStatus, text: rText } = await _orch.getStatusText().catch(() => ({ status: 'idle', text: 'Ready — /research submit <url>' }));
    await agentPersonas.setStatus('researchdesk', rStatus, rText).catch(() => {});
    await agentPersonas.post('researchdesk', 'research-feed',
      '🔬 **ResearchJohn online** — queue-driven research ready.\nSubmit a paper with `/research submit <url>`, then run `/research start` to process the queue.'
    ).catch(() => {});
    // 30-second live token monitor — updates ResearchJohn's Discord presence with budget %
    setInterval(async () => {
      try {
        const { status, text } = await getResearchOrch().getStatusText();
        await agentPersonas.setStatus('researchdesk', status, text);
      } catch { /* non-critical */ }
    }, 30_000);
    console.log('[bot] Research team online, 30s monitor started');
  } catch (err) {
    console.warn('[bot] Research team init failed:', err.message);
  }

  // Start background data pipeline — broadcasts to #pipeline-feed
  if (collector) {
    const pipelineNotify = (data) => {
      // SSE broadcast for dashboard
      if (dashboardBroadcast) dashboardBroadcast(data);
      // DataBot posts to #pipeline-feed for phase completions and errors only (not every tick)
      if (data.message && (
        data.message.includes('✅') || data.message.includes('⚠️') || data.message.includes('📅') || data.message.includes('🚀')
      )) {
        agentPersonas.post('databot', 'pipeline-feed', `\`${new Date().toISOString().slice(11, 19)}\` ${data.message}`).catch(() => {});
      }
    };

    // DataBot posts progress to #data-alerts every 10 tickers
    const alertPost = (msg) => {
      agentPersonas.post('databot', 'data-alerts', msg).catch(() => {});
      // Broadcast pipeline event to dashboard after each phase completion
      if (msg.includes('✅') && dashboardBroadcast) {
        dashboardBroadcast({ type: 'pipeline', message: msg });
      }
    };

    // Expose alertPost for data-task-executor (used by /data command handler)
    _dataAlertsPost = alertPost;

    // Discord presence — routes to BotJohn's status
    const setPresence = (text) => {
      agentPersonas.setStatus('botjohn', 'busy', text.slice(0, 128)).catch(() => {});
    };

    // Completion callback — fires once when all tickers are fully covered
    const onComplete = async ({ covered, total, fromDate, toDate, stats }) => {
      const totalPrices = stats.prices.toLocaleString();
      const totalOptions = stats.options.toLocaleString();
      const msg = [
        `🎉 **Initial data collection complete!**`,
        `All **${covered}/${total}** tickers fully covered`,
        `Price history: \`${fromDate}\` → \`${toDate}\``,
        `Rows collected — prices: **${totalPrices}** | options: **${totalOptions}**`,
        `Pipeline is now in steady-state — only new rows fetched each cycle`,
        ``,
        `When done: \`!john /pipeline pause\` to halt | \`!john /shutdown confirm\` to stop the bot`,
      ].join('\n');
      await agentPersonas.post('databot', 'data-alerts', msg).catch(() => {});
      await agentPersonas.setStatus('databot', 'idle', 'Steady-state — awaiting next cycle');
    };

    collector.setBroadcast(pipelineNotify);
    collector.setDiscordHooks({ presence: setPresence, alertPost, onComplete });
    await agentPersonas.setStatus('databot', 'online', 'Pipeline running');
    collector.start().catch((err) => console.warn('[bot] Pipeline start error:', err.message));
    console.log('[bot] Data pipeline started');
  }

  // Start background cron jobs — token budget reset, strategist activation
  try {
    const cronSchedule = require('../../engine/cron-schedule');
    const notifyBotjohn = (msg) => agentPersonas.post('botjohn', 'botjohn-log', msg).catch(() => {});
    cronSchedule.start(swarm, uuidv4, notifyBotjohn);
    console.log('[bot] Cron schedule started (token reset, strategist checks)');
  } catch (err) {
    console.warn('[bot] Cron schedule failed to start:', err.message);
  }

  client.user.setActivity('!john /help | /pipeline status | /fetch TICKER', { type: 4 });
  await agentPersonas.setStatus('botjohn', 'online').catch(() => null);
  await notifications.postStartup(client).catch(() => null);

  // Log restart to fund journal so BotJohn knows it restarted
  memoryWriter.journalEntry('OBSERVATION', `BotJohn v2 online — ${new Date().toUTCString()}`);
  const activeTasks = memoryWriter.getActiveTasks();
  if (activeTasks.length > 0) {
    console.log(`[memory] ${activeTasks.length} active task(s) restored from memory`);
    memoryWriter.journalEntry('OBSERVATION', `Resumed with ${activeTasks.length} open task(s): ${activeTasks.map(t => t.split('|')[1]?.trim()).join(', ')}`);
  }
});

// ── Position recommendation buttons ───────────────────────────────────────────
client.on('interactionCreate', async (interaction) => {
  if (!interaction.isButton()) return;
  if (!interaction.customId.startsWith('rec:')) return;

  const parts  = interaction.customId.split(':');
  const action = parts[1];
  const recId  = parts[2];
  if (!['approve', 'reject'].includes(action) || !recId) return;

  await interaction.deferUpdate().catch(() => null);
  const userId = interaction.user.id;

  if (action === 'reject') {
    const { Pool } = require('pg');
    const p = new Pool({ connectionString: process.env.POSTGRES_URI });
    try {
      await p.query(
        `UPDATE position_recommendations
         SET status='rejected', resolved_at=NOW(), resolved_by=$1
         WHERE id=$2 AND status='pending'`,
        [userId, recId]
      );
    } finally {
      await p.end();
    }
    await interaction.editReply({
      content:    interaction.message.content + '\n\n❌ **REJECTED** — no action taken.',
      components: [],
    }).catch(() => null);
    return;
  }

  // approve → execute via Python script
  const { execFile } = require('child_process');
  const pyScript = path.join(OPENCLAW_DIR, 'src', 'execution', 'execute_recommendation.py');

  const result = await new Promise(resolve => {
    execFile('python3', [pyScript, recId], {
      cwd: OPENCLAW_DIR,
      env: { ...process.env, PYTHONPATH: OPENCLAW_DIR },
      timeout: 30_000,
    }, (err, stdout) => {
      try {
        const lastLine = (stdout || '').trim().split('\n').pop();
        resolve(JSON.parse(lastLine));
      } catch {
        resolve({ ok: false, error: err?.message || 'parse error' });
      }
    });
  });

  if (result.ok) {
    const detail = `order_id=${result.order_id || 'n/a'}`;
    await interaction.editReply({
      content:    interaction.message.content + `\n\n✅ **APPROVED & EXECUTED** — ${result.action} on ${result.ticker} | ${detail}`,
      components: [],
    }).catch(() => null);
    agentPersonas.post('tradedesk', 'trade-reports',
      `✅ **Rec Executed** — ${result.action} on **${result.ticker}** | ${detail} | by <@${userId}>`
    ).catch(() => null);
  } else {
    await interaction.editReply({
      content:    interaction.message.content + `\n\n⚠️ **EXECUTION FAILED** — ${result.error || result.detail}`,
      components: [],
    }).catch(() => null);
    agentPersonas.post('tradedesk', 'trade-reports',
      `⚠️ **Rec execution failed** — rec_id \`${recId}\` | ${result.error || result.detail}`
    ).catch(() => null);
  }
});

client.on('error', (err) => console.error('[bot] Discord error:', err));
process.on('unhandledRejection', (err) => console.error('[bot] Unhandled rejection:', err));

client.login(BOT_TOKEN);
