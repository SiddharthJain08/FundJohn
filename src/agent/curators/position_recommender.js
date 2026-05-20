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
 *
 * Stop-replacement application: when a memo carries a non-zero
 * stop_delta_pct, the recommender shells to
 * `python3 src/execution/alpaca_replace_stop.py` per currently-filled
 * Alpaca position for that strategy. The Python helper itself is gated
 * by OPENCLAW_ALPACA_LIVE_REPLACE — if unset (default), it dry-logs the
 * intended replacement and exits clean. Set the env var only after a
 * dedicated review of the proposed deltas.
 */

const fs = require('fs');
const { spawn } = require('child_process');

const elig        = require('./_critique_eligibility.js');
const synthesizer = require('./synthesizer.js');

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

function _sourceRecommendedSize(synthesisRow, memoRecommendation) {
  if (synthesisRow && synthesisRow.adjusted_recommended_size_pct != null) {
    return Number(synthesisRow.adjusted_recommended_size_pct);
  }
  if (memoRecommendation && memoRecommendation.recommended_size_pct != null) {
    return Number(memoRecommendation.recommended_size_pct);
  }
  return null;
}

async function _loadSynthesisRow(strategyId, weekOf) {
  const { rows } = await _query(
    `SELECT adjusted_recommended_size_pct, adjustment_reason, critics_accepted
       FROM strategy_synthesis
      WHERE strategy_id = $1 AND week_of = $2 LIMIT 1`,
    [strategyId, weekOf]);
  return rows[0] || null;
}

async function _runSynthesisIfEligible(memo, deltasRecSize, weekOf) {
  if (process.env.OPENCLAW_MEMO_CRITIQUE !== '1') return null;
  const eligible = await elig.filter();
  if (!eligible.includes(memo.strategy_id)) return null;

  // Load critiques from strategy_memo_critiques (written by --mode critique earlier)
  const { rows: critiques } = await _query(
    `SELECT critic_role, critique_text, cited_metrics
       FROM strategy_memo_critiques
      WHERE strategy_id = $1 AND week_of = $2`,
    [memo.strategy_id, weekOf]);

  // Load 30d P&L + open positions (JOIN execution_signals to recover ticker / entry / exit)
  const { rows: trades } = await _query(
    `SELECT es.ticker,
            es.signal_date     AS entry_date,
            sp.closed_at       AS exit_date,
            sp.realized_pnl_pct,
            sp.days_held       AS hold_days
       FROM signal_pnl sp
       JOIN execution_signals es ON es.id = sp.signal_id
      WHERE sp.strategy_id = $1
        AND sp.closed_at IS NOT NULL
        AND sp.closed_at >= CURRENT_DATE - INTERVAL '30 days'
      ORDER BY sp.closed_at DESC LIMIT 100`, [memo.strategy_id]);
  const { rows: open } = await _query(
    `SELECT ticker, signal_date, direction, entry_price
       FROM execution_signals
      WHERE strategy_id = $1 AND status = 'open'`, [memo.strategy_id]);

  const originalSize = Number(deltasRecSize ?? 0);
  return await synthesizer.synthesize(memo, critiques, trades, open, originalSize,
                                       { weekOf });
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
  // Webhook-based path for one-shot curator scripts that run outside
  // johnbot.service (where discord.js Client is uninitialised). The
  // previous code path required notifications.post() with a live Client
  // and silently returned false on every Saturday's position-recs run.
  try {
    const { postToChannel } = require('./_discord_webhook');
    const r = await postToChannel('botjohn', channelName, text);
    if (r.ok) return true;
    console.error(`[position-recs] Discord post failed: ${r.reason || r.status}: ${r.detail || r.body || ''}`);
  } catch (e) {
    console.error(`[position-recs] Discord post failed: ${e.message}`);
  }
  return false;
}

