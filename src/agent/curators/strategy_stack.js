'use strict';

/**
 * strategy_stack.js — MastermindJohn weekly strategy-stack review.
 *
 * Fires Friday 20:00 ET via docs/mastermind-weekly.timer. Reads the live +
 * monitoring strategy stack, pulls each strategy's lifetime performance,
 * trade history, and veto drift, asks Opus 4.7 (1M context) for a
 * comprehensive-but-concise memo focused on:
 *
 *   - Per-strategy recent performance with regime attribution
 *   - Cross-strategy correlation / allocation rebalancing recommendations
 *   - Specific sizing deltas per strategy for the coming week
 *
 * Outputs:
 *   • markdown memo   → #strategy-memos
 *   • sizing deltas   → #position-recommendations  (JSON-structured)
 *   • DB row          → mastermind_weekly_reports  (migration 047)
 *
 * The daily 10am orchestrator reads the latest row through
 * trade_handoff_builder.py and forwards `recommendations` to TradeJohn
 * so Monday's sizing reflects the Friday review.
 */

const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { Pool } = require('pg');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const NODE_CLI     = path.join(OPENCLAW_DIR, 'src/agent/run-subagent-cli.js');
const WORKSPACE    = path.join(OPENCLAW_DIR, 'workspaces/default');

const KEEP_STATES = ['live', 'monitoring'];
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src', 'strategies', 'manifest.json');

let _pool = null;
function pool() {
  if (!_pool) _pool = new Pool({ connectionString: process.env.POSTGRES_URI });
  return _pool;
}

async function query(sql, params = []) {
  return pool().query(sql, params);
}

// ── Data loaders ─────────────────────────────────────────────────────────────

// Stack lives in manifest.json (canonical) not strategy_registry.status.
// CLAUDE.md: "manifest.json is the recovery artifact; the strategy_registry
// Postgres table is operational truth" — for lifecycle state we use the
// manifest because registry.status uses older granular labels.
async function loadStack() {
  const manifest = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  const strategies = manifest.strategies || {};
  const out = [];
  for (const [sid, rec] of Object.entries(strategies)) {
    if (KEEP_STATES.includes(rec.state)) {
      out.push({
        id:          sid,
        state:       rec.state,
        state_since: rec.state_since,
        metadata:    rec.metadata || {},
      });
    }
  }
  return out.sort((a, b) => a.id.localeCompare(b.id));
}

async function loadStrategyStats(ids) {
  if (!ids.length) return {};
  const { rows } = await query(
    `SELECT strategy_id, total_count, open_count, closed_count, wins, losses,
            win_rate, avg_realized_pct, avg_unrealized_pct, best_trade_pct,
            worst_trade_pct, avg_days_held, last_signal_date, dominant_regime
       FROM strategy_stats
      WHERE strategy_id = ANY($1::text[])`,
    [ids],
  );
  const out = {};
  for (const r of rows) out[r.strategy_id] = r;
  return out;
}

async function loadRecentExecutions(ids, days = 30) {
  if (!ids.length) return {};
  const { rows } = await query(
    `SELECT strategy_id, run_date, ticker, direction, pct_nav, notional_usd,
            alpaca_status
       FROM alpaca_submissions
      WHERE strategy_id = ANY($1::text[])
        AND run_date >= CURRENT_DATE - ($2 || ' days')::interval
      ORDER BY run_date DESC, strategy_id`,
    [ids, String(days)],
  );
  const out = {};
  for (const r of rows) {
    (out[r.strategy_id] = out[r.strategy_id] || []).push(r);
  }
  return out;
}

async function loadVetoDrift(ids, days = 30) {
  if (!ids.length) return {};
  const { rows } = await query(
    `SELECT strategy_id, veto_reason, COUNT(*) AS n
       FROM veto_log
      WHERE strategy_id = ANY($1::text[])
        AND run_date >= CURRENT_DATE - ($2 || ' days')::interval
      GROUP BY strategy_id, veto_reason`,
    [ids, String(days)],
  );
  const out = {};
  for (const r of rows) {
    (out[r.strategy_id] = out[r.strategy_id] || {})[r.veto_reason] = parseInt(r.n, 10);
  }
  return out;
}

async function loadDailySignalSummary(days = 30) {
  const { rows } = await query(
    `SELECT run_date, n_signals, avg_ev, ev_pos, ev_neg, high_conv_count,
            port_sharpe, worst_dd
       FROM daily_signal_summary
      WHERE run_date >= CURRENT_DATE - ($1 || ' days')::interval
      ORDER BY run_date DESC`,
    [String(days)],
  );
  return rows;
}

