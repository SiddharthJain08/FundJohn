# Blueprint Fast Lane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import already-coded strategies from a git repo (clean-room) and crawl explicit-rule HTML sites, route them through a prioritized "blueprint fast lane" that surfaces them as research candidates, and give StrategyCoder a porting assist — all reusing the existing tier→code→register tail.

**Architecture:** Two source families feed `research_candidates` tagged by a new `origin` column. **Git** (pre-coded LEAN files) is extracted by a cheap Sonnet one-shot into the existing `hunter_result_json` shape, SKIPPING PaperHunter, and carries a reference implementation for porting. **HTML/blog** sources crawl into `research_corpus` and go through normal hunt, but tagged blueprint for priority. Both reuse `saturday_brain` Phases 5–8 unchanged. Blueprint candidates are coded first and get a larger share of the weekly coding budget.

**Tech Stack:** Node (curators: `runOneShot` from `_opus_oneshot.js`, `pg` pool), Python (`expanded_sources.py` parser, pytest), Postgres (additive migration 136), git CLI (shallow clone to gitignored scratch).

**Spec:** `docs/superpowers/specs/2026-06-16-blueprint-fast-lane-design.md`

**Refinement vs. spec:** the git ingester is **Node** (`src/agent/curators/git_strategy_ingest.js`), not Python — it must call the LLM extractor (`runOneShot`) and the DB pool, exactly like its sibling `paper_expansion_ingestor.js`. Everything else matches the spec.

**Global constraints (every task):**
- Stage ONLY the files the task touches (the live branch has uncommitted operator WIP in `manifest.json`/`registry.py`/`strategy_signatures.json` — never `git add` those).
- Additive only; never drop columns/rows. Candidates only — nothing auto-promotes to live.
- Never execute cloned code (read-as-text → LLM only).
- 2-core/8GB box: run pytest/backtests sequentially with `nice -n 19`.
- Run Python tests with `PYTHONPATH=/root/openclaw`. Run from `/root/openclaw`.

---

## Phase 1 — Schema + origin plumbing + priority knobs

### Task 1.1: Migration 136 — origin + reference_url on research_candidates

**Files:**
- Create: `src/database/migrations/136_research_candidate_origin.sql`
- Test: `tests/test_migration_136_origin.py`

- [ ] **Step 1: Write the migration SQL**

```sql
-- 136_research_candidate_origin.sql — tag a candidate's source lane (Blueprint Fast Lane).
-- Additive. Default 'paper' keeps every existing row + the academic lane byte-identical.
ALTER TABLE research_candidates
  ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'paper'
    CHECK (origin IN ('paper','git_blueprint','blog_blueprint'));
ALTER TABLE research_candidates
  ADD COLUMN IF NOT EXISTS reference_url TEXT;
CREATE INDEX IF NOT EXISTS idx_research_candidates_origin ON research_candidates(origin);
```

- [ ] **Step 2: Write the failing test**

```python
import os, psycopg2, pytest
PG = os.environ.get('POSTGRES_URI')

@pytest.mark.skipif(not PG, reason='no POSTGRES_URI')
def test_origin_column_default_paper():
    conn = psycopg2.connect(PG); cur = conn.cursor()
    cur.execute("""SELECT column_name, column_default FROM information_schema.columns
                   WHERE table_name='research_candidates' AND column_name IN ('origin','reference_url')
                   ORDER BY column_name""")
    cols = dict((r[0], r[1]) for r in cur.fetchall())
    conn.close()
    assert 'origin' in cols and 'reference_url' in cols
    assert "'paper'" in (cols['origin'] or '')
```

- [ ] **Step 3: Apply the migration** — `PYTHONPATH=/root/openclaw python3 -c "import psycopg2,os; c=psycopg2.connect(os.environ['POSTGRES_URI']); cur=c.cursor(); cur.execute(open('src/database/migrations/136_research_candidate_origin.sql').read()); c.commit(); print('applied')"`
- [ ] **Step 4: Run test** — `PYTHONPATH=/root/openclaw python3 -m pytest tests/test_migration_136_origin.py -q` → PASS
- [ ] **Step 5: Commit** — `git add src/database/migrations/136_research_candidate_origin.sql tests/test_migration_136_origin.py && git commit -m "feat(research): migration 136 — origin/reference_url on research_candidates"`

