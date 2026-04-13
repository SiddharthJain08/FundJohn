'use strict';

/**
 * Flash Agent — lightweight mode for quick lookups.
 * No sandbox, no subagent spawning. Uses JSON snapshot tools directly.
 * Target response time: <10 seconds.
 */

const quote       = require('./tools/snapshot/quote');
const profile     = require('./tools/snapshot/profile');
const calendar    = require('./tools/snapshot/earnings-calendar');
const marketStatus = require('./tools/snapshot/market-status');
const { getBucketStatus } = require('../database/redis');
const { getAllSubagentStatuses } = require('../database/redis');
const { verdictCache, query } = require('../database/postgres');
const { formatStatus } = require('./subagents/lifecycle');
const store    = require('../pipeline/store');
const tokenDb  = require('../database/tokens');

const DASHBOARD_URL = `http://69.62.68.201`;

const COMMANDS = {
  ping:       handlePing,
  status:     handleStatus,
  quote:      handleQuote,
  profile:    handleProfile,
  calendar:   handleCalendar,
  market:     handleMarketStatus,
  rate:       handleRateLimits,
  verdict:    handleVerdictLookup,
  dashboard:  handleDashboard,
  coverage:   handleCoverage,
  prices:     handlePrices,
  greeks:     handleGreeks,
  options:    handleGreeks,
  spend:      handleSpend,
  cost:       handleCost,
  estimate:   handleEstimate,
  config:     handleConfig,
  cycles:     handleCycles,
  budget:     handleBudget,
  help:       handleHelp,
};

/**
 * Dispatch a flash command.
 * @param {string} command — e.g. 'ping', 'status', 'quote AAPL'
 * @param {string} threadId
 * @returns {Promise<string>} — response text for Discord
 */