async function loadPositionRecs(days = 30) {
  const { rows } = await query(
    `SELECT run_date, ticker, strategy_id, action, unrealized_pnl_pct,
            days_held, rationale, status
       FROM position_recommendations
      WHERE run_date >= CURRENT_DATE - ($1 || ' days')::interval
      ORDER BY run_date DESC
      LIMIT 200`,
    [String(days)],
  );
  return rows;
}

// ── Opus call ────────────────────────────────────────────────────────────────

function _spawnSubagent(type, ticker, contextPayload) {
  return new Promise((resolve, reject) => {
    const tmp = path.join('/tmp', `mastermind-stack-${Date.now()}.json`);
    fs.writeFileSync(tmp, JSON.stringify(contextPayload));
    const args = [
      NODE_CLI,
      '--type', type,
      '--ticker', ticker,
      '--workspace', WORKSPACE,
      '--context-file', tmp,
    ];
    const child = spawn('node', args, {
      cwd: OPENCLAW_DIR,
      env: { ...process.env, OPENCLAW_DIR },
    });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { err += d; process.stderr.write(d); });
    child.on('exit', (code) => {
      try { fs.unlinkSync(tmp); } catch { /* ignore */ }
      if (code === 0) resolve(out);
      else reject(new Error(`${type} exited ${code}: ${err.slice(0, 400)}`));
    });
  });
}

function parseSubagentEnvelope(raw) {
  // run-subagent-cli prints one JSON envelope per line; last success wins.
  let memo = null;
  let cost = 0;
  for (const line of raw.split('\n').reverse()) {
    const l = line.trim();
    if (!l.startsWith('{')) continue;
    try {
      const env = JSON.parse(l);
      if (env.subtype === 'success' && typeof env.result === 'string') {
        memo = env.result;
        cost = env.total_cost_usd || 0;
        break;
      }
    } catch { /* ignore */ }
  }
  return { memo, cost };
}

function extractSizingBlock(memo) {
  if (!memo) return null;
  const m = /```sizing_recommendations\s*([\s\S]*?)```/i.exec(memo);
  if (!m) return null;
  try {
    return JSON.parse(m[1].trim());
  } catch {
    return null;
  }
}

// ── Discord posting ──────────────────────────────────────────────────────────

function httpsRequest(urlStr, opts, body) {
  return new Promise((resolve) => {
    const https = require('https');
    const u = new URL(urlStr);
    const req = https.request({
      hostname: u.hostname, port: 443, path: u.pathname + (u.search || ''),
      method: opts.method || 'POST',
      headers: opts.headers || {},
    }, (res) => {
      let chunks = '';
      res.on('data', (d) => chunks += d);
      res.on('end', () => resolve({ ok: res.statusCode < 300, status: res.statusCode, body: chunks }));
    });
    req.on('error', (e) => resolve({ ok: false, status: 0, body: e.message }));
    if (body) req.write(body);
    req.end();
  });
}

async function findChannelId(botToken, name) {
  const guilds = await httpsRequest(
    'https://discord.com/api/v10/users/@me/guilds',
    { method: 'GET', headers: { Authorization: `Bot ${botToken}` } },
  );
  if (!guilds.ok) return null;
  for (const g of JSON.parse(guilds.body)) {
    const channels = await httpsRequest(
      `https://discord.com/api/v10/guilds/${g.id}/channels`,
      { method: 'GET', headers: { Authorization: `Bot ${botToken}` } },
    );
    if (!channels.ok) continue;
    for (const ch of JSON.parse(channels.body)) {
      if (ch.name === name && ch.type === 0) return ch.id;
    }
  }
  return null;
}

