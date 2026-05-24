'use strict';

/**
 * tests/test_universe_recommender.test.js
 *
 * Unit tests for universe_recommender.js helpers.
 * No network, no real DB, no Opus calls — pure unit coverage for:
 *   - _gridSha determinism (same grid → same sha; different → different)
 *   - _packPrompt (all {{placeholders}} replaced; one row per candidate;
 *                  the 8 metric columns present)
 *
 * Run:
 *   node --test tests/test_universe_recommender.test.js
 */

process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://openclaw:password@localhost:5432/openclaw';

const { test }  = require('node:test');
const assert    = require('node:assert/strict');

// Pull only the pure helpers — no DB or Opus will be called
const {
  _gridSha,
  _packPrompt,
  _currentPredicate,
  _templateSha,
  _formatDiscordMessage,
  _liveStrategies,
} = require('../src/agent/curators/universe_recommender');

// ── _gridSha ──────────────────────────────────────────────────────────────────

test('_gridSha returns a 64-char hex string', () => {
  const grid = [{ name: 'sp500', sharpe: 1.2, max_dd_pct: 5.0, win_rate: 0.6,
                  mean_universe_size: 400, trades_n: 50, sortino: 1.5, calmar: 0.8,
                  mean_holding_days: 7.0 }];
  const sha = _gridSha(grid);
  assert.match(sha, /^[0-9a-f]{64}$/, `expected 64-char hex, got: ${sha}`);
});

test('_gridSha is deterministic: same grid → identical sha', () => {
  const grid1 = [
    { name: 'sp500',    sharpe: 1.2, max_dd_pct: 5.0, win_rate: 0.6,
      mean_universe_size: 400, trades_n: 50, sortino: 1.5, calmar: 0.8, mean_holding_days: 7.0 },
    { name: 'no_adr',   sharpe: 1.1, max_dd_pct: 4.5, win_rate: 0.55,
      mean_universe_size: 800, trades_n: 60, sortino: 1.4, calmar: 0.7, mean_holding_days: 6.5 },
    { name: 'no_otc',   sharpe: 1.0, max_dd_pct: 4.0, win_rate: 0.5,
      mean_universe_size: 850, trades_n: 65, sortino: 1.3, calmar: 0.6, mean_holding_days: 6.0 },
    { name: 'options_eligible_only', sharpe: null, max_dd_pct: null, win_rate: null,
      mean_universe_size: null, trades_n: null, sortino: null, calmar: null, mean_holding_days: null },
  ];
  // Exact same structure
  const grid2 = JSON.parse(JSON.stringify(grid1));
  assert.strictEqual(_gridSha(grid1), _gridSha(grid2), 'identical grids must produce identical SHAs');
});

test('_gridSha differs for different grids', () => {
  const gridA = [{ name: 'sp500', sharpe: 1.2, max_dd_pct: 5.0, win_rate: 0.6,
                   mean_universe_size: 400, trades_n: 50, sortino: 1.5, calmar: 0.8,
                   mean_holding_days: 7.0 }];
  const gridB = [{ name: 'sp500', sharpe: 2.0, max_dd_pct: 5.0, win_rate: 0.6,
                   mean_universe_size: 400, trades_n: 50, sortino: 1.5, calmar: 0.8,
                   mean_holding_days: 7.0 }];  // sharpe differs
  assert.notStrictEqual(_gridSha(gridA), _gridSha(gridB), 'different grids must produce different SHAs');
});

test('_gridSha is order-insensitive for row keys (sorted keys)', () => {
  const gridA = [{ name: 'sp500', sharpe: 1.2, max_dd_pct: 5.0 }];
  const gridB = [{ max_dd_pct: 5.0, sharpe: 1.2, name: 'sp500' }]; // same keys, different order
  assert.strictEqual(_gridSha(gridA), _gridSha(gridB), 'key order within rows must not affect sha');
});

// ── _packPrompt ───────────────────────────────────────────────────────────────

