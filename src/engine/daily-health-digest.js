'use strict';

/**
 * Daily health digest — posted to Discord so regressions surface in one glance.
 *
 * Reports:
 *  - Last pipeline cycle outcome (trade step ran Y/N + gate reason)
 *  - Signals posted (delta vs yesterday)
 *  - Data freshness alerts
 *  - Curator status (last run, any false positives)
 *  - Open/closed position counts
 */

const { query: dbQuery } = require('../database/postgres');
const { getDataFreshness } = require('../pipeline/freshness');

async function buildDigest(date = new Date()) {
  const [signalRow, prevSignalRow, openCounts, closedStats, freshness, curatorRow] = await Promise.all([
    dbQuery(`SELECT run_date, regime, n_signals, ev_pos, avg_ev, high_conv_count
             FROM daily_signal_summary ORDER BY run_date DESC, created_at DESC LIMIT 1`),
    dbQuery(`SELECT n_signals, ev_pos FROM daily_signal_summary
             ORDER BY run_date DESC, created_at DESC OFFSET 1 LIMIT 1`),
    dbQuery(`SELECT COUNT(*) FILTER (WHERE status='open')   AS open_count,
                    COUNT(*) FILTER (WHERE status='closed') AS closed_count
             FROM execution_signals`),
    dbQuery(`SELECT ROUND(AVG(realized_pnl_pct)::numeric, 4) AS avg_pnl,
                    COUNT(*) FILTER (WHERE realized_pnl_pct > 0) AS wins,
                    COUNT(*) AS total
             FROM signal_pnl WHERE status='closed' AND closed_at > NOW() - INTERVAL '7 days'`),
    getDataFreshness().catch(() => []),
    dbQuery(`SELECT run_id, started_at, input_count, output_count, total_cost_usd
             FROM curator_runs ORDER BY started_at DESC LIMIT 1`).catch(() => ({ rows: [] })),
  ]);

  const sig   = signalRow.rows[0];
  const prev  = prevSignalRow.rows[0];
  const open  = openCounts.rows[0];
  const closed = closedStats.rows[0];
  const cur   = curatorRow.rows[0];

  const staleAlerts = freshness.filter(f => ['stale', 'very_stale', 'empty'].includes(f.status));

  // Gate outcome
  let gateLine;
  if (!sig) {
    gateLine = '⚠️ No signal summary rows yet (daily_signal_summary empty)';
  } else {
    const avgEvPct = (parseFloat(sig.avg_ev) * 100).toFixed(2);
    const greenCnt = Number(sig.ev_pos);
    const totalCnt = Number(sig.n_signals);
    const highConv = Number(sig.high_conv_count);
    const evOk     = parseFloat(avgEvPct) >= -0.5 || greenCnt >= 1;
    gateLine = `${evOk ? '✅' : '🚫'} Latest run (${sig.run_date.toISOString().slice(0,10)}): ${totalCnt} signals, ${greenCnt} green, avg EV ${avgEvPct}%, ${highConv} high-conv — ${evOk ? 'trade step eligible' : 'trade step blocked by quality gate'}`;
  }

  // Signal delta
  let deltaLine = '';
  if (sig && prev) {
    const d = Number(sig.n_signals) - Number(prev.n_signals);
    const dg = Number(sig.ev_pos) - Number(prev.ev_pos);
    deltaLine = `   Δ vs prior: ${d >= 0 ? '+' : ''}${d} signals, ${dg >= 0 ? '+' : ''}${dg} green`;
  }

  // Positions
  const posLine = `📊 Positions: ${open.open_count} open | ${open.closed_count} lifetime closed`;
  let pnlLine   = '';
  if (Number(closed.total) > 0) {
    const avgPct = (parseFloat(closed.avg_pnl) * 100).toFixed(2);
    pnlLine = `   7d closed: ${closed.total} trades, ${closed.wins} wins, avg ${avgPct}%`;
  }

  // Freshness
  let stalenessLine = '✅ Data freshness: all current';
  if (staleAlerts.length > 0) {
    stalenessLine = `⚠️ Data freshness: ${staleAlerts.length} stale — ${staleAlerts.map(s => `${s.dataset}(Δ${s.deltaDays}d)`).join(', ')}`;
  }

  // Curator
  let curatorLine = '🔎 Curator: no runs on record';
  if (cur) {
    const ago = Math.round((Date.now() - new Date(cur.started_at).getTime()) / 3600000);
    curatorLine = `🔎 Curator last run: ${ago}h ago, ${cur.input_count}→${cur.output_count} papers, $${parseFloat(cur.total_cost_usd).toFixed(2)}`;
  }

  const header = `📰 **OpenClaw daily health digest — ${date.toISOString().slice(0,10)}**`;
  return [header, gateLine, deltaLine, posLine, pnlLine, stalenessLine, curatorLine].filter(Boolean).join('\n');
}

/** Register a Mon–Fri 08:15 ET cron via node-cron. */
function register(cron, postFn) {
  cron.schedule('15 8 * * 1-5', async () => {
    try {
      const text = await buildDigest();
      await postFn(text);
    } catch (err) {
      console.warn('[health-digest] Failed:', err.message);
    }
  }, { timezone: 'America/New_York' });
}

module.exports = { buildDigest, register };