async function dispatch(command, threadId) {
  const parts = command.trim().split(/\s+/);
  const cmd = parts[0].toLowerCase().replace(/^\//, '');
  const args = parts.slice(1);

  const handler = COMMANDS[cmd];
  if (!handler) {
    // Not a flash command — signal to route to PTC mode
    return null;
  }

  try {
    return await handler(args, threadId);
  } catch (err) {
    return `❌ Flash error: ${err.message}`;
  }
}

async function handlePing() {
  return `🦞 BotJohn online | ${new Date().toISOString()}`;
}

async function handleStatus(args, threadId) {
  const ps = require('../database/pipeline-state');
  const { getBudgetStatus } = require('../budget/enforcer');

  const [agentStatuses, interrupted, budget] = await Promise.all([
    getAllSubagentStatuses().catch(() => []),
    ps.findInterruptedRuns().catch(() => []),
    getBudgetStatus().catch(() => null),
  ]);

  const lines = [];

  // Budget mode
  if (budget) {
    const modeEmoji = { GREEN: '🟢', YELLOW: '🟡', RED: '🔴' }[budget.mode] || '⚪';
    lines.push(`${modeEmoji} Budget: **${budget.mode}** — $${budget.dailyUsd.toFixed(2)}/day | $${budget.monthlyUsd.toFixed(2)} of $${budget.budgetUsd}/mo`);
  }

  // Active subagents
  const active = agentStatuses.filter((s) => !threadId || s.threadId === threadId);
  if (active.length) {
    lines.push('\n**Active subagents:**');
    active.forEach((s) => {
      const elapsed = s.startedAt ? `${Math.round((Date.now() - s.startedAt) / 1000)}s` : '?';
      const emoji   = s.status === 'running' ? '⚙️' : s.status === 'complete' ? '✅' : '❌';
      lines.push(`${emoji} **${s.type}** [${s.ticker}] — ${s.status} | ${elapsed}`);
    });
  } else {
    lines.push('✅ No active subagents');
  }

  // Interrupted runs (operator decision required)
  if (interrupted.length > 0) {
    lines.push(`\n⚠️ **${interrupted.length} INTERRUPTED run(s)** — operator review required:`);
    interrupted.forEach((r) => {
      const age = Math.round((Date.now() - new Date(r.started_at).getTime()) / 60000);
      lines.push(`  • \`${r.run_id.slice(0,8)}\` ${r.ticker || 'N/A'} — stopped at **${r.current_stage}** (${age}m ago)`);
    });
    lines.push('  Re-run manually: `/diligence TICKER` to restart');
  }

  return lines.join('\n') || '✅ System idle';
}

async function handleQuote([ticker]) {
  if (!ticker) return 'Usage: `/quote AAPL`';
  const data = await quote.get(ticker);
  if (!data) return `No quote data for ${ticker}`;
  return `**${ticker}** $${data.price} (${data.change >= 0 ? '+' : ''}${data.change?.toFixed(2) ?? 'N/A'} / ${data.changePct?.toFixed(2) ?? 'N/A'}%) | Vol: ${fmtVol(data.volume)} | Mkt Cap: ${fmtCap(data.marketCap)}`;
}

async function handleProfile([ticker]) {
  if (!ticker) return 'Usage: `/profile AAPL`';
  const data = await profile.get(ticker);
  if (!data) return `No profile for ${ticker}`;
  return `**${data.companyName}** (${ticker}) | ${data.sector} → ${data.industry} | Exchange: ${data.exchangeShortName} | Mkt Cap: ${fmtCap(data.mktCap)}`;
}

async function handleCalendar([ticker]) {
  if (!ticker) return 'Usage: `/calendar AAPL`';
  const data = await calendar.get(ticker);
  if (!data || !data.length) return `No upcoming earnings for ${ticker}`;
  const next = data[0];
  return `**${ticker}** next earnings: **${next.date}** | EPS est: ${next.epsEstimated ?? 'N/A'} | Rev est: ${next.revenueEstimated ? fmtCap(next.revenueEstimated) : 'N/A'}`;
}

async function handleMarketStatus() {
  const data = await marketStatus.get();
  const status = data?.isTheStockMarketOpen ? '🟢 OPEN' : '🔴 CLOSED';
  return `Market status: **${status}** | ${new Date().toLocaleTimeString('en-US', { timeZone: 'America/New_York' })} ET`;
}

async function handleRateLimits() {
  const { getProviderRateLimitStatus } = require('../database/redis');
  const [buckets, providerState] = await Promise.all([
    getBucketStatus().catch(() => ({})),
    getProviderRateLimitStatus().catch(() => ({})),
  ]);

  const lines = ['**Rate Limits**'];

  // Provider-level 429 state
  lines.push('Provider state:');
  for (const [p, s] of Object.entries(providerState)) {
    if (s.limited) {
      lines.push(`  ⛔ ${p}: RATE LIMITED — resets ${s.resetAt}`);
    } else {
      lines.push(`  ✅ ${p}: ready`);
    }
  }

  // Per-minute token buckets
  lines.push('Token buckets (per-min):');
  if (!Object.keys(buckets).length) {
    lines.push('  ⚠️ No bucket data (Redis not connected)');
  } else {
    Object.entries(buckets).forEach(([p, n]) => lines.push(`  ${p}: ${n} tokens remaining`));
  }

  return lines.join('\n');
}

async function handleBudget() {
  const { getBudgetStatus } = require('../budget/enforcer');
  const s = await getBudgetStatus().catch(() => null);
  if (!s) return '⚠️ Budget data unavailable';

  const modeEmoji = { GREEN: '🟢', YELLOW: '🟡', RED: '🔴' }[s.mode] || '⚪';
  const bar = (() => {
    const pct = Math.min(100, Math.round(s.pctUsed || 0));
    const filled = Math.round(pct / 5);
    return '[' + '█'.repeat(filled) + '░'.repeat(20 - filled) + `] ${pct}%`;
  })();

  return [
    `**Budget** ${modeEmoji} ${s.mode}`,
    `Monthly: $${s.monthlyUsd.toFixed(2)} / $${s.budgetUsd} ${bar}`,
    `Today:   $${s.dailyUsd.toFixed(2)} | Remaining: $${s.remainingUsd.toFixed(2)}`,
    `Proj EOM: $${s.projectedMonthUsd.toFixed(2)}`,
    `Thresholds — Yellow: $${s.thresholds?.yellowDaily}/day or ${s.thresholds?.yellowPct}% | Red: $${s.thresholds?.redDaily}/day or ${s.thresholds?.redPct}%`,
  ].join('\n');
}

async function handleVerdictLookup([ticker]) {
  if (!ticker) return 'Usage: `/verdict AAPL`';
  const cached = await verdictCache.getFresh(ticker.toUpperCase(), 'diligence').catch(() => null);
  if (!cached) return `No fresh verdict for ${ticker}. Run \`!john /diligence ${ticker}\` to analyze.`;
  const stale = new Date(cached.stale_after).toLocaleDateString();
  return `**${ticker}** — **${cached.verdict}** (${cached.score}) | ${cached.analysis_date} | Fresh until: ${stale}`;
}

async function handleDashboard() {
  return `📊 **OpenClaw Dashboard** — ${DASHBOARD_URL}\nLive pipeline status, price charts, coverage stats. Open in any browser.`;
}

async function handleCoverage() {
  const cov = await store.getCoverageStats().catch(() => null);
  if (!cov) return '⚠️ Coverage stats unavailable — DB not connected';
  const bar = (n, max = 100) => {
    const filled = Math.round((Math.min(n, max) / max) * 10);
    return '█'.repeat(filled) + '░'.repeat(10 - filled) + ` ${n}/${max}`;
  };
  const fmtNum = (n) => n ? Number(n).toLocaleString() : '0';
  return [
    '**📡 S&P 100 — Backtest Archive**',
    `Prices:      ${bar(cov.price_coverage)}  (${fmtNum(cov.price_rows_total)} rows | ${cov.price_earliest?.toISOString?.().slice(0,10) ?? '—'} → ${cov.price_latest?.toISOString?.().slice(0,10) ?? '—'})`,
    `Options:     ${bar(cov.options_coverage)}  (${fmtNum(cov.options_rows_total)} contracts)`,
    `Technicals:  ${bar(cov.tech_coverage)}`,
    `Fundamentals:${bar(cov.fund_coverage)}`,
    `Snapshots:   ${bar(cov.snapshot_tickers)}  (${fmtNum(cov.snapshot_rows_total)} intraday rows)`,
    `─────────────────────────────`,
    `Pipeline health — live: ${cov.live_coverage}/100 | runs/hr: ${cov.runs_last_hour} | errors/hr: ${cov.errors_last_hour}`,
  ].join('\n');
}

async function handlePrices([ticker, daysArg]) {
  if (!ticker) return 'Usage: `!john /prices AAPL [days]`';
  ticker = ticker.toUpperCase();
  const days = parseInt(daysArg) || 10;
  const res = await query(
    `SELECT date, open, high, low, close, volume FROM price_data
     WHERE ticker=$1 ORDER BY date DESC LIMIT $2`,
    [ticker, days]
  ).catch(() => null);
  if (!res || !res.rows.length) return `No price data for **${ticker}** — run \`!john /fetch ${ticker}\` first`;
  const rows = res.rows.reverse();
  const header = `**${ticker}** — last ${rows.length} sessions\n\`\`\``;
  const lines = rows.map(r =>
    `${r.date}  O:${Number(r.open).toFixed(2).padStart(8)}  H:${Number(r.high).toFixed(2).padStart(8)}  L:${Number(r.low).toFixed(2).padStart(8)}  C:${Number(r.close).toFixed(2).padStart(8)}  Vol:${fmtVol(Number(r.volume)).padStart(7)}`
  );
  return header + lines.join('\n') + '\n```';
}

async function handleGreeks([ticker]) {
  if (!ticker) return 'Usage: `!john /greeks AAPL`';
  ticker = ticker.toUpperCase();
  // Top 8 contracts near ATM sorted by open interest
  const res = await query(
    `SELECT expiry, strike, contract_type, delta, gamma, theta, vega, iv, open_interest
     FROM options_data WHERE ticker=$1
     ORDER BY snapshot_date DESC, open_interest DESC NULLS LAST LIMIT 8`,
    [ticker]
  ).catch(() => null);
  if (!res || !res.rows.length) return `No options data for **${ticker}** yet — pipeline collecting`;
  const fmt = (v, d = 4) => v != null ? Number(v).toFixed(d) : 'N/A';
  const header = `**${ticker}** options Greeks (top by OI)\n\`\`\``;
  const lines = res.rows.map(r =>
    `${r.expiry} $${Number(r.strike).toFixed(0).padStart(6)} ${r.contract_type.toUpperCase().padEnd(4)}  Δ:${fmt(r.delta).padStart(7)}  γ:${fmt(r.gamma).padStart(7)}  θ:${fmt(r.theta).padStart(7)}  IV:${fmt(r.iv, 2).padStart(6)}  OI:${(r.open_interest || 0).toLocaleString().padStart(8)}`
  );
  return header + lines.join('\n') + '\n```';
}

async function handleSpend([daysArg]) {
  const days = parseInt(daysArg) || 7;
  const [totals, breakdown] = await Promise.all([
    tokenDb.getTotalSpend(days),
    tokenDb.getSpendSummary(days),
  ]);
  if (!totals) return '⚠️ Cost data unavailable';

  const fmt = (n) => n != null ? `$${Number(n).toFixed(4)}` : '$0.0000';
  const lines = [
    `**💰 Spend Summary** (last ${days}d)`,
    `Today: **${fmt(totals.today_usd)}** | Week: **${fmt(totals.week_usd)}** | Total: **${fmt(totals.total_usd)}** (${totals.total_runs} runs)`,
  ];
  if (breakdown.length) {
    lines.push('');
    for (const row of breakdown) {
      lines.push(`\`${row.task_type.padEnd(12)}\` ${String(row.runs).padStart(3)} runs | avg ${fmt(row.avg_cost)} | max ${fmt(row.max_cost)} | total **${fmt(row.total_cost)}**`);
    }
  } else {
    lines.push('No completed tasks recorded yet.');
  }
  return lines.join('\n');
}

async function handleCost([taskId]) {
  if (!taskId) return 'Usage: `!john /cost {task_id}`';
  const record = await tokenDb.getTaskCost(taskId);
  if (!record) return `No cost record for task \`${taskId}\``;
  const subs = (record.subagents || []).filter(Boolean);
  const lines = [
    `**Task ${taskId.slice(0, 8)}…** — ${record.task_type} ${record.ticker || ''} | Status: ${record.status}`,
    `Actual: **$${Number(record.cost_usd).toFixed(4)}**${record.est_cost_usd ? ` | Estimate was: $${Number(record.est_cost_usd).toFixed(4)}` : ''} | ${record.num_subagents} subagents | ${Math.round((record.duration_ms || 0) / 1000)}s`,
  ];
  if (subs.length) {
    lines.push('```');
    for (const s of subs) {
      lines.push(`${s.type?.padEnd(16)} $${Number(s.cost || 0).toFixed(4)}  ${Math.round((s.duration_ms || 0) / 1000)}s  ${s.turns ?? '?'} turns`);
    }
    lines.push('```');
  }
  return lines.join('\n');
}

async function handleEstimate([taskType, ticker]) {
  if (!taskType) return 'Usage: `!john /estimate diligence [AAPL]`';
  const type = taskType.toLowerCase().replace(/^\//, '');
  const est = await tokenDb.estimateCost(type, ticker?.toUpperCase());
  if (!est.estimated) return `No historical data for **${type}** yet — run a few tasks first (${est.samples} samples so far)`;
  return [
    `**💰 Cost Estimate: ${type}${ticker ? ` ${ticker.toUpperCase()}` : ''}**`,
    `Expected: **$${est.estimated.toFixed(4)}** | Range: $${est.low.toFixed(4)} – $${est.high.toFixed(4)}`,
    `Based on **${est.samples}** prior runs | avg ${est.avgSubagents} subagents | ~${Math.round(est.avgDurationS / 60)}m`,
  ].join('\n');
}

async function handleConfig([key, ...rest]) {
  const value = rest.join(' ');
  if (key && value) {
    // Set a config value
    await store.setConfig(key, value);
    return `✅ Config updated: \`${key}\` = \`${value}\``;
  }
  // Show all config
  const rows = await store.getAllConfig();
  if (!rows.length) return '⚠️ Config table not yet populated';
  const lines = ['**⚙️ Pipeline Config**', '```'];
  for (const r of rows) {
    lines.push(`${r.key.padEnd(26)} ${r.value.padEnd(12)} ${r.description || ''}`);
  }
  lines.push('```', '*Usage: `!john /config <key> <value>` to update*');
  return lines.join('\n');
}

async function handleCycles([nArg]) {
  const n = Math.min(parseInt(nArg) || 10, 50);
  const cycles = await store.getCycleHistory(n).catch(() => null);
  if (!cycles) return '⚠️ Cycle history unavailable — DB not connected';
  if (!cycles.length) return '📊 No completed collection cycles yet.';

  const fmtDur = (ms) => {
    if (!ms) return '—';
    if (ms < 60000) return `${Math.round(ms / 1000)}s`;
    return `${Math.round(ms / 60000)}m ${Math.round((ms % 60000) / 1000)}s`;
  };
  const fmtNum = (n) => n != null ? Number(n).toLocaleString() : '0';
  const statusIcon = (s) => s === 'complete' ? '✅' : s === 'running' ? '⚙️' : '❌';

  const lines = ['**📊 Collection Cycle History**', '```'];
  lines.push('Date/Time (ET)        Dur     Prices  Opts   Tech   Fund   API(P/F/Y)  Rows    Err  St');
  lines.push('─'.repeat(96));

  for (const c of cycles) {
    const dt = new Date(c.started_at).toLocaleString('en-US', {
      timeZone: 'America/New_York', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
    const api = `${c.polygon_calls||0}/${c.fmp_calls||0}/${c.yfinance_calls||0}`;
    lines.push(
      `${dt.padEnd(22)}${fmtDur(c.duration_ms).padEnd(8)}` +
      `${fmtNum(c.price_rows).padStart(7)}  ` +
      `${fmtNum(c.options_contracts).padStart(5)}  ` +
      `${fmtNum(c.technical_rows).padStart(5)}  ` +
      `${fmtNum(c.fundamental_records).padStart(5)}  ` +
      `${api.padEnd(12)}` +
      `${fmtNum(c.total_rows).padStart(6)}  ` +
      `${String(c.errors||0).padStart(3)}  ` +
      `${statusIcon(c.status)}`
    );
  }
  lines.push('```');
  lines.push(`*Showing last ${cycles.length} cycle(s). API columns: Polygon / FMP / YFinance calls.*`);
  return lines.join('\n');
}

async function handleHelp() {
  return [
    '**⚡ Flash** *(instant)*',
    '`!john /ping` — health check',
    '`!john /status` — active subagent status',
    '`!john /market` — market open/closed',
    '`!john /rate` — API rate limit buckets',
    '`!john /quote TICKER` — real-time quote',
    '`!john /profile TICKER` — company profile',
    '`!john /calendar TICKER` — next earnings date',
    '`!john /verdict TICKER` — cached diligence verdict',
    '',
    '**📡 Dashboard & Data**',
    '`!john /dashboard` — dashboard link',
    '`!john /coverage` — S&P 100 pipeline coverage',
    '`!john /prices TICKER [days]` — OHLCV table from DB',
    '`!john /chart TICKER` — price chart (PNG attachment)',
    '`!john /greeks TICKER` — options Greeks from DB',
    '',
    '**💰 Cost Tracking**',
    '`!john /spend [days]` — spend summary (default 7d)',
    '`!john /estimate diligence [TICKER]` — pre-task cost estimate',
    '`!john /cost {task_id}` — breakdown for a specific task',
    '',
    '**🔬 Research & Trade** *(PTC subagents)*',
    '`!john /fetch TICKER` — fetch + chart via data agent',
    '`!john /diligence TICKER` — full diligence pipeline',
    '`!john /trade TICKER` — trade pipeline + sizing',
    '`!john /approve {id}` · `!john /reject {id}`',
    '',
    '**📡 Pipeline**',
    '`!john /pipeline status` — coverage stats + sleep/quota state',
    '`!john /pipeline pause` · `!john /pipeline resume`',
    '`!john /pipeline cycles [n]` — last N collection cycle metrics (default 10)',
    '',
    '**🔌 System**',
    '`!john /shutdown confirm` — stop BotJohn process',
    '`!john /shutdown server confirm` — stop BotJohn + poweroff VPS',
  ].join('\n');
}

// Formatters
function fmtVol(n) {
  if (!n) return 'N/A';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  return n.toLocaleString();
}

function fmtCap(n) {
  if (!n) return 'N/A';
  if (n >= 1e12) return `$${(n / 1e12).toFixed(2)}T`;
  if (n >= 1e9) return `$${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `$${(n / 1e6).toFixed(0)}M`;
  return `$${n.toLocaleString()}`;
}

module.exports = { dispatch };
