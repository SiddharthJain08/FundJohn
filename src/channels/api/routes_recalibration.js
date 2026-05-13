'use strict';

/**
 * Phase 2F — /api/recalibration/*
 *
 * Mastermind prompt-recalibration loop endpoints. All write paths require
 * decided_by in body; FOR UPDATE locks live in the Python module.
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

const ALLOWED_STATUSES = new Set(['pending', 'active', 'expired', 'rejected', 'superseded', 'all']);

// GET /api/recalibration/addenda?status=active|pending|all
router.get('/recalibration/addenda', async (req, res) => {
  const status = (req.query.status || 'all').toLowerCase();
  if (!ALLOWED_STATUSES.has(status)) return res.status(400).json({ error: 'invalid status' });
  try {
    const rows = (status === 'all')
      ? await query(`SELECT id, status, source, addendum_text, rationale,
                            valid_from, valid_until, created_at, decided_at, decided_by
                       FROM mastermind_prompt_addenda
                      ORDER BY created_at DESC`)
      : await query(`SELECT id, status, source, addendum_text, rationale,
                            valid_from, valid_until, created_at, decided_at, decided_by
                       FROM mastermind_prompt_addenda
                      WHERE status = $1
                      ORDER BY created_at DESC`, [status]);
    res.json({ addenda: rows.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /api/recalibration/addenda
// body: { addendum_text, rationale?, decided_by, valid_until? }
router.post('/recalibration/addenda', async (req, res) => {
  const { addendum_text, rationale = '', decided_by, valid_until = null } = req.body || {};
  if (!addendum_text || typeof addendum_text !== 'string') {
    return res.status(400).json({ error: 'addendum_text required' });
  }
  if (!decided_by || typeof decided_by !== 'string') {
    return res.status(400).json({ error: 'decided_by required' });
  }
  const args = ['-m', 'agent.mastermind_recalibration', '--add', addendum_text,
                '--rationale', rationale, '--decided-by', decided_by];
  if (valid_until) args.push('--valid-until', String(valid_until));
  try {
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

function _decisionRoute(action) {
  return async (req, res) => {
    const id = parseInt(req.params.id, 10);
    if (!Number.isFinite(id) || id <= 0) {
      return res.status(400).json({ error: 'invalid id' });
    }
    const { decided_by, reason = '' } = req.body || {};
    if (!decided_by || typeof decided_by !== 'string') {
      return res.status(400).json({ error: 'decided_by required' });
    }
    const args = ['-m', 'agent.mastermind_recalibration',
                  `--${action}`, String(id),
                  '--decided-by', decided_by,
                  '--reason', reason];
    try {
      const out = await runPython(args);
      const result = JSON.parse(out);
      if (result.status === 'ILLEGAL_TRANSITION') return res.status(409).json(result);
      if (result.status === 'NOT_FOUND') return res.status(404).json(result);
      res.json(result);
    } catch (e) {
      res.status(500).json({ error: e.message });
    }
  };
}

router.post('/recalibration/addenda/:id/approve', _decisionRoute('approve'));
router.post('/recalibration/addenda/:id/reject',  _decisionRoute('reject'));
router.post('/recalibration/addenda/:id/expire',  _decisionRoute('expire'));

// POST /api/recalibration/detect
router.post('/recalibration/detect', async (_req, res) => {
  try {
    const out = await runPython(['-m', 'agent.mastermind_recalibration', '--detect']);
    res.json({ biases: JSON.parse(out) });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// POST /api/recalibration/emit?dry_run=1
router.post('/recalibration/emit', async (req, res) => {
  const args = ['-m', 'agent.mastermind_recalibration', '--emit'];
  if (req.query.dry_run === '1' || req.body?.dry_run === true) args.push('--dry-run');
  try {
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
