#!/usr/bin/env node
'use strict';

/**
 * drive_pipeline.js — CLI-only driver for the research pipeline.
 *
 * Invokes ResearchOrchestrator.processQueue() in a loop until the pending
 * queue empties or a max batch count is hit. Used during calibration-data
 * collection sessions to avoid routing through the Discord bot.
 *
 * Usage:
 *   node src/agent/research/drive_pipeline.js [--max-batches N]
 */

const path = require('path');
const fs = require('fs');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
try {
  const envPath = path.join(OPENCLAW_DIR, '.env');
  if (fs.existsSync(envPath)) {
    for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
      const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
      if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
    }
  }
} catch { /* ignore */ }

const ResearchOrchestrator = require('./research-orchestrator');

function getArg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  if (i < 0) return fallback;
  return process.argv[i + 1] || fallback;
}

(async () => {
  const maxBatches = parseInt(getArg('--max-batches', '10'), 10);
  const orch = new ResearchOrchestrator();

  const notify        = (t) => console.error(`[notify] ${t}`);
  const channelNotify = (t) => console.error(`[channel] ${t}`);

  for (let i = 0; i < maxBatches; i++) {
    const { rows } = await orch._query(
      `SELECT COUNT(*)::int AS n FROM research_candidates WHERE status='pending'`
    );
    const pending = rows[0].n;
    console.error(`[drive] batch ${i + 1}/${maxBatches} — pending=${pending}`);
    if (pending === 0) { console.error('[drive] queue empty'); break; }
    await orch.processQueue({ notify, channelNotify });
  }

  // Summarize.
  const { rows: final } = await orch._query(
    `SELECT status, COUNT(*)::int AS n FROM research_candidates
     WHERE submitted_by IN ('curator','curator_spotcheck')
     GROUP BY status ORDER BY status`
  );
  console.error('\n[drive] final research_candidates state:');
  for (const r of final) console.error(`  ${r.status}: ${r.n}`);

  process.exit(0);
})().catch((e) => {
  console.error(`[drive] FATAL: ${e.message}`);
  console.error(e.stack);
  process.exit(1);
});
