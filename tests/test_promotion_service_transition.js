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
  // (a) gate-fail (candidate:live, non-positive total sharpe, !force) -> ok:false+failedGates; manifest UNCHANGED; NO registry write
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: { total_sharpe: -0.3, total_max_dd_pct: 5, total_trades: 500 } });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true });
    assert.strictEqual(r.ok, false); assert.ok(r.failedGates.includes('sharpe'));
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'candidate');
    assert.ok(!wroteRegistry(q), 'no registry write on gate-fail');
    fs.unlinkSync(mp);
  }
  // (b) registry-sync throw -> ok:false+error; manifest UNCHANGED (C7 invariant)
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: { total_sharpe: 0.9, total_max_dd_pct: 5, total_trades: 500 }, throwRegistry: true });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true });
    assert.strictEqual(r.ok, false); assert.ok(/registry sync/.test(r.error));
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'candidate');
    fs.unlinkSync(mp);
  }
  // (c) happy path candidate->live -> ok; manifest state=live + 1 history; registry synced; weights flag true
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: { total_sharpe: 0.9, total_max_dd_pct: 5, total_trades: 500 } });
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
    const q = mkQuery({ runRow: { total_sharpe: -2, total_max_dd_pct: 90, total_trades: 500 } });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: true, actor: 't', instrumentClass: 'equity', gateApplies: true });
    assert.strictEqual(r.ok, true);
    fs.unlinkSync(mp);
  }
  // (e) non-gated transition (gateApplies:false) skips the gate even with bad metrics
  {
    const mp = tmpManifest('live');
    const q = mkQuery({ runRow: { total_sharpe: -2, total_max_dd_pct: 90, total_trades: 500 } });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'deprecated', fromState: 'live', force: false, actor: 't', instrumentClass: 'equity', gateApplies: false });
    assert.strictEqual(r.ok, true); assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'deprecated');
    assert.strictEqual(r.weights_rebuild_triggered, true); // live -> deprecated removes from active stack
    fs.unlinkSync(mp);
  }
  // (f) manifestMutator runs inside the lock: deletes a manifest field AND
  //     mutates event.metadata, which the lifecycle_events insert then persists.
  {
    const mp = path.join(os.tmpdir(), `t3c3_manifest_${process.pid}_${_n++}.json`);
    fs.writeFileSync(mp, JSON.stringify({ strategies: { X: { state: 'candidate', history: [], foo: 'bar' } } }, null, 2));
    const lifecycleCalls = [];
    const q = async (sql, params) => {
      if (/strategy_backtest_runs/.test(sql)) return { rows: [{ total_sharpe: 0.9, total_max_dd_pct: 5, total_trades: 500 }] };
      if (/strategy_registry/.test(sql))      return { rows: [] };
      if (/lifecycle_events/.test(sql))       lifecycleCalls.push({ sql, params });
      return { rows: [] };
    };
    const mutator = (r, event) => { delete r.foo; event.metadata = Object.assign({}, event.metadata || {}, { x: 1 }); };
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true, manifestMutator: mutator });
    assert.strictEqual(r.ok, true);
    const m = JSON.parse(fs.readFileSync(mp));
    assert.ok(!('foo' in m.strategies.X), 'manifestMutator deleted foo from the manifest record');
    assert.strictEqual(m.strategies.X.state, 'live');
    assert.strictEqual(lifecycleCalls.length, 1, 'exactly one lifecycle_events insert');
    assert.ok(/"x":1/.test(lifecycleCalls[0].params[5]), 'lifecycle_events metadata carries the mutator-set x=1');
    fs.unlinkSync(mp);
  }
  // (g) unknown-toState guard (controller addendum): ok:false with NO registry
  //     and NO manifest write; consistent failure shape carries fromState/toState.
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: { total_sharpe: 0.9, total_max_dd_pct: 5, total_trades: 500 } });
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'bogus_state', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: false });
    assert.strictEqual(r.ok, false);
    assert.ok(/unknown toState/.test(r.error));
    assert.strictEqual(r.fromState, 'candidate'); assert.strictEqual(r.toState, 'bogus_state');
    assert.strictEqual(r.weights_rebuild_triggered, false);
    assert.ok(!wroteRegistry(q), 'no registry write on unknown-toState guard');
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'candidate'); // manifest untouched
    fs.unlinkSync(mp);
  }
  // (h) Option B (2026-07-05): no canonical backtest row at all (registry
  //     mirror retired as a fallback) -> gate hard-fails 'no_backtest';
  //     manifest UNCHANGED; NO registry write. Guards against the old
  //     silent-pass-on-NaN behavior regressing through the transition path.
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ runRow: null }); // no strategy_backtest_runs row
    const r = await transitionStrategy({ dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate', force: false, actor: 't', instrumentClass: 'equity', gateApplies: true });
    assert.strictEqual(r.ok, false); assert.ok(r.failedGates.includes('no_backtest'));
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'candidate');
    assert.ok(!wroteRegistry(q), 'no registry write when gate hard-fails on no_backtest');
    fs.unlinkSync(mp);
  }
  console.log('ok test_promotion_service_transition');
})().catch(e => { console.error(e); process.exit(1); });
