'use strict';

/**
 * /api/regime-eligibility/* routes — operator trim/expand surface.
 *
 * GET  /                      list strategies, current eligibility, latest rollup metrics
 * POST /:strategy             update one strategy (validates, audits, atomic write)
 * GET  /audit?limit=N         recent eligibility changes
 *
 * Reads go straight to Postgres (rollup) + manifest.json (eligibility).
 * Writes shell out to the Python eligibility_manager so the
 * audit-before-write + atomic-replace logic lives in one canonical place.
 */

const express = require('express');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { query } = require('../../database/postgres');

const router = express.Router();

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..');
const MANIFEST_PATH = path.join(REPO_ROOT, 'src', 'strategies', 'manifest.json');
const PY_BIN = process.env.PYTHON_BIN || '/usr/bin/python3';
const PY_ENV = {
  ...process.env,
  PYTHONPATH: process.env.PYTHONPATH
    ? `${path.join(REPO_ROOT, 'src')}:${process.env.PYTHONPATH}`
    : path.join(REPO_ROOT, 'src'),
};


function runPython(args, { timeoutMs = 15_000 } = {}) {
  return new Promise((resolve, reject) => {
    const proc = spawn(PY_BIN, args, { cwd: REPO_ROOT, env: PY_ENV });
    let stdout = '';
    let stderr = '';
    const timer = setTimeout(() => {
      try { proc.kill('SIGKILL'); } catch (_) { /* ignore */ }
      reject(new Error(`python timeout after ${timeoutMs}ms`));
    }, timeoutMs);
    proc.stdout.on('data', (c) => { stdout += c; });
    proc.stderr.on('data', (c) => { stderr += c; });
    proc.on('error', (err) => { clearTimeout(timer); reject(err); });
    proc.on('close', (code) => {
      clearTimeout(timer);
      if (code === 0) return resolve(stdout);
      reject(new Error(`python exit ${code}: ${stderr || stdout}`));
    });
  });
}


function loadManifest() {
  const raw = fs.readFileSync(MANIFEST_PATH, 'utf8');
  const data = JSON.parse(raw);
  const strategies = data.strategies || {};
  return Object.entries(strategies).map(([strategy_id, record]) => ({
    strategy_id,
    eligible_regimes: record && Array.isArray(record.eligible_regimes)
      ? record.eligible_regimes
      : null,
  }));
}


async function loadLatestRollup() {
  const sql = `
    SELECT strategy_id, regime_state, window_days,
           trade_count, win_count,
           total_pnl_pct::float AS total_pnl_pct,
           avg_pnl_pct::float   AS avg_pnl_pct,
           stdev_pnl_pct::float AS stdev_pnl_pct,
           sharpe_proxy::float  AS sharpe_proxy,
           max_dd_proxy::float  AS max_dd_proxy,
           avg_hold_days::float AS avg_hold_days,
           last_signal_at
      FROM strategy_regime_live_pnl_rollup
     WHERE run_at = (SELECT MAX(run_at) FROM strategy_regime_live_pnl_rollup)
  `;
  const result = await query(sql);
  return result.rows;
}


router.get('/', async (req, res) => {
  try {
    const strategies = loadManifest();
    const metrics = await loadLatestRollup();
    const byStrategy = {};
    for (const m of metrics) {
      byStrategy[m.strategy_id] = byStrategy[m.strategy_id] || {};
      byStrategy[m.strategy_id][m.regime_state] = byStrategy[m.strategy_id][m.regime_state] || {};
      byStrategy[m.strategy_id][m.regime_state][m.window_days] = m;
    }
    res.json({
      strategies: strategies.map((s) => ({
        ...s,
        metrics: byStrategy[s.strategy_id] || {},
      })),
    });
  } catch (err) {
    console.error('[regime-eligibility] GET / failed:', err.message);
    res.status(500).json({ error: err.message });
  }
});


router.post('/:strategy', async (req, res) => {
  const { strategy } = req.params;
  const { regimes, actor, reason, source } = req.body || {};
  if (!Array.isArray(regimes) || regimes.length === 0) {
    return res.status(400).json({ error: 'regimes must be a non-empty array' });
  }
  if (typeof actor !== 'string' || !actor.trim()) {
    return res.status(400).json({ error: 'actor required' });
  }
  // Validate strategy/regime tokens before shelling out — defense in depth.
  if (!/^[A-Za-z0-9_:.-]+$/.test(strategy)) {
    return res.status(400).json({ error: 'invalid strategy id' });
  }
  for (const r of regimes) {
    if (typeof r !== 'string' || !/^[A-Z_]+$/.test(r)) {
      return res.status(400).json({ error: `invalid regime token: ${r}` });
    }
  }
  try {
    const args = [
      '-m', 'strategies.eligibility_manager',
      '--set', strategy, ...regimes,
      '--actor', actor,
      '--reason', reason || '',
      '--source', source || '',
    ];
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (err) {
    const msg = err.message || String(err);
    if (/KeyError/.test(msg)) {
      return res.status(404).json({ error: 'unknown strategy' });
    }
    if (/ValueError/.test(msg)) {
      // Strip the python traceback — keep just the ValueError message.
      const m = msg.match(/ValueError:\s*([^\n]+)/);
      return res.status(400).json({ error: m ? m[1].trim() : 'invalid input' });
    }
    console.error('[regime-eligibility] POST failed:', msg);
    res.status(500).json({ error: msg });
  }
});


router.get('/audit', async (req, res) => {
  const limit = Math.min(parseInt(req.query.limit, 10) || 50, 500);
  try {
    const result = await query(`
      SELECT changed_at, actor, strategy_id,
             before_regimes, after_regimes, reason, source
        FROM regime_eligibility_changes
       ORDER BY changed_at DESC
       LIMIT $1
    `, [limit]);
    res.json(result.rows);
  } catch (err) {
    console.error('[regime-eligibility] audit failed:', err.message);
    res.status(500).json({ error: err.message });
  }
});


module.exports = router;
