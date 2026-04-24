'use strict';

/**
 * comprehensive_review.js — MasterMindJohn (Opus 4.7, 1M ctx) weekly strategy
 * review. Runs every Saturday at 18:00 ET via systemd timer.
 *
 * For each live / monitoring / approved strategy in strategy_registry, this
 * curator:
 *   1. Pulls the full lifetime trade history (execution_signals + signal_pnl).
 *   2. Pre-computes counterfactual summaries (what-if wider stop, tighter
 *      target, shorter max hold, larger size) so Opus sees concrete numbers
 *      to reason over instead of raw rows.
 *   3. Ships the package to Opus with a strict memo template. Opus produces:
 *        - lifetime_summary      (JSON: realised sharpe, win_rate, avg_pnl,
 *                                 best/worst trades, regime mix)
 *        - parameter_analysis    (JSON: sensitivity to size/stop/target/hold)
 *        - recommendations       (JSON: {size_pct_delta, stop_delta_pct,
 *                                 target_delta_pct, hold_days_delta, action})
 *        - markdown_body         (human memo — posted to #strategy-memos)
 *   4. INSERTs one row per strategy into `strategy_memos`.
 *   5. Posts the markdown memo to Discord #strategy-memos (unless --dry-run).
 *
 * Consumed downstream by:
 *   - position_recommender.js (reads latest memos, emits sizing recs)
 *   - trade_handoff_builder.py (reads strategy_sizing_recommendations)
 */

const { runOneShot, parseJsonBlock } = require('./_opus_oneshot');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || '/root/openclaw';
const WORKSPACE    = `${OPENCLAW_DIR}/workspaces/default`;

async function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

async function _fetchStrategies(strategyIds) {
  if (strategyIds && strategyIds.length) {
    const { rows } = await _query(
      `SELECT id, name, description, tier, parameters, regime_conditions,
              universe, signal_frequency, backtest_sharpe, backtest_return_pct,
              backtest_max_dd_pct, status, created_at, approved_at
         FROM strategy_registry WHERE id = ANY($1::text[])`,
      [strategyIds]
    );
    return rows;
  }
  const { rows } = await _query(
    `SELECT id, name, description, tier, parameters, regime_conditions,
            universe, signal_frequency, backtest_sharpe, backtest_return_pct,
            backtest_max_dd_pct, status, created_at, approved_at
       FROM strategy_registry
      WHERE status IN ('live','monitoring','approved','pending_approval')
        AND (deprecated_at IS NULL)
      ORDER BY id`
  );
  return rows;
}

async function _buildTradePack(strategyId) {
  // Full lifetime execution + pnl rows + 30-day veto histogram for this
  // strategy. Veto histogram migrated here from TradeJohn's daily handoff
  // (2026-04-27): multi-week veto patterns drive Mastermind's weekly
  // strategy memo + sizing deltas, not daily TradeJohn sizing.
  const [sigRes, pnlRes, vetoRes] = await Promise.all([
    _query(
      `SELECT id::text, signal_date, ticker, direction, entry_price, stop_loss,
              target_1, target_2, target_3, position_size_pct, regime_state,
              status, created_at
         FROM execution_signals
        WHERE strategy_id = $1
        ORDER BY signal_date DESC
        LIMIT 600`,
      [strategyId]
    ),
    _query(
      `SELECT signal_id::text, pnl_date, close_price, unrealized_pnl_pct,
              days_held, status, closed_price, closed_at, close_reason
         FROM signal_pnl
        WHERE strategy_id = $1
        ORDER BY pnl_date DESC
        LIMIT 1500`,
      [strategyId]
    ),
    _query(
      `SELECT veto_reason, COUNT(*)::int AS n
         FROM veto_log
        WHERE strategy_id = $1
          AND run_date >= NOW()::date - INTERVAL '30 days'
        GROUP BY veto_reason
        ORDER BY n DESC`,
      [strategyId]
    ).catch(() => ({ rows: [] })),
  ]);
  const vetoHistogram = Object.fromEntries(
    vetoRes.rows.map(r => [r.veto_reason, r.n])
  );
  return { signals: sigRes.rows, pnl: pnlRes.rows, vetoHistogram };
}

