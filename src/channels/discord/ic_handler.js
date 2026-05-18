'use strict';

/**
 * ic_handler.js — Phase 2A Renaissance IC approval gate, Discord side.
 *
 * Listens for short slash-style commands in #ic-approvals and UPDATEs the
 * `ic_decisions` table accordingly:
 *
 *   approve N           classification → APPROVED
 *   veto N              classification → VETOED
 *   scale N PCT         classification → SCALED, scaled_size_pct = PCT/100 (clamped 0..1)
 *
 * `N` is the row number from the consolidated prompt the runner
 * (src/execution/ic_gate_runner.py) posted earlier this cycle. We look it
 * up by ORDER BY (strategy_id, ticker) among today's IC_REQUIRED rows so
 * the numbering matches the prompt exactly.
 *
 * Wired by src/channels/discord/bot.js inside the messageCreate handler
 * (a thin if-channel-equals-ic-approvals guard delegates to handleMessage
 * here). Keeping the SQL out of bot.js means the channel can be tested
 * end-to-end with just a Postgres connection — no Discord client required.
 *
 * Spec: docs/superpowers/plans/2026-05-15-fincept-imports-phase-2-master-plan.md
 * (Project 2A — task A2.6.)
 */

const { Pool } = require('pg');

const CHANNEL_NAME = 'ic-approvals';

let _pool = null;
function getPool() {
  if (_pool) return _pool;
  _pool = new Pool({
    connectionString:
      process.env.DATABASE_URL ||
      process.env.POSTGRES_URI ||
      'postgresql://openclaw:password@localhost:5432/openclaw',
  });
  return _pool;
}

/**
 * Parse `approve N` / `veto N` / `scale N PCT`. Case-insensitive. Returns
 * null if the text is not a recognised command. Tolerates extra whitespace
 * and trailing punctuation but rejects bad numerics so a typo cannot
 * silently flip the wrong row.
 */
function parseCommand(text) {
  if (!text || typeof text !== 'string') return null;
  const s = text.trim().toLowerCase();

  // approve N
  let m = s.match(/^approve\s+(\d+)\s*\.?$/);
  if (m) return { action: 'APPROVED', n: parseInt(m[1], 10) };

  // veto N
  m = s.match(/^veto\s+(\d+)\s*\.?$/);
  if (m) return { action: 'VETOED', n: parseInt(m[1], 10) };

  // scale N PCT  (PCT is 0..100; 100 => full size)
  m = s.match(/^scale\s+(\d+)\s+([\d.]+)\s*%?\s*\.?$/);
  if (m) {
    const n = parseInt(m[1], 10);
    const pct = parseFloat(m[2]);
    if (!Number.isFinite(pct)) return null;
    let frac = pct / 100.0;
    if (frac > 1.0) frac = 1.0;
    if (frac < 0.0) frac = 0.0;
    return { action: 'SCALED', n, scaled_size_pct: frac };
  }

  return null;
}

/**
 * Look up the Nth IC_REQUIRED row for today (UTC). Returns {id, strategy_id, ticker}
 * or null. Numbering matches the runner's prompt (ORDER BY strategy_id, ticker).
 */
async function findNthPending(client, runDate, n) {
  const r = await client.query(
    `SELECT id, strategy_id, ticker FROM ic_decisions
       WHERE run_date = $1 AND classification = 'IC_REQUIRED'
       ORDER BY strategy_id, ticker
       OFFSET $2 LIMIT 1`,
    [runDate, n - 1],
  );
  return r.rows[0] || null;
}

/**
 * Apply the operator's decision. Returns a status object the caller can
 * relay back to the operator via message.reply.
 */
async function applyDecision({ action, n, scaled_size_pct }, decidedBy, runDate) {
  const pool = getPool();
  const client = await pool.connect();
  try {
    const row = await findNthPending(client, runDate, n);
    if (!row) {
      return { ok: false, reason: 'no_pending_row',
               detail: `no IC_REQUIRED row #${n} for ${runDate}` };
    }
    if (action === 'SCALED') {
      await client.query(
        `UPDATE ic_decisions
            SET classification = 'SCALED',
                decided_by = $1,
                decided_at = NOW(),
                scaled_size_pct = $2
          WHERE id = $3`,
        [decidedBy, scaled_size_pct, row.id],
      );
    } else {
      await client.query(
        `UPDATE ic_decisions
            SET classification = $1,
                decided_by = $2,
                decided_at = NOW()
          WHERE id = $3`,
        [action, decidedBy, row.id],
      );
    }
    return { ok: true, action, row, scaled_size_pct: scaled_size_pct ?? null };
  } finally {
    client.release();
  }
}

/**
 * Discord messageCreate handler. bot.js calls this once per message; we
 * short-circuit if the message isn't from #ic-approvals so the cost on
 * the hot path is one channel-name comparison.
 *
 * Returns:
 *   { handled: false }                            — not for us
 *   { handled: true, ok: true,  action, row }     — applied OK
 *   { handled: true, ok: false, reason, detail }  — failed (bad command,
 *                                                    no row, DB error)
 */
async function handleMessage(message, opts = {}) {
  // Skip bot messages — only the operator (human) should be deciding.
  if (message.author?.bot) return { handled: false };

  const channelName = message.channel?.name;
  if (channelName !== CHANNEL_NAME) return { handled: false };

  const cmd = parseCommand(message.content);
  if (!cmd) return { handled: false };

  const decidedBy = message.author?.username || message.author?.id || 'unknown';
  const runDate = opts.runDate || new Date().toISOString().slice(0, 10);

  try {
    const result = await applyDecision(cmd, decidedBy, runDate);
    if (result.ok) {
      const tag = result.action === 'SCALED'
        ? `${result.action} @ ${(result.scaled_size_pct * 100).toFixed(1)}%`
        : result.action;
      await message.reply?.(
        `${tag} #${cmd.n} → ${result.row.strategy_id} / ${result.row.ticker}`
      ).catch(() => null);
    } else {
      await message.reply?.(`IC handler: ${result.reason} (${result.detail || ''})`)
        .catch(() => null);
    }
    return { handled: true, ...result };
  } catch (err) {
    console.error('[ic_handler] db error:', err.message);
    await message.reply?.(`IC handler error: ${err.message}`).catch(() => null);
    return { handled: true, ok: false, reason: 'db_error', error: err.message };
  }
}

module.exports = {
  CHANNEL_NAME,
  parseCommand,
  applyDecision,
  findNthPending,
  handleMessage,
  // Test seam: clear the cached pool so unit/integration tests can re-init.
  _resetPool: () => { _pool = null; },
};
