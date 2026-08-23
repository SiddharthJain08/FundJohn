'use strict';
// data_provider_health from the JS collector (2026-08-23). The Python writer
// (src/maintenance/provider_health.py) is one psycopg2 connect per call; the
// collector makes ~8k FMP calls a cycle, so it records through the pg pool.
// Same hourly-bucket upsert, same best-effort contract.
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
require('dotenv').config({ path: path.resolve(__dirname, '..', '..', '.env') });
const store = require(path.resolve(__dirname, '..', '..', 'src/pipeline/store.js'));
const { query } = require(path.resolve(__dirname, '..', '..', 'src/database/postgres.js'));
const PROVIDER = '_test_provider_health';

after(async () => {
  await query('DELETE FROM data_provider_health WHERE provider = $1', [PROVIDER]).catch(() => null);
});

test('recordProviderCall upserts hourly success/error counters through the pool', async () => {
  await query('DELETE FROM data_provider_health WHERE provider = $1', [PROVIDER]);
  await store.recordProviderCall(PROVIDER, 'income_statement', true);
  await store.recordProviderCall(PROVIDER, 'income_statement', true);
  await store.recordProviderCall(PROVIDER, 'income_statement', false, 'HTTP 429: slow down');
  const r = await query(
    'SELECT success_count, error_count, last_error, last_error_at FROM data_provider_health WHERE provider=$1 AND endpoint=$2',
    [PROVIDER, 'income_statement']);
  assert.equal(r.rows.length, 1);
  assert.equal(r.rows[0].success_count, 2);
  assert.equal(r.rows[0].error_count, 1);
  assert.equal(r.rows[0].last_error, 'HTTP 429: slow down');
  assert.ok(r.rows[0].last_error_at);
});

test('recordProviderCall never throws (best-effort)', async () => {
  await assert.doesNotReject(store.recordProviderCall(null, null, true));
});

test('collector._fmpCallOutcome maps error kinds to provider health', () => {
  const collector = require(path.resolve(__dirname, '..', '..', 'src/pipeline/collector.js'));
  const gated = collector._httpError(402, "Premium Query Parameter: 'Special Endpoint : not available under your current subscription");
  assert.deepEqual(collector._fmpCallOutcome(null), { ok: true, error: null, kind: 'ok' });
  assert.deepEqual(collector._fmpCallOutcome(gated), { ok: true, error: null, kind: 'symbol_gated' });
  assert.deepEqual(collector._fmpCallOutcome(collector._httpError(404, '')), { ok: true, error: null, kind: 'not_found' });
  const q = collector._fmpCallOutcome(collector._httpError(402, 'Limit Reach'));
  assert.equal(q.ok, false); assert.equal(q.kind, 'quota'); assert.ok(q.error.startsWith('HTTP 402'));
  const t = collector._fmpCallOutcome(new Error('Request timeout'));
  assert.equal(t.ok, false); assert.equal(t.kind, 'other'); assert.equal(t.error, 'Request timeout');
});
