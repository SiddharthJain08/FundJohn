'use strict';

const { Pool } = require('pg');
const fs = require('fs');
const path = require('path');

let pool = null;

function getPool() {
  if (!pool) {
    pool = new Pool({
      connectionString: process.env.POSTGRES_URI || 'postgresql://openclaw:password@localhost:5432/openclaw',
      max: 10,
      idleTimeoutMillis: 30_000,
      connectionTimeoutMillis: 5_000,
    });
    pool.on('error', (err) => console.error('[postgres] Unexpected pool error:', err.message));
  }
  return pool;
}

async function query(sql, params = []) {
  const client = await getPool().connect();
  try {
    return await client.query(sql, params);
  } finally {
    client.release();
  }
}

async function migrate() {
  const migrationsDir = path.join(__dirname, 'migrations');
  const files = fs.readdirSync(migrationsDir).filter(f => f.endsWith('.sql')).sort();
  for (const file of files) {
    const sql = fs.readFileSync(path.join(migrationsDir, file), 'utf8');
    console.log(`[postgres] Running migration ${file}...`);
    await query(sql).catch((err) => {
      // Idempotency: "already exists" errors are expected on re-runs — skip silently
      if (err.message.includes('already exists')) return;
      console.warn(`[postgres] Migration ${file} warning: ${err.message}`);
    });
  }
  console.log('[postgres] All migrations complete.');
}

// Verdict cache queries
const verdictCache = {
  async upsert(workspaceId, record) {
    const {
      ticker, analysis_date, analysis_type, verdict, checklist, score,
      signals, bull_target, bear_target, ev_pct, position_size_pct,
      risk_verdict, memo_path, stale_after,
    } = record;
    return query(
      `INSERT INTO verdict_cache
         (workspace_id, ticker, analysis_date, analysis_type, verdict, checklist, score,
          signals, bull_target, bear_target, ev_pct, position_size_pct, risk_verdict, memo_path, stale_after)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
       ON CONFLICT (ticker, analysis_date, analysis_type) DO UPDATE SET
         verdict=EXCLUDED.verdict, checklist=EXCLUDED.checklist, score=EXCLUDED.score,
         signals=EXCLUDED.signals, stale_after=EXCLUDED.stale_after`,
      [workspaceId, ticker, analysis_date, analysis_type, verdict,
       JSON.stringify(checklist), score, signals, bull_target, bear_target,
       ev_pct, position_size_pct, risk_verdict, memo_path, stale_after]
    );
  },

  async getFresh(ticker, analysisType) {
    const res = await query(
      `SELECT * FROM verdict_cache
       WHERE ticker=$1 AND analysis_type=$2 AND stale_after > NOW()
       ORDER BY analysis_date DESC LIMIT 1`,
      [ticker, analysisType]
    );
    return res.rows[0] || null;
  },

  async getPendingReviews() {
    const res = await query(
      `SELECT t.ticker, t.created_at, t.veto_path, t.id
       FROM trades t WHERE t.status='pending_review' ORDER BY t.created_at ASC`
    );
    return res.rows;
  },

  async getKillSignals() {
    const res = await query(
      `SELECT ticker, analysis_date, signals FROM verdict_cache
       WHERE 'KILL SIGNAL' = ANY(signals) AND stale_after > NOW()`
    );
    return res.rows;
  },
};

// Trade operations
const trades = {
  async create(workspaceId, trade) {
    const res = await query(
      `INSERT INTO trades
         (workspace_id, ticker, direction, entry_low, entry_high, stop_loss, targets,
          position_size_pct, ev_pct, risk_verdict, timing_signal, veto_path, status)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING id`,
      [workspaceId, trade.ticker, trade.direction, trade.entry_low, trade.entry_high,
       trade.stop_loss, trade.targets, trade.position_size_pct, trade.ev_pct,
       trade.risk_verdict, trade.timing_signal, trade.veto_path, trade.status || 'pending']
    );
    if (!res.rows[0]) throw new Error(`trades.create: INSERT returned no rows for ${trade.ticker}`);
    return res.rows[0].id;
  },

  async updateStatus(tradeId, status) {
    return query(`UPDATE trades SET status=$1 WHERE id=$2`, [status, tradeId]);
  },
};

// Workspace operations
const workspaces = {
  async create(name, description = '') {
    const res = await query(
      `INSERT INTO workspaces (name, description) VALUES ($1, $2) RETURNING id`,
      [name, description]
    );
    if (!res.rows[0]) throw new Error(`workspaces.create: INSERT returned no rows for "${name}"`);
    return res.rows[0].id;
  },

  async getDefault() {
    const res = await query(`SELECT * FROM workspaces ORDER BY created_at ASC LIMIT 1`);
    return res.rows[0] || null;
  },
};

// Checkpoint operations
const checkpoints = {
  async save(threadId, subagentType, ticker, state) {
    const res = await query(
      `INSERT INTO checkpoints (thread_id, subagent_type, ticker, state, status)
       VALUES ($1,$2,$3,$4,'running') RETURNING id`,
      [threadId, subagentType, ticker, JSON.stringify(state)]
    );
    if (!res.rows[0]) throw new Error(`checkpoints.save: INSERT returned no rows for thread ${threadId}`);
    return res.rows[0].id;
  },

  async complete(checkpointId) {
    return query(
      `UPDATE checkpoints SET status='completed', completed_at=NOW() WHERE id=$1`,
      [checkpointId]
    );
  },

  async get(checkpointId) {
    const res = await query(`SELECT * FROM checkpoints WHERE id=$1`, [checkpointId]);
    return res.rows[0] || null;
  },
};

module.exports = { query, migrate, verdictCache, trades, workspaces, checkpoints };