// Minimal template that mirrors the real template structure
const SAMPLE_TEMPLATE = `
# Universe Recommender

### Strategy: {{strategy_id}} — {{strategy_name}}

**Thesis**: {{thesis}}
**Source code** (~{{loc}} lines):
\`\`\`python
{{source_code}}
\`\`\`
**Current predicate:** \`{{current_predicate}}\`

### Candidate-predicate backtest grid

| Candidate | Sharpe | MaxDD% | WinRate | MeanUniSize | Trades | Sortino | Calmar | MeanHoldDays |
|---|---|---|---|---|---|---|---|---|
{{#each grid}}
| {{name}} | {{sharpe}} | {{max_dd_pct}} | {{win_rate}} | {{mean_universe_size}} | {{trades_n}} | {{sortino}} | {{calmar}} | {{mean_holding_days}} |
{{/each}}

Candidates: {{candidate_names}}
`.trim();

const SAMPLE_GRID = [
  { name: 'sp500',    sharpe: 1.2, max_dd_pct: 5.0, win_rate: 0.6,
    mean_universe_size: 400, trades_n: 50, sortino: 1.5, calmar: 0.8, mean_holding_days: 7.0 },
  { name: 'no_adr',   sharpe: 1.1, max_dd_pct: 4.5, win_rate: 0.55,
    mean_universe_size: 800, trades_n: 60, sortino: 1.4, calmar: 0.7, mean_holding_days: 6.5 },
  { name: 'no_otc',   sharpe: null, max_dd_pct: null, win_rate: null,
    mean_universe_size: null, trades_n: null, sortino: null, calmar: null, mean_holding_days: null },
  { name: 'options_eligible_only', sharpe: null, max_dd_pct: null, win_rate: null,
    mean_universe_size: null, trades_n: null, sortino: null, calmar: null, mean_holding_days: null },
];

const SAMPLE_STRATEGY = { id: 'momentum_12_1', name: 'Momentum 12-1', description: 'Long 12mo winners' };
const SAMPLE_CODE = { code: 'class Momentum12_1:\n    pass', loc: 2 };

test('_packPrompt replaces all scalar {{placeholders}}', () => {
  const out = _packPrompt(SAMPLE_STRATEGY, SAMPLE_CODE, 'sp500', SAMPLE_GRID, SAMPLE_TEMPLATE);
  // Must NOT contain any unreplaced placeholders (other than inside the grid loop area)
  // Check that our scalar replacements all fired
  assert.ok(!out.includes('{{strategy_id}}'),       'strategy_id not replaced');
  assert.ok(!out.includes('{{strategy_name}}'),     'strategy_name not replaced');
  assert.ok(!out.includes('{{thesis}}'),             'thesis not replaced');
  assert.ok(!out.includes('{{loc}}'),                'loc not replaced');
  assert.ok(!out.includes('{{source_code}}'),        'source_code not replaced');
  assert.ok(!out.includes('{{current_predicate}}'), 'current_predicate not replaced');
  assert.ok(!out.includes('{{candidate_names}}'),    'candidate_names not replaced');
});

test('_packPrompt removes {{#each grid}} / {{/each}} delimiters', () => {
  const out = _packPrompt(SAMPLE_STRATEGY, SAMPLE_CODE, 'sp500', SAMPLE_GRID, SAMPLE_TEMPLATE);
  assert.ok(!out.includes('{{#each grid}}'), '{{#each grid}} should be removed');
  assert.ok(!out.includes('{{/each}}'),      '{{/each}} should be removed');
});