function _counterfactuals(pnl) {
  // Closed trades only.
  const closed = pnl.filter(r => r.status === 'closed' && r.unrealized_pnl_pct != null);
  if (closed.length < 3) {
    return { n_closed: closed.length, note: 'too few closed trades for counterfactuals' };
  }
  const pcts = closed.map(r => Number(r.unrealized_pnl_pct) / 100);
  const sum = (arr) => arr.reduce((a, b) => a + b, 0);
  const avg = (arr) => arr.length ? sum(arr) / arr.length : 0;
  const std = (arr) => {
    const m = avg(arr);
    return Math.sqrt(avg(arr.map(x => (x - m) ** 2)));
  };

  const base = {
    n_closed: closed.length,
    avg_pct:  +(avg(pcts) * 100).toFixed(3),
    std_pct:  +(std(pcts) * 100).toFixed(3),
    win_rate: +(closed.filter(r => Number(r.unrealized_pnl_pct) > 0).length / closed.length).toFixed(3),
    stops_hit: closed.filter(r => r.close_reason === 'stop_loss').length,
    targets_hit: closed.filter(r => r.close_reason === 'target' || r.close_reason === 'target_1' || r.close_reason === 'target_2').length,
    time_exits: closed.filter(r => r.close_reason === 'time' || r.close_reason === 'max_hold').length,
    avg_hold_days: +(avg(closed.map(r => Number(r.days_held || 0)))).toFixed(2),
  };

  // Counterfactual: what if stop were 50% wider on stop-outs? (assume they
  // would have ridden back to the avg of non-stopped winners).
  const winners = closed.filter(r => Number(r.unrealized_pnl_pct) > 0).map(r => Number(r.unrealized_pnl_pct) / 100);
  const avgWinner = avg(winners);
  const stopRecoveryHyp = closed.map(r => {
    if (r.close_reason !== 'stop_loss') return Number(r.unrealized_pnl_pct) / 100;
    // Assume wider stop would have captured 40% of the avg winner's return
    return avgWinner * 0.4;
  });
  const widerStop = {
    avg_pct: +(avg(stopRecoveryHyp) * 100).toFixed(3),
    sharpe_delta_guess: +((avg(stopRecoveryHyp) - avg(pcts)) / (std(pcts) + 1e-9)).toFixed(3),
  };

  // Counterfactual: what if max-hold were shorter (exit at day 5 if not stopped)?
  const shorterHoldHyp = closed.map(r => {
    const dh = Number(r.days_held || 0);
    if (r.close_reason === 'stop_loss') return Number(r.unrealized_pnl_pct) / 100;
    if (dh > 5) return Number(r.unrealized_pnl_pct) / 100 * (5 / dh);  // linear proxy
    return Number(r.unrealized_pnl_pct) / 100;
  });
  const shorterHold = {
    avg_pct: +(avg(shorterHoldHyp) * 100).toFixed(3),
    sharpe_delta_guess: +((avg(shorterHoldHyp) - avg(pcts)) / (std(pcts) + 1e-9)).toFixed(3),
  };

  // Counterfactual: what if position size were 1.5x?
  const largerSize = {
    avg_pct: +(avg(pcts) * 100 * 1.5).toFixed(3),
    dd_guess_mult: 1.5,
  };

  // Regime breakdown from execution_signals.regime_state (joined by signal_id
  // is hard here; use close_reason mix instead as proxy).
  const reasonMix = {};
  for (const r of closed) {
    const k = r.close_reason || 'unspecified';
    reasonMix[k] = (reasonMix[k] || 0) + 1;
  }

  return {
    base,
    counterfactuals: {
      wider_stop_50pct: widerStop,
      shorter_hold_5d:  shorterHold,
      larger_size_1_5x: largerSize,
    },
    close_reason_mix: reasonMix,
  };
}

