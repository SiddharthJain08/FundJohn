'use strict';
// W4-T3-C3 — route-level behavior of POST /api/strategies/:id/transition after
// it was refactored to call the shared promotion service. Full HTTP testing of
// server.js is heavy; instead we drive the SAME promotion-service contract the
// route calls and assert (1) the exact 422 object the route builds from a gate
// failure, and (2) the class-aware delta: a Sharpe 0.6 / DD 50 fixture FAILS the
// equity gate (DD>20) but PASSES the crypto gate (DD<=70) — the one intended
// behavior change. Equity gate decision is byte-identical to the deleted inline
// CANDIDATE_TO_LIVE_* constants (0.5 / 20).
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { transitionStrategy } = require('../src/lib/promotion_service');

let _n = 0;
function tmpManifest(state, extra) {
  const p = path.join(os.tmpdir(), `t3c3_route_${process.pid}_${_n++}.json`);
  fs.writeFileSync(p, JSON.stringify({ strategies: { X: Object.assign({ state, history: [] }, extra || {}) } }, null, 2));
  return p;
}
function mkQuery(runRow) {
  const calls = [];
  const q = async (sql) => {
    calls.push(sql);
    if (/strategy_backtest_runs/.test(sql)) return { rows: runRow ? [runRow] : [] };
    if (/strategy_registry/.test(sql))      return { rows: [] };
    return { rows: [] };
  };
  q.calls = calls;
  return q;
}
const wroteRegistry = (q) => q.calls.some(s => /INSERT INTO strategy_registry|UPDATE strategy_registry/.test(s));

// EXACT 422 object the route constructs from a gate-failed service result.
// Mirrors src/channels/api/server.js verbatim — if the route shape changes,
// this literal must change too (that's the point of pinning it).
function route422(result) {
  return {
    error: `candidate→live blocked: ${result.failedGates.join(', ')} gate(s) failed`,
    failed_gates: result.failedGates,
    allow_override: true,
  };
}

(async () => {
  // (1) Equity sub-floor candidate→live → 422 with the documented shape; the
  //     C7 registry-first invariant means NOTHING is written.
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ total_sharpe: 0.3, total_max_dd_pct: 5 }); // sub-0.5 Sharpe
    const result = await transitionStrategy({
      dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate',
      force: false, actor: 'manual:dashboard', reason: 'promote to live after passing backtest guards',
      instrumentClass: 'equity', gateApplies: true,
    });
    assert.strictEqual(result.ok, false, 'equity sub-floor blocked');
    assert.ok(result.failedGates && result.failedGates.includes('sharpe'));
    // The route maps this to status 422 with EXACTLY this body:
    assert.deepStrictEqual(route422(result), {
      error: 'candidate→live blocked: sharpe gate(s) failed',
      failed_gates: ['sharpe'],
      allow_override: true,
    });
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'candidate', 'manifest untouched on 422');
    assert.ok(!wroteRegistry(q), 'no registry write on 422 (registry-first invariant)');
    fs.unlinkSync(mp);
  }

  // (2a) Sharpe 0.6 / DD 50 under EQUITY → blocked on max_dd (DD 50 > 20).
  {
    const mp = tmpManifest('candidate');
    const q = mkQuery({ total_sharpe: 0.6, total_max_dd_pct: 50 });
    const result = await transitionStrategy({
      dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate',
      force: false, actor: 'manual:dashboard', reason: 'x', instrumentClass: 'equity', gateApplies: true,
    });
    assert.strictEqual(result.ok, false, 'equity blocks DD 50');
    assert.deepStrictEqual(route422(result), {
      error: 'candidate→live blocked: max_dd gate(s) failed',
      failed_gates: ['max_dd'],
      allow_override: true,
    });
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'candidate');
    fs.unlinkSync(mp);
  }

  // (2b) SAME metrics under CRYPTO (instrument_class) → PASSES (DD 50 <= 70,
  //      Sharpe 0.6 >= 0.50). The transition completes; manifest flips to live.
  {
    const mp = tmpManifest('candidate', { instrument_class: 'crypto' });
    const q = mkQuery({ total_sharpe: 0.6, total_max_dd_pct: 50 });
    const result = await transitionStrategy({
      dbQuery: q, manifestPath: mp, sid: 'X', toState: 'live', fromState: 'candidate',
      force: false, actor: 'manual:dashboard', reason: 'x', instrumentClass: 'crypto', gateApplies: true,
    });
    assert.strictEqual(result.ok, true, 'crypto passes the SAME metrics equity blocked');
    assert.strictEqual(result.weights_rebuild_triggered, true, 'candidate→live adds to active stack');
    assert.strictEqual(JSON.parse(fs.readFileSync(mp)).strategies.X.state, 'live');
    assert.ok(wroteRegistry(q), 'registry synced on the passing class-aware transition');
    fs.unlinkSync(mp);
  }

  console.log('ok test_transition_route_refactor');
})().catch(e => { console.error(e); process.exit(1); });
