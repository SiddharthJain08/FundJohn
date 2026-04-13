'use strict';

const Redis = require('ioredis');

let client = null;

function getClient() {
  if (!client) {
    client = new Redis(process.env.REDIS_URL || 'redis://localhost:6379', {
      maxRetriesPerRequest: 3,
      enableReadyCheck: true,
      lazyConnect: true,
    });
    client.on('error', (err) => console.error('[redis] Error:', err.message));
    client.on('connect', () => console.log('[redis] Connected'));
  }
  return client;
}

// ── Steering Queue ─────────────────────────────────────────────────────────────
// Redis list per thread ID. Middleware drains before every LLM call.

async function pushSteering(threadId, message) {
  return getClient().rpush(`steering:${threadId}`, message);
}

async function drainSteering(threadId) {
  const r = getClient();
  const messages = [];
  while (true) {
    const msg = await r.lpop(`steering:${threadId}`);
    if (!msg) break;
    messages.push(msg);
  }
  return messages;
}

// ── Rate Limiting (Token Bucket) ───────────────────────────────────────────────
// One bucket per provider. Shared across all subagents.

async function initRateLimitBuckets(preferences) {
  const limits = preferences.rate_limits || {};
  const r = getClient();

  // Polygon/Massive are always unlimited — set sentinel before processing other limits
  await r.set('rate_limit:polygon', '9999');
  await r.set('rate_limit:massive', '9999');

  for (const [provider, config] of Object.entries(limits)) {
    // Skip polygon/massive — already set to unlimited sentinel above
    if (provider === 'polygon' || provider === 'massive') continue;
    const bucketKey = `rate_limit:${provider}`;
    const existing = await r.get(bucketKey);
    if (!existing) {
      const capacity = config.per_minute || (config.per_second ? config.per_second * 60 : 60);
      await r.set(bucketKey, capacity);
    }
  }

  // Refill every 60 seconds (polygon/massive stay at 9999 always)
  setInterval(async () => {
    await r.set('rate_limit:polygon', '9999');
    await r.set('rate_limit:massive', '9999');
    for (const [provider, config] of Object.entries(limits)) {
      if (provider === 'polygon' || provider === 'massive') continue;
      const capacity = config.per_minute || (config.per_second ? config.per_second * 60 : 60);
      await r.set(`rate_limit:${provider}`, capacity);
    }
  }, 60_000);

  console.log(`[redis] Rate limit buckets initialized for: ${Object.keys(limits).join(', ')} (polygon/massive: unlimited)`);
}

async function acquireToken(provider, cost = 1) {
  const bucketKey = `rate_limit:${provider}`;
  const remaining = await getClient().get(bucketKey);
  if (remaining && parseInt(remaining, 10) >= cost) {
    await getClient().decrby(bucketKey, cost);
    return true;
  }
  return false;
}

async function getBucketStatus() {
  const r = getClient();
  const keys = await r.keys('rate_limit:*');
  const status = {};
  for (const key of keys) {
    const provider = key.replace('rate_limit:', '');
    status[provider] = parseInt(await r.get(key) || '0', 10);
  }
  return status;
}

// ── Subagent Status ────────────────────────────────────────────────────────────

async function setSubagentStatus(subagentId, status) {
  await getClient().setex(`subagent:${subagentId}`, 3600, JSON.stringify(status));
}

async function getSubagentStatus(subagentId) {
  const raw = await getClient().get(`subagent:${subagentId}`);
  return raw ? JSON.parse(raw) : null;
}

async function getAllSubagentStatuses() {
  const r = getClient();
  const keys = await r.keys('subagent:*');
  const statuses = [];
  for (const key of keys) {
    const raw = await r.get(key);
    if (raw) statuses.push(JSON.parse(raw));
  }
  return statuses;
}

// ── API Response Cache (5-min TTL) ────────────────────────────────────────────

async function cacheSet(key, value, ttlSeconds = 300) {
  return getClient().setex(`cache:${key}`, ttlSeconds, JSON.stringify(value));
}

async function cacheGet(key) {
  const raw = await getClient().get(`cache:${key}`);
  return raw ? JSON.parse(raw) : null;
}

// ── Operator Last Activity ─────────────────────────────────────────────────────

async function updateOperatorActivity(userId) {
  return getClient().set(`operator:last_activity:${userId}`, Date.now());
}

async function isOperatorOnline(userId, windowMs = 30 * 60 * 1000) {
  const last = await getClient().get(`operator:last_activity:${userId}`);
  if (!last) return false;
  return Date.now() - parseInt(last, 10) < windowMs;
}

// ── Provider-level Rate Limit Tracking ───────────────────────────────────────
// Tracks 429 state per provider (anthropic, openai) across all models.
// swarm.js calls recordProviderRateLimit() on 429; checkProviderReady() before spawn.

async function recordProviderRateLimit(provider, retryAfterSec) {
  const r = getClient();
  const resetAt = Date.now() + retryAfterSec * 1000;
  await r.set(`ratelimit:${provider}:reset_at`, resetAt, 'EX', retryAfterSec + 5);
  await r.set(`ratelimit:${provider}:remaining`, 0, 'EX', retryAfterSec + 5);
}

async function checkProviderReady(provider) {
  const r = getClient();
  const resetAt = await r.get(`ratelimit:${provider}:reset_at`);
  if (!resetAt) return { ready: true, waitMs: 0 };
  const waitMs = Math.max(0, parseInt(resetAt, 10) - Date.now() + 1000); // +1s jitter
  return { ready: waitMs === 0, waitMs };
}

async function getProviderRateLimitStatus() {
  const r = getClient();
  const providers = ['anthropic', 'openai'];
  const status = {};
  for (const p of providers) {
    const resetAt   = await r.get(`ratelimit:${p}:reset_at`);
    const remaining = await r.get(`ratelimit:${p}:remaining`);
    status[p] = {
      limited:   !!resetAt && Date.now() < parseInt(resetAt || '0', 10),
      resetAt:   resetAt ? new Date(parseInt(resetAt, 10)).toISOString() : null,
      remaining: remaining !== null ? parseInt(remaining, 10) : null,
    };
  }
  return status;
}

module.exports = {
  getClient,
  pushSteering,
  drainSteering,
  initRateLimitBuckets,
  acquireToken,
  getBucketStatus,
  setSubagentStatus,
  getSubagentStatus,
  getAllSubagentStatuses,
  cacheSet,
  cacheGet,
  updateOperatorActivity,
  isOperatorOnline,
  recordProviderRateLimit,
  checkProviderReady,
  getProviderRateLimitStatus,
};