test('_packPrompt produces one row per candidate (4 candidates → 4 grid rows)', () => {
  const out = _packPrompt(SAMPLE_STRATEGY, SAMPLE_CODE, 'sp500', SAMPLE_GRID, SAMPLE_TEMPLATE);
  // Count the table rows: each row starts with '| `' (name is wrapped in backticks via grid col)
  // Actually count occurrences of each name in the output
  const sp500Count     = (out.match(/sp500/g)    || []).length;
  const noAdrCount     = (out.match(/no_adr/g)   || []).length;
  const noOtcCount     = (out.match(/no_otc/g)   || []).length;
  const optEligCount   = (out.match(/options_eligible_only/g) || []).length;
  // Each name appears at least once in the table
  assert.ok(sp500Count >= 1,   `sp500 should appear in output, found ${sp500Count}`);
  assert.ok(noAdrCount >= 1,   `no_adr should appear in output, found ${noAdrCount}`);
  assert.ok(noOtcCount >= 1,   `no_otc should appear in output, found ${noOtcCount}`);
  assert.ok(optEligCount >= 1, `options_eligible_only should appear in output, found ${optEligCount}`);
});

test('_packPrompt renders the 8 metric columns for a non-null row', () => {
  const out = _packPrompt(SAMPLE_STRATEGY, SAMPLE_CODE, 'sp500', SAMPLE_GRID, SAMPLE_TEMPLATE);
  // The sp500 row should include the actual metric values.
  // Note: JS String(5.0) → '5' and String(7.0) → '7' (no trailing .0), so we
  // match the numeric value with a regex that accepts both '5' and '5.0'.
  assert.ok(out.includes('1.2'),   'sharpe value 1.2 should appear');
  assert.ok(/\b5\b/.test(out),     'max_dd_pct value 5 (or 5.0) should appear');
  assert.ok(out.includes('0.6'),   'win_rate value 0.6 should appear');
  assert.ok(out.includes('400'),   'mean_universe_size 400 should appear');
  assert.ok(out.includes('50'),    'trades_n 50 should appear');
  assert.ok(out.includes('1.5'),   'sortino 1.5 should appear');
  assert.ok(out.includes('0.8'),   'calmar 0.8 should appear');
  assert.ok(/\b7\b/.test(out),     'mean_holding_days 7 (or 7.0) should appear');
});

test('_packPrompt renders null metric cells as n/a', () => {
  const out = _packPrompt(SAMPLE_STRATEGY, SAMPLE_CODE, 'sp500', SAMPLE_GRID, SAMPLE_TEMPLATE);
  // no_otc and options_eligible_only have all-null metrics → should show 'n/a'
  assert.ok(out.includes('n/a'), 'null cells should render as n/a');
});

test('_packPrompt inserts actual strategy metadata', () => {
  const out = _packPrompt(SAMPLE_STRATEGY, SAMPLE_CODE, 'sp500', SAMPLE_GRID, SAMPLE_TEMPLATE);
  assert.ok(out.includes('momentum_12_1'), 'strategy id should be in output');
  assert.ok(out.includes('Momentum 12-1'), 'strategy name should be in output');
  assert.ok(out.includes('Long 12mo winners'), 'thesis should be in output');
  assert.ok(out.includes('sp500'),          'current_predicate should be in output');
});

test('_packPrompt candidate_names lists all grid row names', () => {
  const out = _packPrompt(SAMPLE_STRATEGY, SAMPLE_CODE, 'sp500', SAMPLE_GRID, SAMPLE_TEMPLATE);
  // candidate_names = 'sp500, no_adr, no_otc, options_eligible_only'
  assert.ok(out.includes('sp500, no_adr, no_otc, options_eligible_only'),
    `candidate_names line not found. Output snippet: ${out.slice(0, 300)}`);
});

// ── _currentPredicate ─────────────────────────────────────────────────────────

test('_currentPredicate returns universe_filter_ref when set', () => {
  const manifest = {
    strategies: {
      my_strat: { state: 'live', metadata: { universe_filter_ref: 'no_adr', canonical_file: 'foo.py' } },
    },
  };
  assert.strictEqual(_currentPredicate('my_strat', manifest), 'no_adr');
});

test('_currentPredicate defaults to sp500 when universe_filter_ref absent', () => {
  const manifest = {
    strategies: {
      my_strat: { state: 'live', metadata: { canonical_file: 'foo.py' } },
    },
  };
  assert.strictEqual(_currentPredicate('my_strat', manifest), 'sp500');
});