### Task 1.2: Priority helpers in saturday_brain (pure, unit-tested)

**Files:**
- Modify: `src/agent/curators/saturday_brain.js` (add two exported pure helpers + wire into `_code`)
- Test: `tests/test_blueprint_priority.test.js` (node, plain asserts)

Context: Phase 6 `_code` reads `tierA` (array of `{candidate_id, hunterResult, ...}`) and codes up to `cap = min(DEFAULT_TIER_A_CAP=80, maxByBudget, tierA.length)`. We add origin-aware ordering + budget split.

- [ ] **Step 1: Write the failing test**

```javascript
const assert = require('assert');
const { partitionBlueprintBudget, promotionThresholdFor } = require('../src/agent/curators/saturday_brain.js');

// blueprint candidates ordered first; budget split by share
const cands = [
  { candidate_id: 'p1', origin: 'paper' },
  { candidate_id: 'g1', origin: 'git_blueprint' },
  { candidate_id: 'b1', origin: 'blog_blueprint' },
  { candidate_id: 'p2', origin: 'paper' },
];
const r = partitionBlueprintBudget(cands, /*cap*/ 3, /*blueprintShare*/ 0.5);
assert.deepStrictEqual(r.ordered.map(c => c.candidate_id), ['g1','b1','p1','p2']); // blueprint first
assert.strictEqual(r.blueprintCap, 2);   // ceil(3*0.5)=2 reserved, 2 blueprint exist → 2
assert.strictEqual(r.paperCap, 1);       // remaining
// lower bar for blueprint origins
assert.ok(promotionThresholdFor('git_blueprint').min_sharpe < promotionThresholdFor('paper').min_sharpe);
assert.strictEqual(promotionThresholdFor('paper').min_sharpe, 0.5);
console.log('ok');
```

- [ ] **Step 2: Run test to verify it fails** — `node tests/test_blueprint_priority.test.js` → throws (helpers undefined).

- [ ] **Step 3: Implement the helpers + export**

Add near the budget constants in `saturday_brain.js`:

```javascript
const BLUEPRINT_ORIGINS = new Set(['git_blueprint', 'blog_blueprint']);
const BLUEPRINT_TIER_A_SHARE = (() => {
  const v = parseFloat(process.env.OPENCLAW_BLUEPRINT_TIER_A_SHARE || '0.5');
  return (Number.isFinite(v) && v >= 0 && v <= 1) ? v : 0.5;
})();
const PROMOTION_MIN_SHARPE = { paper: 0.5, git_blueprint: 0.3, blog_blueprint: 0.3 };

function promotionThresholdFor(origin) {
  return { min_sharpe: PROMOTION_MIN_SHARPE[origin] ?? PROMOTION_MIN_SHARPE.paper };
}

// Blueprint candidates first; reserve ceil(cap*share) slots for blueprint, rest for papers.
function partitionBlueprintBudget(cands, cap, blueprintShare = BLUEPRINT_TIER_A_SHARE) {
  const isBp = c => BLUEPRINT_ORIGINS.has(c.origin);
  const blueprint = cands.filter(isBp);
  const papers    = cands.filter(c => !isBp(c));
  const ordered   = [...blueprint, ...papers];
  const reserved  = Math.ceil(cap * blueprintShare);
  const blueprintCap = Math.min(reserved, blueprint.length);
  const paperCap     = Math.min(cap - blueprintCap, papers.length);
  return { ordered, blueprintCap, paperCap };
}
```

Add to `module.exports`: `partitionBlueprintBudget, promotionThresholdFor`.

- [ ] **Step 4: Wire into `_code`** — in Phase 6, after computing `cap`, replace the plain `tierA.slice(0, cap)` loop with: `const { ordered, blueprintCap, paperCap } = partitionBlueprintBudget(tierA, cap);` then iterate `ordered`, coding blueprint up to `blueprintCap` and papers up to `paperCap` (track two counters). Read the file first to match the exact loop; preserve all existing per-candidate logic. `origin` comes from the candidate row (default 'paper' for existing).

