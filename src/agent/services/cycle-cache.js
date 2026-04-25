'use strict';

const { getClient } = require('../../database/redis');

/**
 * Cross-subagent per-cycle data cache (JS side — secondary path).
 *
 * The PRIMARY cycle cache is Python-side, in workspace/tools/_rate_limiter.py
 * (_cycle_cache_get / _cycle_cache_set), because all FMP/Polygon/EDGAR/etc.
 * tools run inside Python subagent processes spawned by swarm.js. That's
 * where the real cross-subagent dedup happens.
 *
 * This JS module is the equivalent helper for any future graph-node-level
 * JS fetchers (none today) AND owns the `clear()` lifecycle hook called from
 * graph.js at cycle END. Keep it: same Redis key namespace (`cycle:*`) so
 * either side can drop the cycle cleanly.
 *
 * Problem this solves: ResearchJohn and TradeJohn both fetch AAPL prices in
 * the same daily cycle. Two API hits, two big tool-result blocks injected
 * into context, two cache-write paths.
 *
 * Solution: shared Redis namespace keyed by cycleId. First fetcher populates;
 * subsequent fetchers hit Redis.
 *
 * Lifecycle: cycleId is the LangGraph thread_id of the current daily-cycle run
 * (graph.js sets it). Keys auto-expire 24h after cycle start so stale cycles
 * never leak into a fresh day.
 *
 * Invariants:
 *   - cycleId is required; if missing, wrap() is a transparent passthrough so
 *     ad-hoc Discord queries don't pollute a cycle namespace.
 *   - Values are JSON-serialized; tools must accept whatever they emit.
 *   - Cache misses never throw — we always fall back to the live fetcher.
 */

const TTL_SECONDS = 24 * 60 * 60;

function key(cycleId, scope, params) {
  const paramKey = typeof params === 'string'
    ? params
    : JSON.stringify(params, Object.keys(params).sort());
  return `cycle:${cycleId}:${scope}:${paramKey}`;
}

async function get(cycleId, scope, params) {
  if (!cycleId) return null;
  try {
    const raw = await getClient().get(key(cycleId, scope, params));
    return raw == null ? null : JSON.parse(raw);
  } catch (err) {
    console.warn('[cycle-cache] get failed:', err.message);
    return null;
  }
}

async function set(cycleId, scope, params, value) {
  if (!cycleId) return false;
  try {
    await getClient().set(
      key(cycleId, scope, params),
      JSON.stringify(value),
      'EX',
      TTL_SECONDS,
    );
    return true;
  } catch (err) {
    console.warn('[cycle-cache] set failed:', err.message);
    return false;
  }
}

/**
 * Wrap a fetcher with cycle-cache. cycleId is read from the surrounding
 * agent state — passed via state.cycleId by graph.js or swarm.js.
 *
 *   const fetchPrice = wrap('polygon:price', async ({ ticker, date }) => {...});
 *   const data = await fetchPrice({ ticker: 'AAPL', date: '2026-04-25' }, { cycleId });
 */
function wrap(scope, fetcher) {
  return async function wrapped(params, ctx = {}) {
    const cycleId = ctx.cycleId || null;
    if (cycleId) {
      const hit = await get(cycleId, scope, params);
      if (hit !== null) {
        if (process.env.CYCLE_CACHE_DEBUG) {
          console.log(`[cycle-cache] HIT ${scope} ${JSON.stringify(params)}`);
        }
        return hit;
      }
    }
    const value = await fetcher(params, ctx);
    if (cycleId && value !== undefined && value !== null) {
      await set(cycleId, scope, params, value);
    }
    return value;
  };
}

/**
 * Drop all keys for a cycle. Call from graph.js END node so completed cycles
 * don't sit in Redis until TTL.
 */
async function clear(cycleId) {
  if (!cycleId) return 0;
  try {
    const client = getClient();
    const stream = client.scanStream({ match: `cycle:${cycleId}:*`, count: 200 });
    let deleted = 0;
    for await (const keys of stream) {
      if (keys.length) {
        await client.del(...keys);
        deleted += keys.length;
      }
    }
    return deleted;
  } catch (err) {
    console.warn('[cycle-cache] clear failed:', err.message);
    return 0;
  }
}

module.exports = { get, set, wrap, clear };
