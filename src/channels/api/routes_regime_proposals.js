'use strict';

/**
 * /api/regime-proposals/* — operator approval queue for Mastermind-emitted
 * per-(strategy, regime) parameter proposals.
 *
 * GET  /?status=pending&limit=50     list proposals
 * POST /:id/approve                   approve as-is
 * POST /:id/reject                    reject with reason
 * POST /:id/modify                    approve with operator overrides
 *
 * Writes shell out to the Python proposal_manager so the FOR UPDATE +
 * eligibility_manager call stays in one canonical place.
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

router.get('/', async (req, res) => {
  const status = String(req.query.status || 'pending');
  const limit = Math.min(parseInt(req.query.limit, 10) || 50, 500);
  try {
    const result = await query(`
      SELECT p.id, p.proposed_at, p.proposer,
             p.strategy_id, p.regime_state,
             p.proposed_eligible,
             p.proposed_size_scalar::float    AS proposed_size_scalar,
             p.proposed_stop_pct::float       AS proposed_stop_pct,
             p.proposed_target_pct::float     AS proposed_target_pct,
             p.proposed_max_hold_days,
             p.confidence::float              AS confidence,
             p.reasoning, p.memo_id, p.status,
             p.decided_at, p.decided_by, p.decision_reason,
             m.markdown_body AS memo_markdown
        FROM strategy_regime_param_proposals p
        LEFT JOIN strategy_memos m ON m.id = p.memo_id
       WHERE p.status = $1
       ORDER BY p.proposed_at DESC
       LIMIT $2
    `, [status, limit]);
    res.json({ proposals: result.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Recent changes: every applied per-(strategy, regime) param change in the
// window — Saturday backtest-coupling (with the ΔSharpe that justified it),
// Saturday confidence auto-approval, and any manual dashboard/CLI decisions.
// Read-only; powers "Strategy Adjustments → Recent changes".
router.get('/applied', async (req, res) => {
  const days = Math.min(parseInt(req.query.days, 10) || 7, 90);
  try {
    const result = await query(`
      SELECT id, changed_at, strategy_id, regime_state, source, actor,
             (before_row->>'stop_pct')::float     AS stop_before,
             (after_row ->>'stop_pct')::float      AS stop_after,
             (before_row->>'target_pct')::float    AS target_before,
             (after_row ->>'target_pct')::float    AS target_after,
             (before_row->>'size_scalar')::float   AS size_before,
             (after_row ->>'size_scalar')::float   AS size_after,
             (before_row->>'max_hold_days')::int   AS hold_before,
             (after_row ->>'max_hold_days')::int   AS hold_after,
             (before_row->>'eligible')::boolean    AS eligible_before,
             (after_row ->>'eligible')::boolean    AS eligible_after,
             bt_sharpe_before::float               AS bt_sharpe_before,
             bt_sharpe_after::float                AS bt_sharpe_after,
             bt_n_trades, reason
        FROM strategy_regime_param_changes
       WHERE source IN ('saturday_coupling', 'auto-approval', 'dashboard', 'cli')
         AND changed_at >= NOW() - ($1 || ' days')::interval
       ORDER BY changed_at DESC, strategy_id, regime_state
    `, [String(days)]);
    res.json({ applied: result.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// Noted (2026-07-14 full-auto Saturday): low-confidence suggestions parked
// instead of applied — regime param proposals (status='noted', still
// operator-decidable) plus low-confidence sizing recs (action_taken='noted').
// Superseded/re-evaluated by the next weekly review.
router.get('/noted', async (req, res) => {
  const days = Math.min(parseInt(req.query.days, 10) || 21, 90);
  try {
    const [props, recs] = await Promise.all([
      query(`
        SELECT p.id, p.proposed_at, p.strategy_id, p.regime_state,
               p.proposed_eligible,
               p.proposed_size_scalar::float AS proposed_size_scalar,
               p.confidence::float           AS confidence,
               p.reasoning, p.decision_reason
          FROM strategy_regime_param_proposals p
         WHERE p.status = 'noted'
           AND p.proposed_at >= NOW() - ($1 || ' days')::interval
         ORDER BY p.proposed_at DESC
         LIMIT 200
      `, [String(days)]),
      query(`
        SELECT id, rec_date, strategy_id,
               size_delta_pct::float   AS size_delta_pct,
               stop_delta_pct::float   AS stop_delta_pct,
               target_delta_pct::float AS target_delta_pct,
               hold_days_delta, confidence::float AS confidence,
               coupling_outcome, reasoning
          FROM strategy_sizing_recommendations
         WHERE action_taken = 'noted'
           AND rec_date >= CURRENT_DATE - ($1 || ' days')::interval
         ORDER BY rec_date DESC, strategy_id
         LIMIT 200
      `, [String(days)]),
    ]);
    res.json({ proposals: props.rows, sizing_recs: recs.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

function _validateDecision(body) {
  if (typeof body?.actor !== 'string' || !body.actor.trim()) {
    return 'actor required';
  }
  return null;
}

router.post('/:id/approve', async (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!Number.isFinite(id)) return res.status(400).json({ error: 'invalid id' });
  const err = _validateDecision(req.body);
  if (err) return res.status(400).json({ error: err });
  try {
    const out = await runPython(['-m', 'strategies.proposal_manager',
                                  '--approve', String(id),
                                  '--actor', req.body.actor,
                                  '--reason', req.body.reason || '',
                                  '--source', 'dashboard']);
    res.json(JSON.parse(out));
  } catch (e) {
    const msg = e.message || String(e);
    if (/KeyError/.test(msg)) return res.status(404).json({ error: 'proposal not found' });
    if (/ValueError/.test(msg)) {
      const m = msg.match(/ValueError:\s*([^\n]+)/);
      return res.status(409).json({ error: m ? m[1].trim() : 'conflict' });
    }
    res.status(500).json({ error: msg });
  }
});

router.post('/:id/reject', async (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!Number.isFinite(id)) return res.status(400).json({ error: 'invalid id' });
  const err = _validateDecision(req.body);
  if (err) return res.status(400).json({ error: err });
  try {
    const out = await runPython(['-m', 'strategies.proposal_manager',
                                  '--reject', String(id),
                                  '--actor', req.body.actor,
                                  '--reason', req.body.reason || '',
                                  '--source', 'dashboard']);
    res.json(JSON.parse(out));
  } catch (e) {
    const msg = e.message || String(e);
    if (/KeyError/.test(msg)) return res.status(404).json({ error: 'proposal not found' });
    if (/ValueError/.test(msg)) {
      const m = msg.match(/ValueError:\s*([^\n]+)/);
      return res.status(409).json({ error: m ? m[1].trim() : 'conflict' });
    }
    res.status(500).json({ error: msg });
  }
});

router.post('/:id/modify', async (req, res) => {
  const id = parseInt(req.params.id, 10);
  if (!Number.isFinite(id)) return res.status(400).json({ error: 'invalid id' });
  const err = _validateDecision(req.body);
  if (err) return res.status(400).json({ error: err });
  const overrides = req.body.overrides || {};
  const args = ['-m', 'strategies.proposal_manager',
                '--modify', String(id),
                '--actor', req.body.actor,
                '--reason', req.body.reason || '',
                '--source', 'dashboard'];
  if (overrides.eligible === true)  args.push('--eligible');
  if (overrides.eligible === false) args.push('--ineligible');
  if (typeof overrides.size_scalar   === 'number') args.push('--size', String(overrides.size_scalar));
  if (typeof overrides.stop_pct      === 'number') args.push('--stop', String(overrides.stop_pct));
  if (typeof overrides.target_pct    === 'number') args.push('--target', String(overrides.target_pct));
  if (typeof overrides.max_hold_days === 'number') args.push('--max-hold', String(overrides.max_hold_days));

  try {
    const out = await runPython(args);
    res.json(JSON.parse(out));
  } catch (e) {
    const msg = e.message || String(e);
    if (/KeyError/.test(msg)) return res.status(404).json({ error: 'proposal not found' });
    if (/ValueError/.test(msg)) {
      const m = msg.match(/ValueError:\s*([^\n]+)/);
      return res.status(409).json({ error: m ? m[1].trim() : 'conflict' });
    }
    res.status(500).json({ error: msg });
  }
});

module.exports = router;
