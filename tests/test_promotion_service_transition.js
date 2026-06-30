'use strict';
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { transitionStrategy } = require('../src/lib/promotion_service');

let _n = 0;
function tmpManifest(state) {
  const p = path.join(os.tmpdir(), `t3c2_manifest_${process.pid}_${_n++}.json`);
  fs.writeFileSync(p, JSON.stringify({ strategies: { X: { state, history: [] } } }, null, 2));
  return p;
}
// injected dbQuery: records calls; optionally throws on the registry write
function mkQuery({ runRow, throwRegistry } = {}) {
  const calls = [];
  const q = async (sql) => {
    calls.push(sql);
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_registry/.test(sql))      { if (throwRegistry) throw new Error('db down'); return { rows: [] }; }
    return { rows: [] }; // lifecycle_events etc.
  };
  q.calls = calls;
  return q;
}
const wroteRegistry = (q) => q.calls.some(s => /INSERT INTO strategy_registry|UPDATE strategy_registry/.test(s));

(async () => {
  // (a) gate-fail (candidate:live, sub-floor, !force) -> ok:false+failedGates; manifest UNCHANGED; NO registry write
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: { total_sharpe: 0.3, total_max_dd_pct: 5 } });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true });
    assert.strictEqual(r.ok, false); assert.ok(r.failedGates.includes('sharpe'));
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'candidate');
    assert.ok(!wroteRegistry(q), 'no registry write on gate-fail');
    fs.unlinkSync(mp);
  }
  // (b) registry-sync throw -> ok:false+error; manifest UNCHANGED (C7 invariant)
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: { total_sharpe: 0.9, total_max_dd_pct: 5 }, throwRegistry: true });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true });
    assert.strictEqual(r.ok, false); assert.ok(/registry sync/.test(r.error));
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'candidate');
    fs.unlinkSync(mp);
  }
  // (c) happy path candidate->live -> ok; manifest state=live + 1 history; registry synced; weights flag true
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: { total_sharpe: 0.9, total_max_dd_pct: 5 } });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true });
    assert.strictEqual(r.ok, true); assert.strictEqual(r.weights_rebuild_triggered, true);
    const m = JSON.parse(fs.readFileSync(mp));
    assert.strictEqual(m.strategies.X.state, 'live'); assert.strictEqual(m.strategies.X.history.length, 1);
    assert.ok(wroteRegistry(q), 'registry synced on happy path');
    fs.unlinkSync(mp);
  }
  // (d) force bypasses the gate (sub-floor but force=true) -> ok
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: { total_sharpe: -2, total_max_dd_pct: 90 } });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: true, actor: 't', instrumentClass: 'equity', gateApplies: true });
    assert.strictEqual(r.ok, true);
    fs.unlinkSync(mp);
  }
  // (e) non-gated transition (gateApplies:false) skips the gate even with bad metrics
  {
    const mp = tmpManifest('live');
    const q = mkQuery({ runRow: { total_sharpe: -2, total_max_dd_pct: 90 } });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'deprecated', fromState: 'live', force: false, actor: 't', instrumentClass: 'equity', gateApplies: false });
    assert.strictEqual(r.ok, true); assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'deprecated');
    assert.strictEqual(r.weights_rebuild_triggered, true); // live -> deprecated removes from active stack
    fs.unlinkSync(mp);
  }
  console.log('ok test_promotion_service_transition');
})().catch(e => { console.error(e); process.exit(1); });
