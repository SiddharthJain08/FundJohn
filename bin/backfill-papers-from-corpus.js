#!/usr/bin/env node
/**
 * Backfill papers/ from research_corpus → markdown notes.
 *
 *   node bin/backfill-papers-from-corpus.js                  # all papers
 *   node bin/backfill-papers-from-corpus.js --dry-run        # preview
 *   node bin/backfill-papers-from-corpus.js --limit 50       # cap to N
 *   node bin/backfill-papers-from-corpus.js --min-confidence 0.7
 *   node bin/backfill-papers-from-corpus.js --promoted-only  # paper_truth_flags.promoted=true
 *
 * Joins research_corpus + curated_candidates (highest-confidence eval per
 * paper) + paper_truth_flags. Emits one .md per paper with valid YAML
 * frontmatter (tags WITHOUT # prefix per the bug-fix in c04e17c).
 *
 * Filename: {YYYY-MM-DD-or-noyear}-{slug}-{short-id}.md
 * Idempotent: skips files whose mtime > paper.ingested_at unless --force.
 *
 * Cost: file-IO only, no Voyage calls. Embed via
 *       node bin/backfill-memory-chunks.js --dir papers
 * once you decide which subset to vector-index.
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const fs   = require('fs');
const path = require('path');
const { query } = require('../src/database/postgres');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const PAPERS_DIR   = path.join(OPENCLAW_DIR, 'workspaces/default/papers');

function parseArgs(argv) {
  const out = { dryRun: false, force: false, limit: null, minConfidence: null, promotedOnly: false };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--dry-run') out.dryRun = true;
    else if (a === '--force') out.force = true;
    else if (a === '--limit') out.limit = parseInt(argv[++i], 10);
    else if (a === '--min-confidence') out.minConfidence = parseFloat(argv[++i]);
    else if (a === '--promoted-only') out.promotedOnly = true;
  }
  return out;
}

function slugify(s, max = 60) {
  return String(s || '')
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, '')
    .trim()
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .slice(0, max);
}

function shortId(uuid) {
  return String(uuid || '').replace(/-/g, '').slice(0, 8);
}

function inferAssetClass(text) {
  const t = String(text || '').toLowerCase();
  if (/\boption|implied vol|\bvix\b|\biv\b/.test(t)) return 'options';
  if (/\bfutures?\b|\bcommodit/.test(t)) return 'futures';
  if (/\bfx\b|currency|forex|exchange rate/.test(t)) return 'fx';
  if (/yield curve|treasury|bond|fixed income/.test(t)) return 'rates';
  if (/crypto|bitcoin|ethereum|token/.test(t)) return 'crypto';
  return 'equities';
}

function inferFactor(predictedBucket, text) {
  if (predictedBucket) {
    const b = String(predictedBucket).toLowerCase();
    if (b.includes('momentum')) return 'momentum';
    if (b.includes('value'))    return 'value';
    if (b.includes('quality'))  return 'quality';
    if (b.includes('vol'))      return 'volatility';
    if (b.includes('reversal')) return 'reversal';
    if (b.includes('sentiment'))return 'sentiment';
    if (b.includes('macro'))    return 'macro';
    if (b.includes('event'))    return 'event';
  }
  const t = String(text || '').toLowerCase();
  if (/momentum/.test(t)) return 'momentum';
  if (/value\b|book.to.market|earnings yield/.test(t)) return 'value';
  if (/quality|profitability|gross profitability/.test(t)) return 'quality';
  if (/volatility|vix|implied vol|realized vol/.test(t)) return 'volatility';
  if (/reversal|short.term reversal/.test(t)) return 'reversal';
  if (/sentiment|news|tweet/.test(t)) return 'sentiment';
  if (/macro|monetary|inflation|gdp/.test(t)) return 'macro';
  if (/event|earnings announcement|insider/.test(t)) return 'event';
  return 'other';
}

function emitYaml(fm) {
  const lines = ['---'];
  for (const [k, v] of Object.entries(fm)) {
    if (v == null) continue;
    if (Array.isArray(v)) {
      // YAML inline array; tag values are bare (no #) per the no-comment-collision convention
      const safe = v.map((x) => {
        const s = String(x);
        // Quote if the string contains a special char that would break inline YAML
        return /[:,\[\]{}#"&*!|>'%@`?]/.test(s) ? JSON.stringify(s) : s;
      });
      lines.push(`${k}: [${safe.join(', ')}]`);
    } else if (typeof v === 'string' && /[:#]/.test(v)) {
      lines.push(`${k}: ${JSON.stringify(v)}`);
    } else {
      lines.push(`${k}: ${v}`);
    }
  }
  lines.push('---', '');
  return lines.join('\n');
}

function renderPaper(paper, eval_, flags) {
  const dateStr = paper.published_date ? new Date(paper.published_date).toISOString().slice(0, 10) : 'noyear';
  const slug = slugify(paper.title);
  const sid = shortId(paper.paper_id);
  const filename = `${dateStr}-${slug}-${sid}.md`;

  const factor = inferFactor(eval_?.predicted_bucket, [paper.title, paper.abstract].join(' '));
  const assetClass = inferAssetClass([paper.title, paper.abstract].join(' '));
  const tags = ['paper'];
  if (factor && factor !== 'other') tags.push(`factor/${factor}`);
  if (assetClass) tags.push(`asset/${assetClass}`);
  if (flags?.promoted) tags.push('status/promoted');
  else if (flags?.hunter_rejected || flags?.backtest_failed) tags.push('status/rejected');
  else if (eval_?.confidence >= 0.7) tags.push('status/candidate');

  const fm = {
    type: 'paper',
    paper_id: paper.paper_id,
    title: paper.title || '(untitled)',
    source: paper.source || null,
    venue: paper.venue || null,
    url: paper.source_url || null,
    published_date: paper.published_date ? new Date(paper.published_date).toISOString().slice(0, 10) : null,
    date_ingested: paper.ingested_at ? new Date(paper.ingested_at).toISOString().slice(0, 10) : null,
    authors: Array.isArray(paper.authors) ? paper.authors.slice(0, 8) : [],
    asset_class: assetClass,
    factor,
    confidence: eval_?.confidence != null ? Number(eval_.confidence) : null,
    predicted_bucket: eval_?.predicted_bucket || null,
    promoted: flags?.promoted ?? null,
    backtest_passed: flags?.backtest_passed ?? null,
    backtest_failed: flags?.backtest_failed ?? null,
    keywords: Array.isArray(paper.keywords) ? paper.keywords.slice(0, 8) : [],
    tags,
  };

  const sections = [];
  sections.push(`# ${paper.title || '(untitled paper)'}`);
  if (paper.authors?.length) sections.push(`**Authors:** ${paper.authors.slice(0, 8).join(', ')}${paper.authors.length > 8 ? ', et al.' : ''}`);
  if (paper.venue) sections.push(`**Venue:** ${paper.venue}`);
  if (paper.source_url) sections.push(`**URL:** [${paper.source_url}](${paper.source_url})`);
  sections.push('');

  if (paper.abstract) {
    sections.push('## Abstract');
    sections.push(paper.abstract);
    sections.push('');
  }

  if (eval_?.reasoning) {
    sections.push('## Curator notes');
    sections.push(eval_.reasoning);
    sections.push('');
    if (eval_.predicted_failure_modes?.length) {
      sections.push('**Predicted failure modes:** ' + eval_.predicted_failure_modes.join(', '));
      sections.push('');
    }
  }

  if (flags) {
    const lifecycle = [];
    if (flags.hunter_passed)    lifecycle.push('hunter ✓');
    if (flags.hunter_rejected)  lifecycle.push('hunter ✗');
    if (flags.classified_ready) lifecycle.push('classified');
    if (flags.validated)        lifecycle.push('validated');
    if (flags.backtest_passed)  lifecycle.push('backtest ✓');
    if (flags.backtest_failed)  lifecycle.push('backtest ✗');
    if (flags.promoted)         lifecycle.push('PROMOTED');
    if (lifecycle.length) {
      sections.push('## Lifecycle');
      sections.push('- ' + lifecycle.join(' → '));
      sections.push('');
    }
  }

  sections.push('## Linked strategies');
  sections.push('<!-- agents add [[wikilinks]] here as papers map to live strategies -->');
  sections.push('');

  return { filename, body: emitYaml(fm) + sections.join('\n') };
}

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  console.log(`[backfill-papers] dryRun=${opts.dryRun} limit=${opts.limit ?? '∞'} minConfidence=${opts.minConfidence ?? 'none'} promotedOnly=${opts.promotedOnly}`);

  // Build the SQL with optional filters
  const whereParts = [];
  const sqlParams = [];
  if (opts.minConfidence != null) {
    whereParts.push(`coalesce(eval.confidence, 0) >= $${sqlParams.length + 1}`);
    sqlParams.push(opts.minConfidence);
  }
  if (opts.promotedOnly) {
    whereParts.push('coalesce(flags.promoted, false) = true');
  }
  const whereClause = whereParts.length ? 'WHERE ' + whereParts.join(' AND ') : '';
  const limitClause = opts.limit ? `LIMIT ${opts.limit}` : '';

  const sql = `
    SELECT
      p.paper_id, p.source, p.source_url, p.title, p.abstract, p.authors,
      p.venue, p.published_date, p.keywords, p.ingested_at,
      eval.confidence, eval.predicted_bucket, eval.reasoning, eval.predicted_failure_modes,
      flags.hunter_passed, flags.hunter_rejected, flags.classified_ready, flags.validated,
      flags.backtest_passed, flags.backtest_failed, flags.promoted
    FROM research_corpus p
    LEFT JOIN LATERAL (
      SELECT confidence, predicted_bucket, reasoning, predicted_failure_modes
      FROM curated_candidates ce
      WHERE ce.paper_id = p.paper_id
      ORDER BY ce.confidence DESC NULLS LAST, ce.created_at DESC
      LIMIT 1
    ) eval ON true
    LEFT JOIN paper_truth_flags flags ON flags.paper_id = p.paper_id
    ${whereClause}
    ORDER BY coalesce(eval.confidence, 0) DESC, p.ingested_at DESC
    ${limitClause}
  `;
  const { rows } = await query(sql, sqlParams);
  console.log(`[backfill-papers] fetched ${rows.length} papers`);

  if (!opts.dryRun) fs.mkdirSync(PAPERS_DIR, { recursive: true });

  let written = 0, skipped = 0, errors = 0;
  for (const row of rows) {
    try {
      const { filename, body } = renderPaper(row, row, row);
      const fullPath = path.join(PAPERS_DIR, filename);
      if (!opts.force && !opts.dryRun && fs.existsSync(fullPath)) {
        const stat = fs.statSync(fullPath);
        if (row.ingested_at && stat.mtime >= new Date(row.ingested_at)) {
          skipped++;
          continue;
        }
      }
      if (opts.dryRun) {
        console.log(`  DRY ${filename} (conf=${row.confidence ?? 'n/a'} promoted=${row.promoted ?? 'n/a'})`);
      } else {
        fs.writeFileSync(fullPath, body, 'utf8');
      }
      written++;
    } catch (err) {
      console.warn(`  ERR paper_id=${row.paper_id}: ${err.message}`);
      errors++;
    }
  }

  console.log(`\n[backfill-papers] ${opts.dryRun ? '(DRY-RUN) ' : ''}written=${written} skipped=${skipped} errors=${errors}`);
  process.exit(errors > 0 ? 1 : 0);
}

main().catch((err) => { console.error('[backfill-papers] fatal:', err); process.exit(2); });
