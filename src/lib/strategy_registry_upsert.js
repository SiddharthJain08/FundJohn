'use strict';

/**
 * strategy_registry_upsert.js — single canonical writer for strategy_registry.
 *
 * Why this exists:
 *   strategy_registry was being written from 6+ call sites with subtly
 *   different INSERT...ON CONFLICT statements. Drift led to:
 *     - Some writers omitted `implementation_path` (NOT NULL → INSERT
 *       failure observed yesterday in saturday_brain_recovery)
 *     - Some writers' DO UPDATE clauses preserved status='approved' (right),
 *       others didn't (wrong — would clobber an approved strategy back to
 *       pending_approval)
 *     - Race observation where two near-simultaneous INSERT...ON CONFLICT
 *       calls produced a brief window of duplicate rows visible to other
 *       sessions (later reconciled by PG, but disturbing while it lasted)
 *
 *   This module is the single source of truth. All registry writers should
 *   import upsertStrategyRegistry() and stop hand-rolling SQL.
 *
 * Schema reference (data/master/schema_registry.json + migrations 012, 058):
 *   id, name, implementation_path → NOT NULL
 *   status → text (pending_approval | approved | deprecated | paused)
 *   data_requirements_planned → JSONB (Saturday-brain Tier-B planned fetches)
 *   staging_approved_at → timestamptz (operator's STAGING approval click)
 *   parameters, regime_conditions, universe → JSONB / text[] / etc.
 */

const DEFAULT_STATUS = 'pending_approval';

/**
 * Canonical upsert.
 *
 * Required:
 *   - id (text, primary key)
 *   - name (text)
 *   - implementationPath (text — path the strategycoder will/did write to)
 *
 * Optional (with sensible defaults):
 *   - status                  default 'pending_approval'
 *   - dataRequirementsPlanned default null
 *   - parameters              default {}
 *   - universe                default ['SP500']
 *   - signalFrequency         default 'daily'
 *   - tier                    default 2
 *   - preserveApprovedStatus  default true — never overwrite an existing
 *                             status='approved' row's status (matches
 *                             saturday_brain.js _stage's intent).
 *   - dbQuery                 (sql, params) → Promise — defaults to a
 *                             singleton pg.Pool against POSTGRES_URI.
 *
 * Returns the upserted row (RETURNING *).
 */
async function upsertStrategyRegistry(opts) {
  const {
    id,
    name,
    implementationPath,
    status                  = DEFAULT_STATUS,
    dataRequirementsPlanned = null,
    parameters              = {},
    universe                = ['SP500'],
    signalFrequency         = 'daily',
    tier                    = 2,
    preserveApprovedStatus  = true,
    dbQuery                 = _defaultDbQuery,
  } = opts;

  if (!id) throw new Error('upsertStrategyRegistry: id required');
  if (!name) throw new Error('upsertStrategyRegistry: name required');
  if (!implementationPath) {
    throw new Error('upsertStrategyRegistry: implementationPath required (NOT NULL in schema)');
  }

  // Build the ON CONFLICT clause. preserveApprovedStatus uses CASE so an
  // already-approved strategy keeps its status when a Tier-B re-tier or
  // similar non-promotion path runs upsert.
  const statusClause = preserveApprovedStatus
    ? `status = CASE WHEN strategy_registry.status = 'approved'
                     THEN strategy_registry.status
                     ELSE EXCLUDED.status END`
    : `status = EXCLUDED.status`;

  const sql = `
    INSERT INTO strategy_registry
      (id, name, implementation_path, status, data_requirements_planned,
       parameters, universe, signal_frequency, tier)
    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7, $8, $9)
    ON CONFLICT (id) DO UPDATE
      SET name                      = EXCLUDED.name,
          implementation_path       = EXCLUDED.implementation_path,
          ${statusClause},
          data_requirements_planned = EXCLUDED.data_requirements_planned,
          parameters                = EXCLUDED.parameters,
          universe                  = EXCLUDED.universe,
          signal_frequency          = EXCLUDED.signal_frequency,
          tier                      = EXCLUDED.tier
    RETURNING *
  `;

  const params = [
    id,
    name,
    implementationPath,
    status,
    dataRequirementsPlanned == null ? null : JSON.stringify(dataRequirementsPlanned),
    parameters == null ? null : JSON.stringify(parameters),
    Array.isArray(universe) ? universe : [String(universe || 'SP500')],
    signalFrequency,
    tier,
  ];

  const result = await dbQuery(sql, params);
  return result.rows[0];
}

/**
 * Default pg.Pool getter — singleton across the process. Lazily created
 * so calling code that imports this module without ever using it doesn't
 * pay the connection-pool startup cost.
 */
let _pool = null;
function _defaultDbQuery(sql, params) {
  if (!_pool) {
    const { Pool } = require('pg');
    _pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  }
  return _pool.query(sql, params);
}

module.exports = { upsertStrategyRegistry };