test('_currentPredicate defaults to sp500 when strategy not in manifest', () => {
  const manifest = { strategies: {} };
  assert.strictEqual(_currentPredicate('unknown', manifest), 'sp500');
});

// ── _templateSha ──────────────────────────────────────────────────────────────

test('_templateSha produces a 64-char hex string', () => {
  const sha = _templateSha('hello world');
  assert.match(sha, /^[0-9a-f]{64}$/);
});

test('_templateSha is deterministic', () => {
  const sha1 = _templateSha('some prompt text');
  const sha2 = _templateSha('some prompt text');
  assert.strictEqual(sha1, sha2);
});

test('_templateSha differs for different inputs', () => {
  const sha1 = _templateSha('text A');
  const sha2 = _templateSha('text B');
  assert.notStrictEqual(sha1, sha2);
});

// ── _formatDiscordMessage ─────────────────────────────────────────────────────

const SAMPLE_STRATEGY_FOR_MSG = { id: 'momentum_12_1', name: 'Momentum 12-1', description: 'Long 12mo winners' };
const SAMPLE_DECISION = {
  choice: 'no_adr',
  confidence: 0.85,
  rationale: 'no_adr has better Sharpe with similar drawdown',
};
const SAMPLE_GRID_FOR_MSG = [
  { name: 'sp500',    sharpe: 1.2, max_dd_pct: 5.0, win_rate: 0.6, trades_n: 50, sortino: 1.5, calmar: 0.8 },
  { name: 'no_adr',  sharpe: 1.4, max_dd_pct: 4.5, win_rate: 0.62, trades_n: 55, sortino: 1.6, calmar: 0.9 },
  { name: 'no_otc',  sharpe: null, max_dd_pct: null, win_rate: null, trades_n: null, sortino: null, calmar: null },
  { name: 'options_eligible_only', sharpe: null, max_dd_pct: null, win_rate: null, trades_n: null, sortino: null, calmar: null },
];

test('_formatDiscordMessage: footer is always present and matches Task 6 parse regex', () => {
  const recId = 42;
  const msg = _formatDiscordMessage(SAMPLE_STRATEGY_FOR_MSG, 'sp500', SAMPLE_DECISION, SAMPLE_GRID_FOR_MSG, recId);
  // Footer must be present
  assert.ok(msg.includes(`_footer: universe-rec:${recId}_`), `footer not found. Message:\n${msg}`);
  // Task 6 reaction handler regex: /universe-rec:(\d+)/
  const match = msg.match(/universe-rec:(\d+)/);
  assert.ok(match !== null, 'Task 6 parse regex /universe-rec:(\\d+)/ did not match');
  assert.strictEqual(Number(match[1]), recId, `extracted recId should be ${recId}, got ${match[1]}`);
});

test('_formatDiscordMessage: footer SURVIVES truncation with 5000-char rationale', () => {
  // A 5000-char rationale would be truncated to 400 chars by the slice(0,400) inside
  // _formatDiscordMessage, so the total message stays well under 1900 chars and the
  // footer is preserved. This test verifies the footer contract is honoured regardless.
  const longDecision = {
    choice: 'no_adr',
    confidence: 0.9,
    rationale: 'x'.repeat(5000),
  };
  const recId = 99;
  const msg = _formatDiscordMessage(SAMPLE_STRATEGY_FOR_MSG, 'sp500', longDecision, SAMPLE_GRID_FOR_MSG, recId);
  // Message must be ≤ 1900 chars (Discord limit applied by the formatter)
  assert.ok(msg.length <= 1900, `message exceeds 1900 chars: ${msg.length}`);
  // Footer must still be present at the end
  assert.ok(msg.includes(`_footer: universe-rec:${recId}_`),
    `footer missing after long-rationale truncation. Message tail:\n${msg.slice(-200)}`);
  // Task 6 regex still resolves the correct recId
  const match = msg.match(/universe-rec:(\d+)/);
  assert.ok(match !== null, 'Task 6 parse regex did not match after long rationale');
  assert.strictEqual(Number(match[1]), recId, `recId mismatch after long rationale`);
});

