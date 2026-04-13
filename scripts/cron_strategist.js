#!/usr/bin/env node
/**
 * cron_strategist.js — Off-hours strategist session cron entry point
 *
 * Checks activation conditions (off-hours, budget, pipeline idle) and
 * either starts/resumes a strategist session or logs the block reason.
 *
 * Usage: node scripts/cron_strategist.js [--workspace <uuid|name>] [--force]
 */

'use strict';

require('dotenv').config({ path: require('path').resolve(__dirname, '..', '.env') });

const { query }     = require('../src/database/postgres');
const scheduler     = require('../src/agent/graph/strategist-scheduler');
const swarm         = require('../src/agent/subagents/swarm');
const notifications = require('../src/channels/discord/notifications');

async function resolveWorkspaceId(nameOrId) {
    if (!nameOrId) {
        const r = await query(`
            SELECT id, name FROM workspaces
            ORDER BY (name='default') DESC, created_at ASC
            LIMIT 1
        `);
        if (!r.rows.length) throw new Error('No workspaces found in DB');
        return r.rows[0];
    }
    if (/^[0-9a-f-]{36}$/i.test(nameOrId)) {
        const r = await query('SELECT id, name FROM workspaces WHERE id=$1 LIMIT 1', [nameOrId]);
        if (!r.rows.length) throw new Error(`Workspace not found: ${nameOrId}`);
        return r.rows[0];
    }
    const r = await query('SELECT id, name FROM workspaces WHERE name=$1 LIMIT 1', [nameOrId]);
    if (!r.rows.length) throw new Error(`Workspace not found: ${nameOrId}`);
    return r.rows[0];
}

async function main() {
    const args    = process.argv.slice(2);
    const wsIdx   = args.indexOf('--workspace');
    const nameArg = wsIdx !== -1 ? (args[wsIdx + 1] || null) : null;
    const force   = args.includes('--force');

    let workspace;
    try {
        workspace = await resolveWorkspaceId(nameArg);
    } catch (e) {
        console.error(`[cron_strategist] Workspace resolution failed: ${e.message}`);
        process.exit(1);
    }

    const { id: workspaceId, name: workspaceName } = workspace;
    console.log(`[cron_strategist] Checking activation for workspace: ${workspaceName} (${workspaceId}) force=${force}`);

    // Run the activation check
    const sessionResult = await scheduler.startSession(workspaceId, force);

    if (!sessionResult.started) {
        console.log(`[cron_strategist] Not activated — ${sessionResult.reason}`);
        process.exit(0);
    }

    const mode = sessionResult.mode; // 'NEW' or 'RESUMED'
    console.log(`[cron_strategist] Session ${mode} — id: ${sessionResult.session_id}`);

    // Notify Discord
    try {
        const emoji = mode === 'RESUMED' ? '▶️' : '🧠';
        await notifications.notifyStrategistStatus(
            `${emoji} **Strategist ${mode}** — workspace: \`${workspaceName}\`\n` +
            `Budget: ${sessionResult.check.budget.formatted} | Session: \`${sessionResult.session_id.slice(0, 8)}\``
        );
    } catch (_) {}

    // Spawn strategist via swarm
    try {
        // swarm expects workspace as a path string (used as cwd)
        const workspacePath = require('path').resolve(__dirname, '..', 'workspaces', workspaceName);
        // Inject workspace UUID so the strategist prompt can use it
        process.env.WORKSPACE_ID = workspaceId;
        const notifyFn = (msg) => {
            notifications.notifyStrategistStatus(msg).catch(() => {});
        };

        const result = await swarm.init({
            type:      'strategist',
            ticker:    null,
            workspace: workspacePath,
            threadId:  `strategist-${Date.now()}`,
            prompt:    `session_id: ${sessionResult.session_id}\nmode: ${mode}\nphase: ${sessionResult.state?.phase || 'REVIEW'}`,
            notify:    notifyFn,
        });

        console.log('[cron_strategist] Session completed:', JSON.stringify({
            cost:  result?.cost,
            turns: result?.numTurns,
        }));

        await scheduler.completeSession(workspaceId, `Completed via cron. Mode: ${mode}.`);

        process.exit(0);
    } catch (err) {
        console.error(`[cron_strategist] Strategist session failed: ${err.message}`);
        await scheduler.pauseSession(workspaceId, sessionResult.state || {}, `cron_error: ${err.message}`);
        try {
            await notifications.notifyStrategistStatus(
                `⚠️ **Strategist session error** — ${err.message.slice(0, 200)}`
            );
        } catch (_) {}
        process.exit(1);
    }
}

main();