- [ ] **Step 5: Run test** — `node tests/test_blueprint_priority.test.js` → `ok`. Then `node --check src/agent/curators/saturday_brain.js`.
- [ ] **Step 6: Commit** — `git add src/agent/curators/saturday_brain.js tests/test_blueprint_priority.test.js && git commit -m "feat(research): blueprint-first budget split + lower-bar helpers in saturday_brain"`

---

## Phase 2 — Git ingester (clean-room, Node)

### Task 2.1: LEAN file parser + source config

**Files:**
- Create: `src/ingestion/git_strategy_sources.json`
- Create: `src/agent/curators/git_strategy_ingest.js` (parser only this task)
- Create: `tests/fixtures/lean_asset_class_trend_following.py` (copied verbatim from a real AST file for the test)
- Test: `tests/test_git_lean_parser.test.js`

- [ ] **Step 1: Write the source config**

```json
{
  "version": 1,
  "description": "Git repos of already-coded strategies to clean-room import (Blueprint Fast Lane). We extract the rule (stated in each file's comment block + cited source URL) and re-implement; we never vendor or execute the cloned code.",
  "repos": [
    {
      "name": "awesome-systematic-trading",
      "repo": "https://github.com/paperswithbacktest/awesome-systematic-trading.git",
      "branch": "main",
      "strategies_glob": "static/strategies/*.py",
      "file_url_template": "https://github.com/paperswithbacktest/awesome-systematic-trading/blob/main/static/strategies/{file}",
      "kind": "lean"
    }
  ]
}
```

- [ ] **Step 2: Create the fixture** — copy one real AST file's content into `tests/fixtures/lean_asset_class_trend_following.py` (the `asset-class-trend-following.py` content: header comment with `# https://quantpedia.com/strategies/asset-class-trend-following/` + the plain-English rule + the `class AssetClassTrendFollowing(QCAlgorithm)` body).

- [ ] **Step 3: Write the failing test**

```javascript
const assert = require('assert');
const fs = require('fs');
const { parseLeanFile } = require('../src/agent/curators/git_strategy_ingest.js');

const text = fs.readFileSync(__dirname + '/fixtures/lean_asset_class_trend_following.py', 'utf8');
const p = parseLeanFile(text, 'asset-class-trend-following.py');
assert.strictEqual(p.slug, 'asset_class_trend_following');
assert.strictEqual(p.strategy_id, 'S_ast_asset_class_trend_following');
assert.ok(p.cited_url && p.cited_url.includes('quantpedia.com'));
assert.ok(/10 month|SMA|equal weight/i.test(p.rule_comment)); // rule captured from comment
assert.ok(p.code.includes('QCAlgorithm'));                    // raw code retained (for porting only)
console.log('ok');
```

- [ ] **Step 4: Run test** — `node tests/test_git_lean_parser.test.js` → fails (undefined).

- [ ] **Step 5: Implement `parseLeanFile`** in `git_strategy_ingest.js`:

```javascript
'use strict';
// Clean-room note: we read cloned files as TEXT only — never import/exec them.
function parseLeanFile(text, filename) {
  const slug = filename.replace(/\.py$/, '').replace(/[^a-z0-9]+/gi, '_').toLowerCase().replace(/^_+|_+$/g, '');
  const strategy_id = `S_ast_${slug}`;
  // Header comment block = contiguous leading `#` lines (LEAN files put the rule + source there).
  const lines = text.split('\n');
  const commentLines = [];
  for (const ln of lines) {
    const t = ln.trim();
    if (t.startsWith('#') || t === '' ) commentLines.push(t.replace(/^#\s?/, ''));
    else if (t.startsWith('from ') || t.startsWith('import ') || t.startsWith('# region')) continue;
    else break;
  }
  const rule_comment = commentLines.join('\n').trim();
  const urlMatch = rule_comment.match(/https?:\/\/[^\s)]+/);
  const cited_url = urlMatch ? urlMatch[0] : null;
  return { slug, strategy_id, rule_comment, cited_url, code: text };
}
module.exports = { parseLeanFile };
```

(Adjust the comment-capture loop against the fixture so `region imports`/`from AlgorithmImports` lines don't terminate capture before the rule block — verify with the test.)

- [ ] **Step 6: Run test** — `node tests/test_git_lean_parser.test.js` → `ok`.
- [ ] **Step 7: Commit** — `git add src/ingestion/git_strategy_sources.json src/agent/curators/git_strategy_ingest.js tests/fixtures/lean_asset_class_trend_following.py tests/test_git_lean_parser.test.js && git commit -m "feat(git-ingest): LEAN file parser + source config"`

### Task 2.2: LLM extractor → hunter_result_json

**Files:**
- Modify: `src/agent/curators/git_strategy_ingest.js` (add `EXTRACTOR_PROMPT`, `extractSpec`, `validateSpec`)
- Test: `tests/test_git_extractor.test.js`

- [ ] **Step 1: Write the failing test** (mock the LLM — no network)

```javascript
const assert = require('assert');
const mod = require('../src/agent/curators/git_strategy_ingest.js');

