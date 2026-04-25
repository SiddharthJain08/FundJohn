#!/usr/bin/env node
/**
 * One-shot backfill for memory_chunks.
 *
 *   node bin/backfill-memory-chunks.js                # full backfill
 *   node bin/backfill-memory-chunks.js --dry-run      # parse + chunk only
 *   node bin/backfill-memory-chunks.js --force        # re-embed unchanged files
 *   node bin/backfill-memory-chunks.js --dir results  # restrict to one subdir
 *
 * Walks workspaces/default/{memory,results}/ by default. Skips _templates/.
 * Strategies/ is Python-only and gets refreshed via periodic cron, not here.
 *
 * Cost (Voyage voyage-3 list): $0.06 / 1M input tokens.
 *   Typical workspace today: 5 memory files + 12 results = 17 files
 *   ~3-5 chunks/file × 500 tokens = 25-50k tokens = $0.0015-$0.003 per backfill.
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const fs   = require('fs');
const path = require('path');
const { embedDirectory, parseFrontmatter, chunkBody } = require('../src/agent/memory/embed');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const WORKSPACE    = path.join(OPENCLAW_DIR, 'workspaces/default');
const DEFAULT_DIRS = ['memory', 'results'];

function parseArgs(argv) {
  const out = { dryRun: false, force: false, dirs: DEFAULT_DIRS, throttleMs: 30_000 };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') out.dryRun = true;
    else if (a === '--force') out.force = true;
    else if (a === '--dir')   out.dirs = [argv[++i]];
    else if (a === '--throttle-ms') out.throttleMs = parseInt(argv[++i], 10);
  }
  return out;
}

async function dryRun(rootDir) {
  let files = 0;
  let chunks = 0;
  let totalChars = 0;
  function walk(d) {
    if (!fs.existsSync(d)) return;
    for (const ent of fs.readdirSync(d, { withFileTypes: true })) {
      if (ent.name.startsWith('.')) continue;
      if (ent.name === '_templates') continue;
      const full = path.join(d, ent.name);
      if (ent.isDirectory()) walk(full);
      else if (ent.isFile() && ent.name.endsWith('.md')) {
        const text = fs.readFileSync(full, 'utf8');
        const { body } = parseFrontmatter(text);
        const cs = chunkBody(body);
        files++;
        chunks += cs.length;
        totalChars += cs.reduce((s, c) => s + c.text.length, 0);
        console.log(`  ${cs.length}× ${path.relative(WORKSPACE, full)} (${(text.length / 1024).toFixed(1)} KB)`);
      }
    }
  }
  walk(rootDir);
  return { files, chunks, totalChars };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  const mode = opts.dryRun ? 'DRY-RUN' : (opts.force ? 'FORCE' : 'INCREMENTAL');
  console.log(`[backfill] mode=${mode} workspace=${WORKSPACE} dirs=[${opts.dirs.join(', ')}] throttle=${opts.throttleMs}ms`);

  if (!opts.dryRun && !process.env.VOYAGE_API_KEY) {
    console.error('[backfill] VOYAGE_API_KEY not set. Add it to .env and re-run.');
    process.exit(2);
  }
  // Inter-call throttle to stay under voyage's 3 RPM free-tier ceiling.
  // Override with --throttle-ms 0 once paid-tier limits apply.
  process.env.VOYAGE_INTER_CALL_MS = String(opts.throttleMs);

  let totFiles = 0, totWritten = 0, totSkipped = 0, totErrors = 0, totChunksDryRun = 0, totCharsDryRun = 0;
  for (const sub of opts.dirs) {
    const dir = path.join(WORKSPACE, sub);
    console.log(`\n[backfill] scanning ${dir}`);
    if (opts.dryRun) {
      const r = await dryRun(dir);
      console.log(`  → ${r.files} files, ${r.chunks} chunks, ${(r.totalChars / 1024).toFixed(1)} KB total`);
      totFiles        += r.files;
      totChunksDryRun += r.chunks;
      totCharsDryRun  += r.totalChars;
    } else {
      const r = await embedDirectory({ workspace: WORKSPACE, dir, force: opts.force });
      console.log(`  → ${r.files} files: written=${r.written} skipped=${r.skipped} errors=${r.errors}`);
      totFiles   += r.files;
      totWritten += r.written;
      totSkipped += r.skipped;
      totErrors  += r.errors;
    }
  }

  console.log('\n[backfill] summary:');
  if (opts.dryRun) {
    const tokens = Math.ceil(totCharsDryRun * 0.25);
    const costUsd = (tokens / 1_000_000) * 0.06;
    console.log(`  files=${totFiles} chunks=${totChunksDryRun}`);
    console.log(`  est tokens=${tokens.toLocaleString()} est cost=$${costUsd.toFixed(4)} (Voyage voyage-3 list)`);
  } else {
    console.log(`  files=${totFiles} written=${totWritten} skipped=${totSkipped} errors=${totErrors}`);
  }
  process.exit(totErrors > 0 ? 1 : 0);
}

main().catch((err) => { console.error('[backfill] fatal:', err); process.exit(2); });
