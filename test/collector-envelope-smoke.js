// test/collector-envelope-smoke.js
// SP-7 Phase C Task 10 — resolver envelope merge: union + active=false hard
// exclusion + dot→dash bridge + never-shrink fail-open.
// Run: node test/collector-envelope-smoke.js
'use strict';

const assert = require('node:assert');
const path = require('node:path');
const Module = require('module');

const ROOT = path.resolve(__dirname, '..');

let execCalls = [];
let execResult = JSON.stringify(['AAPL', 'BRK.B', 'NEWT', 'BADCO']);
let execThrows = false;
const redisStore = {};

const origLoad = Module._load;
Module._load = function (request, parent, ...rest) {
  if (request === 'child_process') {
    return {
      execSync: (cmd, opts) => {
        execCalls.push(cmd);
        if (execThrows) throw new Error('resolver down');
        return execResult;
      },
    };
  }
  if (request.includes('database/redis')) {
    return {
      getClient: () => ({
        get: async (k) => redisStore[k] || null,
        set: async (k, v, exFlag, exSecs) => {
          assert.strictEqual(exFlag, 'EX', 'ioredis positional EX required');
          assert.strictEqual(typeof exSecs, 'number');
          redisStore[k] = v;
        },
      }),
    };
  }
  if (request.includes('database/postgres')) {
    return {
      query: async (text) => {
        if (/active = false/.test(text)) return { rows: [{ ticker: 'BADCO' }] };
        if (/active = true/.test(text)) {
          return { rows: [
            { ticker: 'AAPL', category: 'equity', has_options: true, has_fundamentals: true },
            { ticker: 'SPY', category: 'etf', has_options: true, has_fundamentals: false },
          ] };
        }
        return { rows: [] };
      },
    };
  }
  if (request.includes('data/parquet_store')) {
    return new Proxy({}, { get: () => async () => ({}) });
  }
  if (request.includes('budget/enforcer')) {
    return { checkBudget: async () => ({ mode: 'GREEN' }), enforceBudget: () => ({}) };
  }
  return origLoad.call(this, request, parent, ...rest);
};

const collector = require(path.join(ROOT, 'src/pipeline/collector.js'));

(async () => {
  const base = ['AAPL'];

  // 1. Gate OFF → identity
  delete process.env.OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE;
  assert.deepStrictEqual(await collector.applyResolverEnvelope(base, '2026-06-08'), base);
  assert.strictEqual(execCalls.length, 0, 'no resolver call when gate off');

  // 2. Gate ON → union + dot→dash + active=false exclusion
  process.env.OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE = '1';
  const merged = await collector.applyResolverEnvelope(base, '2026-06-08');
  assert.ok(merged.includes('AAPL'));
  assert.ok(merged.includes('BRK-B'), 'dot→dash bridge');
  assert.ok(merged.includes('NEWT'), 'envelope name fetched');
  assert.ok(!merged.includes('BADCO'), 'active=false is a hard exclusion');
  assert.ok(merged.length >= base.length, 'never shrink');
  assert.ok(execCalls[0].includes('--envelope'), 'uses the no-floor envelope');

  // 3. Resolver failure → fail-open to config list
  execThrows = true;
  delete redisStore['universe:envelope:2026-06-08:live'];
  assert.deepStrictEqual(await collector.applyResolverEnvelope(base, '2026-06-08'), base);

  // 4. Adopted-union scope helper (fundamentals/insider — spec §5)
  execThrows = false;
  execCalls = [];
  execResult = JSON.stringify(['AAPL', 'BRK.B', 'ADOPTED1']);
  const scoped = await collector.adoptedUnionScope(['AAPL'], '2026-06-08');
  assert.ok(scoped.includes('ADOPTED1'), 'adopted name in scope');
  assert.ok(scoped.includes('AAPL'), 'config name kept (expansion-only)');
  assert.ok(!scoped.includes('BADCO'), 'active=false excluded');
  assert.ok(!execCalls[0].includes('--envelope'), 'fundamentals use the FLOORED union');

  // 5. Union failure → config scope unchanged
  execThrows = true;
  delete redisStore['universe:union:2026-06-08:live'];
  assert.deepStrictEqual(await collector.adoptedUnionScope(['AAPL'], '2026-06-08'), ['AAPL']);

  console.log('collector-envelope-smoke: ALL PASS');
  process.exit(0);
})().catch((e) => {
  console.error('FAIL:', e.message);
  process.exit(1);
});