// validateSpec accepts a well-formed hunter_result_json, rejects missing fields.
const good = {
  strategy_id: 'S_ast_x', hypothesis_one_liner: 'hold ETF over 10mo SMA',
  signal_logic: '...', data_requirements: { required: ['prices'], optional: [] },
  universe: 'ETF_BASKET', inferred_universe_filter: null, inferred_instrument_class: 'etp',
};
assert.strictEqual(mod.validateSpec(good).ok, true);
assert.strictEqual(mod.validateSpec({ strategy_id: 'x' }).ok, false);
// extractSpec composes the parsed file into a spec via an injected runner (DI for testing)
const parsed = { strategy_id: 'S_ast_x', slug: 'x', rule_comment: 'hold ETF over 10mo SMA; source quantpedia', cited_url: 'https://quantpedia.com/x', code: 'class X(QCAlgorithm): pass' };
const fakeRunner = async () => ({ text: '```json\n' + JSON.stringify(good) + '\n```', costUsd: 0.01, error: null });
mod.extractSpec(parsed, { runner: fakeRunner }).then(spec => {
  assert.strictEqual(spec.strategy_id, 'S_ast_x');
  assert.strictEqual(spec.inferred_instrument_class, 'etp');
  console.log('ok');
});
```

- [ ] **Step 2: Run test** → fails.

- [ ] **Step 3: Implement** — add to `git_strategy_ingest.js`:

```javascript
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
module.exports = { parseLeanFile, validateSpec, extractSpec, EXTRACTOR_PROMPT };
```

- [ ] **Step 4: Run test** → `ok`.
- [ ] **Step 5: Commit** — `git add src/agent/curators/git_strategy_ingest.js tests/test_git_extractor.test.js && git commit -m "feat(git-ingest): Sonnet extractor → hunter_result_json + validation"`

### Task 2.3: Clone + idempotent insert + run()

**Files:**
- Modify: `src/agent/curators/git_strategy_ingest.js` (add `run`, clone, DB insert, idempotency)
- Test: `tests/test_git_ingest_run.test.js` (uses a LOCAL fixture repo dir — no network/no LLM via DI)

- [ ] **Step 1: Write the failing test**

```javascript
const assert = require('assert');
const fs = require('fs'); const os = require('os'); const path = require('path');
const mod = require('../src/agent/curators/git_strategy_ingest.js');

// Build a tiny local "repo" dir of fixture strategy files.
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gitfix-'));
fs.mkdirSync(path.join(dir, 'static', 'strategies'), { recursive: true });
fs.copyFileSync(__dirname + '/fixtures/lean_asset_class_trend_following.py', path.join(dir, 'static/strategies/asset-class-trend-following.py'));

