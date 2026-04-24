'use strict';

/**
 * run_collector_once.js — standalone wrapper around collector.start().
 *
 * The daily orchestrator (src/execution/pipeline_orchestrator.py) invokes this
 * script as the `collect` step. The collector runs one cycle against the
 * master parquets + configured universe and exits 0 on clean completion,
 * non-zero on any phase error. Posts a descriptive end-of-run summary to
 * #data-alerts via the DataBot webhook (bypasses bot-token 403s).
 */

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

// Ensure any transitive require of src/channels/api/server.js inside the
// collector's broadcast helpers does NOT try to listen on :3000 (johnbot
// already owns that socket). We only need the broadcast exports here.
process.env.OPENCLAW_NO_HTTP_LISTEN = '1';

const https = require('https');
const { Client } = require('pg');

const collector = require('./collector');

async function getWebhook() {
  const client = new Client({ connectionString: process.env.POSTGRES_URI });
  await client.connect();
  try {
    const r = await client.query(
      "SELECT webhook_urls FROM agent_registry WHERE id='databot'"
    );
    return ((r.rows[0]?.webhook_urls) || {})['data-alerts'] || null;
  } finally { await client.end(); }
}

function post(url, content) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const body = JSON.stringify({ content: content.slice(0, 1900) });
    const req = https.request({
      hostname: u.hostname, path: u.pathname + u.search, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => { res.on('data',()=>{}); res.on('end', () => resolve(res.statusCode)); });
    req.on('error', () => resolve(0));
    req.write(body); req.end();
  });
}

// Scrape the collector's stdout as it runs so we can attribute rows-written
// and time-per-phase into the summary message without having to re-query
// the collector's internal state.
const phases = []; // { phase, line, ts }
const phaseStart = Date.now();
const origLog = console.log;
console.log = (...args) => {
  const line = args.join(' ');
  origLog(...args);
  // Capture collector's phase summary lines
  if (/\[collector\]\s+✅|\[collector\]\s+⚠️|\[collector\]\s+❌|\[collector\]\s+🎲|\[collector\]\s+🌐/.test(line)) {
    phases.push({ line: line.replace(/^.*?\[collector\]\s+/, ''), ts: Date.now() });
  }
};

async function main() {
  console.log('[collector-once] starting single-shot collection cycle');
  let ok = true;
  try {
    await collector.runDailyCollection();
    console.log('[collector-once] cycle complete');
  } catch (err) {
    console.error('[collector-once] cycle failed:', err && err.stack || err);
    ok = false;
  }

  // End-of-run summary to #data-alerts
  try {
    const total_s = Math.round((Date.now() - phaseStart) / 1000);
    const url = await getWebhook();
    if (url) {
      const phaseLines = phases.slice(-15).map((p) => `• ${p.line}`).join('\n') || '(no phase output captured)';
      const header = ok
        ? `📦 **Daily ingestion complete** — ${new Date().toISOString().slice(0,10)} · ${total_s}s`
        : `❌ **Daily ingestion FAILED** — ${new Date().toISOString().slice(0,10)} · ${total_s}s`;
      await post(url, `${header}\n${phaseLines}`);
    }
  } catch (err) {
    console.warn('[collector-once] data-alerts summary post failed:', err.message);
  }

  process.exit(ok ? 0 : 1);
}

main();
