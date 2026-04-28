#!/usr/bin/env node
'use strict';

/**
 * daily_health_digest.js — end-of-cycle health digest step.
 *
 * Invoked by pipeline_orchestrator.py as the final `health` step after
 * all trading has completed. Builds the digest (via the existing
 * src/engine/daily-health-digest buildDigest) and posts it to
 * #pipeline-feed via the DataBot persona webhook persisted in
 * agent_registry.webhook_urls.
 *
 * Posting via webhook URL (not bot token) bypasses Discord channel
 * role-permission issues that blocked direct bot POSTs.
 */

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const https = require('https');
const { Client } = require('pg');

const { buildDigest } = require('../engine/daily-health-digest');

async function getWebhook(agentId, channelKey) {
  const client = new Client({ connectionString: process.env.POSTGRES_URI });
  await client.connect();
  try {
    const r = await client.query(
      'SELECT webhook_urls FROM agent_registry WHERE id=$1',
      [agentId]
    );
    return (r.rows[0]?.webhook_urls || {})[channelKey] || null;
  } finally {
    await client.end();
  }
}

function postWebhook(url, content) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const body = JSON.stringify({ content: content.slice(0, 1900) });
    const req = https.request({
      hostname: u.hostname,
      path:     u.pathname + u.search,
      method:   'POST',
      headers:  { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => {
      let buf = '';
      res.on('data', (c) => buf += c);
      res.on('end', () => resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode, body: buf }));
    });
    req.on('error', (err) => resolve({ ok: false, status: 0, body: err.message }));
    req.write(body);
    req.end();
  });
}

function _parseArg(name) {
  const flag = `--${name}`;
  const i = process.argv.indexOf(flag);
  return i >= 0 ? process.argv[i + 1] : null;
}

async function main() {
  try {
    // Parse optional failure-context flags. The orchestrator passes these
    // when it aborts mid-cycle so the digest flags the failure rather
    // than reporting business-as-usual on a broken cycle.
    const failedStep = _parseArg('failed-step');
    const completed  = _parseArg('completed');
    const errorMsg   = _parseArg('error');
    const failureCtx = failedStep ? {
      step:      failedStep,
      completed: completed ? completed.split(',').map(s => s.trim()).filter(Boolean) : [],
      error:     errorMsg || null,
    } : null;

    const dryRun = process.argv.includes('--dry-run');

    const text = await buildDigest(new Date(), failureCtx);
    console.log('[health] digest built:', text.length, 'chars');

    if (dryRun) {
      console.log('[health] DRY-RUN — printing digest to stdout (skipping webhook POST)');
      console.log(text);
      process.exit(0);
    }

    const url = await getWebhook('botjohn', 'botjohn-log');
    if (!url) {
      console.warn('[health] no botjohn:botjohn-log webhook in agent_registry — printing to stdout only');
      console.log(text);
      process.exit(0);
    }

    const r = await postWebhook(url, text);
    if (!r.ok) {
      console.warn(`[health] webhook post failed: ${r.status} ${r.body.slice(0, 200)}`);
      process.exit(1);
    }
    console.log('[health] posted to #botjohn-log');
    process.exit(0);
  } catch (err) {
    console.error('[health] error:', err && err.stack || err);
    process.exit(1);
  }
}

main();
