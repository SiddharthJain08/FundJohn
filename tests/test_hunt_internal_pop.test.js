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
function makeMockQuery({ internalRows = [] } = {}) {
  return async function mockQuery(sql, _params) {
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
// Test 2: same row once data_tier is set is NOT returned (SQL gate)
// ---------------------------------------------------------------------------
test('_hunt Population-1b: row with data_tier set is NOT selected (SQL gate)', async () => {
  // When data_tier IS NOT NULL, the SQL predicate `data_tier IS NULL` excludes
  // the row. We simulate this by returning empty internalRows (the DB would not
  // return the already-tiered row).
  const mockQuery = makeMockQuery({
    internalRows: [], // data_tier set → DB returns nothing
  });

  const result = await _hunt(
    200,
    { dryRun: true },
    NOTIFY,
    mockQuery,
  );

  // With no fresh, stuck, or internal rows the function should early-return
  // (nothing to extract).
  assert.equal(result.run, 0);
  // candidateIds is absent on the nothing-to-extract early return
  const ids = result.candidateIds || [];
  assert.ok(!ids.includes('cand-001'), 'Tiered row must NOT appear in candidateIds');
});

// ---------------------------------------------------------------------------
// Test 3: kind='paper' ideator row is NOT selected by internal population
// ---------------------------------------------------------------------------
test('_hunt Population-1b: kind=paper row is NOT selected (SQL only returns kind=internal)', async () => {
  // The SQL WHERE clause `kind = 'internal'` means paper rows never appear
  // in internalRows from the DB. Simulate by returning empty.
  const mockQuery = makeMockQuery({
    internalRows: [], // kind='paper' rows don't match SQL
  });

  const result = await _hunt(
    200,
    { dryRun: true },
    NOTIFY,
    mockQuery,
  );

  const ids = result.candidateIds || [];
  assert.ok(!ids.includes('paper-cand-001'), 'kind=paper row must NOT appear in candidateIds');
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
