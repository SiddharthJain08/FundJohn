'use strict';

/**
 * _critique_eligibility.js — selects strategies eligible for the
 * F3 Saturday critique pass.
 *
 * Eligibility: ≥1 closed trade in the last 7 calendar days, AND
 *               exit_date IS NOT NULL (i.e. realized P&L exists).
 *
 * Open positions alone do NOT trigger critique. Strategies are
 * allowed to complete their hold-period cadence before being judged.
 */

let _queryOverride = null;

async function _query(sql, params = []) {
  if (_queryOverride) return _queryOverride(sql, params);
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

function _setQueryForTests(fn) {
  _queryOverride = fn;
}

async function filter() {
  const sql = `
    SELECT DISTINCT strategy_id
      FROM signal_pnl
     WHERE exit_date IS NOT NULL
       AND exit_date >= CURRENT_DATE - INTERVAL '7 days'
     ORDER BY strategy_id
  `;
  const { rows } = await _query(sql);
  return rows.map(r => r.strategy_id).sort();
}

module.exports = { filter, _setQueryForTests };
