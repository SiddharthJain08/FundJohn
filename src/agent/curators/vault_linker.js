'use strict';

/**
 * vault_linker.js — Phase 8 of the Saturday brain.
 *
 * Writes Obsidian-vault notes that link papers ↔ strategies ↔ data categories.
 * Every write honors the closed-enum frontmatter taxonomy from
 * `workspaces/default/_templates/README.md` and the `fundjohn:obsidian-link`
 * skill rules:
 *
 *   - YAML frontmatter is mandatory.
 *   - `type` is closed: paper | strategy | position | thesis_checkin |
 *     weekly_review | morning_note | initiating.
 *   - Tags are written WITHOUT the `#` prefix in YAML.
 *   - Use [[wikilinks]], never relative paths.
 *
 * The functions are best-effort — if a write fails (permission, disk full,
 * etc.) we log and return null rather than crash the brain. The vault is a
 * *side-effect* of the run; the DB is the source of truth.
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR ||
  path.join(__dirname, '..', '..', '..');
const VAULT_ROOT = path.join(OPENCLAW_DIR, 'workspaces', 'default');

const PAPERS_DIR        = path.join(VAULT_ROOT, 'papers');
const PAPERS_DEFERRED   = path.join(VAULT_ROOT, 'papers', '_deferred');
const STRATEGY_NOTES    = path.join(VAULT_ROOT, 'strategy-notes');
const SATURDAY_SUMMARY  = path.join(VAULT_ROOT, 'results', 'saturday-brain');

// Closed enum from the obsidian-link skill — keep this in sync with
// _templates/README.md if the operator extends it.
const ALLOWED_TYPES = new Set([
  'paper', 'strategy', 'position', 'thesis_checkin',
  'weekly_review', 'morning_note', 'initiating',
]);
const ALLOWED_FACTORS = new Set([
  'momentum', 'value', 'quality', 'volatility', 'reversal',
  'sentiment', 'macro', 'event', 'other',
]);
const ALLOWED_ASSET_CLASSES = new Set([
  'equities', 'options', 'futures', 'fx', 'rates', 'crypto', 'multi',
]);

function _slugify(s, maxLen = 80) {
  return String(s || 'untitled')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, maxLen) || 'untitled';
}

function _today() {
  return new Date().toISOString().slice(0, 10);
}

function _isoDate(d) {
  if (!d) return null;
  if (typeof d === 'string') return d.slice(0, 10);
  try { return new Date(d).toISOString().slice(0, 10); } catch (_) { return null; }
}

function _ensureDir(dir) {
  try { fs.mkdirSync(dir, { recursive: true }); } catch (_) {}
}

/**
 * Produce a YAML frontmatter block from a plain JS object. Respects the
 * obsidian-link skill rules: no `#` prefix on tags, arrays inline when short,
 * one-key-per-line otherwise. Best-effort — values are coerced to safe YAML
 * scalars (no embedded quotes inside quoted strings, no multi-line strings).
 */
