/**
 * Token Budget Monitor
 *
 * Tracks cumulative token usage across the current calendar day.
 * The strategist only runs if >= 20% of the daily session budget remains.
 *
 * Usage is tracked in two places:
 *   1. Redis — fast real-time check (reset daily at midnight)
 *   2. Postgres — persistent audit log
 *
 * All other agents record usage here too so the budget is accurate.
 */

'use strict';

const { getClient } = require('../../database/redis');
const { Pool } = require('pg');
const pool   = new Pool({ connectionString: process.env.POSTGRES_URI });

// Keys
const USAGE_KEY  = (workspaceId) => `token_usage:${workspaceId}:${todayStr()}`;

function todayStr() {
    return new Date().toISOString().slice(0, 10);
}

// Read session token limit from preferences (default 100,000)
async function getSessionLimit(workspaceId) {
    try {
        const fs   = require('fs');
        // Try both workspace UUID path and default path
        const paths = [
            `workspaces/${workspaceId}/.agents/user/preferences.json`,
            `workspaces/default/.agents/user/preferences.json`,
        ];
        for (const p of paths) {
            if (fs.existsSync(p)) {
                const prefs = JSON.parse(fs.readFileSync(p));
                if (prefs?.token_budget?.daily_limit) return prefs.token_budget.daily_limit;
            }
        }
    } catch (_) {}
    return 100_000;
}

// Record token usage after every API call (call this from the agent runner)
async function recordUsage(workspaceId, agentType, tokensIn, tokensOut) {
    const total = (tokensIn || 0) + (tokensOut || 0);
    if (total === 0) return;

    // Redis increment (atomic, fast)
    const r   = getClient();
    const key = USAGE_KEY(workspaceId);
    await r.incrby(key, total);

    // Set TTL to 48h so we don't lose today's data at midnight rollover
    const ttl = await r.ttl(key);
    if (ttl < 0) await r.expire(key, 172_800);

    // Postgres log (async, non-blocking)
    pool.query(
        `INSERT INTO token_usage_log (workspace_id, agent_type, tokens_in, tokens_out)
         VALUES ($1, $2, $3, $4)`,
        [workspaceId, agentType, tokensIn || 0, tokensOut || 0]
    ).catch(() => {}); // don't throw on log failure
}

// Get current usage and remaining budget
async function getBudgetStatus(workspaceId) {
    const r        = getClient();
    const limit    = await getSessionLimit(workspaceId);
    const usedRaw  = await r.get(USAGE_KEY(workspaceId));
    const used     = parseInt(usedRaw || '0', 10);
    const remaining = Math.max(0, limit - used);
    const pct_remaining = remaining / limit;

    return {
        daily_limit:     limit,
        used_today:      used,
        remaining:       remaining,
        pct_remaining:   pct_remaining,
        pct_used:        1 - pct_remaining,
        budget_ok:       pct_remaining >= 0.20,  // strategist threshold
        critical:        pct_remaining < 0.10,   // under 10% = critical, pause everything
        formatted:       `${(pct_remaining * 100).toFixed(1)}% remaining (${remaining.toLocaleString()} / ${limit.toLocaleString()} tokens)`,
    };
}

// Hard check — call this before starting the strategist
async function canStrategistRun(workspaceId) {
    const budget = await getBudgetStatus(workspaceId);
    if (!budget.budget_ok) {
        return {
            allowed: false,
            reason:  `Token budget too low: ${budget.formatted}. Need >= 20% remaining.`,
            budget,
        };
    }
    return { allowed: true, budget };
}

// Estimate remaining steps (heuristic: ~3,000 tokens per hypothesis exploration step)
function estimateStepsRemaining(budget) {
    return Math.floor(budget.remaining / 3_000);
}

module.exports = {
    recordUsage,
    getBudgetStatus,
    canStrategistRun,
    estimateStepsRemaining,
    getSessionLimit,
};