const inserted = [];
const deps = {
  cloneFn: async () => dir,                          // skip real clone
  runner: async () => ({ text: '```json\n{"strategy_id":"S_ast_asset_class_trend_following","hypothesis_one_liner":"x","signal_logic":"y","data_requirements":{"required":["prices"],"optional":[]},"universe":"ETF","inferred_universe_filter":null,"inferred_instrument_class":"etp"}\n```', costUsd: 0.01, error: null }),
  existsFn: async (url) => false,                     // not yet ingested
  insertFn: async (row) => { inserted.push(row); },
};
mod.run({ dryRun: false, deps, repo: { strategies_glob: 'static/strategies/*.py', file_url_template: 'https://x/{file}', branch: 'main', repo: 'r' } }).then(res => {
  assert.strictEqual(inserted.length, 1);
  assert.strictEqual(inserted[0].origin, 'git_blueprint');
  assert.ok(inserted[0].source_url.includes('asset-class-trend-following.py'));
  assert.strictEqual(inserted[0].hunter_result_json.inferred_instrument_class, 'etp');
  // idempotency: re-run with existsFn → true inserts nothing
  const ins2 = [];
  return mod.run({ dryRun: false, repo: { strategies_glob: 'static/strategies/*.py', file_url_template: 'https://x/{file}', branch:'main', repo:'r' },
    deps: { ...deps, existsFn: async () => true, insertFn: async (r) => ins2.push(r) } }).then(() => {
      assert.strictEqual(ins2.length, 0); console.log('ok');
    });
});
```

- [ ] **Step 2: Run test** → fails.

- [ ] **Step 3: Implement `run`** with dependency injection (so tests skip network/LLM/DB). Real deps: `cloneFn` shallow-clones to a gitignored scratch dir under `workspaces/default/.git-ingest/<name>` via `execFileSync('git', ['clone','--depth','1','--branch',branch,repo,dest])`; `existsFn(url)` = `SELECT 1 FROM research_candidates WHERE source_url=$1`; `insertFn(row)` = INSERT with `origin='git_blueprint'`, `kind='git'`, `submitted_by='blueprint_lane'`, `reference_url=cited_url`, `hunter_result_json` (with `reference_impl: parsed.code`, `provenance`). Use `glob`/`fs.readdirSync` to resolve `strategies_glob`. Per file: parseLeanFile → if existsFn skip → extractSpec → insertFn. Wrap per-file in try/catch (skip-on-error, log). Return `{ inserted, skipped, errored }`.

- [ ] **Step 4: Run test** → `ok`.
- [ ] **Step 5: Commit** — `git add src/agent/curators/git_strategy_ingest.js tests/test_git_ingest_run.test.js && git commit -m "feat(git-ingest): clone + idempotent candidate insert (origin=git_blueprint)"`

### Task 2.4: CLI + bulk driver + saturday_brain wiring

**Files:**
- Modify: `src/agent/curators/run_mastermind.js` (add `--mode git-ingest`)
- Create: `scripts/bulk_git_ingest.sh` (chunked/resumable, nice; operator one-off)
- Modify: `src/agent/curators/saturday_brain.js` (run incremental git-ingest before Phase 5; merge results into `_tier` input; gate `OPENCLAW_GIT_INGEST`)
- Test: `tests/test_git_ingest_cli.test.js` (asserts `--mode git-ingest` dispatches; smoke)

- [ ] **Step 1:** Add `git-ingest` mode to `run_mastermind.js` calling `git_strategy_ingest.run({ dryRun, incremental })`. Mirror how existing modes dispatch.
- [ ] **Step 2:** `scripts/bulk_git_ingest.sh` — loops the repo's files in chunks, `nice -n 19 node ... --mode git-ingest`, resumable via the DB idempotency (existsFn), logs progress. Mirror `scripts/build_all_oxford.sh` style.
- [ ] **Step 3:** In `saturday_brain.js`, behind `OPENCLAW_GIT_INGEST !== '0'`, call git-ingest before Phase 5 and merge its freshly-inserted candidate rows (origin='git_blueprint') into the array passed to `_tier`, so they flow through tier→code alongside papers. Read the Phase 4→5 handoff first.
- [ ] **Step 4:** `node tests/test_git_ingest_cli.test.js` + `node --check` both modified JS. Commit.

---

## Phase 3 — Coder-assist (porting)

### Task 3.1: QuantConnect → BaseStrategy porting guide

**Files:**
- Create: `docs/strategy-coding/quantconnect-to-basestrategy.md`

- [ ] **Step 1:** Write the mapping reference: QC `Initialize/SetWarmUp/AddEquity/SMA/RSI` → our self-load + house indicators; `OnData`+`SetHoldings`/`Liquidate` → `generate_signals(prices, regime, universe, aux_data) -> List[Signal]`; monthly/weekly rebalance via date checks; `compute_stops_and_targets` for brackets; `should_run(regime)`; daily-bar fill model (close[t+1]); the `Signal` field list (from `strategies.base`, do-not-invent). Include 1 worked example (the asset-class-trend-following file → a BaseStrategy skeleton). Note the clean-room rule: re-implement from the rule, cite source in docstring, do not copy verbatim.
- [ ] **Step 2: Commit** — `git add docs/strategy-coding/quantconnect-to-basestrategy.md && git commit -m "docs(coder): QuantConnect→BaseStrategy porting guide"`

### Task 3.2: StrategyCoder porting hook

**Files:**
- Modify: `src/agent/research/research-orchestrator.js` (`_codeStrategy` ctx — add `REFERENCE_IMPLEMENTATION` + `PORTING_GUIDE` when present)
- Modify: `src/agent/prompts/subagents/strategycoder.md` (add a "Porting mode" section)
- Test: `tests/test_coder_porting_ctx.test.js`

- [ ] **Step 1: Write the failing test** — extract a pure `buildCoderContext(strategySpec)` helper from `_codeStrategy` and test it:

```javascript
const assert = require('assert');
const { buildCoderContext } = require('../src/agent/research/research-orchestrator.js');
// git-origin spec with reference_impl → ctx carries porting fields
const ctx1 = buildCoderContext({ strategy_id: 'S_ast_x', inferred_instrument_class: 'etp', reference_impl: 'class X(QCAlgorithm): pass', reference_url: 'https://quantpedia.com/x' });
assert.ok(ctx1.REFERENCE_IMPLEMENTATION.includes('QCAlgorithm'));
assert.ok(ctx1.PORTING_GUIDE && ctx1.PORTING_GUIDE.includes('quantconnect-to-basestrategy'));
assert.strictEqual(ctx1.SOURCE_URL, 'https://quantpedia.com/x');
// paper spec without reference_impl → no porting fields (unchanged behavior)
const ctx2 = buildCoderContext({ strategy_id: 'S_p', inferred_instrument_class: 'equity' });
assert.strictEqual(ctx2.REFERENCE_IMPLEMENTATION, undefined);
console.log('ok');
```

- [ ] **Step 2: Run test** → fails.
- [ ] **Step 3: Implement** — extract `buildCoderContext(strategySpec)` returning the existing ctx fields (role/STRATEGY_SPEC/INFERRED_*) PLUS, when `strategySpec.reference_impl` is truthy: `REFERENCE_IMPLEMENTATION = strategySpec.reference_impl`, `PORTING_GUIDE = 'docs/strategy-coding/quantconnect-to-basestrategy.md'`, `SOURCE_URL = strategySpec.reference_url`. Call it from `_codeStrategy`. Read `_codeStrategy` first to preserve all current fields.
- [ ] **Step 4: Prompt section** — append to `strategycoder.md`: "## Porting mode — if `REFERENCE_IMPLEMENTATION` is present you are PORTING an already-coded strategy. Translate its rule into our contract using `PORTING_GUIDE`; do NOT copy code verbatim; cite `SOURCE_URL` in the class docstring. Absent → implement from `STRATEGY_SPEC` as usual."
- [ ] **Step 5: Run test** → `ok`. `node --check` the JS. Commit.

---

## Phase 4 — HTML clean-URL crawler

### Task 4.1: Pattern-based HTML index parser

**Files:**
- Modify: `src/ingestion/expanded_sources.py` (add `_parse_html_index_pattern`, dispatch in `fetch_source`)
- Create: `tests/fixtures/turingtrader_index.html` (saved sample with strategy links + nav junk)
- Test: `tests/test_html_pattern_parser.py`

- [ ] **Step 1: Create fixture** — save a representative index page (or a hand-built fixture) with ~3 real strategy `<a href="/portfolios/xyz/">Title</a>` plus nav/footer links to prove filtering.

- [ ] **Step 2: Write the failing test**

```python
import importlib
es = importlib.import_module('src.ingestion.expanded_sources')

