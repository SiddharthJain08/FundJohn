#!/usr/bin/env node
// src/agent/curators/weekly_live_sharpe.js — Sunday 06:00 ET cron.
//
// Refreshes per-regime strategy weights from closed-trade data, runs the
// opt-in auto-demote chain, posts a summary to #general.
//
// Coexistence with MasterMindJohn:
//   - This cron is the HARDCODED refresh: closed-trade Sharpe → weights.
//   - MasterMind's Saturday comprehensive-review writes recommendations
//     to strategy_memos; the operator applies them separately.
//   - Both are wanted: hardcoded keeps the book breathing weekly; the
//     Opus review escalates the changes that need human judgment.

const { execSync } = require('child_process');
const path = require('path');
const { Pool } = require('pg');

const ROOT = path.resolve(__dirname, '../../..');

function loadEnv() {
  // Inherit cron env; .env was already EnvironmentFile-loaded by systemd.
  return { ...process.env };
}

async function postDiscord(content) {
  const token = process.env.DISCORD_BOT_TOKEN || process.env.DATABOT_TOKEN || process.env.BOT_TOKEN || '';
  const channel = process.env.DISCORD_GENERAL_CHANNEL_ID || '';
  if (!token || !channel) {
    console.log('discord: skipped (missing token or channel id)');
    return;
  }
  const https = require('https');
  await new Promise((res, rej) => {
    const body = JSON.stringify({ content });
    const req = https.request({
      hostname: 'discord.com',
      path: `/api/v10/channels/${channel}/messages`,
      method: 'POST',
      headers: {
        'Authorization': `Bot ${token}`,
        'Content-Type': 'application/json',
        'User-Agent': 'OpenClawBot (openclaw, 1.0)',
        'Content-Length': Buffer.byteLength(body),
      },
    }, r => { r.on('data', () => {}); r.on('end', res); });
    req.on('error', rej); req.write(body); req.end();
  });
}

(async () => {
  const pool = new Pool({ connectionString: process.env.POSTGRES_URI });

  // Snapshot pre-rebuild state for the diff
  const pre = await pool.query("SELECT strategy_id, regime_state, weight FROM strategy_weights_by_regime WHERE is_current");
  const preMap = {};
  for (const r of pre.rows) preMap[`${r.strategy_id}|${r.regime_state}`] = parseFloat(r.weight);

  // Run the Python rebuild — auto-demote chain is opt-in via OPENCLAW_AUTO_DEMOTE
  console.log('rebuilding strategy_weights via python module…');
  try {
    execSync(`cd ${ROOT} && PYTHONPATH=src python3 -m execution.strategy_weights --rebuild --trigger=weekly_cron --verbose`,
             { stdio: 'inherit', env: loadEnv() });
  } catch (e) {
    console.error('rebuild failed:', e.message);
    await pool.end();
    process.exit(1);
  }

  // Snapshot post + diff
  const post = await pool.query("SELECT strategy_id, regime_state, weight FROM strategy_weights_by_regime WHERE is_current");
  const postMap = {};
  for (const r of post.rows) postMap[`${r.strategy_id}|${r.regime_state}`] = parseFloat(r.weight);

  const deltas = [];
  for (const key of new Set([...Object.keys(preMap), ...Object.keys(postMap)])) {
    const a = preMap[key] || 0, b = postMap[key] || 0;
    if (Math.abs(b - a) > 1e-4) deltas.push({ key, before: a, after: b, change: b - a });
  }
  deltas.sort((a, b) => Math.abs(b.change) - Math.abs(a.change));

  const fmtKey = k => k.replace('|', ' · ');
  const fmtPct = v => (v * 100).toFixed(2) + '%';
  const fmtChg = v => ((v >= 0 ? '+' : '') + (v * 100).toFixed(2));

  const stratCount = new Set(post.rows.map(r => r.strategy_id)).size;
  const regimeCount = new Set(post.rows.map(r => r.regime_state)).size;

  const lines = [
    `**Weekly strategy-weights refresh** — ${new Date().toISOString().slice(0, 10)}`,
    `Active stack: ${stratCount} strategies across ${regimeCount} regimes (${post.rows.length} positive (strategy, regime) entries).`,
  ];

  if (deltas.length === 0) {
    lines.push('', 'No weight changes this week (closed-trade base is stable).');
  } else {
    const gains = deltas.filter(d => d.change > 0).slice(0, 5);
    const losses = deltas.filter(d => d.change < 0).slice(0, 5);
    if (gains.length) {
      lines.push('', '**Top 5 weight gains:**');
      for (const d of gains) lines.push(`  ${fmtKey(d.key)} — ${fmtPct(d.before)} → ${fmtPct(d.after)} (${fmtChg(d.change)})`);
    }
    if (losses.length) {
      lines.push('', '**Top 5 weight losses:**');
      for (const d of losses) lines.push(`  ${fmtKey(d.key)} — ${fmtPct(d.before)} → ${fmtPct(d.after)} (${fmtChg(d.change)})`);
    }
  }

  // Note auto-demote state
  const demoteFlag = process.env.OPENCLAW_AUTO_DEMOTE === '1';
  lines.push('', `OPENCLAW_AUTO_DEMOTE=${demoteFlag ? 'on' : 'off (dry-run only)'}`);

  const content = lines.join('\n');
  console.log('---\n' + content + '\n---');
  await postDiscord(content);

  await pool.end();
})().catch(e => { console.error(e); process.exit(1); });
