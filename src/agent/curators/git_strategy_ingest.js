'use strict';
// Clean-room note: we read cloned files as TEXT only — never import/exec them.
function parseLeanFile(text, filename) {
  const slug = filename.replace(/\.py$/, '').replace(/[^a-z0-9]+/gi, '_').toLowerCase().replace(/^_+|_+$/g, '');
  const strategy_id = `S_ast_${slug}`;
  // Header comment block = contiguous leading `#` lines (LEAN files put the rule + source there).
  // LEAN files lead with a `# region imports` / `from AlgorithmImports import *` / `# endregion`
  // preamble before the rule block, so we skip import/region lines instead of terminating on them.
  const lines = text.split('\n');
  const commentLines = [];
  for (const ln of lines) {
    const t = ln.trim();
    if (t.startsWith('from ') || t.startsWith('import ') || t.startsWith('# region') || t.startsWith('# endregion')) continue;
    if (t.startsWith('#') || t === '') commentLines.push(t.replace(/^#\s?/, ''));
    else break;
  }
  const rule_comment = commentLines.join('\n').trim();
  const urlMatch = rule_comment.match(/https?:\/\/[^\s)]+/);
  const cited_url = urlMatch ? urlMatch[0] : null;
  return { slug, strategy_id, rule_comment, cited_url, code: text };
}

const { runOneShot, parseJsonBlock } = require('./_opus_oneshot');

const HUNTER_FIELDS = ['strategy_id','hypothesis_one_liner','signal_logic','data_requirements','universe','inferred_universe_filter','inferred_instrument_class'];
function validateSpec(s) {
  if (!s || typeof s !== 'object') return { ok: false, why: 'not an object' };
  for (const f of ['strategy_id','hypothesis_one_liner','data_requirements','inferred_instrument_class']) {
    if (s[f] === undefined || s[f] === null && f !== 'inferred_universe_filter') return { ok: false, why: `missing ${f}` };
  }
  if (!['equity','etp','option','crypto','futures'].includes(s.inferred_instrument_class)) return { ok: false, why: 'bad class' };
  return { ok: true };
}

const EXTRACTOR_PROMPT = (parsed) => `You are extracting a trading-strategy spec from an already-coded reference (QuantConnect/LEAN). The RULE is stated in the comment block; the code shows exact params. Emit ONLY a fenced json block matching this shape (the same our PaperHunter emits):
{ "strategy_id": "${parsed.strategy_id}", "hypothesis_one_liner": "...", "signal_logic": "<explicit entry/exit + params>", "data_requirements": {"required":["prices"],"optional":[]}, "universe": "<e.g. SP500 | a fixed ETF list | NASDAQ>", "inferred_universe_filter": null, "inferred_instrument_class": "equity|etp|option|crypto" }
Rules: daily-bar US equity/ETF only; if it needs intraday/futures/options-chain we lack, still emit but note in signal_logic. inferred_instrument_class = 'etp' for ETF-basket rotations, 'equity' for single-name. Cite the source: ${parsed.cited_url || 'n/a'}.

--- RULE COMMENT ---
${parsed.rule_comment}
--- REFERENCE CODE (for params; do NOT copy verbatim downstream) ---
${parsed.code.slice(0, 6000)}`;

async function extractSpec(parsed, { runner = runOneShot } = {}) {
  const out = await runner({ prompt: EXTRACTOR_PROMPT(parsed), model: 'claude-sonnet-4-6',
    allowedTools: [], disallowedTools: ['Write','Edit','Bash','WebSearch','WebFetch'], timeoutMs: 240000 });
  if (out.error) throw new Error(`extractor failed: ${out.error}`);
  const spec = parseJsonBlock(out.text) || {};
  spec.strategy_id = spec.strategy_id || parsed.strategy_id;
  const v = validateSpec(spec);
  if (!v.ok) throw new Error(`invalid spec: ${v.why}`);
  return spec;
}

const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

// Lazy pg pool — constructing it inside the default deps (not at module top-level)
// keeps `require()` of this module DB-connection-free, so the unit tests (which all
// require it) never touch Postgres.
function _pool() {
  if (!_pool._p) {
    const { Pool } = require('pg');
    _pool._p = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  }
  return _pool._p;
}

// Default dependency set. Real clone → gitignored scratch under workspaces/default/.git-ingest/<name>.
function _defaultDeps() {
  return {
    cloneFn: async ({ repo, branch, name }) => {
      const dest = path.join(process.cwd(), 'workspaces', 'default', '.git-ingest', name || 'repo');
      fs.rmSync(dest, { recursive: true, force: true });
      fs.mkdirSync(path.dirname(dest), { recursive: true });
      execFileSync('git', ['clone', '--depth', '1', '--branch', branch || 'main', repo, dest], { stdio: 'pipe' });
      return dest;
    },
    runner: runOneShot,
    existsFn: async (sourceUrl) => {
      const r = await _pool().query('SELECT 1 FROM research_candidates WHERE source_url=$1 LIMIT 1', [sourceUrl]);
      return r.rowCount > 0;
    },
    insertFn: async (row) => {
      // NOTE: research_candidates has NO top-level strategy_id column — the
      // strategy_id lives inside hunter_result_json (set in run() below) and
      // every consumer reads it from there (_hunt / _tier / the finisher's
      // hunter_result_json->>'strategy_id'). The `row.strategy_id` field stays
      // on the row object for callers/tests; it is NOT a DB column.
      await _pool().query(
        `INSERT INTO research_candidates (source_url, origin, kind, submitted_by, reference_url, hunter_result_json)
         VALUES ($1,$2,$3,$4,$5,$6)`,
        [row.source_url, row.origin, row.kind, row.submitted_by, row.reference_url,
         JSON.stringify(row.hunter_result_json)]);
    },
  };
}

// Resolve a `dir/*.py` glob against repoDir using readdirSync (version-proof; no glob dep).
function _resolveGlob(repoDir, strategiesGlob) {
  const subdir = path.dirname(strategiesGlob);   // e.g. 'static/strategies'
  const ext = path.extname(strategiesGlob) || '.py';
  const abs = path.join(repoDir, subdir);
  let names = [];
  try { names = fs.readdirSync(abs); } catch { return []; }
  return names.filter(n => n.endsWith(ext)).sort()
    .map(n => ({ filename: n, fullPath: path.join(abs, n) }));
}

/**
 * Ingest already-coded strategies from a git repo as research candidates
 * (origin='git_blueprint'), clean-room: parse rule comment → LLM extract spec →
 * idempotent insert. Never executes cloned code.
 *
 * @param {Object} opts
 * @param {boolean} [opts.dryRun]      — when true, skip insertFn (count what would insert).
 * @param {Object}  [opts.deps]        — DI overrides: cloneFn, runner, existsFn, insertFn.
 * @param {Object}  opts.repo          — { name, repo, branch, strategies_glob, file_url_template }.
 * @returns {Promise<{inserted:number, skipped:number, errored:number}>}
 */
async function run({ dryRun = false, deps, repo, incremental = false } = {}) {
  const d = { ..._defaultDeps(), ...(deps || {}) };
  const repoDir = await d.cloneFn({ repo: repo.repo, branch: repo.branch, name: repo.name });
  const files = _resolveGlob(repoDir, repo.strategies_glob);

  let inserted = 0, skipped = 0, errored = 0;
  for (const f of files) {
    try {
      const text = fs.readFileSync(f.fullPath, 'utf8');
      const parsed = parseLeanFile(text, f.filename);
      const source_url = repo.file_url_template.replace('{file}', f.filename);

      if (await d.existsFn(source_url)) { skipped++; continue; }

      const spec = await extractSpec(parsed, { runner: d.runner });
      const row = {
        strategy_id: spec.strategy_id,
        source_url,
        origin: 'git_blueprint',
        kind: 'git',
        submitted_by: 'blueprint_lane',
        reference_url: parsed.cited_url,
        hunter_result_json: {
          ...spec,
          reference_impl: parsed.code,
          // reference_url also lives INSIDE hunter_result_json (not just the column)
          // so it rides onto the coded strategy_spec — the finisher builds
          // strategy_spec = {...hunter_result_json}, and buildCoderContext reads
          // strategySpec.reference_url to set SOURCE_URL for porting-mode coding.
          reference_url: parsed.cited_url,
          provenance: { repo: repo.repo, branch: repo.branch, path: source_url, source: parsed.cited_url },
        },
      };
      if (!dryRun) await d.insertFn(row);
      inserted++;
    } catch (e) {
      errored++;
      console.error(`[git-ingest] skip ${f.filename}: ${e.message}`);
    }
  }
  return { inserted, skipped, errored };
}

module.exports = { parseLeanFile, validateSpec, extractSpec, EXTRACTOR_PROMPT, run };
