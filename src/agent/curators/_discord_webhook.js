'use strict';

/**
 * _discord_webhook.js — shared webhook lookup + POST for one-shot curator
 * scripts that run outside the johnbot.service process.
 *
 * For curators running INSIDE the bot (johnbot.service), use the
 * notifications.js client-bound helper instead. This file exists because
 * one-shot scripts (weekly_live_sharpe, position_recommender, etc.) don't
 * have a discord.js Client; they have to do an HTTP POST to a webhook URL.
 *
 * 2026-05-13..18: position_recs + weekly-strategy-weights silently logged
 * "discord: skipped (missing token or channel id)" every Saturday/Sunday
 * because they were checking DISCORD_GENERAL_CHANNEL_ID (never set)
 * instead of looking up the webhook in agent_registry like
 * run_maintenance.js does. This helper standardises the lookup.
 *
 * Usage:
 *   const { postToChannel } = require('./_discord_webhook');
 *   await postToChannel('botjohn', 'general', 'hello');
 */

const https = require('https');
const { Client } = require('pg');

async function getWebhook(agentId, channelKey) {
  const client = new Client({ connectionString: process.env.POSTGRES_URI });
  await client.connect();
  try {
    const r = await client.query(
      'SELECT webhook_urls FROM agent_registry WHERE id=$1',
      [agentId],
    );
    return (r.rows[0]?.webhook_urls || {})[channelKey] || null;
  } finally {
    await client.end();
  }
}

function postWebhook(url, content) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const body = JSON.stringify({ content: String(content).slice(0, 1900) });
    const req = https.request({
      hostname: u.hostname,
      path:     u.pathname + u.search,
      method:   'POST',
      headers:  {
        'Content-Type':   'application/json',
        'Content-Length': Buffer.byteLength(body),
        // Identify so Cloudflare's bot filter doesn't 403 us.
        'User-Agent':     'OpenClaw-Curator/1.0 (+botjohn)',
      },
    }, (res) => {
      let buf = '';
      res.on('data', (c) => buf += c);
      res.on('end', () => resolve({
        ok: res.statusCode >= 200 && res.statusCode < 300,
        status: res.statusCode,
        body: buf,
      }));
    });
    req.on('error', (err) => resolve({ ok: false, status: 0, body: err.message }));
    req.write(body);
    req.end();
  });
}

/**
 * One-call helper: look up the agent+channel webhook and POST content.
 * Returns { ok, status, reason? }. Never throws.
 */
async function postToChannel(agentId, channelKey, content) {
  let url;
  try {
    url = await getWebhook(agentId, channelKey);
  } catch (e) {
    return { ok: false, reason: 'lookup_failed', error: e.message };
  }
  if (!url) {
    return { ok: false, reason: 'no_webhook', detail: `${agentId}/${channelKey} not in agent_registry.webhook_urls` };
  }
  const r = await postWebhook(url, content);
  return { ok: r.ok, status: r.status, body: r.body };
}

module.exports = { getWebhook, postWebhook, postToChannel };