async function postToChannel(botToken, channelId, text) {
  let remaining = text;
  while (remaining) {
    const chunk = remaining.slice(0, 1900);
    remaining = remaining.slice(1900);
    const r = await httpsRequest(
      `https://discord.com/api/v10/channels/${channelId}/messages`,
      { method: 'POST', headers: {
          Authorization: `Bot ${botToken}`,
          'Content-Type': 'application/json',
      } },
      JSON.stringify({ content: chunk }),
    );
    if (!r.ok) {
      console.error(`[mastermind] discord post ${channelId} failed: ${r.status} ${r.body.slice(0, 200)}`);
      return false;
    }
  }
  return true;
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function run({ dryRun = false, notify = null } = {}) {
  const log = (m) => { notify?.(m); console.error(`[mastermind:strategy-stack] ${m}`); };
  const runDate = new Date().toISOString().slice(0, 10);

  log('Loading strategy stack (live + monitoring)...');
  const stack = await loadStack();
  const sids = stack.map((s) => s.id);
  log(`${stack.length} strategies in scope: ${sids.join(', ')}`);

  if (!stack.length) {
    log('No live/monitoring strategies — nothing to analyse.');
    return { runDate, stats: { strategies: 0 }, memo: null, recommendations: null, costUsd: 0 };
  }

  const [stats, executions, vetoDrift, summary, recs] = await Promise.all([
    loadStrategyStats(sids),
    loadRecentExecutions(sids),
    loadVetoDrift(sids),
    loadDailySignalSummary(),
    loadPositionRecs(),
  ]);

  const ctx = {
    run_date: runDate,
    scope: KEEP_STATES,
    strategies: stack.map((s) => ({
      id:             s.id,
      state:          s.state,
      state_since:    s.state_since,
      description:    s.metadata?.description || null,
      stats:          stats[s.id] || null,
      recent_orders:  executions[s.id] || [],
      veto_30d:       vetoDrift[s.id] || {},
    })),
    daily_signal_summary_30d: summary,
    recent_position_recommendations: recs,
    instructions: [
      "Produce a weekly strategy-stack memo for #strategy-memos that is",
      "comprehensive but concise (≤ 6000 chars).",
      "Sections required, in order:",
      "1. Portfolio summary: aggregate EV / Sharpe / DD / notable regime drift.",
      "2. Per-strategy note (live + monitoring): realised vs strategy-reported EV,",
      "   recent veto drift, regime alignment, promote/demote/hold signal.",
      "3. Cross-strategy correlation + allocation rebalancing.",
      "4. Explicit sizing deltas for the week ahead.",
      "",
      "After the memo, append a fenced block exactly:",
      "```sizing_recommendations",
      "{ \"run_date\": \"...\", \"deltas\": [ { \"strategy_id\": \"...\", \"current_pct\": 0.0,",
      "   \"recommended_pct\": 0.0, \"rationale\": \"...\" }, ... ],",
      "  \"notes\": [\"...\"] }",
      "```",
      "Do not post commentary outside these two blocks.",
    ],
  };

  log('Calling MastermindJohn (Opus 4.7, 1M ctx)...');
  let memo = null;
  let cost = 0;
  try {
    const raw = await _spawnSubagent('mastermind', `strategy-stack-${runDate}`, ctx);
    const parsed = parseSubagentEnvelope(raw);
    memo = parsed.memo;
    cost = parsed.cost;
  } catch (e) {
    log(`Opus call failed: ${e.message}`);
    if (!dryRun) {
      await query(
        `INSERT INTO mastermind_weekly_reports (run_date, mode, status, error)
         VALUES ($1, 'strategy-stack', 'failed', $2)`,
        [runDate, e.message.slice(0, 1000)],
      );
    }
    return { runDate, stats: { strategies: stack.length }, memo: null, recommendations: null, costUsd: 0, error: e.message };
  }

  if (!memo) {
    log('No memo returned — aborting');
    return { runDate, stats: { strategies: stack.length }, memo: null, recommendations: null, costUsd: cost, error: 'empty_memo' };
  }
  log(`memo: ${memo.length} chars, $${cost.toFixed(4)}`);

  const recommendations = extractSizingBlock(memo) || { deltas: [], notes: ['no sizing_recommendations block found'] };

  // Discord posting (non-dry-run)
  if (!dryRun) {
    const botToken = process.env.DATABOT_TOKEN || process.env.BOT_TOKEN;
    if (botToken) {
      const [memosId, recsId] = await Promise.all([
        findChannelId(botToken, 'strategy-memos'),
        findChannelId(botToken, 'position-recommendations'),
      ]);
      if (memosId) await postToChannel(botToken, memosId, memo);
      if (recsId) {
        const recText = `📊 **Weekly sizing recommendations — ${runDate}**\n\`\`\`json\n${
          JSON.stringify(recommendations, null, 2).slice(0, 1700)
        }\n\`\`\``;
        await postToChannel(botToken, recsId, recText);
      }
    } else {
      log('DATABOT_TOKEN/BOT_TOKEN unset — skipping Discord posts');
    }

    await query(
      `INSERT INTO mastermind_weekly_reports
         (run_date, mode, memo_md, recommendations, input_stats, cost_usd, status)
       VALUES ($1, 'strategy-stack', $2, $3::jsonb, $4::jsonb, $5, 'ok')`,
      [
        runDate, memo, JSON.stringify(recommendations),
        JSON.stringify({ strategies: stack.length, tokens: { memo_chars: memo.length } }),
        cost,
      ],
    );
    log('Persisted to mastermind_weekly_reports');
  }

  return {
    runDate,
    stats: { strategies: stack.length, memo_chars: memo.length },
    memo,
    recommendations,
    costUsd: cost,
  };
}

module.exports = { run };