async function run({ dryRun = false, notify = () => {}, maxMemoAgeHours = 24 } = {}) {
  // Freshness gate — refuse to run on stale memos. Without this guard,
  // 2026-05-13..16: comprehensive_review was crashing every Saturday
  // (spawn E2BIG in _opus_oneshot), strategy_memos went silent for a
  // week, and position-recs happily wrote tautological "no change"
  // recommendations off stale memos. The split-source-of-truth pattern
  // hid the upstream failure under a green downstream cycle.
  const { rows: freshRows } = await _query(
    `SELECT MAX(created_at) AS newest_at,
            EXTRACT(EPOCH FROM NOW() - MAX(created_at)) / 3600 AS hours_old,
            COUNT(*) AS recent_count
       FROM strategy_memos
      WHERE created_at >= NOW() - INTERVAL '14 days'`
  );
  const hoursOld    = freshRows[0]?.hours_old;
  const recentCount = Number(freshRows[0]?.recent_count || 0);
  if (recentCount === 0 || hoursOld == null || Number(hoursOld) > maxMemoAgeHours) {
    const detail = recentCount === 0
      ? 'no memos in last 14d — comprehensive_review likely broken'
      : `newest memo ${Number(hoursOld).toFixed(1)}h old (limit ${maxMemoAgeHours}h)`;
    notify(`refusing to run — ${detail}`);
    if (!dryRun) {
      await _postToDiscord('botjohn-log',
        `⚠️ **position_recommender refused to run** — ${detail}.` +
        ` Fix comprehensive_review or pass --max-memo-age-hours.`);
    }
    return { inserted: 0, posted: false, note: 'stale_memos', detail };
  }

  const memos = await _latestMemos();
  notify(`${memos.length} memo(s) to distil`);
  if (!memos.length) {
    return { inserted: 0, posted: false, note: 'no recent memos' };
  }

  const persisted = [];
  for (const memo of memos) {
    const currentSize = await _currentSize(memo.strategy_id);
    const deltas = _deriveDeltas(memo.recommendations || {}, currentSize);

    // F3 synthesis pass: if OPENCLAW_MEMO_CRITIQUE=1 and the strategy is
    // eligible, invoke the Mastermind synthesizer over critiques +
    // last-30d P&L. Otherwise try to read a synthesis row that may have
    // been written earlier by `run_mastermind --mode critique`. The
    // resulting `effectiveSize` is the final recommended_size_pct; falls
    // through to the memo's computed value when no synthesis exists.
    const weekOf = new Date().toISOString().slice(0, 10);
    const memoRec = { recommended_size_pct: deltas.recommended_size_pct };
    let synthRow = null;
    try {
      synthRow = await _runSynthesisIfEligible(memo, deltas.recommended_size_pct, weekOf)
              || await _loadSynthesisRow(memo.strategy_id, weekOf);
    } catch (e) {
      notify(`  synthesis lookup failed for ${memo.strategy_id}: ${e.message}`);
    }
    const effectiveSize = _sourceRecommendedSize(synthRow, memoRec);
    if (effectiveSize != null) {
      deltas.recommended_size_pct = effectiveSize;
      if (synthRow && synthRow.adjustment_reason) {
        deltas.reasoning = `[synth] ${synthRow.adjustment_reason}`;
      }
    }

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

  // Apply stop replacements per recommendation (gated default-OFF in the
  // Python helper itself via OPENCLAW_ALPACA_LIVE_REPLACE). Any rec with
  // a non-trivial stop_delta_pct (|x| >= 0.005, ie 0.5%) gets each
  // currently-filled Alpaca position for that strategy snapped to a new
  // stop = current_stop * (1 + stop_delta_pct).
  //
  // Even in dry-run mode we compute the proposed (coid, old_stop, new_stop)
  // tuples — the operator wants to SEE what would change before flipping
  // OPENCLAW_ALPACA_LIVE_REPLACE=1. reportOnly=true skips the Python
  // subprocess spawn (no broker calls) and emits only the planned deltas.
  const stopReplacements = await _applyStopReplacements(persisted, notify,
                                                        { reportOnly: dryRun });
  if (stopReplacements.length) {
    const live   = stopReplacements.filter(r => r.status === 'replaced').length;
    const dryLog = stopReplacements.filter(r => r.status === 'skipped_dry_run').length;
    const planned = stopReplacements.filter(r => r.status === 'planned_dry_run').length;
    notify(`stop replacements: ${live} live, ${dryLog} dry-logged, ${planned} planned, ${stopReplacements.length - live - dryLog - planned} other`);
  }

  let posted = false;
  if (!dryRun) {
    posted = await _postToDiscord('position-recommendations', digest);
    if (posted) {
      // Mark exactly the rows we just inserted as posted (NOT
      // `WHERE rec_date = CURRENT_DATE` — that misses rows inserted
      // before midnight when the curator crosses a date boundary, and
      // on retry re-posts duplicates to #position-recommendations).
      const ids = persisted.map(r => r.rec_id).filter(Boolean);
      if (ids.length) {
        await _query(
          `UPDATE strategy_sizing_recommendations
              SET posted_to_discord = TRUE
            WHERE id = ANY($1::uuid[])`,
          [ids],
        );
      }
    }
  }

  return {
    inserted: persisted.filter(r => r.rec_id).length,
    posted,
    digest_preview: digest.slice(0, 600),
    recommendations:    persisted,
    stop_replacements:  stopReplacements,
  };
}

// ── Stop-replacement application ────────────────────────────────────────────

async function _filledPositionsForStrategy(strategyId, days = 14) {
  // Find currently-filled positions: alpaca_submissions with broker_status
  // ='filled' or 'partial' and submitted in the recent window. We don't
  // attempt to detect closed positions here — Alpaca rejects replace on
  // already-closed orders cleanly, and the result lands in the digest as
  // 'replace_failed'. Future enhancement: cross-ref against signal_pnl
  // status='open'.
  const { rows } = await _query(
    `SELECT client_order_id, ticker, stop_price
       FROM alpaca_submissions
      WHERE strategy_id = $1
        AND submitted_at >= NOW() - INTERVAL '${parseInt(days, 10)} days'
        AND broker_status IN ('filled', 'partial')
        AND alpaca_order_id IS NOT NULL
        AND stop_price IS NOT NULL`,
    [strategyId],
  );
  return rows;
}

function _spawnReplaceStop(coid, newStop) {
  return new Promise((resolve) => {
    const args = [
      `${OPENCLAW_DIR}/src/execution/alpaca_replace_stop.py`,
      '--coid',     coid,
      '--new-stop', String(newStop),
    ];
    let stdout = '';
    let stderr = '';
    const proc = spawn('python3', args, {
      env: process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    proc.stdout.on('data', (c) => { stdout += c; });
    proc.stderr.on('data', (c) => { stderr += c; });
    proc.on('close', (code) => {
      let parsed = null;
      try { parsed = JSON.parse(stdout); } catch (_) {}
      resolve({
        ok: code === 0,
        result: parsed || { status: 'spawn_error', error: stderr || `exit ${code}`, coid },
      });
    });
    proc.on('error', (err) => {
      resolve({ ok: false, result: { status: 'spawn_error', error: err.message, coid } });
    });
  });
}

async function _applyStopReplacements(persisted, notify, { reportOnly = false } = {}) {
  const all = [];
  for (const rec of persisted) {
    const delta = Number(rec.stop_delta_pct);
    if (!delta || Math.abs(delta) < 0.005) continue;     // < 0.5% → noise
    const positions = await _filledPositionsForStrategy(rec.strategy_id);
    if (!positions.length) continue;
    notify(`  ${rec.strategy_id}: stop_delta_pct=${delta.toFixed(3)} → ${positions.length} open positions`);
    for (const p of positions) {
      const currentStop = Number(p.stop_price);
      if (!Number.isFinite(currentStop) || currentStop <= 0) continue;
      const newStop = Math.max(0.01, currentStop * (1 + delta));
      const base = {
        strategy_id: rec.strategy_id,
        ticker:      p.ticker,
        coid:        p.client_order_id,
        old_stop:    currentStop,
        new_stop:    newStop,
      };
      if (reportOnly) {
        // Skip the Python subprocess entirely. Useful for `--dry-run`
        // operator review before flipping OPENCLAW_ALPACA_LIVE_REPLACE=1.
        all.push({ ...base, status: 'planned_dry_run' });
        continue;
      }
      const r = await _spawnReplaceStop(p.client_order_id, newStop);
      all.push({ ...base, ...r.result });
    }
  }
  return all;
}

module.exports = { run, _applyStopReplacements, _sourceRecommendedSize };