def test_pattern_parser_harvests_only_matching():
    body = open('tests/fixtures/turingtrader_index.html','rb').read()
    items = es._parse_html_index_pattern(body, 'https://www.turingtrader.com/portfolios/',
                                         r'/portfolios/[a-z0-9-]+/?$', 'expanded:TuringTrader', max_links=50)
    urls = [i['source_url'] for i in items]
    assert all('/portfolios/' in u for u in urls)
    assert len(items) >= 2          # caught the real strategy links
    assert not any('/about' in u or '#' in u for u in urls)   # nav/footer filtered
```

- [ ] **Step 3: Run test** → fails.
- [ ] **Step 4: Implement** `_parse_html_index_pattern(body, source_url, pattern, source_tag, max_links)`: regex over `<a ... href="(...)">(text)</a>`, keep hrefs whose absolute URL matches `pattern` (re.search), anchor text length ≥ 8, dedupe, cap at `max_links`; return the same item dict shape as `_parse_html_index`. In `fetch_source`, if `source_spec.get('link_pattern')`, dispatch to it (pass `link_prefix`/pattern) — else current behavior unchanged.
- [ ] **Step 5: Run test** — `PYTHONPATH=/root/openclaw python3 -m pytest tests/test_html_pattern_parser.py -q` → PASS. Commit.

### Task 4.2: Seed TuringTrader + Quantpedia (verify URLs live)

**Files:**
- Modify: `src/ingestion/blueprint_seed_sources.json` (add 2 HTML sources with `link_pattern` + `origin_hint:'blog_blueprint'`)
- Modify: `tests/test_blueprint_seed_sources.py` (assert the 2 HTML sources load with link_pattern)

- [ ] **Step 1:** First VERIFY live (curl the candidate index URLs + confirm static HTML, not JS-rendered). If a target is JS-only, note it and fall back to the Firecrawl skill for that one source (don't add to the urllib seed). Record the working index URL + the link pattern.
- [ ] **Step 2:** Add the verified sources, e.g.:

```json
{ "domain": "turingtrader.com", "name": "TuringTrader", "feed_url": "https://www.turingtrader.com/portfolios/", "kind": "html", "link_pattern": "/portfolios/[a-z0-9-]+/?$", "origin_hint": "blog_blueprint", "strategy_types": ["taa","rotation","etf"], "notes": "Per-strategy pages with full numeric rules; clean URLs." }
```

- [ ] **Step 3:** Extend the existing seed test to assert these load and carry `link_pattern`. Run `PYTHONPATH=/root/openclaw python3 -m pytest tests/test_blueprint_seed_sources.py -q` → PASS. Commit.

---

## Self-review notes
- Spec coverage: A→schema (1.1), priority (1.2), B git ingester (2.1–2.4), E coder-assist (3.1–3.2), C HTML crawler (4.1–4.2). All spec sections mapped.
- The "lower bar" is scoped to ordering/threshold helpers (1.2) — candidates still all surface (like the oxford set); live promotion stays operator-gated.
- Bulk git import is the one-off `scripts/bulk_git_ingest.sh` (Task 2.4), chunked/resumable, operator-run — NOT auto. Weekly is incremental + idempotent (cheap).

## Verification (end-to-end, after all phases)
1. `node tests/test_blueprint_priority.test.js`, `node tests/test_git_*.test.js`, `node tests/test_coder_porting_ctx.test.js` → all `ok`.
2. `PYTHONPATH=/root/openclaw python3 -m pytest tests/test_migration_136_origin.py tests/test_html_pattern_parser.py tests/test_blueprint_seed_sources.py -q` → green.
3. Dry-run git ingest on 2–3 real AST files: `node -e "require('./src/agent/curators/git_strategy_ingest.js').run({dryRun:true,...})"` → candidate rows shaped right, NO files written into the repo, no code executed.
4. Operator bulk: `bash scripts/bulk_git_ingest.sh` (off-hours), then `curl -s localhost:3000/api/strategies | grep -c S_ast_` and spot-check a coded port backtests + surfaces as a candidate.
5. Regression: paper lane unchanged — `origin` defaults to 'paper', `partitionBlueprintBudget` with zero blueprint candidates returns papers in original order.
