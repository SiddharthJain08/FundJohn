'use strict';
// D5 (2026-08-23): /stable/earnings-surprises does not exist (404). The
// actual-vs-estimate payload lives at /stable/earnings?symbol= — same
// date/epsActual/epsEstimated fields the consumers read.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const ROOT = path.resolve(__dirname, '..', '..');

test('generated sub-agent FMP tool hits /stable/earnings, never earnings-surprises', () => {
  const { generatePython } = require(path.join(ROOT, 'src/agent/tools/mcp/fmp.js'));
  const src = generatePython({ name: 'fmp', description: 'test' });
  assert.ok(!src.includes('earnings-surprises'), 'earnings-surprises is 404 on /stable/');
  assert.ok(/_get\("earnings",/.test(src), 'expected _get("earnings", …)');
});

test('snapshot earnings-calendar builds a /stable/earnings URL and returns upcoming rows', async () => {
  process.env.FMP_API_KEY = 'k';
  const calls = [];
  const realFetch = global.fetch;
  global.fetch = async (url) => {
    calls.push(url);
    return { ok: true, json: async () => [
      { symbol: 'AAPL', date: '2099-10-29', epsActual: null, epsEstimated: 1.98 },
      { symbol: 'AAPL', date: '2020-07-30', epsActual: 2.02, epsEstimated: 1.89 },
    ] };
  };
  const redisPath = path.join(ROOT, 'src/database/redis.js');
  require.cache[require.resolve(redisPath)] = { id: redisPath, filename: redisPath, loaded: true,
    exports: { cacheGet: async () => null, cacheSet: async () => null } };
  try {
    const mod = require(path.join(ROOT, 'src/agent/tools/snapshot/earnings-calendar.js'));
    const rows = await mod.get('AAPL');
    assert.equal(calls.length, 1);
    assert.ok(calls[0].startsWith('https://financialmodelingprep.com/stable/earnings?symbol=AAPL'), calls[0]);
    assert.ok(!calls[0].includes('earnings-surprises'));
    assert.deepEqual(rows.map(r => r.date), ['2099-10-29']);
  } finally {
    global.fetch = realFetch;
  }
});
