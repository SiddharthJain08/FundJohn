#!/usr/bin/env node
'use strict';

/**
 * run_mastermind.js — CLI entry for MastermindJohn (Opus 4.7, 1M ctx).
 *
 * Modes (required):
 *   --mode corpus                 Paper curation flow (Sat 10:00 ET timer).
 *   --mode comprehensive-review   Per-strategy lifetime memos (Sat 18:00 ET).
 *   --mode position-recs          Sizing recs from latest memos (Sat 19:00 ET).
 *   --mode paper-expansion        Opus-steered paper hunt w/ web scraping (Sun 08:00 ET).
 *
 * Corpus-mode flags (unchanged from legacy run_curator.js):
 *   --full              Curate every paper in research_corpus not yet curated.
 *   --paper-ids file    Curate only the UUIDs in this newline-delimited file.
 *   --dry-run           Do everything except persist; emit calibration report.
 *   --batch-size N      Override batch size (default 100).
 *   --no-promote        Skip the high-bucket → research_candidates promotion.
 *   --max-promote N     Hard cap on promotions this run (default 600).
 *
 * Comprehensive-review / position-recs / paper-expansion flags:
 *   --dry-run           Build outputs but do not post or persist.
 *
 * Examples:
 *   node src/agent/curators/run_mastermind.js --mode corpus --full
 *   node src/agent/curators/run_mastermind.js --mode comprehensive-review
 *   node src/agent/curators/run_mastermind.js --mode position-recs
 *   node src/agent/curators/run_mastermind.js --mode paper-expansion
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

async function runComprehensiveReview() {
  const { run } = require('./comprehensive_review');
  const dryRun = !!getArg('--dry-run', false);
  const strategyIdArg = getArg('--strategy-id');
  const strategyIds = strategyIdArg && strategyIdArg !== true ? [strategyIdArg] : null;
  console.error(`[mastermind:review] Starting${dryRun ? ' (DRY RUN)' : ''}...`);
  const t0 = Date.now();
  const result = await run({
    dryRun, strategyIds,
    notify: (m) => console.error(`[mastermind:review] ${m}`),
  });
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.error(`[mastermind:review] Done in ${elapsed}s — $${(result.costUsd || 0).toFixed(4)}.`);
  console.log(JSON.stringify({ mode: 'comprehensive-review', dry_run: dryRun, ...result }, null, 2));
}

async function runPositionRecs() {
  const { run } = require('./position_recommender');
  const dryRun = !!getArg('--dry-run', false);
  console.error(`[mastermind:position-recs] Starting${dryRun ? ' (DRY RUN)' : ''}...`);
  const t0 = Date.now();
  const result = await run({
    dryRun,
    notify: (m) => console.error(`[mastermind:position-recs] ${m}`),
  });
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.error(`[mastermind:position-recs] Done in ${elapsed}s.`);
  console.log(JSON.stringify({ mode: 'position-recs', dry_run: dryRun, ...result }, null, 2));
}

async function runPaperExpansion() {
  const { run } = require('./paper_expansion_ingestor');
  const dryRun = !!getArg('--dry-run', false);
  console.error(`[mastermind:expansion] Starting${dryRun ? ' (DRY RUN)' : ''}...`);
  const t0 = Date.now();
  const result = await run({
    dryRun,
    notify: (m) => console.error(`[mastermind:expansion] ${m}`),
  });
  const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
  console.error(`[mastermind:expansion] Done in ${elapsed}s — $${(result.costUsd || 0).toFixed(4)}.`);
  console.log(JSON.stringify({ mode: 'paper-expansion', dry_run: dryRun, ...result }, null, 2));
}

(async () => {
  const mode = getArg('--mode', 'corpus');
  if (mode === 'corpus')                return runCorpusMode();
  if (mode === 'comprehensive-review')  return runComprehensiveReview();
  if (mode === 'position-recs')         return runPositionRecs();
  if (mode === 'paper-expansion')       return runPaperExpansion();
  console.error(`Unknown --mode ${JSON.stringify(mode)}. Expected: corpus | comprehensive-review | position-recs | paper-expansion`);
  process.exit(2);
})().catch((e) => {
  console.error(`[mastermind] FATAL: ${e.message}`);
  console.error(e.stack);
  process.exit(1);
});