function _frontmatter(obj) {
  const lines = ['---'];
  for (const [k, v] of Object.entries(obj)) {
    if (v == null) continue;
    if (Array.isArray(v)) {
      if (v.length === 0) {
        lines.push(`${k}: []`);
      } else {
        const items = v.map(x => {
          const s = String(x).replace(/"/g, "'");
          if (/^\[\[.*\]\]$/.test(s)) return s;          // wikilink
          if (/[:#]/.test(s)) return `"${s}"`;
          return s;
        });
        // Obsidian convention: arrays of wikilinks render as comma-separated
        // bare values WITHOUT YAML `[ ]` wrappers (Dataview parses them; the
        // wrapper would produce nested-bracket display like `[[[name]]]`).
        // Anything else uses the standard inline YAML array.
        const allWikilinks = items.every(s => /^\[\[.*\]\]$/.test(s));
        if (allWikilinks) {
          lines.push(`${k}: ${items.join(', ')}`);
        } else {
          lines.push(`${k}: [${items.join(', ')}]`);
        }
      }
    } else if (typeof v === 'object') {
      // Nested objects flatten to JSON one-liners — Obsidian Dataview
      // tolerates this and our skill doesn't gate on nested keys.
      lines.push(`${k}: ${JSON.stringify(v)}`);
    } else {
      const s = String(v).replace(/"/g, "'");
      if (/[:#\n]/.test(s) || s === '' || /^\d/.test(s) === false && /^[+-]?\d/.test(s)) {
        lines.push(`${k}: "${s}"`);
      } else {
        lines.push(`${k}: ${s}`);
      }
    }
  }
  lines.push('---');
  return lines.join('\n');
}

function _wikilinkList(names) {
  return (names || []).filter(Boolean).map(n => `[[${n}]]`);
}

/**
 * Write a paper note. Returns the absolute path on success, null on failure.
 * Idempotent — overwrites the existing note for the same slug so re-runs
 * stay current with new bucket / tier / linked-strategy info.
 *
 * @param {object} paper          row from research_corpus
 * @param {object} rating         from curated_candidates: {confidence,
 *                                predicted_bucket, implementability_score}
 * @param {object} hunterResult   from research_candidates.hunter_result_json
 *                                (may be null if paperhunter hasn't run yet)
 * @param {string} dataTier       'A' | 'B' | 'C'
 * @param {object} opts
 *   - linkedStrategies: array of strategy_ids that descended from this paper
 *   - linkedDataCategories: array of data_type strings
 */
function writePaperNote(paper, rating, hunterResult, dataTier, opts = {}) {
  _ensureDir(PAPERS_DIR);
  const slug = _slugify(`${_isoDate(paper.published_date) || ''}-${paper.title}`);
  const file = path.join(PAPERS_DIR, `${slug}.md`);

  const factor = opts.factor && ALLOWED_FACTORS.has(opts.factor) ? opts.factor : 'other';
  const asset  = opts.asset_class && ALLOWED_ASSET_CLASSES.has(opts.asset_class) ? opts.asset_class : 'equities';
  const status = (() => {
    const b = rating?.predicted_bucket;
    if (b === 'high' || b === 'implementable_candidate') return 'promoted';
    if (b === 'reject') return 'rejected';
    return 'candidate';
  })();

  const fm = {
    type:                  'paper',
    title:                 paper.title || '',
    authors:               paper.authors || [],
    source:                paper.source || 'unknown',
    url:                   paper.source_url,
    paper_id:              paper.paper_id,
    venue:                 paper.venue,
    date_published:        _isoDate(paper.published_date),
    date_ingested:         _isoDate(paper.ingested_at) || _today(),
    asset_class:           asset,
    factor,
    confidence:            rating?.confidence != null ? Number(rating.confidence) : null,
    predicted_bucket:      rating?.predicted_bucket || null,
    implementability_score:rating?.implementability_score != null ? Number(rating.implementability_score) : null,
    data_tier:             dataTier || null,
    status,
    linked_strategies:     _wikilinkList(opts.linkedStrategies),
    linked_data_categories: opts.linkedDataCategories || [],
    tags: [
      'paper',
      `factor/${factor}`,
      `asset/${asset}`,
      `status/${status}`,
      ...(dataTier ? [`tier/${dataTier}`] : []),
    ],
  };

  const body = [
    _frontmatter(fm),
    '',
    `# ${paper.title || '(untitled)'}`,
    '',
    `**URL:** [${paper.source_url}](${paper.source_url})`,
    '',
    '## TL;DR',
    (paper.abstract || '').slice(0, 800).replace(/\s+/g, ' ').trim(),
    '',
    '## Curator rating',
    `- predicted_bucket: ${rating?.predicted_bucket || '—'}`,
    `- confidence: ${rating?.confidence ?? '—'}`,
    `- implementability_score: ${rating?.implementability_score ?? '—'}`,
    `- data_tier: ${dataTier || '—'}`,
    '',
    '## PaperHunter extraction',
    hunterResult
      ? `- hypothesis: ${hunterResult.hypothesis_one_liner || '—'}\n` +
        `- regime_applicability: ${(hunterResult.regime_applicability || []).join(', ')}\n` +
        `- min_lookback_required: ${hunterResult.min_lookback_required ?? '—'}\n` +
        `- required_data: ${(hunterResult.data_requirements?.required || []).join(', ')}`
      : '_pending — paperhunter has not been spawned for this candidate yet._',
    '',
    '## Linked strategies',
    (opts.linkedStrategies && opts.linkedStrategies.length)
      ? _wikilinkList(opts.linkedStrategies).join('\n')
      : '<!-- agents add [[wikilinks]] here as papers map to live strategies -->',
    '',
  ].join('\n');

  try {
    fs.writeFileSync(file, body);
    return file;
  } catch (e) {
    return null;
  }
}

/**
 * Write a Tier-C deferred paper note. These live in papers/_deferred/ so
 * Obsidian's graph view groups them visually as "future provider unlock"
 * candidates.
 */
function writeDeferredPaperNote(paper, missingColumns, unlockEstimate, opts = {}) {
  _ensureDir(PAPERS_DEFERRED);
  const slug = _slugify(`${_isoDate(paper.published_date) || ''}-${paper.title}`);
  const file = path.join(PAPERS_DEFERRED, `${slug}.md`);

  const fm = {
    type:                  'paper',
    title:                 paper.title || '',
    authors:               paper.authors || [],
    source:                paper.source || 'unknown',
    url:                   paper.source_url,
    paper_id:              paper.paper_id,
    date_published:        _isoDate(paper.published_date),
    date_ingested:         _isoDate(paper.ingested_at) || _today(),
    asset_class:           opts.asset_class || 'equities',
    factor:                opts.factor || 'other',
    data_tier:             'C',
    status:                'deferred',
    linked_data_categories: missingColumns || [],
    unlock_provider_estimate: unlockEstimate || '',
    tags: ['paper', 'status/deferred', 'tier/C'],
  };

  const body = [
    _frontmatter(fm),
    '',
    `# ${paper.title || '(untitled)'} — DEFERRED (Tier C)`,
    '',
    `**URL:** [${paper.source_url}](${paper.source_url})`,
    '',
    '## Why deferred',
    `Required columns NOT in our current data stack: **${(missingColumns || []).join(', ') || '?'}**.`,
    '',
    '## Provider unlock estimate',
    unlockEstimate || '_no notes_',
    '',
    '## TL;DR',
    (paper.abstract || '').slice(0, 600).replace(/\s+/g, ' ').trim(),
    '',
  ].join('\n');

  try {
    fs.writeFileSync(file, body);
    return file;
  } catch (e) {
    return null;
  }
}

/**
 * Write a strategy note backlinking to source paper(s) and required data
 * categories. Idempotent on strategy_id.
 */
function writeStrategyNote(strategyId, manifestEntry, hunterResult, parentPapers, opts = {}) {
  _ensureDir(STRATEGY_NOTES);
  const file = path.join(STRATEGY_NOTES, `${strategyId}.md`);

  const factor = opts.factor && ALLOWED_FACTORS.has(opts.factor) ? opts.factor : 'other';
  const asset  = opts.asset_class && ALLOWED_ASSET_CLASSES.has(opts.asset_class) ? opts.asset_class : 'equities';
  const state  = manifestEntry?.state || 'candidate';

  const fm = {
    type:                  'strategy',
    strategy_id:           strategyId,
    name:                  manifestEntry?.metadata?.class || strategyId,
    state,
    state_since:           _isoDate(manifestEntry?.state_since),
    canonical_file:        manifestEntry?.metadata?.canonical_file,
    class_name:            manifestEntry?.metadata?.class,
    asset_class:           asset,
    factor,
    parent_papers:         _wikilinkList(parentPapers),
    required_data:         (hunterResult?.data_requirements?.required || []),
    optional_data:         (hunterResult?.data_requirements?.optional || []),
    regime_applicability:  hunterResult?.regime_applicability || [],
    min_lookback_required: hunterResult?.min_lookback_required ?? null,
    last_touched:          _today(),
    tags: [
      'strategy',
      `strategy/${strategyId}`,
      `state/${state}`,
      `factor/${factor}`,
      `asset/${asset}`,
    ],
  };

  const body = [
    _frontmatter(fm),
    '',
    `# ${strategyId}`,
    '',
    '## Hypothesis',
    hunterResult?.hypothesis_one_liner || manifestEntry?.metadata?.description || '_no hypothesis recorded_',
    '',
    '## Signal definition',
    '```',
    hunterResult?.signal_formula_pseudocode || '<no formula extracted>',
    '```',
    '',
    '## Source papers',
    parentPapers && parentPapers.length
      ? _wikilinkList(parentPapers).join('\n')
      : '_none — likely strategist-ideator generated or hand-added_',
    '',
    '## Required data',
    (hunterResult?.data_requirements?.required || []).map(c => `- ${c}`).join('\n') || '_none recorded_',
    '',
    '## Lifecycle',
    `- state: **${state}**`,
    `- state_since: ${_isoDate(manifestEntry?.state_since) || '?'}`,
    '',
  ].join('\n');

  try {
    fs.writeFileSync(file, body);
    return file;
  } catch (e) {
    return null;
  }
}

/**
 * Write the Saturday-brain run summary. Type `weekly_review` is the closest
 * fit in the closed enum — a weekly research aggregate. Cross-links every
 * notable paper + strategy of the run via wikilinks so Obsidian's graph
 * view shows the full week's brain output as connected.
 */
function writeRunSummary(runRow, results, opts = {}) {
  _ensureDir(SATURDAY_SUMMARY);
  const dateStr = _isoDate(runRow.started_at) || _today();
  const file = path.join(SATURDAY_SUMMARY, `${dateStr}.md`);

  const fm = {
    type:               'weekly_review',
    title:              `Saturday brain — ${dateStr}`,
    run_id:             runRow.run_id,
    date:               dateStr,
    cost_usd:           runRow.cost_usd,
    sources_discovered: runRow.sources_discovered,
    papers_ingested:    runRow.papers_ingested,
    papers_rated:       runRow.papers_rated,
    implementable_n:    runRow.implementable_n,
    paperhunters_run:   runRow.paperhunters_run,
    tier_a_count:       runRow.tier_a_count,
    tier_b_count:       runRow.tier_b_count,
    tier_c_count:       runRow.tier_c_count,
    coded_synchronous:  runRow.coded_synchronous,
    tags: ['weekly-review', 'saturday-brain'],
  };

  const promotedLinks = (results?.tier_a_strategies || []).map(s => `[[${s}]]`);
  const stagedLinks   = (results?.tier_b_strategies || []).map(s => `[[${s}]]`);

  const body = [
    _frontmatter(fm),
    '',
    `# Saturday brain — ${dateStr}`,
    '',
    `**Cost:** $${(runRow.cost_usd || 0).toFixed(2)} · **Status:** ${runRow.status}`,
    '',
    '## Pipeline totals',
    `- Sources discovered: ${runRow.sources_discovered ?? 0}`,
    `- Papers ingested: ${runRow.papers_ingested ?? 0}`,
    `- Papers rated: ${runRow.papers_rated ?? 0}`,
    `- Implementable candidates: ${runRow.implementable_n ?? 0}`,
    `- PaperHunters run: ${runRow.paperhunters_run ?? 0}`,
    '',
    '## Tier breakdown',
    `- **Tier A (synchronous code+backtest):** ${runRow.tier_a_count ?? 0} → coded ${runRow.coded_synchronous ?? 0}`,
    `- **Tier B (staging — awaiting operator approval):** ${runRow.tier_b_count ?? 0}`,
    `- **Tier C (deferred — provider unlock needed):** ${runRow.tier_c_count ?? 0}`,
    '',
    '## Tier-A strategies promoted to PAPER',
    promotedLinks.length ? promotedLinks.join('\n') : '_none_',
    '',
    '## Tier-B strategies in STAGING',
    stagedLinks.length ? stagedLinks.join('\n') : '_none_',
    '',
  ].join('\n');

  try {
    fs.writeFileSync(file, body);
    return file;
  } catch (e) {
    return null;
  }
}

module.exports = {
  writePaperNote,
  writeDeferredPaperNote,
  writeStrategyNote,
  writeRunSummary,
  ALLOWED_TYPES,
  VAULT_ROOT,
  PAPERS_DIR,
  PAPERS_DEFERRED,
  STRATEGY_NOTES,
  SATURDAY_SUMMARY,
};
