#!/usr/bin/env node
/**
 * Frontmatter backfill for existing strategy/results notes.
 *
 *   node bin/backfill-frontmatter.js              # apply
 *   node bin/backfill-frontmatter.js --dry-run    # preview only
 *
 * Why: notes pre-dating the obsidian-link skill have no YAML frontmatter,
 * so memory_chunks.note_type / tags / tickers are all NULL — type-filtered
 * pgvector retrieval can't kick in. This walks workspaces/default/results/
 * and infers a frontmatter block from filename + content heuristics, then
 * prepends it. mtime change triggers re-embed on the next backfill run.
 *
 * Heuristics (path-based; closed enum from _templates/README.md):
 *   results/strategies/<id>-deployed-*.md   → type: strategy, status: live
 *   results/strategies/<id>-*.md            → type: strategy
 *   results/strategies/review-*.md          → type: strategy_review (NOT in
 *                                              closed enum — uses 'strategy'
 *                                              with tag #review)
 *   results/strategies/session-*.md         → type: strategy (tag #session)
 *   results/strategies/strategist-*-findings.md → type: strategy (tag #ideation)
 *   results/initiating-<TICKER>.md          → type: initiating, ticker
 *   results/thesis-<TICKER>.md              → type: thesis_checkin, ticker
 *
 * memory/*.md left alone — they're rolling operational logs, not navigable
 * notes.
 *
 * Idempotent: skips files that already have `^---\n` frontmatter.
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const RESULTS_DIR  = path.join(OPENCLAW_DIR, 'workspaces/default/results');

function inferFrontmatter(absPath) {
  const rel  = path.relative(RESULTS_DIR, absPath);
  const base = path.basename(absPath, '.md');
  const today = new Date().toISOString().slice(0, 10);

  // results/initiating-AAPL.md
  let m = base.match(/^initiating-([A-Z][A-Z0-9.\-]{0,9})$/);
  if (m) {
    return {
      type: 'initiating',
      ticker: m[1],
      tickers: [m[1]],
      tags: ['#initiating', `#ticker/${m[1]}`],
      backfilled: today,
    };
  }

  // results/thesis-AAPL.md
  m = base.match(/^thesis-([A-Z][A-Z0-9.\-]{0,9})$/);
  if (m) {
    return {
      type: 'thesis_checkin',
      ticker: m[1],
      tickers: [m[1]],
      tags: ['#thesis-checkin', `#ticker/${m[1]}`],
      backfilled: today,
    };
  }

  // results/strategies/...
  if (rel.startsWith('strategies/')) {
    // review-S9_dual_momentum-2026-04-13.md
    m = base.match(/^review-([A-Z]+\d*[A-Za-z0-9_]*)-(\d{4}-\d{2}-\d{2})$/);
    if (m) {
      return {
        type: 'strategy',
        strategy_id: m[1],
        review_date: m[2],
        tags: ['#strategy', '#review', `#strategy/${m[1]}`],
        backfilled: today,
      };
    }
    // session-XYZ-report-YYYY-MM-DD.md  OR  session-s4-YYYY-MM-DD.md
    m = base.match(/^session-(.+?)-(\d{4}-\d{2}-\d{2})$/);
    if (m) {
      return {
        type: 'strategy',
        session_id: m[1],
        date: m[2],
        tags: ['#strategy', '#session'],
        backfilled: today,
      };
    }
    // strategist-session-YYYY-MM-DD-findings.md
    m = base.match(/^strategist-session-(\d{4}-\d{2}-\d{2})-findings$/);
    if (m) {
      return {
        type: 'strategy',
        session_date: m[1],
        tags: ['#strategy', '#ideation'],
        backfilled: today,
      };
    }
    // <ID>-deployed-YYYY-MM-DD.md  OR  <ID>-YYYY-MM-DD.md
    m = base.match(/^([A-Z]+\d*[A-Za-z0-9_]*)-(deployed-)?(\d{4}-\d{2}-\d{2})$/);
    if (m) {
      const fm = {
        type: 'strategy',
        strategy_id: m[1],
        date: m[3],
        tags: ['#strategy', `#strategy/${m[1]}`],
        backfilled: today,
      };
      if (m[2]) {
        fm.status = 'deployed';
        fm.tags.push('#state/live');
      }
      return fm;
    }
    // catch-all: tagged as strategy but no specific id
    return { type: 'strategy', tags: ['#strategy'], backfilled: today };
  }

  // Anything else under results/ that doesn't match — skip
  return null;
}

function emitYaml(fm) {
  const lines = ['---'];
  for (const [k, v] of Object.entries(fm)) {
    if (Array.isArray(v)) {
      lines.push(`${k}: [${v.map((x) => x).join(', ')}]`);
    } else {
      lines.push(`${k}: ${v}`);
    }
  }
  lines.push('---', '');
  return lines.join('\n');
}

function walk(dir) {
  const out = [];
  if (!fs.existsSync(dir)) return out;
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    if (ent.name.startsWith('.') || ent.name === '_templates') continue;
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) out.push(...walk(full));
    else if (ent.isFile() && ent.name.endsWith('.md')) out.push(full);
  }
  return out;
}

function main() {
  const dryRun = process.argv.includes('--dry-run');
  const files = walk(RESULTS_DIR);
  let modified = 0;
  let skipped = 0;
  let unmatched = 0;

  for (const f of files) {
    const text = fs.readFileSync(f, 'utf8');
    if (text.startsWith('---\n') || text.startsWith('---\r\n')) {
      skipped++;
      continue;
    }
    const fm = inferFrontmatter(f);
    if (!fm) {
      console.log(`  ? UNMATCHED ${path.relative(OPENCLAW_DIR, f)}`);
      unmatched++;
      continue;
    }
    const yaml = emitYaml(fm);
    console.log(`  ${dryRun ? 'DRY' : '+'} ${path.relative(OPENCLAW_DIR, f)}: ${fm.type}${fm.strategy_id ? ' / ' + fm.strategy_id : ''}${fm.ticker ? ' / ' + fm.ticker : ''}`);
    if (!dryRun) {
      fs.writeFileSync(f, yaml + text, 'utf8');
    }
    modified++;
  }

  console.log(`\n[backfill-frontmatter] ${dryRun ? '(DRY-RUN) ' : ''}files=${files.length} modified=${modified} skipped(already-has-fm)=${skipped} unmatched=${unmatched}`);
}

main();
