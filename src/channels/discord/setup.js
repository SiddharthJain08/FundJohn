'use strict';

/**
 * Discord server setup — OpenClaw v2 (PTC Architecture)
 *
 * Idempotent: safe to call on every startup. Creates missing channels,
 * moves existing ones to the right categories, and refreshes #server-map.
 *
 * Channel layout:
 *   📌 OPENCLAW — INFO   : #server-map, #botjohn-log
 *   📡 DATA PIPELINE     : #pipeline-feed, #data-alerts
 *   🔬 RESEARCH DESK     : #research-feed, #strategy-memos
 *   📈 TRADING DESK      : #trade-signals, #trade-reports
 *   💬 COMMAND CENTER    : #general (existing), #agent-chat
 */

const { ChannelType, PermissionsBitField } = require('discord.js');
const fs   = require('fs');
const path = require('path');

const MAP_FILE = path.join(process.env.OPENCLAW_DIR || '/root/openclaw', 'output', 'channel-map.json');

// Channels from old v1 build or removed in current version — deleted on next startup
const DEPRECATED_CHANNELS = [
  'scenario-lab',
  'position-sizing',
  'entry-timing',
  'risk-desk',
  'alerts',          // removed — other agents already post risk/block notices inline
  'diligence-memos', // renamed to strategy-memos
];

// ── Channel structure ─────────────────────────────────────────────────────────

const STRUCTURE = [
  {
    category: '📌 OPENCLAW — INFO',
    channels: [
      {
        name: 'server-map', key: 'server-map', operatorWrite: false,
        topic: 'Channel directory and command reference. Auto-updated by BotJohn on startup.',
      },
      {
        name: 'botjohn-log', key: 'botjohn-log', operatorWrite: false,
        topic: 'BotJohn system events, startup messages, migration logs, and pipeline errors.',
      },
    ],
  },
  {
    category: '📡 DATA PIPELINE',
    channels: [
      {
        name: 'pipeline-feed', key: 'pipeline-feed', operatorWrite: false,
        topic: 'Live background collection progress — S&P 500 prices, options Greeks, technicals, fundamentals.',
      },
      {
        name: 'data-alerts', key: 'data-alerts', operatorWrite: false,
        topic: 'API errors, rate limit warnings, missing data gaps, and coverage drops.',
      },
    ],
  },
  {
    category: '🔬 RESEARCH DESK',
    channels: [
      {
        name: 'research-feed', key: 'research-feed', operatorWrite: false,
        topic: 'ResearchJohn live research loop: phase updates, papers found, strategies coded. Status dot = budget % consumed. Use /research start to begin.',
      },
      {
        name: 'strategy-memos', key: 'strategy-memos', operatorWrite: false,
        topic: 'Daily strategy execution memos + PROCEED / REVIEW / KILL verdicts from diligence runs.',
      },
    ],
  },
  {
    category: '📈 TRADING DESK',
    channels: [
      {
        name: 'trade-signals', key: 'trade-signals', operatorWrite: false,
        topic: 'Trade pipeline outputs — sizing, risk verdict, analyst sign-off. APPROVED / BLOCKED / PENDING.',
      },
      {
        name: 'trade-reports', key: 'trade-reports', operatorWrite: false,
        topic: 'Final trade reports. Use !john /approve or /reject to act on pending trades.',
      },
      {
        name: 'position-recommendations', key: 'position-recommendations', operatorWrite: false,
        topic: 'TradeDesk position recommendations. Click Approve to execute immediately on Alpaca paper. Click Reject to dismiss.',
      },
    ],
  },
  {
    category: '💬 COMMAND CENTER',
    channels: [
      {
        name: 'general', key: 'general', operatorWrite: true,
        topic: 'Main command input. Type !john /help for full command list.',
      },
      {
        name: 'agent-chat', key: 'agent-chat', operatorWrite: true,
        topic: 'Direct freeform tasks. !john <anything> — PTC mode handles it.',
      },
    ],
  },
];

// ── Server map message ────────────────────────────────────────────────────────

