'use strict';
// Tests for approveStrategyCommand — the DI-injected helper extracted from
// the Discord /approve-strategy case (W4-T3-C4). Tests the per-manifest-state
// routing without standing up Discord or a real DB.
const assert = require('assert');
const fs     = require('fs');
const os     = require('os');
const path   = require('path');

const { approveStrategyCommand } = require('../src/channels/discord/relay');

let _n = 0;

/** Write a temp manifest with the given strategies map and return its path. */
function tmpManifest(strats) {
  const p = path.join(os.tmpdir(), `t3c4_manifest_${process.pid}_${_n++}.json`);
  fs.writeFileSync(p, JSON.stringify({ strategies: strats }, null, 2));
  return p;
}

/** Build a call-recording mock set. */
function mkMocks({ transitionResult, syncShouldThrow } = {}) {
  const calls = { transitionStrategy: [], syncRegistryStatus: [], replies: [] };
  const reply = async (msg) => calls.replies.push(msg);
  const transitionStrategy = async (opts) => {
    calls.transitionStrategy.push(opts);
    return transitionResult !== undefined ? transitionResult : { ok: true, weights_rebuild_triggered: false };
  };
  const syncRegistryStatus = async (opts) => {
    calls.syncRegistryStatus.push(opts);
    if (syncShouldThrow) throw new Error('db error');
  };
  return { calls, reply, transitionStrategy, syncRegistryStatus };
}

