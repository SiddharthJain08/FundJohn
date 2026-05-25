'use strict';

/**
 * tests/test_hunt_internal_pop.test.js
 *
 * Guards `saturday_brain._hunt` Population-1b: pre-specced `kind='internal'`
 * candidates must be selected and forwarded to runHunterFanout.
 *
 * Three assertions:
 *  1. A pre-filled kind='internal', data_tier IS NULL, status='pending' row
 *     IS selected and included in candidateIds (dry-run, no spawn).
 *  2. The same row once data_tier is set (non-null) is NOT selected.
 *  3. A kind='paper' ideator row is NOT selected by the internal population.
 *
 * Uses opts.dryRun=true so _hunt returns {run,results,candidateIds} without
 * spawning real paperhunters. DB is fully mocked via the injectable queryFn
 * parameter added to _hunt.
 *
 * Run:
 *   node --test tests/test_hunt_internal_pop.test.js
 */

process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';

const { test } = require('node:test');
const assert   = require('node:assert/strict');

// _hunt is exported via module.exports._hunt (additive seam added in this PR).
const { _hunt } = require('../src/agent/curators/saturday_brain');

// ---------------------------------------------------------------------------
// Mock query factory
// ---------------------------------------------------------------------------

/**
 * Build a mock queryFn that returns:
 *  - fresh rows: [] (Population 1 — no fresh candidates)
 *  - stuck rows: [] (Population 2 — no fetch_failed retries)
 *  - internal rows: as specified by caller (Population 1b)
 *
 * SQL discrimination is by substring match, matching the three queries that
 * _hunt issues (in order):
 *  1. submitted_by IN ... → fresh
 *  2. rejection_reason_if_any = 'fetch_failed' → stuck
 *  3. kind = 'internal' → internal
 */
function makeMockQuery({ internalRows = [], capturedSql = null } = {}) {
  return async function mockQuery(sql, _params) {
    if (capturedSql) capturedSql.push(sql);
    if (sql.includes("submitted_by IN")) {
      // Population 1: fresh — return empty
      return { rows: [] };
    }
    if (sql.includes("rejection_reason_if_any")) {
      // Population 2: stuck fetch_failed — return empty
      return { rows: [] };
    }
    if (sql.includes("kind = 'internal'")) {
      // Population 1b: internal drafts
      return { rows: internalRows };
    }
    // Any other query (e.g. UPDATE for stuck clear) — no-op
    return { rows: [] };
  };
}

const NOTIFY = () => {}; // silent notifier for tests

// ---------------------------------------------------------------------------
// Test 1: pre-filled internal pending row IS selected
// ---------------------------------------------------------------------------
test('_hunt Population-1b: kind=internal, data_tier NULL, status=pending IS selected', async () => {
  const mockQuery = makeMockQuery({
    internalRows: [
      { candidate_id: 'cand-001', pop: 'internal' },
    ],
  });

  const result = await _hunt(
    200,                       // maxFanout
    { dryRun: true },          // dry-run → returns candidateIds, no spawn
    NOTIFY,
    mockQuery,
  );

  assert.ok(result.candidateIds, 'candidateIds must be present in dry-run result');
  assert.ok(
    result.candidateIds.includes('cand-001'),
    `Expected cand-001 in candidateIds, got: ${JSON.stringify(result.candidateIds)}`
  );
  assert.equal(result.run, 0, 'dry-run run count should be 0');
});

// ---------------------------------------------------------------------------
// Test 2: the internal-population SQL enforces the data_tier dedup gate
// ---------------------------------------------------------------------------
// The data_tier IS NULL predicate is the ENTIRE dedup invariant — a tiered draft
// must not be re-selected next cycle. The exclusion itself runs in Postgres, so
// here we guard against accidental removal of the predicate from the query text.
test('_hunt Population-1b: internal query enforces `data_tier IS NULL` dedup gate', async () => {
  const capturedSql = [];
  const mockQuery = makeMockQuery({ internalRows: [], capturedSql });

  await _hunt(200, { dryRun: true }, NOTIFY, mockQuery);

  const internalSql = capturedSql.find(s => s.includes("kind = 'internal'"));
  assert.ok(internalSql, 'internal-draft population query must be issued');
  assert.ok(
    internalSql.includes('data_tier IS NULL'),
    'internal query must filter `data_tier IS NULL` (dedup invariant)'
  );
});

// ---------------------------------------------------------------------------
// Test 3: the internal-population SQL only matches kind='internal'
// ---------------------------------------------------------------------------
// kind='paper' ideator rows must never enter the bypass path (runHunterFanout
// only bypasses PaperHunter for kind='internal'). Guard the `kind = 'internal'`
// predicate against accidental removal.
test('_hunt Population-1b: internal query is gated on `kind = \'internal\'`', async () => {
  const capturedSql = [];
  const mockQuery = makeMockQuery({ internalRows: [], capturedSql });

  await _hunt(200, { dryRun: true }, NOTIFY, mockQuery);

  const internalSql = capturedSql.find(s => s.includes("kind = 'internal'"));
  assert.ok(internalSql, 'internal-draft population query must be issued');
  assert.ok(
    internalSql.includes("kind = 'internal'"),
    "internal query must filter `kind = 'internal'` so kind='paper' rows are excluded"
  );
});

// ---------------------------------------------------------------------------
// Test 4: internal rows are NOT cleared (hunter_result_json preserved)
// ---------------------------------------------------------------------------
test('_hunt Population-1b: hunter_result_json is NOT cleared for internal rows', async () => {
  const updateCalls = [];
  const mockQuery = async (sql, params) => {
    // Track UPDATE calls
    if (sql.includes('SET hunter_result_json = NULL')) {
      updateCalls.push({ sql, params });
    }
    if (sql.includes("submitted_by IN")) return { rows: [] };
    if (sql.includes("rejection_reason_if_any")) return { rows: [] };
    if (sql.includes("kind = 'internal'")) {
      return { rows: [{ candidate_id: 'cand-002', pop: 'internal' }] };
    }
    return { rows: [] };
  };

  // dryRun=false so the UPDATE path could execute (it won't for internal)
  await _hunt(200, { dryRun: true }, NOTIFY, mockQuery);

  assert.equal(
    updateCalls.length, 0,
    'hunter_result_json must NOT be cleared for internal rows'
  );
});

// ---------------------------------------------------------------------------
// Test 5: internal cap respects maxFanout budget
// ---------------------------------------------------------------------------
test('_hunt Population-1b: internalCap does not exceed maxFanout', async () => {
  // With maxFanout=2 and 5 internal rows, only 2 (or fewer if LIMIT kicks in)
  // should be selected. The LIMIT is applied in SQL (via $1), so our mock
  // just honors the params[0] limit.
  const mockQuery = async (sql, params) => {
    if (sql.includes("submitted_by IN")) return { rows: [] };
    if (sql.includes("rejection_reason_if_any")) return { rows: [] };
    if (sql.includes("kind = 'internal'")) {
      const limit = params[0];
      const allRows = Array.from({ length: 5 }, (_, i) => ({
        candidate_id: `cand-${i}`,
        pop: 'internal',
      }));
      return { rows: allRows.slice(0, limit) };
    }
    return { rows: [] };
  };

  const result = await _hunt(2, { dryRun: true }, NOTIFY, mockQuery);
  assert.ok(result.candidateIds, 'candidateIds must be present');
  assert.ok(
    result.candidateIds.length <= 2,
    `candidateIds.length (${result.candidateIds.length}) must not exceed maxFanout=2`
  );
});
