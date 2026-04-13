#!/usr/bin/env node
/**
 * cron_engine.js — Daily execution engine cron entry point
 *
 * Resolves the default workspace, runs the zero-token execution engine,
 * and posts results to Discord. Called by crontab after market close.
 *
 * Usage: node scripts/cron_engine.js [--workspace <uuid|name>]
 */

'use strict';

require('dotenv').config({ path: require('path').resolve(__dirname, '..', '.env') });

const { query }       = require('../src/database/postgres');
const runner          = require('../src/execution/runner');
const notifications   = require('../src/channels/discord/notifications');

async function resolveWorkspaceId(nameOrId) {
    if (!nameOrId) {
        // Default: first workspace named 'default', else the first row
        const r = await query(`
            SELECT id FROM workspaces
            ORDER BY (name='default') DESC, created_at ASC
            LIMIT 1
        `);
        if (!r.rows.length) throw new Error('No workspaces found in DB');
        return r.rows[0].id;
    }
    // Check if it's already a UUID
    if (/^[0-9a-f-]{36}$/i.test(nameOrId)) return nameOrId;
    // Look up by name
    const r = await query('SELECT id FROM workspaces WHERE name=$1 LIMIT 1', [nameOrId]);
    if (!r.rows.length) throw new Error(`Workspace not found: ${nameOrId}`);
    return r.rows[0].id;
}

async function main() {
    const args    = process.argv.slice(2);
    const wsIdx   = args.indexOf('--workspace');
    const nameArg = wsIdx !== -1 ? (args[wsIdx + 1] || null) : null;

    let workspaceId;
    try {
        workspaceId = await resolveWorkspaceId(nameArg);
    } catch (e) {
        console.error(`[cron_engine] Workspace resolution failed: ${e.message}`);
        process.exit(1);
    }

    console.log(`[cron_engine] Starting daily close — workspace: ${workspaceId}`);

    try {
        const result = await runner.runDailyClose(workspaceId);
        console.log('[cron_engine] Done:', JSON.stringify(result));
        process.exit(0);
    } catch (err) {
        console.error(`[cron_engine] Engine run failed: ${err.message}`);
        try {
            await notifications.notifyStrategistStatus(
                `**Execution Engine ERROR** — ${err.message}\n_Workspace: ${workspaceId}_`
            );
        } catch (_) {}
        process.exit(1);
    }
}

main();
