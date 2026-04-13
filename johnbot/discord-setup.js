'use strict';

/**
 * discord-setup.js — Creates and maintains the OpenClaw channel structure.
 *
 * Idempotent — safe to call on every startup. Skips existing channels.
 * Requires ManageChannels + ManageRoles (or Administrator) on the bot role.
 *
 * Channel layout:
 *   OPENCLAW — INFO     : #server-map, #botjohn-log
 *   RESEARCH DESK       : #research-feed, #diligence-memos, #scenario-lab
 *   TRADING DESK        : #trade-signals, #position-sizing, #entry-timing, #risk-desk, #trade-reports
 *   ALERTS              : #alerts
 *   COMMAND CENTER      : #general (existing), #agent-chat
 */

const { ChannelType, PermissionsBitField } = require('discord.js');
const fs   = require('fs');
const path = require('path');

const WORKDIR  = process.env.OPENCLAW_DIR || '/root/openclaw';
const MAP_FILE = path.join(WORKDIR, 'output', 'channel-map.json');

// ── Channel structure definition ──────────────────────────────────────────────
const STRUCTURE = [
  {
    category: '📌 OPENCLAW — INFO',
    channels: [
      { name: 'server-map',  key: 'server-map',  operatorWrite: false,
        topic: 'Channel directory and command reference. Auto-updated by BotJohn on startup.' },
      { name: 'botjohn-log', key: 'botjohn-log', operatorWrite: false,
        topic: 'BotJohn system events, startup logs, auth syncs, and pipeline errors.' },
    ],
  },
  {
    category: '🔬 RESEARCH DESK',
    channels: [
      { name: 'research-feed',   key: 'research-feed',   operatorWrite: false,
        topic: 'Live research agent outputs: 🐂 Bull 🐻 Bear 📋 Mgmt 📄 Filing 📊 Revenue — posted as each agent completes' },
      { name: 'diligence-memos', key: 'diligence-memos', operatorWrite: false,
        topic: 'Completed 12-section diligence memos. PROCEED/REVIEW/KILL verdicts.' },
      { name: 'scenario-lab',    key: 'scenario-lab',    operatorWrite: false,
        topic: 'Base / bull / bear scenario comparisons from !john /scenario' },
    ],
  },
  {
    category: '📈 TRADING DESK',
    channels: [
      { name: 'trade-signals',   key: 'trade-signals',   operatorWrite: false,
        topic: '📡 Screener signals. R/R ≥ 2.0 threshold. Live during /trade-scan.' },
      { name: 'position-sizing', key: 'position-sizing', operatorWrite: false,
        topic: '⚖️ Sizer outputs — position %, dollar amount, share count, vol adjustment.' },
      { name: 'entry-timing',    key: 'entry-timing',    operatorWrite: false,
        topic: '⏱️ Timer outputs — entry windows, order type, catalyst calendar.' },
      { name: 'risk-desk',       key: 'risk-desk',       operatorWrite: false,
        topic: '🛡️ Risk assessments, limit checks, stress scenarios, vetoes.' },
      { name: 'trade-reports',   key: 'trade-reports',   operatorWrite: false,
        topic: '📊 Final approved trade reports. [TRADE ALERT] and [EXIT ALERT] signals.' },
    ],
  },
  {
    category: '🚨 ALERTS',
    channels: [
      { name: 'alerts', key: 'alerts', operatorWrite: false,
        topic: 'Kill signals, trade alerts, exit alerts, risk vetoes. High priority only.' },
    ],
  },
  {
    category: '💬 COMMAND CENTER',
    channels: [
      { name: 'general',    key: 'general',    operatorWrite: true,
        topic: 'Main command input. !john /diligence AAPL | !john /trade-scan | !john @bull AAPL' },
      { name: 'agent-chat', key: 'agent-chat', operatorWrite: true,
        topic: 'Direct agent communication. @agentname <query> — address any research or trading agent.' },
    ],
  },
];

