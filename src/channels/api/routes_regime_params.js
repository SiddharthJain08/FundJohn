'use strict';

/**
 * /api/regime-params/* — operator surface for per-(strategy, regime) params.
 *
 * GET  /                              list all rows
 * POST /:strategy/:regime             upsert one row
 * GET  /audit?limit=N                 recent audit rows
 *
 * All writes shell out to the Python eligibility_manager so the audit-then-
 * upsert transaction + cache invalidation stays in one canonical place.
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
    const t = setTimeout(() => {
      try { proc.kill('SIGKILL'); } catch (_) {}
      reject(new Error(`python timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    proc.stdout.on('data', (c) => { stdout += c; });
    proc.stderr.on('data', (c) => { stderr += c; });
    proc.on('error', (e) => { clearTimeout(t); reject(e); });
    proc.on('close', (code) => {
      clearTimeout(t);
      if (code === 0) resolve(stdout);
      else reject(new Error(`python exit ${code}: ${stderr || stdout}`));
    });
  });
}

router.get('/', async (req, res) => {
  try {
    const result = await query(`
      SELECT strategy_id, regime_state, eligible,
             size_scalar::float AS size_scalar,
             stop_pct::float    AS stop_pct,
             target_pct::float  AS target_pct,
             max_hold_days
        FROM strategy_regime_params
       ORDER BY strategy_id, regime_state
    `);
    res.json({ rows: result.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/:strategy/:regime', async (req, res) => {
  const { strategy, regime } = req.params;
  const body = req.body || {};
  if (!REGIMES.includes(regime)) {
    return res.status(400).json({ error: `invalid regime: ${regime}` });
  }
  if (!/^[A-Za-z0-9_:.-]+$/.test(strategy)) {
    return res.status(400).json({ error: 'invalid strategy id' });
  }
  if (typeof body.actor !== 'string' || !body.actor.trim()) {
    return res.status(400).json({ error: 'actor required' });
  }
  const args = ['-m', 'strategies.eligibility_manager',
                '--set', strategy, regime,
                '--actor', body.actor,
                '--reason', body.reason || '',
                '--source', body.source || 'dashboard'];
  if (body.eligible === true)  args.push('--eligible');
  if (body.eligible === false) args.push('--ineligible');
  if (typeof body.size_scalar   === 'number') args.push('--size', String(body.size_scalar));
  if (typeof body.stop_pct      === 'number') args.push('--stop', String(body.stop_pct));
  if (typeof body.target_pct    === 'number') args.push('--target', String(body.target_pct));

  try {
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (err) {
    const msg = err.message || String(err);
    if (/ValueError/.test(msg)) {
      const m = msg.match(/ValueError:\s*([^\n]+)/);
      return res.status(400).json({ error: m ? m[1].trim() : 'invalid input' });
    }
    res.status(500).json({ error: msg });
  }
});

router.get('/audit', async (req, res) => {
  const limit = Math.min(parseInt(req.query.limit, 10) || 50, 500);
  try {
    const result = await query(`
      SELECT changed_at, actor, strategy_id, regime_state,
             before_row, after_row, reason, source
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
