// tests/test_registry_sync.js — retriable registry status sync (registry-first gating).
const assert = require('assert');
const { syncRegistryStatus } = require('../src/channels/api/registry_sync');
const noSleep = () => Promise.resolve();

(async () => {
  // 1. success on first try (approved → UPSERT)
  {
    let calls = 0, sql = '';
    await syncRegistryStatus({ dbQuery: async (q) => { calls++; sql = q; }, sid: 'S_x', targetStatus: 'approved', actor: 'op', sleep: noSleep });
    assert.strictEqual(calls, 1);
    assert.ok(/INSERT INTO strategy_registry/.test(sql) && /ON CONFLICT/.test(sql), 'approved → upsert');
  }
  // 2. non-approved → UPDATE path
  {
    let sql = '';
    await syncRegistryStatus({ dbQuery: async (q) => { sql = q; }, sid: 'S_x', targetStatus: 'pending_approval', actor: 'op', sleep: noSleep });
    assert.ok(/UPDATE strategy_registry SET status/.test(sql), 'non-approved → update');
  }
  // 3. retry-then-succeed: fails twice, succeeds on the 3rd attempt
  {
    let calls = 0;
    await syncRegistryStatus({ dbQuery: async () => { calls++; if (calls < 3) throw new Error('blip'); }, sid: 'S_x', targetStatus: 'deprecated', actor: 'op', retries: 3, sleep: noSleep });
    assert.strictEqual(calls, 3);
  }
  // 4. throws after N persistent failures (and tries exactly N times)
  {
    let calls = 0;
    await assert.rejects(
      syncRegistryStatus({ dbQuery: async () => { calls++; throw new Error('down'); }, sid: 'S_x', targetStatus: 'approved', actor: 'op', retries: 3, sleep: noSleep }),
      /registry sync failed after 3 attempts/
    );
    assert.strictEqual(calls, 3);
  }
  console.log('ok test_registry_sync');
})().catch((e) => { console.error(e); process.exit(1); });
