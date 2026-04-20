'use strict';

/**
 * gate-decisions.js — structured emit helper for paper_gate_decisions.
 *
 * Phase 1 instrumentation. One call per gate decision anywhere in the research
 * pipeline. Non-blocking: swallows its own errors so a DB glitch never stops
 * the pipeline.
 *
 * Usage:
 *   const { emitGateDecision } = require('./gate-decisions');
 *   await emitGateDecision({
 *     candidateId: 'uuid',
 *     gateName:    'convergence',
 *     outcome:     'reject',
 *     reasonCode:  'sharpe_below_floor',
 *     reasonDetail:'avg sharpe 0.31 < 0.5',
 *     metadata:    { sharpe: 0.31, windows_passed: 1 },
 *   });
 */

let _pool = null;

function getPool() {
  if (_pool) return _pool;
  const { Pool } = require('pg');
  _pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 3 });
  _pool.on('error', (e) => console.error('[gate-decisions] pool error:', e.message));
  return _pool;
}

/**
 * Emit a gate decision. All fields optional except gateName + outcome.
 * paperId and candidateId are both allowed — pass whichever is known.
 */
async function emitGateDecision({
  paperId      = null,
  candidateId  = null,
  strategyId   = null,
  gateName,
  outcome,
  reasonCode   = null,
  reasonDetail = null,
  metadata     = null,
} = {}) {
  if (!gateName || !outcome) {
    console.error('[gate-decisions] missing gateName/outcome — skipping');
    return;
  }
  if (!process.env.POSTGRES_URI) return;  // tests / dry runs

  try {
    await getPool().query(
      `INSERT INTO paper_gate_decisions
         (paper_id, candidate_id, strategy_id, gate_name, outcome,
          reason_code, reason_detail, metadata)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)`,
      [
        paperId, candidateId, strategyId, gateName, outcome,
        reasonCode,
        reasonDetail ? String(reasonDetail).slice(0, 2000) : null,
        metadata ? JSON.stringify(metadata) : null,
      ]
    );
  } catch (e) {
    console.error(`[gate-decisions] insert failed (${gateName}/${outcome}):`, e.message);
  }
}

/** Resolve the paper_id for a candidate (if the corpus holds a matching row). Cheap. */
async function paperIdForCandidate(candidateId) {
  if (!candidateId || !process.env.POSTGRES_URI) return null;
  try {
    const { rows } = await getPool().query(
      `SELECT p.paper_id
         FROM research_candidates rc
         JOIN research_corpus p ON p.source_url = rc.source_url
        WHERE rc.candidate_id = $1
        LIMIT 1`,
      [candidateId]
    );
    return rows[0]?.paper_id || null;
  } catch {
    return null;
  }
}

module.exports = { emitGateDecision, paperIdForCandidate };
