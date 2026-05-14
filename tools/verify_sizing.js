#!/usr/bin/env node
// tools/verify_sizing.js — math-invariant probe for the Sharpe×cadence sizer.
//
// Per-regime: Σ weight = 1.0 ± 1e-6 (snapshot from strategy_weights_by_regime)
// Per-cycle (when a sized handoff is available):
//   - |Σ position_usd − λ × NAV| < $1
//   - max |order.notional_usd| ≤ 0.25 × NAV + $1
//
// Runs against the live DB + the most recent sized handoff under
// workspaces/default/data. Skips per-cycle checks gracefully if no
// handoff has been written by the new sizer yet.

const fs = require('fs');
const path = require('path');
const { Pool } = require('pg');

const BASE = process.env.OPENCLAW_HOME || '/root/openclaw';

(async () => {
  const pool = new Pool({ connectionString: process.env.POSTGRES_URI });
  let exit = 0;

  // 1. Per-regime weight invariant
  const sums = await pool.query(`
    SELECT regime_state, ROUND(SUM(weight)::numeric, 6) AS s, COUNT(*)::int AS n
    FROM strategy_weights_by_regime WHERE is_current
    GROUP BY regime_state ORDER BY regime_state
  `);
  console.log('=== per-regime weight invariant ===');
  for (const r of sums.rows) {
    const ok = Math.abs(parseFloat(r.s) - 1.0) < 1e-6;
    console.log(`  ${r.regime_state.padEnd(14)} n=${String(r.n).padStart(3)} Σw=${r.s}  ${ok ? 'PASS' : 'FAIL'}`);
    if (!ok) exit = 1;
  }
  if (!sums.rows.length) console.log('  (no current weight rows — has strategy_weights.rebuild() been run?)');

  // 2. Sample sample-size blend invariant — verify _effective_sharpe formula on a known row.
  // We replay (bt_n × bt_sharpe + live_n × live_sharpe) / (bt_n + live_n) and
  // compare against stored effective_sharpe.
  const sample = await pool.query(`
    SELECT strategy_id, regime_state, bt_sharpe, bt_n, live_sharpe, live_n, effective_sharpe
    FROM strategy_weights_by_regime
    WHERE is_current AND bt_n IS NOT NULL AND bt_n > 0
    ORDER BY weight DESC LIMIT 5
  `);
  console.log('\n=== sample-size blend invariant (top 5 by weight) ===');
  for (const r of sample.rows) {
    const bt_s = r.bt_sharpe == null ? null : parseFloat(r.bt_sharpe);
    const bt_n = r.bt_n == null ? null : parseInt(r.bt_n);
    const lv_s = r.live_sharpe == null ? null : parseFloat(r.live_sharpe);
    const lv_n = r.live_n == null ? null : parseInt(r.live_n);
    const eff  = parseFloat(r.effective_sharpe);
    let expected;
    if (bt_n && bt_s != null && lv_n && lv_s != null)
      expected = (bt_n * bt_s + lv_n * lv_s) / (bt_n + lv_n);
    else if (bt_n && bt_s != null)
      expected = bt_s;
    else if (lv_n && lv_s != null)
      expected = lv_s;
    else expected = null;
    const ok = expected != null && Math.abs(eff - expected) < 1e-6;
    console.log(`  ${r.strategy_id.padEnd(32)} ${r.regime_state.padEnd(14)} stored=${eff.toFixed(4)} expected=${expected?.toFixed(4) ?? 'n/a'}  ${ok ? 'PASS' : 'FAIL'}`);
    if (!ok) exit = 1;
  }

  // 3. Lambda invariant — only checkable when a sized handoff is on disk
  const handoffsDir = path.join(BASE, 'workspaces/default/data');
  let candidates = [];
  try {
    candidates = fs.readdirSync(handoffsDir).filter(f => /sized.*\.json$/.test(f)).sort().reverse();
  } catch (_) {}
  if (!candidates.length) {
    console.log('\n=== lambda invariant ===');
    console.log('  (no sized handoff on disk yet — run a cycle with OPENCLAW_SHARPE_CADENCE_SIZER=1 first)');
  } else {
    const file = path.join(handoffsDir, candidates[0]);
    const sized = JSON.parse(fs.readFileSync(file, 'utf8'));
    const orders = sized.orders || [];
    const nav = sized.account_equity || sized.equity || sized.nav || sized.account?.equity || 100000;
    const sumUsd = orders.reduce((s, o) => s + (o.notional_usd || 0), 0);
    const lamRow = await pool.query("SELECT value FROM pipeline_config WHERE key='position_sizing_lambda'");
    const lam = parseFloat(lamRow.rows[0]?.value || 2.0);
    const expected = lam * nav;
    const diff = Math.abs(sumUsd - expected);
    const ok1 = diff < 1.0;
    console.log(`\n=== lambda invariant (handoff: ${path.basename(file)}) ===`);
    console.log(`  Σ usd       = $${sumUsd.toFixed(0)}`);
    console.log(`  λ × NAV     = $${expected.toFixed(0)} (λ=${lam}, NAV=$${nav})`);
    console.log(`  Δ           = $${diff.toFixed(2)}  ${ok1 ? 'PASS' : 'FAIL'}`);
    if (!ok1) exit = 1;
    const maxOrder = orders.length ? Math.max(...orders.map(o => o.notional_usd || 0)) : 0;
    const cap = 0.25 * nav;
    const ok2 = maxOrder <= cap + 1;
    console.log(`  max order   = $${maxOrder.toFixed(0)}  cap=$${cap.toFixed(0)}  ${ok2 ? 'PASS' : 'FAIL'}`);
    if (!ok2) exit = 1;
  }

  await pool.end();
  process.exit(exit);
})().catch(e => { console.error(e); process.exit(1); });
