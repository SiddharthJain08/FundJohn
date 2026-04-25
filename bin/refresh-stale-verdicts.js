#!/usr/bin/env node
/**
 * CLI runner for the async verdict refresh worker.
 *
 *   node bin/refresh-stale-verdicts.js               # one wave, default batch size
 *   node bin/refresh-stale-verdicts.js --batch 10    # custom wave size
 *   node bin/refresh-stale-verdicts.js --waves 3     # multiple waves with 30s gap
 *
 * Recommended systemd timer: every 4 hours during off-market hours, e.g.:
 *
 *   # /etc/systemd/system/openclaw-verdict-refresh.service
 *   [Unit]
 *   Description=OpenClaw async verdict refresh
 *   [Service]
 *   Type=oneshot
 *   User=claudebot
 *   WorkingDirectory=/root/openclaw
 *   EnvironmentFile=/root/openclaw/.env
 *   ExecStart=/usr/bin/node bin/refresh-stale-verdicts.js
 *
 *   # /etc/systemd/system/openclaw-verdict-refresh.timer
 *   [Unit]
 *   Description=OpenClaw verdict refresh — every 4h off-market
 *   [Timer]
 *   OnCalendar=02,06,18,22:00:00 UTC
 *   Persistent=true
 *   [Install]
 *   WantedBy=timers.target
 *
 * Tunables (override via env in EnvironmentFile):
 *   VERDICT_REFRESH_GRACE_HOURS  — only refresh rows stale within this window (default 72h)
 *   VERDICT_REFRESH_LOCK_TTL_S   — Redis dedup-lock TTL (default 3600s)
 *   VERDICT_REFRESH_BATCH_SIZE   — rows per wave (default 5)
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const verdictRefresh = require('../src/agent/services/verdict-refresh');

function parseArgs(argv) {
  const out = { batchSize: undefined, waves: 1 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--batch') out.batchSize = parseInt(argv[++i], 10);
    else if (a === '--waves') out.waves = parseInt(argv[++i], 10);
  }
  return out;
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  let totalPicked = 0;
  let totalOk     = 0;
  let totalFail   = 0;

  for (let i = 0; i < opts.waves; i++) {
    const r = await verdictRefresh.refreshWave({ batchSize: opts.batchSize });
    totalPicked += r.picked;
    totalOk     += r.succeeded;
    totalFail   += r.failed;
    console.log(`[verdict-refresh] wave ${i + 1}/${opts.waves}: picked=${r.picked} ok=${r.succeeded} fail=${r.failed} batch=${r.batchId || 'n/a'}`);
    if (r.picked === 0) break;  // nothing to refresh; stop early
    if (i < opts.waves - 1) await sleep(30_000);
  }

  console.log(`[verdict-refresh] total: picked=${totalPicked} ok=${totalOk} fail=${totalFail}`);
  process.exit(totalFail > 0 ? 1 : 0);
}

main().catch((err) => { console.error('[verdict-refresh] fatal:', err); process.exit(2); });