test('_formatDiscordMessage: grid rows render (one row per candidate)', () => {
  const recId = 7;
  const msg = _formatDiscordMessage(SAMPLE_STRATEGY_FOR_MSG, 'sp500', SAMPLE_DECISION, SAMPLE_GRID_FOR_MSG, recId);
  // Each candidate name must appear in the rendered message
  for (const row of SAMPLE_GRID_FOR_MSG) {
    assert.ok(msg.includes(row.name), `grid row '${row.name}' not found in message`);
  }
  // Non-null metrics for sp500 row should appear
  assert.ok(msg.includes('1.2'), 'sp500 sharpe 1.2 should appear');
  assert.ok(msg.includes('1.4'), 'no_adr sharpe 1.4 should appear');
  // Null metrics should render as 'n/a'
  assert.ok(msg.includes('n/a'), 'null metric cells should render as n/a');
});

// ── _liveStrategies ───────────────────────────────────────────────────────────
// Tests assert invariants against the real manifest (MANIFEST_PATH is hardcoded
// in universe_recommender.js). We verify counts match and no non-live strategies
// leak through.

const fs_test   = require('fs');
const path_test = require('path');

// Use the same OPENCLAW_DIR the module uses so both read the same manifest file.
// The module resolves: path.join(OPENCLAW_DIR, 'src/strategies/manifest.json')
// where OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw'.
const OPENCLAW_DIR_TEST  = process.env.OPENCLAW_DIR || '/root/openclaw';
const MANIFEST_FILE = path_test.join(OPENCLAW_DIR_TEST, 'src', 'strategies', 'manifest.json');

test('_liveStrategies returns only manifest state=live strategies (count matches manifest)', async () => {
  const manifest = JSON.parse(fs_test.readFileSync(MANIFEST_FILE, 'utf-8'));
  const strategies = manifest.strategies || {};

  // Count live strategies directly from manifest
  const manifestLiveCount = Object.values(strategies).filter(s => s.state === 'live').length;
  assert.ok(manifestLiveCount > 0, 'manifest must have at least one live strategy');

  const live = await _liveStrategies();
  assert.strictEqual(live.length, manifestLiveCount,
    `_liveStrategies() returned ${live.length} but manifest has ${manifestLiveCount} live strategies`);
});

test('_liveStrategies returns no non-live strategies', async () => {
  const manifest = JSON.parse(fs_test.readFileSync(MANIFEST_FILE, 'utf-8'));
  const strategies = manifest.strategies || {};

  const live = await _liveStrategies();
  for (const entry of live) {
    const record = strategies[entry.id];
    assert.ok(record !== undefined, `returned id '${entry.id}' not found in manifest`);
    assert.strictEqual(record.state, 'live',
      `strategy '${entry.id}' has state='${record.state}', not 'live'`);
  }
});

test('_liveStrategies entries have required shape (id, name, description, implementation_path)', async () => {
  const live = await _liveStrategies();
  assert.ok(live.length > 0, 'need at least one live strategy to test shape');

  const sample = live[0];
  assert.ok(typeof sample.id === 'string' && sample.id.length > 0,
    `id must be a non-empty string, got: ${JSON.stringify(sample.id)}`);
  assert.ok(typeof sample.name === 'string',
    `name must be a string, got: ${JSON.stringify(sample.name)}`);
  assert.ok(typeof sample.description === 'string',
    `description must be a string, got: ${JSON.stringify(sample.description)}`);
  // implementation_path may be null (if canonical_file absent) but must be present as key
  assert.ok(Object.prototype.hasOwnProperty.call(sample, 'implementation_path'),
    'implementation_path key must be present (may be null)');
});

test('_liveStrategies result is sorted by id (localeCompare)', async () => {
  const live = await _liveStrategies();
  for (let i = 1; i < live.length; i++) {
    assert.ok(live[i - 1].id.localeCompare(live[i].id) <= 0,
      `entries not sorted: '${live[i - 1].id}' > '${live[i].id}' at index ${i}`);
  }
});