(async () => {
  // 1. candidate + sub-floor (gate fail) → blocked reply; transitionStrategy called
  //    with gateApplies:true + force:false; syncRegistryStatus NOT called
  {
    const mp = tmpManifest({ S1: { state: 'candidate', instrument_class: 'equity', history: [] } });
    const { calls, reply, transitionStrategy, syncRegistryStatus } = mkMocks({
      transitionResult: { ok: false, failedGates: ['sharpe'] },
    });
    await approveStrategyCommand({ stratId: 'S1', force: false, manifestPath: mp,
      pgQuery: async () => ({ rows: [] }), reply, transitionStrategy, syncRegistryStatus });
    assert.strictEqual(calls.transitionStrategy.length, 1, 'transitionStrategy must be called');
    assert.strictEqual(calls.transitionStrategy[0].gateApplies, true);
    assert.strictEqual(calls.transitionStrategy[0].force, false);
    assert.ok(calls.replies[0].toLowerCase().includes('blocked'),
      `Expected 'blocked' in reply: ${calls.replies[0]}`);
    assert.strictEqual(calls.syncRegistryStatus.length, 0, 'syncRegistryStatus must not be called on gate-fail');
    fs.unlinkSync(mp);
    console.log('  ✓ candidate sub-floor → blocked reply, no syncRegistryStatus');
  }

  // 2. candidate + force → transitionStrategy called with force:true, success reply
  {
    const mp = tmpManifest({ S1: { state: 'candidate', instrument_class: 'equity', history: [] } });
    const { calls, reply, transitionStrategy, syncRegistryStatus } = mkMocks();
    await approveStrategyCommand({ stratId: 'S1', force: true, manifestPath: mp,
      pgQuery: async () => ({ rows: [] }), reply, transitionStrategy, syncRegistryStatus });
    assert.strictEqual(calls.transitionStrategy.length, 1);
    assert.strictEqual(calls.transitionStrategy[0].force, true);
    assert.ok(
      calls.replies[0].includes('promoted') || calls.replies[0].includes('approved'),
      `Expected success reply: ${calls.replies[0]}`
    );
    fs.unlinkSync(mp);
    console.log('  ✓ candidate + force → force:true passed, success reply');
  }

  // 3. candidate + passing gate → success reply (weights_rebuild_triggered:false to suppress spawn)
  {
    const mp = tmpManifest({ S1: { state: 'candidate', instrument_class: 'equity', history: [] } });
    const { calls, reply, transitionStrategy, syncRegistryStatus } = mkMocks({
      transitionResult: { ok: true, weights_rebuild_triggered: false },
    });
    await approveStrategyCommand({ stratId: 'S1', force: false, manifestPath: mp,
      pgQuery: async () => ({ rows: [] }), reply, transitionStrategy, syncRegistryStatus });
    assert.strictEqual(calls.transitionStrategy.length, 1);
    assert.ok(
      calls.replies[0].includes('promoted') || calls.replies[0].includes('approved'),
      `Expected success reply: ${calls.replies[0]}`
    );
    assert.strictEqual(calls.syncRegistryStatus.length, 0, 'direct syncRegistryStatus not called; transitionStrategy handles it');
    fs.unlinkSync(mp);
    console.log('  ✓ candidate passing gate → success reply');
  }

  // 4. live (paused) → syncRegistryStatus(approved) called, transitionStrategy NOT called, "active" reply
  {
    const mp = tmpManifest({ S1: { state: 'live', instrument_class: 'equity', history: [] } });
    const { calls, reply, transitionStrategy, syncRegistryStatus } = mkMocks();
    await approveStrategyCommand({ stratId: 'S1', force: false, manifestPath: mp,
      pgQuery: async () => ({ rows: [] }), reply, transitionStrategy, syncRegistryStatus });
    assert.strictEqual(calls.transitionStrategy.length, 0, 'transitionStrategy must NOT be called for live resume');
    assert.strictEqual(calls.syncRegistryStatus.length, 1);
    assert.strictEqual(calls.syncRegistryStatus[0].targetStatus, 'approved');
    assert.strictEqual(calls.syncRegistryStatus[0].sid, 'S1');
    assert.ok(calls.replies[0].includes('active'), `Expected 'active' in reply: ${calls.replies[0]}`);
    fs.unlinkSync(mp);
    console.log('  ✓ live (resume) → syncRegistryStatus approved, no gate, active reply');
  }

  // 4b. monitoring → same resume path (no gate, no manifest change)
  {
    const mp = tmpManifest({ S1: { state: 'monitoring', instrument_class: 'equity', history: [] } });
    const { calls, reply, transitionStrategy, syncRegistryStatus } = mkMocks();
    await approveStrategyCommand({ stratId: 'S1', force: false, manifestPath: mp,
      pgQuery: async () => ({ rows: [] }), reply, transitionStrategy, syncRegistryStatus });
    assert.strictEqual(calls.transitionStrategy.length, 0);
    assert.strictEqual(calls.syncRegistryStatus.length, 1);
    assert.strictEqual(calls.syncRegistryStatus[0].targetStatus, 'approved');
    assert.ok(calls.replies[0].includes('active'), `Expected 'active' in reply: ${calls.replies[0]}`);
    fs.unlinkSync(mp);
    console.log('  ✓ monitoring (resume) → syncRegistryStatus approved, no gate, active reply');
  }

  // 5. staging → refuse reply, neither transitionStrategy nor syncRegistryStatus called
  {
    const mp = tmpManifest({ S1: { state: 'staging', instrument_class: 'equity', history: [] } });
    const { calls, reply, transitionStrategy, syncRegistryStatus } = mkMocks();
    await approveStrategyCommand({ stratId: 'S1', force: false, manifestPath: mp,
      pgQuery: async () => ({ rows: [] }), reply, transitionStrategy, syncRegistryStatus });
    assert.strictEqual(calls.transitionStrategy.length, 0);
    assert.strictEqual(calls.syncRegistryStatus.length, 0);
    assert.ok(
      calls.replies[0].includes('staging') || calls.replies[0].includes('dashboard'),
      `Expected staging/dashboard in reply: ${calls.replies[0]}`
    );
    fs.unlinkSync(mp);
    console.log('  ✓ staging → refuse (dashboard/pipeline)');
  }

  // 6. deprecated → refuse, neither called
  {
    const mp = tmpManifest({ S1: { state: 'deprecated', instrument_class: 'equity', history: [] } });
    const { calls, reply, transitionStrategy, syncRegistryStatus } = mkMocks();
    await approveStrategyCommand({ stratId: 'S1', force: false, manifestPath: mp,
      pgQuery: async () => ({ rows: [] }), reply, transitionStrategy, syncRegistryStatus });
    assert.strictEqual(calls.transitionStrategy.length, 0);
    assert.strictEqual(calls.syncRegistryStatus.length, 0);
    assert.ok(
      calls.replies[0].includes('deprecated') || calls.replies[0].includes('dashboard'),
      `Expected deprecated/dashboard in reply: ${calls.replies[0]}`
    );
    fs.unlinkSync(mp);
    console.log('  ✓ deprecated → refuse');
  }

  // 7. strategy not in manifest → "not found" reply, neither called
  {
    const mp = tmpManifest({ OTHER: { state: 'candidate', history: [] } });
    const { calls, reply, transitionStrategy, syncRegistryStatus } = mkMocks();
    await approveStrategyCommand({ stratId: 'MISSING', force: false, manifestPath: mp,
      pgQuery: async () => ({ rows: [] }), reply, transitionStrategy, syncRegistryStatus });
    assert.strictEqual(calls.transitionStrategy.length, 0);
    assert.strictEqual(calls.syncRegistryStatus.length, 0);
    assert.ok(
      calls.replies[0].toLowerCase().includes('not found') || calls.replies[0].includes('MISSING'),
      `Expected not-found reply: ${calls.replies[0]}`
    );
    fs.unlinkSync(mp);
    console.log('  ✓ not in manifest → not found reply');
  }

  console.log('ok test_approve_strategy_relay');
})().catch(e => { console.error(e); process.exit(1); });
