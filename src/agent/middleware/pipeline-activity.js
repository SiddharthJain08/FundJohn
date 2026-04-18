/**
 * Pipeline Activity Tracker
 *
 * Tracks which agents are currently running using Redis.
 *
 * Pattern:
 *   - When any subagent starts:  SET pipeline:agent:{workspaceId}:{agentType}:{threadId}  EX 1800
 *   - When any subagent ends:    DEL pipeline:agent:{workspaceId}:{agentType}:{threadId}
 *   - Check active:              KEYS pipeline:agent:{workspaceId}:*
 */

'use strict';

const { getClient } = require('../../database/redis');

const AGENT_KEY = (workspaceId, agentType, threadId) =>
    `pipeline:agent:${workspaceId}:${agentType}:${threadId}`;

const AGENT_PATTERN = (workspaceId) =>
    `pipeline:agent:${workspaceId}:*`;

// Call when any subagent starts
async function registerAgentActive(workspaceId, agentType, threadId) {
    const r   = getClient();
    const key = AGENT_KEY(workspaceId, agentType, threadId);
    await r.set(key, '1', 'EX', 1800); // 30 min TTL prevents stale locks
}

// Call when any subagent finishes
async function registerAgentDone(workspaceId, agentType, threadId) {
    const r   = getClient();
    const key = AGENT_KEY(workspaceId, agentType, threadId);
    await r.del(key);
}

// Returns list of currently active agent types
async function getActivePipelineAgents(workspaceId) {
    const r       = getClient();
    const pattern = AGENT_PATTERN(workspaceId);
    const keys    = await r.keys(pattern);
    return keys
        .map(k => k.split(':')[3])               // extract agentType from key
        .filter(t => t);
}

// Core check: is the pipeline idle?
async function isPipelineIdle(workspaceId) {
    const active = await getActivePipelineAgents(workspaceId);
    return {
        idle:          active.length === 0,
        active_agents: active,
        formatted:     active.length === 0
            ? 'Pipeline idle'
            : `Pipeline active: ${active.join(', ')}`,
    };
}

// Poll until pipeline is idle
async function waitForIdle(workspaceId, timeoutMs = 300_000, pollIntervalMs = 15_000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const status = await isPipelineIdle(workspaceId);
        if (status.idle) return { became_idle: true };
        console.log(`[STRATEGIST YIELD] ${status.formatted} — waiting ${pollIntervalMs/1000}s`);
        await new Promise(r => setTimeout(r, pollIntervalMs));
    }
    return { became_idle: false, reason: 'Timeout waiting for pipeline idle' };
}

module.exports = {
    registerAgentActive,
    registerAgentDone,
    getActivePipelineAgents,
    isPipelineIdle,
    waitForIdle,
};
