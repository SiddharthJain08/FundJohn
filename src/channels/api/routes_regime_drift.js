'use strict';

/**
 * /api/regime-drift  — drift signals between live perf and priors/applied baselines
 * /api/regime-priors — list / upsert per-(strategy, regime) literature priors
 *
 * GET  /api/regime-drift?strategy=<id>&regime=<regime>
 * GET  /api/regime-drift/summary
 * GET  /api/regime-priors
 * POST /api/regime-priors/:strategy/:regime
 *
 * Reads use Postgres directly via the query helper; writes shell out to the
 * Python priors_manager so audit-then-upsert stays in one canonical place.
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

// Drift compute — shells out to python (no JS-side reimplementation of severity logic).
async function _drift({ strategy = null, regime = null } = {}) {
  const args = ['-c', `
import json, sys
sys.path.insert(0, '/root/openclaw/src')
from metrics.regime_param_drift import compute_drift
out = compute_drift(
    strategy_id=${strategy ? JSON.stringify(strategy) : 'None'},
    regime_state=${regime ? JSON.stringify(regime) : 'None'},
)
print(json.dumps(out, default=str))
`];
  const out = await runPython(args);
  return JSON.parse(out);
}

router.get('/regime-drift', async (req, res) => {
  const strategy = req.query.strategy ? String(req.query.strategy) : null;
  const regime = req.query.regime ? String(req.query.regime) : null;
  if (strategy && !/^[A-Za-z0-9_:.-]+$/.test(strategy)) {
    return res.status(400).json({ error: 'invalid strategy id' });
  }
  if (regime && !REGIMES.includes(regime)) {
    return res.status(400).json({ error: 'invalid regime' });
  }
  try {
    const signals = await _drift({ strategy, regime });
    res.json({ signals });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.get('/regime-drift/summary', async (req, res) => {
  try {
    const args = ['-c', `
import json, sys
sys.path.insert(0, '/root/openclaw/src')
from metrics.regime_param_drift import latest_drift_summary
print(json.dumps(latest_drift_summary()))
`];
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.get('/regime-priors', async (req, res) => {
  try {
    const result = await query(`
      SELECT strategy_id, regime_state,
             expected_sharpe::float    AS expected_sharpe,
             expected_win_rate::float  AS expected_win_rate,
             expected_avg_pnl_pct::float AS expected_avg_pnl_pct,
             source, confidence::float AS confidence,
             notes, set_at, set_by
        FROM strategy_regime_priors
       ORDER BY strategy_id, regime_state
    `);
    res.json({ priors: result.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/regime-priors/:strategy/:regime', async (req, res) => {
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
  if (typeof body.source !== 'string' || !body.source.trim()) {
    return res.status(400).json({ error: 'source required (e.g. paper citation)' });
  }
  const args = ['-m', 'strategies.priors_manager',
                '--set', strategy, regime,
                '--source', body.source,
                '--actor', body.actor,
                '--reason', body.reason || ''];
  if (typeof body.expected_sharpe      === 'number') args.push('--sharpe', String(body.expected_sharpe));
  if (typeof body.expected_win_rate    === 'number') args.push('--win-rate', String(body.expected_win_rate));
  if (typeof body.expected_avg_pnl_pct === 'number') args.push('--avg-pnl', String(body.expected_avg_pnl_pct));
  if (typeof body.confidence           === 'number') args.push('--confidence', String(body.confidence));
  if (typeof body.notes === 'string') args.push('--notes', body.notes);
  try {
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (e) {
    const msg = e.message || String(e);
    if (/ValueError/.test(msg)) {
      const m = msg.match(/ValueError:\s*([^\n]+)/);
      return res.status(400).json({ error: m ? m[1].trim() : 'invalid input' });
    }
    res.status(500).json({ error: msg });
  }
});

module.exports = router;
