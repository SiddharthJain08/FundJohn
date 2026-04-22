#!/usr/bin/env node
'use strict';

/**
 * run_mastermind.js — CLI entry for MastermindJohn (Opus 4.7, 1M ctx).
 *
 * Modes (required):
 *   --mode corpus           Existing paper-curation flow (Sat 10:00 ET timer).
 *   --mode strategy-stack   Weekly live+monitoring strategy review (Fri 20:00 ET).
 *
 * Corpus-mode flags (unchanged from legacy run_curator.js):
 *   --full              Curate every paper in research_corpus not yet curated.
 *   --paper-ids file    Curate only the UUIDs in this newline-delimited file.
 *   --dry-run           Do everything except persist; emit calibration report.
 *   --batch-size N      Override batch size (default 100).
 *   --no-promote        Skip the high-bucket → research_candidates promotion.
 *   --max-promote N     Hard cap on promotions this run (default 600).
 *
 * Strategy-stack-mode flags:
 *   --dry-run           Build the memo + recs but do not post to Discord or
 *                       write to mastermind_weekly_reports.
 *
 * Examples:
 *   node src/agent/curators/run_mastermind.js --mode corpus --full
 *   node src/agent/curators/run_mastermind.js --mode strategy-stack
 *   node src/agent/curators/run_mastermind.js --mode strategy-stack --dry-run
 */

const fs = require('fs');
const path = require('path');

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

function getArg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  if (i < 0) return fallback;
  const next = process.argv[i + 1];
  if (!next || next.startsWith('--')) return true;
  return next;
}

async function runCorpusMode() {
  const MastermindCurator = require('./mastermind');
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
    console.error('corpus mode: must specify --full, --paper-ids <file>, or --dry-run');
    process.exit(2);
  }

  const curator = new MastermindCurator();

  console.error(`[mastermind:corpus] Starting${dryRun ? ' (DRY RUN)' : ''}...`);
  const t0 = Date.now();
  const result = await curator.run({
    dryRun, batchSize, paperIds,
    notify: (m) => console.error(`[mastermind:corpus] ${m}`),
  });
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.error(`[mastermind:corpus] Done in ${elapsed}s — $${result.costUsd.toFixed(4)}.`);
  console.error(`[mastermind:corpus] Buckets:`, result.buckets);

  if (dryRun) {
    const report = await curator.calibrationReport(null, result.ratings);
    console.log(JSON.stringify({ mode: 'corpus', dry_run: true, run: { ...result, ratings: undefined }, calibration: report }, null, 2));
    return;
  }

  let promotion = null;
  if (!skipPromote && result.runId) {
    promotion = await curator.promoteHighBucket({ runId: result.runId, maxToPromote: maxPromote });
    console.error(`[mastermind:corpus] Promoted ${promotion.promoted} to research_candidates.`);
  }
  console.log(JSON.stringify({ mode: 'corpus', run: { ...result, ratings: undefined }, promotion }, null, 2));
}

async function runStrategyStackMode() {
  const strategyStack = require('./strategy_stack');
  const dryRun = !!getArg('--dry-run', false);
  console.error(`[mastermind:strategy-stack] Starting${dryRun ? ' (DRY RUN)' : ''}...`);
  const t0 = Date.now();
  const result = await strategyStack.run({
    dryRun,
    notify: (m) => console.error(`[mastermind:strategy-stack] ${m}`),
  });
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.error(`[mastermind:strategy-stack] Done in ${elapsed}s — $${(result.costUsd || 0).toFixed(4)}.`);
  console.log(JSON.stringify({ mode: 'strategy-stack', dry_run: dryRun, ...result }, null, 2));
}

(async () => {
  const mode = getArg('--mode', 'corpus');
  if (mode === 'corpus')         return runCorpusMode();
  if (mode === 'strategy-stack') return runStrategyStackMode();
  console.error(`Unknown --mode ${JSON.stringify(mode)}. Expected: corpus | strategy-stack`);
  process.exit(2);
})().catch((e) => {
  console.error(`[mastermind] FATAL: ${e.message}`);
  console.error(e.stack);
  process.exit(1);
});
