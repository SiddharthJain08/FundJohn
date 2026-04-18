'use strict';

/**
 * agent-personas.js
 *
 * Webhook-based agent identities for OpenClaw.
 * Each agent (DataBot, ResearchDesk, etc.) posts to Discord with its own
 * username using Discord webhooks — no separate bot tokens needed.
 *
 * Usage:
 *   await agentPersonas.initWebhooks(client, channelMap);   // call once at startup
 *   agentPersonas.post('databot', 'data-alerts', 'message'); // post as DataBot
 *   agentPersonas.setStatus('databot', 'busy', 'Collecting AAPL prices');
 *   const all = await agentPersonas.getAllStatuses();
 */

const { WebhookClient } = require('discord.js');
const { query } = require('../../database/postgres');
const { getClient } = require('../../database/redis');

// ── Agent definitions ─────────────────────────────────────────────────────────

const AGENTS = {
  botjohn: {
    displayName: '🦞 BotJohn',
    emoji:       '🦞',
    model:       'claude-sonnet-4-6',
    description: 'Portfolio Manager & Orchestrator',
    channelKeys: ['general', 'botjohn-log'],
  },
  researchdesk: {
    displayName: '🔬 ResearchDesk',
    emoji:       '🔬',
    model:       'claude-sonnet-4-6',
    description: 'Equity Research & Diligence',
    channelKeys: ['research-feed', 'strategy-memos'],
  },
  tradedesk: {
    displayName: '📈 TradeDesk',
    emoji:       '📈',
    model:       'claude-sonnet-4-6',
    description: 'Trade Execution & Risk',
    channelKeys: ['trade-signals', 'trade-reports', 'position-recommendations'],
  },
};

// In-memory state after initWebhooks() runs
const _webhooks  = {};   // { agentId: { channelKey: WebhookClient } }
const _channels  = {};   // { channelKey: TextChannel }

// ── Init ──────────────────────────────────────────────────────────────────────

/**
 * Create or fetch Discord webhooks for each agent/channel pair.
 * Safe to call on every startup — reuses existing webhooks by name.
 *
 * @param {Client}  client      — discord.js Client (logged in)
 * @param {Object}  channelMap  — { channelKey: channelId }
 */
async function initWebhooks(client, channelMap) {
  for (const [agentId, def] of Object.entries(AGENTS)) {
    _webhooks[agentId] = {};

    for (const chKey of def.channelKeys) {
      const chId = channelMap[chKey];
      if (!chId) continue;

      const ch = client.channels.cache.get(chId);
      if (!ch || typeof ch.fetchWebhooks !== 'function') continue;
      _channels[chKey] = ch;

      try {
        const existing = await ch.fetchWebhooks();
        let wh = existing.find(w => w.name === def.displayName && w.token);
        if (!wh) {
          wh = await ch.createWebhook({ name: def.displayName, reason: 'OpenClaw agent persona' });
        }
        _webhooks[agentId][chKey] = new WebhookClient({ id: wh.id, token: wh.token });
        console.log(`[personas] ${def.displayName} → #${chKey} ✓`);

        // Persist webhook URL in DB
        await query(
          `UPDATE agent_registry
             SET webhook_urls = COALESCE(webhook_urls, '{}'::jsonb) || jsonb_build_object($1::text, $2::text)
           WHERE id = $3`,
          [chKey, `https://discord.com/api/webhooks/${wh.id}/${wh.token}`, agentId]
        ).catch((e) => console.warn(`[personas] webhook_urls DB update failed (${agentId}/#${chKey}): ${e.message}`));
      } catch (err) {
        console.warn(`[personas] Webhook setup failed — ${def.displayName}/#${chKey}: ${err.message}`);
      }
    }
  }
}

// ── Post ──────────────────────────────────────────────────────────────────────

/**
 * Post a message as an agent.
 * Falls back to direct channel send if webhook unavailable.
 *
 * @param {string} agentId    — 'databot' | 'researchdesk' | etc.
 * @param {string} channelKey — 'data-alerts' | 'pipeline-feed' | etc.
 * @param {string} message
 */
async function post(agentId, channelKey, message) {
  const text = String(message).slice(0, 2000);

  const wh = _webhooks[agentId]?.[channelKey];
  if (wh) {
    try {
      await wh.send({ content: text });
      return;
    } catch (err) {
      console.warn(`[personas] Webhook send failed (${agentId}/#${channelKey}): ${err.message}`);
    }
  }

  // Fallback: direct channel send (no persona name, but message still gets through)
  const ch = _channels[channelKey];
  if (ch) {
    await ch.send({ content: text }).catch(() => {});
  }
}

/**
 * Convenience: return a poster function for a specific agent+channel.
 * Matches the (msg) => void signature used by collector hooks.
 */
function poster(agentId, channelKey) {
  return (msg) => post(agentId, channelKey, msg).catch(() => {});
}

// ── Status ────────────────────────────────────────────────────────────────────

/**
 * Update an agent's status in DB, Redis, and push to presence-manager
 * so the live Discord member presence (online/idle/busy dot) updates.
 *
 * @param {string} agentId
 * @param {'online'|'busy'|'idle'|'offline'} status
 * @param {string|null} currentTask
 */
async function setStatus(agentId, status, currentTask = null) {
  await query(
    `UPDATE agent_registry SET status=$1, current_task=$2, last_seen_at=NOW() WHERE id=$3`,
    [status, currentTask, agentId]
  ).catch(() => {});

  const r = getClient();

  // Cache in Redis for fast reads (5-min TTL)
  await r.setex(
    `agent_status:${agentId}`,
    300,
    JSON.stringify({ status, currentTask, updatedAt: new Date().toISOString() })
  ).catch(() => {});

  // Publish to presence-manager so the Discord member dot updates
  const def = AGENTS[agentId];
  const activityEmoji = { online: '', busy: '⚙️ ', idle: '💤 ', offline: '' }[status] || '';
  const activity = currentTask
    ? `${activityEmoji}${currentTask}`.slice(0, 128)
    : (def ? def.description : agentId);

  await r.publish('agent:presence', JSON.stringify({ agentId, status, activity })).catch(() => {});
}

/**
 * Read all agent rows for the /agents status board.
 */
async function getAllStatuses() {
  const res = await query(
    `SELECT id, display_name, emoji, model, description, channel_keys,
            status, current_task, last_seen_at
     FROM agent_registry ORDER BY
       CASE id
         WHEN 'botjohn'      THEN 1
         WHEN 'databot'      THEN 2
         WHEN 'researchdesk' THEN 3
         WHEN 'tradedesk'    THEN 4
         ELSE 9
       END`
  ).catch(() => ({ rows: [] }));
  return res.rows;
}

module.exports = { AGENTS, initWebhooks, post, poster, setStatus, getAllStatuses };
