#!/usr/bin/env node
'use strict';

/**
 * run_curator.js — CLI entry for the Opus corpus curator.
 *
 * Modes:
 *   --full              Curate every paper in research_corpus not yet curated
 *                       in a completed run. This is the weekly Saturday-10am mode.
 *   --paper-ids file    Curate only the UUIDs in this newline-delimited file.
 *   --dry-run           Do everything except persist curated_candidates, then
 *                       emit a calibration report against paper_gate_decisions.
 *   --batch-size N      Override batch size (default 100).
 *   --no-promote        Skip the high-bucket → research_candidates promotion step.
 *   --max-promote N     Hard cap on promotions this run (default 600).
 *
 * Examples:
 *   node src/agent/curators/run_curator.js --full
 *   node src/agent/curators/run_curator.js --dry-run --paper-ids /tmp/ids.txt
 */

const fs = require('fs');
const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
// Load .env if present (so scripts can be called outside johnbot.service).
try {
  const envPath = path.join(OPENCLAW_DIR, '.env');
  if (fs.existsSync(envPath)) {
    for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
      const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
      if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
    }
  }
} catch { /* ignore */ }

const CorpusCurator = require('./corpus_curator');

function getArg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  if (i < 0) return fallback;
  const next = process.argv[i + 1];
  if (!next || next.startsWith('--')) return true;   // boolean flag
  return next;
}

(async () => {
  const full        = !!getArg('--full', false);
  const paperIdsArg = getArg('--paper-ids');
  const dryRun      = !!getArg('--dry-run', false);
  const batchSize   = parseInt(getArg('--batch-size', '100'), 10);
  const skipPromote = !!getArg('--no-promote', false);
  const maxPromote  = parseInt(getArg('--max-promote', '600'), 10);

  let paperIds = null;
  if (paperIdsArg && paperIdsArg !== true) {
    paperIds = fs.readFileSync(paperIdsArg, 'utf8')
      .split('\n').map(s => s.trim()).filter(Boolean);
  }

  if (!full && !paperIds && !dryRun) {
    console.error('Must specify --full, --paper-ids <file>, or --dry-run');
    process.exit(2);
  }

  const curator = new CorpusCurator();

  console.error(`[curator-cli] Starting${dryRun ? ' (DRY RUN)' : ''}...`);
  const t0 = Date.now();
  const result = await curator.run({
    dryRun, batchSize, paperIds,
    notify: (m) => console.error(`[curator-cli] ${m}`),
  });
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.error(`[curator-cli] Run done in ${elapsed}s — $${result.costUsd.toFixed(4)}.`);
  console.error(`[curator-cli] Buckets:`, result.buckets);

  if (dryRun) {
    const report = await curator.calibrationReport(null, result.ratings);
    console.log(JSON.stringify({ mode: 'dry_run', run: { ...result, ratings: undefined }, calibration: report }, null, 2));
    process.exit(0);
  }

  let promotion = null;
  if (!skipPromote && result.runId) {
    promotion = await curator.promoteHighBucket({ runId: result.runId, maxToPromote: maxPromote });
    console.error(`[curator-cli] Promoted ${promotion.promoted} to research_candidates (eligible=${promotion.eligible}, capped=${promotion.capped}).`);
  }

  console.log(JSON.stringify({ run: { ...result, ratings: undefined }, promotion }, null, 2));
})().catch((e) => {
  console.error(`[curator-cli] FATAL: ${e.message}`);
  console.error(e.stack);
  process.exit(1);
});
