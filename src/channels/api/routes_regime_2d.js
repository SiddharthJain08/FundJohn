'use strict';

/**
 * Phase 2D — /api/regime-mc, /api/mastermind/calibration, /api/strategy-overlap
 *
 * Reads use Postgres directly; one-off MC runs shell out to Python.
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

function runPython(args, { timeoutMs = 30_000 } = {}) {
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

const REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];

// POST /api/regime-mc/:strategy/:regime
// body: { current_size, proposed_size, n_iter?, window_days?, seed?, proposal_id? }
router.post('/regime-mc/:strategy/:regime', async (req, res) => {
  const { strategy, regime } = req.params;
  const body = req.body || {};
  if (!REGIMES.includes(regime)) return res.status(400).json({ error: 'invalid regime' });
  if (!/^[A-Za-z0-9_:.-]+$/.test(strategy)) return res.status(400).json({ error: 'invalid strategy id' });
  if (typeof body.current_size !== 'number' || typeof body.proposed_size !== 'number') {
    return res.status(400).json({ error: 'current_size and proposed_size required' });
  }
  const args = ['-m', 'metrics.regime_param_montecarlo',
                '--strategy', strategy, '--regime', regime,
                '--current', String(body.current_size),
                '--proposed', String(body.proposed_size)];
  if (typeof body.n_iter === 'number') args.push('--n-iter', String(body.n_iter));
  if (typeof body.window_days === 'number') args.push('--window-days', String(body.window_days));
  if (typeof body.seed === 'number') args.push('--seed', String(body.seed));
  try {
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GET /api/mastermind/calibration
router.get('/mastermind/calibration', async (req, res) => {
  try {
    const args = ['-c', `
import json, sys
sys.path.insert(0, '/root/openclaw/src')
from metrics.mastermind_calibration import calibration_report
print(json.dumps(calibration_report(), default=str))
`];
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GET /api/strategy-overlap/top?limit=20&min_overlap=2
router.get('/strategy-overlap/top', async (req, res) => {
  const limit = Math.min(parseInt(req.query.limit, 10) || 20, 200);
  const minOverlap = parseInt(req.query.min_overlap, 10) || 2;
  try {
    const result = await query(`
      SELECT strategy_a, strategy_b, regime_state,
             overlap_count, a_signal_count, b_signal_count,
             jaccard_idx::float AS jaccard_idx
        FROM strategy_signal_overlap
       WHERE computed_at = (SELECT MAX(computed_at) FROM strategy_signal_overlap)
         AND regime_state = 'ANY'
         AND overlap_count >= $1
       ORDER BY jaccard_idx DESC NULLS LAST
       LIMIT $2
    `, [minOverlap, limit]);
    res.json({ pairs: result.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /api/regime-mc-intraday/:strategy/:regime — Phase 2E path-MC
// body: { current_size, proposed_size, proposed_stop_pct, proposed_target_pct,
//         proposed_max_hold_days, n_iter?, window_days?, seed?, proposal_id? }
// Response: full path-MC result + linear_mc_diff if a 2D run exists for the
// same proposal_id (max_dd_p95 + sharpe_p50 deltas).
router.post('/regime-mc-intraday/:strategy/:regime', async (req, res) => {
  const { strategy, regime } = req.params;
  const body = req.body || {};
  if (!REGIMES.includes(regime)) return res.status(400).json({ error: 'invalid regime' });
  if (!/^[A-Za-z0-9_:.-]+$/.test(strategy)) return res.status(400).json({ error: 'invalid strategy id' });
  const required = ['current_size', 'proposed_size', 'proposed_stop_pct',
                     'proposed_target_pct', 'proposed_max_hold_days'];
  for (const k of required) {
    if (typeof body[k] !== 'number') return res.status(400).json({ error: `${k} required (number)` });
  }
  const args = ['-m', 'metrics.intraday_path_montecarlo',
                '--strategy', strategy, '--regime', regime,
                '--current', String(body.current_size),
                '--proposed', String(body.proposed_size),
                '--stop-pct', String(body.proposed_stop_pct),
                '--target-pct', String(body.proposed_target_pct),
                '--max-hold-days', String(body.proposed_max_hold_days)];
  if (typeof body.n_iter === 'number') args.push('--n-iter', String(body.n_iter));
  if (typeof body.window_days === 'number') args.push('--window-days', String(body.window_days));
  if (typeof body.seed === 'number') args.push('--seed', String(body.seed));
  if (typeof body.proposal_id === 'number') args.push('--proposal-id', String(body.proposal_id));
  try {
    const out = await runPython(args, { timeoutMs: 60_000 });
    const result = JSON.parse(out);
    if (typeof body.proposal_id === 'number') {
      try {
        const linear = await query(`
          SELECT sharpe_p50::float AS sharpe_p50, max_dd_p95::float AS max_dd_p95
            FROM strategy_regime_mc_runs
           WHERE proposal_id = $1
           ORDER BY run_at DESC LIMIT 1`, [body.proposal_id]);
        if (linear.rows.length) {
          const L = linear.rows[0];
          result.linear_mc_diff = {
            sharpe_p50_delta: (result.sharpe_p50 ?? null) === null || L.sharpe_p50 === null
              ? null : (result.sharpe_p50 - L.sharpe_p50),
            max_dd_p95_delta: (result.max_dd_p95 ?? null) === null || L.max_dd_p95 === null
              ? null : (result.max_dd_p95 - L.max_dd_p95),
          };
        }
      } catch (_) { /* linear_mc_diff is best-effort */ }
    }
    res.json(result);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