const MEMO_SYSTEM_PREAMBLE = `\
You are MasterMindJohn (Opus 4.7, 1M ctx) performing a comprehensive
weekly strategy review. Your output will be persisted verbatim and posted
to Discord #strategy-memos. Your task: review one strategy's LIFETIME
trade history and write a deep, actionable memo.

Deliverable format — ALL three sections MUST be present, separated by
lines of exactly '---':

  <<< markdown memo (posted to #strategy-memos) >>>
  ---
  \`\`\`json
  {
    "lifetime_summary":   { ... },
    "parameter_analysis": { ... },
    "recommendations":    {
      "size_pct_delta":     <number, relative delta in absolute pct, e.g. +0.5 or -0.3>,
      "stop_delta_pct":     <number, relative delta to current stop distance, e.g. +0.02 means widen stop by 2%>,
      "target_delta_pct":   <number>,
      "hold_days_delta":    <integer>,
      "action":             "hold" | "size_up" | "size_down" | "widen_stops" | "tighten_stops" | "shorten_hold" | "lengthen_hold" | "deprecate" | "monitor_only",
      "confidence":         <0.0 - 1.0>,
      "reasoning_one_line": "<tight justification, < 200 chars>"
    }
  }
  \`\`\`

Memo content must:
  * open with a 2-sentence TL;DR (current state + recommended action)
  * summarise lifetime P&L with concrete numbers from the data provided
  * identify the single most costly parameter choice and quantify the
    counterfactual improvement (use the counterfactuals block)
  * reference the 30-day veto histogram when it concentrates on a
    single reason code — that reason is a tuning signal (e.g. many
    negative_kelly vetoes → p_t1 calibration is mismatched to R:R;
    many prefilter_negative_ev → EV computation itself is suspect)
  * recommend specific parameter tuning — reference the
    wider_stop / shorter_hold / larger_size counterfactuals by name
  * end with a 3-bullet "next-week actions" list

No hedge language. Every claim must cite a number from the data.
`;

function buildStrategyPrompt(strategy, tradePack, counterfactuals) {
  return `${MEMO_SYSTEM_PREAMBLE}

Strategy: ${strategy.id} (${strategy.name})
Status: ${strategy.status}
Tier: ${strategy.tier}
Backtest: sharpe=${strategy.backtest_sharpe} ret=${strategy.backtest_return_pct}% dd=${strategy.backtest_max_dd_pct}%
Universe: ${(strategy.universe || []).join(', ')}
Signal frequency: ${strategy.signal_frequency}
Parameters: ${JSON.stringify(strategy.parameters || {})}
Regime conditions: ${JSON.stringify(strategy.regime_conditions || {})}
Approved: ${strategy.approved_at || '(not yet)'}
Created: ${strategy.created_at}

--- LIFETIME TRADE PACK ---

Counterfactuals (pre-computed):
${JSON.stringify(counterfactuals, null, 2)}

Recent execution_signals (up to 600 most recent):
${JSON.stringify(tradePack.signals.slice(0, 120), null, 2)}

Recent signal_pnl rows (up to 1500):
${JSON.stringify(tradePack.pnl.slice(0, 400), null, 2)}

30-day veto histogram (veto_reason → count) for this strategy:
${JSON.stringify(tradePack.vetoHistogram || {}, null, 2)}

Now write the memo and the JSON block, separated by '---'.`;
}

function _splitMemo(text) {
  const parts = text.split(/^---\s*$/m);
  if (parts.length < 2) return { markdown: text, json: null };
  const markdown = parts[0].trim();
  const json = parseJsonBlock(parts.slice(1).join('---'));
  return { markdown, json };
}

