'use strict';

/**
 * presence-manager.js
 *
 * Runs as a separate process alongside johnbot.service.
 * Logs each agent in as its own Discord bot client so they appear
 * as real online members in the server member list.
 *
 * Status updates arrive via Redis pub/sub on channel: agent:presence
 * Message format: JSON { agentId, status, activity }
 *   status:   'online' | 'idle' | 'dnd' | 'invisible'
 *   activity: string (shown as custom status)
 *
 * BotJohn publishes to this channel whenever setStatus() is called.
 */

require('dotenv').config({ path: require('path').join(__dirname, '../../../.env') });

const { Client, GatewayIntentBits, ActivityType } = require('discord.js');
const Redis = require('ioredis');

// ── Agent definitions ─────────────────────────────────────────────────────────

const AGENTS = [
  // ResearchDesk presence retired 2026-05-02 (replaced by mastermind webhook persona)
  {
    id:          'tradedesk',
    token:       process.env.TRADEDESK_TOKEN,
    displayName: 'TradeDesk',
    defaultStatus:   'idle',
    defaultActivity: '📈 No active trades',
  },
];

// Map agentId → Discord.js Client
const clients = new Map();

// ── Status helpers ─────────────────────────────────────────────────────────────

const STATUS_MAP = {
  online:  'online',
  busy:    'dnd',       // red dot = do not disturb
  idle:    'idle',      // yellow moon
  offline: 'invisible', // appears offline
};

function applyPresence(client, status, activityText) {
  try {
    const discordStatus = STATUS_MAP[status] || 'online';
    client.user.setPresence({
      status: discordStatus,
      activities: [{
        name: String(activityText || '').slice(0, 128),
        type: ActivityType.Custom,
      }],
    });
  } catch (err) {
    // Non-fatal — presence update may fail briefly after login
  }
}

// ── Start each agent client ────────────────────────────────────────────────────

async function startAgent(def) {
  if (!def.token) {
    console.warn(`[presence] No token for ${def.displayName} — skipping`);
    return null;
  }

  const client = new Client({
    intents: [GatewayIntentBits.Guilds],
  });

  client.once('clientReady', () => {
    console.log(`[presence] ${def.displayName} online as ${client.user.tag}`);
    applyPresence(client, def.defaultStatus, def.defaultActivity);
  });

  client.on('error', (err) => {
    console.warn(`[presence] ${def.displayName} error: ${err.message}`);
  });

  await client.login(def.token);
  clients.set(def.id, client);
  return client;
}

// ── Redis subscriber for live status updates ───────────────────────────────────

function startSubscriber() {
  const sub = new Redis(process.env.REDIS_URL || 'redis://localhost:6379', {
    maxRetriesPerRequest: 3,
    lazyConnect: true,
  });

  sub.on('error', (err) => console.warn('[presence] Redis error:', err.message));

  sub.subscribe('agent:presence', (err) => {
    if (err) console.warn('[presence] Redis subscribe error:', err.message);
    else console.log('[presence] Subscribed to agent:presence channel');
  });

  sub.on('message', (channel, raw) => {
    if (channel !== 'agent:presence') return;
    try {
      const { agentId, status, activity } = JSON.parse(raw);
      const client = clients.get(agentId);
      if (!client || !client.user) return;
      applyPresence(client, status, activity);
      console.log(`[presence] ${agentId} → ${status} | ${activity || ''}`);
    } catch (err) {
      console.warn('[presence] Bad message:', raw, err.message);
    }
  });
}

// ── Main ──────────────────────────────────────────────────────────────────────

async function main() {
  console.log('[presence] Starting OpenClaw presence manager...');

  // Start all agents in parallel
  await Promise.allSettled(AGENTS.map(def => startAgent(def)));

  const online = [...clients.keys()];
  console.log(`[presence] ${online.length} agents online: ${online.join(', ')}`);

  // Listen for status updates from BotJohn
  startSubscriber();

  // Heartbeat — re-apply presences every 5 minutes in case Discord resets them
  setInterval(() => {
    for (const def of AGENTS) {
      const client = clients.get(def.id);
      if (client?.user) {
        // Just poke the client to keep presence alive; actual status
        // will have been updated via Redis by now
        client.user.setPresence({ status: client.user.presence?.status || def.defaultStatus });
      }
    }
  }, 5 * 60 * 1000);
}

process.on('unhandledRejection', (err) => console.error('[presence] Unhandled:', err));
main().catch(console.error);