function buildServerMap(channelMap) {
  const ch = (key) => channelMap[key] ? `<#${channelMap[key]}>` : `#${key}`;
  const today = new Date().toISOString().slice(0, 10);

  // Returned as array — three messages to stay under Discord's 2000-char limit
  return [
    `🦞 **OpenClaw v2 — Server Map**
*Updated: ${today} | Runs daily at 6:00 AM ET | Sleeps when idle*

**📡 DATA PIPELINE**
${ch('pipeline-feed')} — Phase completions, cycle start/end, errors
${ch('data-alerts')} — Progress every 10 tickers (speed, ETA, rows written), sleep/wake notices

**🔬 RESEARCH DESK**
${ch('research-feed')} — ResearchJohn live research loop: phase updates, papers found, strategies coded. Status dot shows budget % consumed.
${ch('strategy-memos')} — Strategy execution memos + PROCEED / REVIEW / KILL verdicts

**📈 TRADING DESK**
${ch('trade-signals')} — Trade pipeline outputs and risk verdicts
${ch('trade-reports')} — Final trade reports. Use /approve or /reject to act.
${ch('position-recommendations')} — Position recommendations with Approve/Reject buttons

**💬 COMMAND CENTER**
${ch('general')} — All \`!john /commands\` — type \`!john /help\` for full list
${ch('agent-chat')} — Freeform chat with BotJohn as PM agent (\`!john <anything>\`)`,

    `**⚡ Flash** *(instant — no subagent)*
\`!john /ping\` · \`!john /status\` · \`!john /market\` · \`!john /rate\` · \`!john /budget\` · \`!john /help\`
\`!john /quote AAPL\` · \`!john /profile AAPL\` · \`!john /calendar AAPL\` · \`!john /verdict AAPL\`
\`!john /dashboard\` · \`!john /coverage\` · \`!john /prices AAPL [days]\` · \`!john /chart AAPL\`
\`!john /greeks AAPL\` · \`!john /options AAPL\`
\`!john /spend [days]\` · \`!john /cost {id}\` · \`!john /estimate {type} [AAPL]\`
\`!john /cycles [n]\` · \`!john /config [key] [value]\`

**📈 Signals & Engine**
\`!john /signals [date]\` · \`!john /engine-status\` · \`!john /engine-run\` · \`!john /strategy-review\`

**🔬 Research** *(queue-driven pipeline)*
\`!john /research submit <url>\` · \`!john /research start\` · \`!john /research pause\`
\`!john /research status\` · \`!john /research queue\` · \`!john /research run-one\`
\`!john /research discover [days]\` · \`!john /risk-scan\`

**📐 Trading**
\`!john /trade AAPL\` · \`!john /approve {id}\` · \`!john /reject {id}\``,

    `**🔧 Strategy Management**
\`!john /build-strategy {desc}\` *(aliases: /deploy-strategy, /new-strategy, /create-strategy)*
\`!john /strategy-report {id}\` · \`!john /strategy-versions {id}\`
\`!john /approve-strategy {id}\` · \`!john /pause-strategy {id}\`
\`!john /adjust-strategy {id} PARAM=val reason: why\` — versioned param update
\`!john /approve-deprecation {id}\`

**📡 Pipeline & Data**
\`!john /fill [AAPL NVDA …]\` · \`!john /fill --force\` · \`!john /fetch AAPL\`
\`!john /data {description}\` · \`!john /data-status\`
\`!john /approve-data {id}\` · \`!john /veto-data {id}\`
\`!john /pipeline status\` · \`!john /pipeline pause\` · \`!john /pipeline resume\`
\`!john /pipeline cycles [n]\`

**🤖 Agents**
\`!john /agents\` — status board: BotJohn · DataBot · ResearchDesk · TradeDesk

**🔌 System**
\`!john /shutdown confirm\` · \`!john /shutdown server confirm\`
\`!john /git sync\` — commit + push all changes to SiddharthJain08/FundJohn`,
  ];
}

// ── Main setup ────────────────────────────────────────────────────────────────

