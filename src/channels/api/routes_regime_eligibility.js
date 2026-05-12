'use strict';

/**
 * Back-compat shim for Phase 1 /api/regime-eligibility/* clients. Translates
 * the old "set eligible_regimes to this list" semantics into a series of
 * per-regime upserts on strategy_regime_params via the new endpoint logic.
 *
 * Keeps old callers (CLI scripts, dashboard cells from before the Phase 2A
 * rewrite if any are still cached) working without code changes.
 */
const express = require('express');
const path = require('path');
const { spawn } = require('child_process');
const { query } = require('../../database/postgres');

const router = express.Router();

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const PY_BIN = process.env.PYTHON_BIN || '/usr/bin/python3';
const PY_ENV = {
  ...process.env,
  PYTHONPATH: process.env.PYTHONPATH
    ? `${path.join(REPO_ROOT, 'src')}:${process.env.PYTHONPATH}`
    : path.join(REPO_ROOT, 'src'),
};
const REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];

function runPython(args, { timeoutMs = 15_000 } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(PY_BIN, args, { cwd: REPO_ROOT, env: PY_ENV });
    let stdout = '', stderr = '';
    const t = setTimeout(() => { try { proc.kill('SIGKILL'); } catch (_) {} reject(new Error(`python timeout after ${timeoutMs}ms`)); }, timeoutMs);
    proc.stdout.on('data', (c) => { stdout += c; });
    proc.stderr.on('data', (c) => { stderr += c; });
    proc.on('error', (e) => { clearTimeout(t); reject(e); });
    proc.on('close', (code) => { clearTimeout(t); code === 0 ? resolve(stdout) : reject(new Error(`python exit ${code}: ${stderr || stdout}`)); });
  });
}

router.get('/', async (req, res) => {
  // Return a /api/regime-eligibility-shaped payload synthesized from the DB.
  try {
    const fs = require('fs');
    const mfPath = path.join(REPO_ROOT, 'src/strategies/manifest.json');
    const manifest = JSON.parse(fs.readFileSync(mfPath, 'utf8'));
    const result = await query(`
      SELECT strategy_id, regime_state, eligible
        FROM strategy_regime_params
    `);
    const byStrat = {};
    for (const row of result.rows) {
      byStrat[row.strategy_id] = byStrat[row.strategy_id] || new Set();
      if (row.eligible) byStrat[row.strategy_id].add(row.regime_state);
    }
    const strategies = Object.entries(manifest.strategies || {}).map(([sid]) => ({
      strategy_id:      sid,
      eligible_regimes: byStrat[sid]
        ? [...byStrat[sid]].filter(r => REGIMES.includes(r))
                            .sort((a, b) => REGIMES.indexOf(a) - REGIMES.indexOf(b))
        : null,
      metrics: {},
    }));
    res.json({ strategies });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/:strategy', express.json(), async (req, res) => {
  const { strategy } = req.params;
  const { regimes, actor, reason, source } = req.body || {};
  if (!Array.isArray(regimes) || regimes.length === 0) {
    return res.status(400).json({ error: 'regimes must be a non-empty array' });
  }
  if (typeof actor !== 'string' || !actor.trim()) {
    return res.status(400).json({ error: 'actor required' });
  }
  if (!/^[A-Za-z0-9_:.-]+$/.test(strategy)) {
    return res.status(400).json({ error: 'invalid strategy id' });
  }
  for (const r of regimes) {
    if (!REGIMES.includes(r)) return res.status(400).json({ error: `invalid regime: ${r}` });
  }
  const errors = [];
  for (const regime of REGIMES) {
    const eligibleFlag = regimes.includes(regime) ? '--eligible' : '--ineligible';
    const args = ['-m', 'strategies.eligibility_manager',
                  '--set', strategy, regime, eligibleFlag,
                  '--actor', actor, '--reason', reason || '',
                  '--source', source || 'shim'];
    try { await runPython(args); }
    catch (err) {
      const msg = err.message || String(err);
      if (/KeyError/.test(msg)) return res.status(404).json({ error: 'unknown strategy' });
      if (/ValueError/.test(msg)) {
        const m = msg.match(/ValueError:\s*([^\n]+)/);
        return res.status(400).json({ error: m ? m[1].trim() : 'invalid input' });
      }
      errors.push(msg);
    }
  }
  if (errors.length) return res.status(500).json({ error: errors.join('; ') });
  res.json({
    strategy_id:   strategy,
    after_regimes: regimes,
    note: 'Phase 1 shim wrote to DB via /api/regime-params; ' +
          'old /api/regime-eligibility surface preserved.',
  });
});

router.get('/audit', async (req, res) => {
  const limit = Math.min(parseInt(req.query.limit, 10) || 50, 500);
  try {
    const result = await query(`
      SELECT changed_at, actor, strategy_id,
             before_row->'eligible' AS before_eligible,
             after_row->'eligible'  AS after_eligible,
             regime_state, reason, source
        FROM strategy_regime_param_changes
       ORDER BY changed_at DESC
       LIMIT $1
    `, [limit]);
    res.json(result.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
