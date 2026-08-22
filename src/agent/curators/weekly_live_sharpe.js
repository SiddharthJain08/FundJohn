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

const { postToChannel } = require('./_discord_webhook');

async function postDiscord(content) {
  // Route through botjohn's #general webhook (looked up in agent_registry).
  // Pre-2026-05-18 this checked DISCORD_GENERAL_CHANNEL_ID which was never
  // set in .env, so every Sunday's weekly-weights rebuild silently logged
  // "discord: skipped (missing token or channel id)" instead of posting.
  const r = await postToChannel('botjohn', 'general', content);
  if (!r.ok) {
    console.error(`discord: post failed — ${r.reason || r.status}: ${r.detail || r.body || ''}`);
  }
}

(async () => {
  const pool = new Pool({ connectionString: process.env.POSTGRES_URI });

  // Snapshot pre-rebuild state for the diff
  const pre = await pool.query("SELECT strategy_id, regime_state, weight FROM strategy_weights_by_regime WHERE is_current");
  const preMap = {};
  for (const r of pre.rows) preMap[`${r.strategy_id}|${r.regime_state}`] = parseFloat(r.weight);

  // Strategy Activation Slider: refresh strategy_regime_params.eligible from
  // the freshest corrected per-regime backtest metrics BEFORE the weights
  // rebuild below, so weights are computed on the up-to-date eligible set.
  // Non-fatal (log + continue) — a deriver hiccup must not block the weekly
  // reweight, same posture as the daily_returns/similarity/fold_report steps
  // further down.
  //
  // GATED default-OFF (OPENCLAW_ACTIVATION_ASSIGNER, mirrors OPENCLAW_AUTO_DEMOTE
  // below): the FIRST live application is a large one-time eligibility shift
  // (~13 strategies dormant at 0.5) and must be operator-controlled — run
  // `activation_assigner --all` manually, eyeball the diff, THEN flip this flag
  // to '1' so subsequent weekly cycles auto-refresh. Until then this weekly
  // job is weights-only (eligibility untouched), and must NOT auto-fire on the
  // Mon 04:00 timer while the §7 re-backtest of the stale strategies is still
  // completing (would derive the not-yet-corrected ones on smeared metrics).
  if (process.env.OPENCLAW_ACTIVATION_ASSIGNER === '1') {
    console.log('refreshing strategy-regime activation eligibility…');
    try {
      execSync(`cd ${ROOT} && PYTHONPATH=src python3 -m backtest.activation_assigner --all --notify --trigger=weekly_cron`,
        { stdio: 'inherit', env: loadEnv() });
    } catch (e) {
      console.error('activation_assigner refresh failed (non-fatal):', e.message);
    }
  } else {
    console.log('activation eligibility refresh SKIPPED (OPENCLAW_ACTIVATION_ASSIGNER!=1); weights-only rebuild');
  }

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

  // Orthogonalization weekly steps (non-fatal). 1) per-strategy daily returns.
  try {
    execSync(`cd ${ROOT} && PYTHONPATH=src python3 -c "from execution import strategy_returns as s; print('daily_returns rows:', s.rebuild_daily_returns())"`,
      { stdio: 'inherit', env: loadEnv() });
  } catch (e) {
    console.error('strategy_daily_returns rebuild failed (non-fatal):', e.message);
  }

  // 2) per-regime similarity + fold/factor groups.
  try {
    console.log('rebuilding strategy similarity (orthogonalization)…');
    execSync(`cd ${ROOT} && PYTHONPATH=src python3 -m execution.strategy_similarity --rebuild --trigger=weekly_cron --verbose`,
      { stdio: 'inherit', env: loadEnv() });
  } catch (e) {
    console.error('similarity rebuild failed (non-fatal):', e.message);
  }

  // 3) chronic-fold report -> #strategy-memos (manual-retirement digest).
  try {
    execSync(`cd ${ROOT} && OPENCLAW_FOLD_REPORT_POST=1 PYTHONPATH=src python3 -m execution.fold_report`,
      { stdio: 'inherit', env: loadEnv() });
  } catch (e) {
    console.error('fold report failed (non-fatal):', e.message);
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
