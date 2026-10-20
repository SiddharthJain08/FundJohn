const test = require('node:test');
const assert = require('node:assert');
const js = require('../../src/lib/job_stat');

function fakePool({ never = false, fail = false } = {}) {
  const calls = [];
  return {
    calls,
    query(cfg) {
      calls.push({ cfg, at: Date.now() });
      if (never) return new Promise(() => {});
      if (fail) return Promise.reject(new Error('boom'));
      return Promise.resolve({ rows: [{ id: 7 }] });
    },
  };
}
const tick = () => new Promise((r) => setImmediate(r));

test('runTracked calls fn before the start write resolves', () => {
  const pool = fakePool({ never: true });
  let ran = false;
  js.runTracked(pool, 'zz', () => { ran = true; });
  assert.ok(ran);
});

test('started_at is captured before a blocking fn; finished_at after', async () => {
  const pool = fakePool();
  let entry, exit;
  js.runTracked(pool, 'zz', () => {
    entry = new Date();
    const t = Date.now(); while (Date.now() - t < 50) { /* busy */ }
    exit = new Date();
  });
  await tick(); await tick();
  const ins = pool.calls[0].cfg;
  assert.ok(ins.values[2] <= entry, 'started_at param captured before fn');
  const upd = pool.calls[1].cfg;
  assert.match(upd.text, /^UPDATE/);
  assert.ok(upd.values[0] >= exit, 'finished_at param after fn');
  assert.strictEqual(upd.values[4], 7);
});

test('every query carries query_timeout 5000', async () => {
  const pool = fakePool();
  await js.recordStart(pool, 'zz');
  await js.recordFinish(pool, { id: 1, job: 'zz', rc: 0 });
  await js.recordFinish(pool, { job: 'zz', rc: 0 });
  assert.strictEqual(pool.calls.length, 3);
  for (const c of pool.calls) assert.strictEqual(c.cfg.query_timeout, 5000);
  assert.match(pool.calls[2].cfg.values[7], /^no-start-row; /);
});

test('runTracked rethrows the original error and records rc=1', async () => {
  const pool = fakePool();
  const e = new Error('bad');
  assert.throws(() => js.runTracked(pool, 'zz', () => { throw e; }), (x) => x === e);
  await tick(); await tick();
  const upd = pool.calls[1].cfg;
  assert.strictEqual(upd.values[1], false);
  assert.strictEqual(upd.values[2], 1);
  assert.match(upd.values[3], /^threw: bad/);
});

test('runTracked returns the same promise and records resolve/reject', async () => {
  const pool = fakePool();
  const p = Promise.resolve(5);
  assert.strictEqual(js.runTracked(pool, 'zz', () => p), p);
  await tick(); await tick();
  assert.strictEqual(pool.calls[1].cfg.values[2], 0);
  assert.strictEqual(pool.calls[1].cfg.values[3], 'callback-resolved');
  const pool2 = fakePool();
  const rej = Promise.reject(new Error('no'));
  const r = js.runTracked(pool2, 'zz', () => rej);
  assert.strictEqual(r, rej);
  await assert.rejects(r, /no/);
  await tick(); await tick();
  assert.strictEqual(pool2.calls[1].cfg.values[2], 1);
});

test('a failing pool never throws and logs [job_stat]', async () => {
  const pool = fakePool({ fail: true });
  const logs = []; const orig = console.log;
  console.log = (...a) => logs.push(a.join(' '));
  try {
    assert.strictEqual(js.runTracked(pool, 'zz', () => 3), 3);
    await tick(); await tick(); await tick();
  } finally { console.log = orig; }
  assert.ok(logs.some((l) => l.includes('[job_stat] zz start write failed')), logs.join('|'));
});

test('OPENCLAW_JOB_STAT=0 issues no queries', async () => {
  const prev = process.env.OPENCLAW_JOB_STAT;
  process.env.OPENCLAW_JOB_STAT = '0';
  try {
    const pool = fakePool();
    js.runTracked(pool, 'zz', () => 1);
    await tick(); await tick();
    assert.strictEqual(pool.calls.length, 0);
  } finally {
    if (prev === undefined) delete process.env.OPENCLAW_JOB_STAT; else process.env.OPENCLAW_JOB_STAT = prev;
  }
});

test('runTracked rethrows the ORIGINAL value even when it cannot be stringified', async () => {
  const pool = fakePool();
  const e = Object.create(null);
  assert.throws(() => js.runTracked(pool, 'zz', () => { throw e; }), (x) => x === e);
  await tick(); await tick();
  assert.strictEqual(pool.calls[1].cfg.values[1], false);
  assert.match(pool.calls[1].cfg.values[3], /^threw: /);
});

test('runTracked with pool undefined still runs the callback and does not throw', () => {
  let ran = false;
  assert.doesNotThrow(() => js.runTracked(undefined, 'zz', () => { ran = true; }));
  assert.ok(ran);
});

test('runTracked with a pool whose query throws synchronously still runs the callback', () => {
  const pool = { query() { throw new Error('sync boom'); } };
  let ran = false;
  assert.doesNotThrow(() => js.runTracked(pool, 'zz', () => { ran = true; }));
  assert.ok(ran);
});