async function _postToDiscord(channelName, text) {
  try {
    const notif = require('../../channels/discord/notifications');
    if (typeof notif.post === 'function') {
      await notif.post(channelName, text);
      return true;
    }
  } catch (e) {
    console.error(`[review] Discord post failed (${channelName}): ${e.message}`);
  }
  return false;
}

async function _reviewOne(strategy, { dryRun, notify }) {
  const log = (m) => { notify?.(`${strategy.id}: ${m}`); };
  const tradePack = await _buildTradePack(strategy.id);
  if (!tradePack.signals.length && !tradePack.pnl.length) {
    log('no trades yet — skipping');
    return { strategy_id: strategy.id, skipped: true, reason: 'no_trades' };
  }

  const counterfactuals = _counterfactuals(tradePack.pnl);
  const prompt = buildStrategyPrompt(strategy, tradePack, counterfactuals);
  log(`prompting Opus (signals=${tradePack.signals.length} pnl=${tradePack.pnl.length})`);

  const out = await runOneShot({
    prompt,
    cwd: WORKSPACE,
    disallowedTools: ['Bash','Write','Edit','NotebookEdit','WebSearch','WebFetch','Task'],
    timeoutMs: 480_000,
  });
  if (out.error) {
    log(`Opus error: ${out.error}`);
    return { strategy_id: strategy.id, error: out.error };
  }

  const { markdown, json } = _splitMemo(out.text);
  if (!json) {
    log(`JSON block missing — saving markdown only, flagging`);
  }
  const memo = {
    strategy_id:        strategy.id,
    lifetime_summary:   json?.lifetime_summary || counterfactuals.base || {},
    parameter_analysis: json?.parameter_analysis || {},
    recommendations:    json?.recommendations || {},
    markdown_body:      markdown || out.text,
    cost_usd:           out.costUsd,
  };

  if (dryRun) {
    log(`DRY — would persist memo (cost=$${out.costUsd.toFixed(3)})`);
    return { strategy_id: strategy.id, dry_run: true, memo };
  }

  const { rows } = await _query(
    `INSERT INTO strategy_memos
       (strategy_id, lifetime_summary, parameter_analysis,
        recommendations, markdown_body, cost_usd)
     VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb, $5, $6)
     RETURNING id`,
    [memo.strategy_id, JSON.stringify(memo.lifetime_summary),
     JSON.stringify(memo.parameter_analysis),
     JSON.stringify(memo.recommendations),
     memo.markdown_body, memo.cost_usd],
  );
  const memoId = rows[0].id;
  log(`persisted memo ${memoId.slice(0, 8)} (cost=$${out.costUsd.toFixed(3)})`);

  const header = `# **${strategy.id}** — weekly review (${new Date().toISOString().slice(0, 10)})\n`;
  const footer = `\n\n_cost: $${out.costUsd.toFixed(3)} · memo id \`${memoId.slice(0, 8)}\`_`;
  const posted = await _postToDiscord('strategy-memos', header + markdown + footer);
  if (posted) {
    await _query(`UPDATE strategy_memos SET posted_to_discord = TRUE WHERE id = $1`, [memoId]);
  }

  return { strategy_id: strategy.id, memo_id: memoId, cost_usd: out.costUsd, posted };
}

async function run({ dryRun = false, strategyIds = null, notify = () => {} } = {}) {
  const strategies = await _fetchStrategies(strategyIds);
  notify(`${strategies.length} strategies to review`);
  const results = [];
  let totalCost = 0;
  for (const s of strategies) {
    const r = await _reviewOne(s, { dryRun, notify });
    results.push(r);
    if (r.cost_usd) totalCost += Number(r.cost_usd);
  }
  return {
    strategiesReviewed: results.filter(r => r.memo_id).length,
    strategiesSkipped:  results.filter(r => r.skipped).length,
    errors:             results.filter(r => r.error).length,
    costUsd:            totalCost,
    results,
  };
}

module.exports = { run };