// ── Server map content ────────────────────────────────────────────────────────
function buildServerMap(channelMap) {
  return `🦞 **OpenClaw — Channel Directory**
*Updated: ${new Date().toISOString().slice(0, 10)}*

───────────────────────────────────────
**🔬 RESEARCH DESK**
<#${channelMap['research-feed']}> — Live agent outputs during diligence runs
<#${channelMap['diligence-memos']}> — Completed diligence memos (file attachments)
<#${channelMap['scenario-lab']}> — Scenario lab outputs

**📈 TRADING DESK**
<#${channelMap['trade-signals']}> — 📡 Screener signals (R/R ≥ 2.0)
<#${channelMap['position-sizing']}> — ⚖️ Position sizing outputs
<#${channelMap['entry-timing']}> — ⏱️ Entry timing recommendations
<#${channelMap['risk-desk']}> — 🛡️ Risk assessments and vetoes
<#${channelMap['trade-reports']}> — 📊 Final trade reports

**🚨 ALERTS**
<#${channelMap['alerts']}> — Kill signals, trade alerts, exit alerts

**💬 COMMAND CENTER**
<#${channelMap['general']}> — Main command input
<#${channelMap['agent-chat']}> — Direct agent communication

───────────────────────────────────────
**Research Commands**
\`!john /diligence AAPL\` → full 5-agent diligence run
\`!john /comps AAPL\` → comparable company analysis
\`!john /diligence-checklist AAPL\` → 6-item checklist
\`!john /scenario AAPL\` → base/bull/bear scenario lab
\`!john /screen sector=tech\` → quantitative screen

**Trading Desk Commands**
\`!john /trade-scan\` → full pipeline: scan→size→time→risk→report
\`!john /trade-report\` → portfolio P&L summary
\`!john /signal {ID} BUY|PASS\` → act on a trade signal
\`!john /risk\` → standalone portfolio risk check
\`!john /exit AAPL\` → exit analysis

**Direct Agent Commands** *(any channel or in #agent-chat)*
\`!john @bull AAPL\` → address Bull agent directly
\`!john @bear AAPL\` → address Bear agent directly
\`!john @mgmt AAPL\` → Management Credibility agent
\`!john @filing AAPL\` → Filing Diff agent
\`!john @revenue AAPL\` → Revenue Quality agent
\`!john @screener\` → address Screener agent
\`!john @sizer\` → address Sizer agent
\`!john @timer\` → address Timer agent
\`!john @risk\` → address Risk agent
\`!john @reporter\` → address Reporter agent

*In #agent-chat, you can omit \`!john\` — just type \`@bull AAPL thesis?\`*`;
}

// ── Main setup function ───────────────────────────────────────────────────────

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

  for (const section of STRUCTURE) {
    // Find or create category
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
        // Build permission overwrites
        const permissionOverwrites = [
          {
            id:   everyoneId,
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
          await delay(700); // rate limit buffer between channel creates
        } catch (err) {
          console.warn(`[setup] Could not create #${ch.name}: ${err.message}`);
          // Try to find it anyway (might exist under a different case)
          chan = guild.channels.cache.find(c => c.name === ch.name);
          if (!chan) continue;
        }
      } else {
        existing.push(ch.name);
        // Move to correct category if not already there
        if (chan.parentId !== category.id && chan.name !== 'general') {
          try {
            await chan.setParent(category.id, { lockPermissions: false });
            await delay(300);
          } catch { /* non-fatal */ }
        }
      }

      channelMap[ch.key] = chan.id;
    }
  }

  // Persist the channel map
  fs.mkdirSync(path.dirname(MAP_FILE), { recursive: true });
  fs.writeFileSync(MAP_FILE, JSON.stringify(channelMap, null, 2));

  // Post server map to #server-map
  const serverMapCh = guild.channels.cache.get(channelMap['server-map']);
  if (serverMapCh) {
    try {
      const recent = await serverMapCh.messages.fetch({ limit: 10 });
      const botMessages = recent.filter(m => m.author.id === client.user.id);
      if (botMessages.size > 0) await serverMapCh.bulkDelete(botMessages).catch(() => {});
      await serverMapCh.send({ content: buildServerMap(channelMap) });
    } catch { /* non-fatal */ }
  }

  // Post setup summary to #botjohn-log
  const logCh = guild.channels.cache.get(channelMap['botjohn-log']);
  if (logCh) {
    const summary = created.length > 0
      ? `✅ Server setup complete — created: ${created.map(n => `#${n}`).join(', ')}`
      : `✅ Server setup complete — all channels already exist (${existing.length} verified)`;
    try { await logCh.send({ content: `\`${new Date().toISOString()}\` ${summary}` }); } catch {}
  }

  console.log(`[setup] Channels: ${created.length} created, ${existing.length} already existed`);
  return channelMap;
}

function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

module.exports = { setupServer, buildServerMap };
