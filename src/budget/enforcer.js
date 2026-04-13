'use strict';

/**
 * Budget enforcer — deterministic cost thresholds, zero token spend.
 * Reads actual costs from DB, computes mode in pure JS math.
 *
 * Modes:
 *   GREEN  — all phases run normally
 *   YELLOW — skip Phase 6 (news), reduce Phase 5 (fundamentals) to weekly
 *   RED    — price collection only; all PTC ops require manual trigger
 */

const fs   = require('fs');
const path = require('path');

const CONFIG_PATH = path.join(__dirname, '../../config/budget.json');

function loadConfig() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  } catch {
    return {
      monthly_budget_usd: 400,
      daily_burn_yellow_usd: 20,
      daily_burn_red_usd: 35,
      monthly_pct_yellow: 75,
      monthly_pct_red: 90,
    };
  }
}

/**
 * Compute current budget mode from DB cost data.
 * Returns { mode, dailyUsd, monthlyUsd, budgetUsd, remainingUsd, projectedMonthUsd }
 */
async function checkBudget() {
  const cfg = loadConfig();
  const { getTotalSpend } = require('../database/tokens');
  const spend = await getTotalSpend(30).catch(() => ({ total_usd: 0, today_usd: 0 }));

  const dailyUsd   = parseFloat(spend.today_usd   || 0);
  const monthlyUsd = parseFloat(spend.total_usd    || 0);
  const budgetUsd  = cfg.monthly_budget_usd;
  const pctUsed    = budgetUsd > 0 ? (monthlyUsd / budgetUsd) * 100 : 0;

  // Project end-of-month based on daily burn rate (30-day month)
  const projectedMonthUsd = dailyUsd * 30;

  let mode = 'GREEN';
  if (dailyUsd >= cfg.daily_burn_red_usd || pctUsed >= cfg.monthly_pct_red) {
    mode = 'RED';
  } else if (dailyUsd >= cfg.daily_burn_yellow_usd || pctUsed >= cfg.monthly_pct_yellow) {
    mode = 'YELLOW';
  }

  // Persist mode to Redis for Flash mode /status and /budget
  try {
    const { getClient } = require('../database/redis');
    await getClient().set('budget:mode', mode, 'EX', 3600); // 1h TTL — refreshed each cycle
    await getClient().set('budget:daily_usd',   dailyUsd.toFixed(4),   'EX', 3600);
    await getClient().set('budget:monthly_usd', monthlyUsd.toFixed(4), 'EX', 3600);
  } catch { /* Redis unavailable — non-blocking */ }

  return { mode, dailyUsd, monthlyUsd, budgetUsd, remainingUsd: budgetUsd - monthlyUsd, projectedMonthUsd, pctUsed };
}

/**
 * Apply budget mode constraints to pipeline collection phases.
 * Returns { skipNews, skipFundamentals, blockPTC }
 * Caller uses these flags to conditionally skip phases.
 */
function enforceBudget(mode) {
  const { broadcast } = (() => {
    try { return require('../channels/api/server'); } catch { return { broadcast: () => {} }; }
  })();

  switch (mode) {
    case 'RED':
      console.warn('[budget] RED — price collection only; PTC ops require manual trigger');
      broadcast({ type: 'budget-red', mode, ts: new Date().toISOString() });
      return { skipNews: true, skipFundamentals: true, blockPTC: true };

    case 'YELLOW':
      console.warn('[budget] YELLOW — skipping news; fundamentals reduced to weekly');
      broadcast({ type: 'budget-yellow', mode, ts: new Date().toISOString() });
      return { skipNews: true, skipFundamentals: _isFundamentalsDay(), blockPTC: false };

    default: // GREEN
      return { skipNews: false, skipFundamentals: false, blockPTC: false };
  }
}

// Fundamentals run weekly (Sunday) in YELLOW mode
function _isFundamentalsDay() {
  return new Date().getDay() !== 0; // skip if not Sunday
}

/**
 * Read current mode from Redis (for Flash commands).
 */
async function getBudgetStatus() {
  try {
    const { getClient } = require('../database/redis');
    const r = getClient();
    const [mode, daily, monthly] = await Promise.all([
      r.get('budget:mode'),
      r.get('budget:daily_usd'),
      r.get('budget:monthly_usd'),
    ]);
    const cfg = loadConfig();
    const m   = parseFloat(monthly || 0);
    const d   = parseFloat(daily   || 0);
    const projectedMonthUsd = d * 30;
    return {
      mode:              mode || 'GREEN',
      dailyUsd:          d,
      monthlyUsd:        m,
      budgetUsd:         cfg.monthly_budget_usd,
      remainingUsd:      cfg.monthly_budget_usd - m,
      projectedMonthUsd,
      pctUsed:           cfg.monthly_budget_usd > 0 ? (m / cfg.monthly_budget_usd) * 100 : 0,
      thresholds: {
        yellowDaily: cfg.daily_burn_yellow_usd,
        redDaily:    cfg.daily_burn_red_usd,
        yellowPct:   cfg.monthly_pct_yellow,
        redPct:      cfg.monthly_pct_red,
      },
    };
  } catch {
    return { mode: 'GREEN', dailyUsd: 0, monthlyUsd: 0, budgetUsd: 400, remainingUsd: 400 };
  }
}

module.exports = { checkBudget, enforceBudget, getBudgetStatus, loadConfig };
