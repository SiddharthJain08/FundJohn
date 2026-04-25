#!/usr/bin/env node
/**
 * Backfill strategy-notes/ — one memo per manifest.json strategy.
 *
 *   node bin/backfill-strategy-memos.js
 *   node bin/backfill-strategy-memos.js --dry-run
 *   node bin/backfill-strategy-memos.js --force      # overwrite existing
 *
 * Joins manifest.json (state, state_since, history, metadata) with the
 * Python source's leading docstring (when canonical_file resolves). Emits
 * one .md per strategy at workspaces/default/strategy-notes/<id>.md with
 * valid YAML frontmatter (no # in tags).
 *
 * Scope: 53 strategies. Cheap to embed afterward — run
 *   node bin/backfill-memory-chunks.js --dir strategy-notes
 * which will take ~3-4 minutes on free-tier voyage (3 RPM × ~60 chunks).
 */
'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '..', '.env') });

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR  = process.env.OPENCLAW_DIR || '/root/openclaw';
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
const IMPL_DIR      = path.join(OPENCLAW_DIR, 'src/strategies/implementations');
const NOTES_DIR     = path.join(OPENCLAW_DIR, 'workspaces/default/strategy-notes');

function parseArgs(argv) {
  const out = { dryRun: false, force: false };
  for (const a of argv) {
    if (a === '--dry-run') out.dryRun = true;
    if (a === '--force')   out.force = true;
  }
  return out;
}

/**
 * Pull the leading triple-quoted docstring from a Python file.
 * Returns the contents (without the """ delimiters) or null if absent.
 */
function readDocstring(filename) {
  if (!filename) return null;
  const candidates = [
    path.join(IMPL_DIR, filename),
    path.join(IMPL_DIR, filename.toLowerCase()),
    path.join(IMPL_DIR, filename.replace(/\.py$/, '').toLowerCase() + '.py'),
  ];
  let pyPath = candidates.find(fs.existsSync);
  if (!pyPath) return null;
  const text = fs.readFileSync(pyPath, 'utf8');
  const m = text.match(/^\s*"""([\s\S]*?)"""/m);
  return m ? m[1].trim() : null;
}

function inferFactor(text) {
  const t = String(text || '').toLowerCase();
  if (/momentum/.test(t)) return 'momentum';
  if (/value\b|book.to.market/.test(t)) return 'value';
  if (/quality|profitability/.test(t)) return 'quality';
  if (/volatility|iv|hv|vix/.test(t)) return 'volatility';
  if (/reversal|mean.revert/.test(t)) return 'reversal';
  if (/sentiment|news/.test(t)) return 'sentiment';
  if (/macro|monetary|inflation|cross.asset/.test(t)) return 'macro';
  if (/event|earnings|insider/.test(t)) return 'event';
  return 'other';
}

function inferAssetClass(text) {
  const t = String(text || '').toLowerCase();
  if (/option|max.pain|gamma|delta|theta|vega|implied vol/.test(t)) return 'options';
  if (/futures?\b|commodit/.test(t)) return 'futures';
  if (/yield|treasury|bond/.test(t)) return 'rates';
  if (/cross.asset|fx|currenc/.test(t)) return 'multi';
  return 'equities';
}

function emitYaml(fm) {
  const lines = ['---'];
  for (const [k, v] of Object.entries(fm)) {
    if (v == null) continue;
    if (Array.isArray(v)) {
      const safe = v.map((x) => {
        const s = String(x);
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

function renderMemo(id, entry) {
  const meta    = entry.metadata || {};
  const docstr  = readDocstring(meta.canonical_file);
  const blob    = [meta.description, docstr, id].filter(Boolean).join(' ');
  const factor  = inferFactor(blob);
  const asset   = inferAssetClass(blob);

  const tags = ['strategy', `strategy/${id}`, `state/${entry.state || 'unknown'}`];
  if (factor && factor !== 'other') tags.push(`factor/${factor}`);
  if (asset)  tags.push(`asset/${asset}`);

  const fm = {
    type: 'strategy',
    strategy_id: id,
    state: entry.state || null,
    state_since: entry.state_since ? String(entry.state_since).slice(0, 10) : null,
    canonical_file: meta.canonical_file || null,
    class_name: meta.class || null,
    factor,
    asset_class: asset,
    history_count: Array.isArray(entry.history) ? entry.history.length : 0,
    last_state_change: Array.isArray(entry.history) && entry.history.length
      ? String(entry.history[entry.history.length - 1].timestamp || '').slice(0, 10)
      : null,
    tags,
  };

  const sections = [];
  sections.push(`# ${id}`);
  if (meta.description) sections.push(`*${meta.description}*`);
  sections.push('');
  sections.push(`**State:** \`${entry.state || 'unknown'}\` since ${fm.state_since || '(unknown)'}`);
  if (meta.class) sections.push(`**Class:** \`${meta.class}\``);
  if (meta.canonical_file) {
    sections.push(`**Source:** \`src/strategies/implementations/${meta.canonical_file}\``);
  }
  sections.push('');

  if (docstr) {
    sections.push('## Strategy description (from source docstring)');
    sections.push('```');
    sections.push(docstr);
    sections.push('```');
    sections.push('');
  }

  if (Array.isArray(entry.history) && entry.history.length) {
    sections.push('## Lifecycle history');
    sections.push('| Date | From | To | Actor | Reason |');
    sections.push('| --- | --- | --- | --- | --- |');
    for (const h of entry.history.slice(-10)) {
      const date = String(h.timestamp || '').slice(0, 10);
      sections.push(`| ${date} | ${h.from_state || '?'} | ${h.to_state || '?'} | ${h.actor || '?'} | ${(h.reason || '').replace(/\|/g, '\\|').slice(0, 80)} |`);
    }
    sections.push('');
  }

  sections.push('## Parent papers');
  sections.push('<!-- agents add [[wikilinks]] here as papers map to this strategy -->');
  sections.push('');

  sections.push('## Recent reviews');
  sections.push('<!-- backlinks to results/strategies/review-' + id + '-* notes appear automatically in the Backlinks pane -->');
  sections.push('');

  return { filename: `${id}.md`, body: emitYaml(fm) + sections.join('\n') };
}

function main() {
  const opts = parseArgs(process.argv.slice(2));
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  const ids = Object.keys(manifest.strategies || {});
  console.log(`[backfill-strategy-memos] dryRun=${opts.dryRun} force=${opts.force} strategies=${ids.length}`);

  if (!opts.dryRun) fs.mkdirSync(NOTES_DIR, { recursive: true });

  let written = 0, skipped = 0, errors = 0;
  for (const id of ids) {
    try {
      const entry = manifest.strategies[id];
      const { filename, body } = renderMemo(id, entry);
      const fullPath = path.join(NOTES_DIR, filename);
      if (!opts.force && !opts.dryRun && fs.existsSync(fullPath)) {
        skipped++;
        continue;
      }
      if (opts.dryRun) {
        console.log(`  DRY ${filename} (state=${entry.state} factor=${inferFactor((entry.metadata||{}).description || '')})`);
      } else {
        fs.writeFileSync(fullPath, body, 'utf8');
        console.log(`  + ${filename}`);
      }
      written++;
    } catch (err) {
      console.warn(`  ERR ${id}: ${err.message}`);
      errors++;
    }
  }

  console.log(`\n[backfill-strategy-memos] ${opts.dryRun ? '(DRY-RUN) ' : ''}written=${written} skipped=${skipped} errors=${errors}`);
  process.exit(errors > 0 ? 1 : 0);
}

main();
