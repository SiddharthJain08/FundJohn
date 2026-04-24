'use strict';

/**
 * position_recommender.js — reads the latest strategy_memos (from Saturday
 * 18:00 comprehensive_review), distils them into exact per-strategy sizing
 * + bracket deltas, persists to strategy_sizing_recommendations, and posts
 * a consolidated digest to Discord #position-recommendations.
 *
 * Runs every Saturday at 19:00 ET via systemd timer (one hour after
 * comprehensive_review has populated strategy_memos).
 *
 * This is a deterministic derivation — it does NOT call Opus. Each memo's
 * JSON `recommendations` block already contains the deltas; this curator
 * joins with current sizing (from strategy_registry.parameters or
 * execution_signals history), produces concrete old → new numbers, and
 * writes rows that trade_handoff_builder.py picks up Monday morning.
 */

const fs = require('fs');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';

async function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

async function _latestMemos() {
  const { rows } = await _query(
    `SELECT DISTINCT ON (strategy_id)
            id, strategy_id, memo_date, recommendations, markdown_body
       FROM strategy_memos
      WHERE memo_date >= CURRENT_DATE - 7
      ORDER BY strategy_id, memo_date DESC, created_at DESC`
  );
  return rows;
}

async function _currentSize(strategyId) {
  // Prefer the median position_size_pct from the last 30 days of
  // execution_signals — reflects actual deployed size, not just the static
  // parameter. Falls back to registry.parameters.position_size_pct.
  const { rows } = await _query(
    `SELECT AVG(position_size_pct)::numeric AS avg_size
       FROM execution_signals
      WHERE strategy_id = $1
        AND signal_date >= CURRENT_DATE - 30
        AND position_size_pct IS NOT NULL`,
    [strategyId]
  );
  if (rows.length && rows[0].avg_size != null) return Number(rows[0].avg_size);
  const r2 = await _query(
    `SELECT parameters FROM strategy_registry WHERE id = $1`, [strategyId]
  );
  const p = r2.rows[0]?.parameters || {};
  if (typeof p.position_size_pct === 'number') return Number(p.position_size_pct);
  return null;
}

function _deriveDeltas(recommendations, currentSize) {
  // `recommendations` was produced by Opus in comprehensive_review:
  //   { size_pct_delta, stop_delta_pct, target_delta_pct, hold_days_delta,
  //     action, confidence, reasoning_one_line }
  // size_pct_delta is an absolute delta in % points (e.g. +0.5 means bump
  // size from 2% → 2.5% of portfolio).
  const sizeDelta = recommendations?.size_pct_delta;
  const recSize = currentSize != null && typeof sizeDelta === 'number'
    ? Math.max(0, Number((currentSize + sizeDelta).toFixed(4)))
    : null;
  return {
    current_size_pct:     currentSize,
    recommended_size_pct: recSize != null ? recSize : (currentSize || 0),
    size_delta_pct:       sizeDelta != null ? Number(sizeDelta) : null,
    stop_delta_pct:       recommendations?.stop_delta_pct != null ? Number(recommendations.stop_delta_pct) : null,
    target_delta_pct:     recommendations?.target_delta_pct != null ? Number(recommendations.target_delta_pct) : null,
    hold_days_delta:      recommendations?.hold_days_delta != null ? parseInt(recommendations.hold_days_delta, 10) : null,
    reasoning:            recommendations?.reasoning_one_line ||
                          recommendations?.action ||
                          'no reasoning provided by review',
  };
}

function _formatDigest(rows) {
  if (!rows.length) return '_No recommendations this week._';
  const header = `# Position sizing — ${new Date().toISOString().slice(0, 10)}\n\n` +
                 `Derived from this evening's strategy memos. ` +
                 `${rows.length} strategies reviewed.\n\n` +
                 `| Strategy | Current | → Rec | Δ Size | Δ Stop | Δ Target | Δ Hold | Reasoning |\n` +
                 `|---|---|---|---|---|---|---|---|\n`;
  const body = rows.map(r => {
    const cur   = r.current_size_pct != null ? `${Number(r.current_size_pct).toFixed(3)}%` : '—';
    const rec   = `${Number(r.recommended_size_pct).toFixed(3)}%`;
    const dS    = r.size_delta_pct   != null ? `${r.size_delta_pct   >= 0 ? '+' : ''}${Number(r.size_delta_pct).toFixed(3)}%` : '—';
    const dStp  = r.stop_delta_pct   != null ? `${r.stop_delta_pct   >= 0 ? '+' : ''}${Number(r.stop_delta_pct).toFixed(3)}`  : '—';
    const dTgt  = r.target_delta_pct != null ? `${r.target_delta_pct >= 0 ? '+' : ''}${Number(r.target_delta_pct).toFixed(3)}` : '—';
    const dHold = r.hold_days_delta  != null ? `${r.hold_days_delta  >= 0 ? '+' : ''}${r.hold_days_delta}d` : '—';
    const why   = (r.reasoning || '').slice(0, 80).replace(/\|/g, '\\|');
    return `| \`${r.strategy_id}\` | ${cur} | ${rec} | ${dS} | ${dStp} | ${dTgt} | ${dHold} | ${why} |`;
  }).join('\n');
  return header + body + '\n\n_TradeJohn will pick these up in Monday\'s handoff. Reply `approve <strategy_id>` / `reject <strategy_id>` to override._';
}

async function _postToDiscord(channelName, text) {
  try {
    const notif = require('../../channels/discord/notifications');
    if (typeof notif.post === 'function') {
      await notif.post(channelName, text);
      return true;
    }
  } catch (e) {
    console.error(`[position-recs] Discord post failed: ${e.message}`);
  }
  return false;
}

async function run({ dryRun = false, notify = () => {} } = {}) {
  const memos = await _latestMemos();
  notify(`${memos.length} memo(s) to distil`);
  if (!memos.length) {
    return { inserted: 0, posted: false, note: 'no recent memos' };
  }

  const persisted = [];
  for (const memo of memos) {
    const currentSize = await _currentSize(memo.strategy_id);
    const deltas = _deriveDeltas(memo.recommendations || {}, currentSize);

    if (dryRun) {
      persisted.push({ strategy_id: memo.strategy_id, dry_run: true, ...deltas });
      continue;
    }

    const { rows } = await _query(
      `INSERT INTO strategy_sizing_recommendations
         (strategy_id, memo_id, current_size_pct, recommended_size_pct,
          size_delta_pct, stop_delta_pct, target_delta_pct, hold_days_delta,
          reasoning)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
       RETURNING id`,
      [memo.strategy_id, memo.id,
       deltas.current_size_pct, deltas.recommended_size_pct,
       deltas.size_delta_pct, deltas.stop_delta_pct,
       deltas.target_delta_pct, deltas.hold_days_delta,
       deltas.reasoning],
    );
    persisted.push({ strategy_id: memo.strategy_id, rec_id: rows[0].id, ...deltas });
  }

  const digest = _formatDigest(persisted);
  notify(`writing digest — ${digest.length} chars`);

  let posted = false;
  if (!dryRun) {
    posted = await _postToDiscord('position-recommendations', digest);
    if (posted) {
      await _query(
        `UPDATE strategy_sizing_recommendations
            SET posted_to_discord = TRUE
          WHERE rec_date = CURRENT_DATE`
      );
    }
  }

  return {
    inserted: persisted.filter(r => r.rec_id).length,
    posted,
    digest_preview: digest.slice(0, 600),
    recommendations: persisted,
  };
}

module.exports = { run };