async function setupServer(client, guild) {
  let channelMap = {};
  try { channelMap = JSON.parse(fs.readFileSync(MAP_FILE, 'utf8')); } catch {}

  await guild.channels.fetch();
  const existingByName = new Map();
  guild.channels.cache.forEach(ch => {
    if (ch) existingByName.set(ch.name.toLowerCase(), ch);
  });

  const botId      = client.user.id;
  const everyoneId = guild.roles.everyone.id;

  const BOT_PERMS = [
    PermissionsBitField.Flags.ViewChannel,
    PermissionsBitField.Flags.SendMessages,
    PermissionsBitField.Flags.ManageMessages,
    PermissionsBitField.Flags.AttachFiles,
    PermissionsBitField.Flags.EmbedLinks,
    PermissionsBitField.Flags.ReadMessageHistory,
  ];

  const created = [];
  const existing = [];
  const deleted = [];

  // Delete deprecated v1 channels
  for (const name of DEPRECATED_CHANNELS) {
    const chan = existingByName.get(name.toLowerCase());
    if (chan) {
      try {
        await chan.delete('Removed — deprecated v1 channel, not used in v2 architecture');
        deleted.push(name);
        await delay(500);
      } catch (err) {
        console.warn(`[setup] Could not delete #${name}: ${err.message}`);
      }
    }
    // Remove from channelMap if present
    delete channelMap[name];
  }

  for (const section of STRUCTURE) {
    let category = existingByName.get(section.category.toLowerCase());
    if (!category) {
      try {
        category = await guild.channels.create({ name: section.category, type: ChannelType.GuildCategory });
        await delay(600);
      } catch (err) {
        console.warn(`[setup] Could not create category "${section.category}": ${err.message}`);
        continue;
      }
    }

    for (const ch of section.channels) {
      let chan = existingByName.get(ch.name.toLowerCase());

      if (!chan) {
        const permissionOverwrites = [
          {
            id:    everyoneId,
            allow: [PermissionsBitField.Flags.ViewChannel, PermissionsBitField.Flags.ReadMessageHistory],
            deny:  ch.operatorWrite ? [] : [PermissionsBitField.Flags.SendMessages],
          },
          { id: botId, allow: BOT_PERMS },
        ];
        try {
          chan = await guild.channels.create({
            name:   ch.name,
            type:   ChannelType.GuildText,
            parent: category.id,
            topic:  ch.topic,
            permissionOverwrites,
          });
          created.push(ch.name);
          await delay(700);
        } catch (err) {
          console.warn(`[setup] Could not create #${ch.name}: ${err.message}`);
          chan = guild.channels.cache.find(c => c.name === ch.name);
          if (!chan) continue;
        }
      } else {
        existing.push(ch.name);
        // Update topic and move to correct category
        try {
          if (ch.topic && chan.topic !== ch.topic) {
            await chan.setTopic(ch.topic);
            await delay(300);
          }
          if (category && chan.parentId !== category.id && ch.name !== 'general') {
            await chan.setParent(category.id, { lockPermissions: false });
            await delay(300);
          }
        } catch { /* non-fatal */ }
      }

      channelMap[ch.key] = chan.id;
    }
  }

  fs.mkdirSync(path.dirname(MAP_FILE), { recursive: true });
  fs.writeFileSync(MAP_FILE, JSON.stringify(channelMap, null, 2));

  // Refresh #server-map
  await refreshServerMap(client, guild, channelMap);

  // Log to #botjohn-log
  const logCh = guild.channels.cache.get(channelMap['botjohn-log']);
  if (logCh) {
    const parts = [];
    if (deleted.length > 0) parts.push(`🗑️ deleted: ${deleted.map(n => `#${n}`).join(', ')}`);
    if (created.length > 0) parts.push(`✅ created: ${created.map(n => `#${n}`).join(', ')}`);
    if (!parts.length) parts.push(`✅ ${existing.length} channels verified`);
    try { await logCh.send({ content: `\`${new Date().toISOString()}\` Server setup — ${parts.join(' | ')}` }); } catch {}
  }

  console.log(`[setup] Channels: ${deleted.length} deleted, ${created.length} created, ${existing.length} verified`);
  return channelMap;
}

async function refreshServerMap(client, guild, channelMap) {
  if (!channelMap) {
    try { channelMap = JSON.parse(fs.readFileSync(MAP_FILE, 'utf8')); } catch { return; }
  }
  const serverMapCh = guild
    ? guild.channels.cache.get(channelMap['server-map'])
    : client?.channels?.cache?.get(channelMap['server-map']);
  if (!serverMapCh) return;

  try {
    const recent = await serverMapCh.messages.fetch({ limit: 10 });
    const botMsgs = recent.filter(m => m.author.id === client.user.id);
    if (botMsgs.size > 0) await serverMapCh.bulkDelete(botMsgs).catch(() => {});
    const parts = buildServerMap(channelMap);
    for (const part of parts) {
      await serverMapCh.send({ content: part });
    }
  } catch (err) {
    console.warn('[setup] Could not refresh #server-map:', err.message);
  }
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

module.exports = { setupServer, refreshServerMap, buildServerMap };
