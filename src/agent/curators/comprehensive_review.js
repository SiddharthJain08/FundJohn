'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

const PYTHON = process.env.PYTHON_BIN || 'python3';

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
const { resolveModel } = require('../config/resolve_model');

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
  const [sigRes, pnlRes, oueRes] = await Promise.all([
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
              realized_pnl_pct, days_held, status, closed_price, closed_at,
              close_reason
         FROM signal_pnl
        WHERE strategy_id = $1
        ORDER BY pnl_date DESC
        LIMIT 1500`,
      [strategyId]
    ),
    // OUE — replaces 2026-05-16 the legacy veto-log histogram. Per closed
    // trade we have one classification: 'over' (realized > GBM expectation
    // by ≥1σ), 'under' (≤-1σ), or 'expected' (within band). Invariant:
    // O + U + E = total closed trades for the strategy. Opus uses this
    // to decide whether the strategy is mis-priced for risk: high
    // over+under ratios suggest the GBM expectation needs tuning;
    // high under ratio alone suggests the bracket geometry needs work.
    _query(
      `SELECT oue_kind, COUNT(*)::int AS n
         FROM execution_signals
        WHERE strategy_id = $1
          AND oue_kind IS NOT NULL
        GROUP BY oue_kind`,
      [strategyId]
    ).catch(() => ({ rows: [] })),
  ]);
  const oue = { over: 0, under: 0, expected: 0 };
  for (const r of oueRes.rows) {
    if (r.oue_kind in oue) oue[r.oue_kind] = r.n;
  }
  oue.total = oue.over + oue.under + oue.expected;
  oue.over_rate     = oue.total > 0 ? oue.over     / oue.total : null;
  oue.under_rate    = oue.total > 0 ? oue.under    / oue.total : null;
  oue.expected_rate = oue.total > 0 ? oue.expected / oue.total : null;
  return { signals: sigRes.rows, pnl: pnlRes.rows, oue };
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
    },
    "regime_recommendations": [
      {
        "regime_state":       "LOW_VOL" | "TRANSITIONING" | "HIGH_VOL" | "CRISIS",
        "eligible":           true | false | null,    // null = no eligibility change recommended
        "size_scalar":        <number 0..2> | null,    // null = no scalar change
        "stop_pct":           <number> | null,
        "target_pct":         <number> | null,
        "max_hold_days":      <integer> | null,
        "confidence":         <0.0 - 1.0>,
        "reasoning_one_line": "<tight justification, < 200 chars>"
      }
      // ... up to 4 entries; emit one per regime where evidence supports a change
      // SKIP entirely (do not emit an entry) for regimes with no actionable evidence
      // The operator approves/rejects each entry via the dashboard
    ]
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

// 2026-05-19: Phase 2F calibration-addenda prepend logic removed.
// Operators no longer interact with MastermindJohn's weekly review prompt —
// the research-page dashboard is the only operator entry into the research
// pipeline (add papers / sources / hand-developed strategies). The
// mastermind_prompt_addenda table stays as a historical record but is
// neither read nor written by this codebase.

// SP-4: mirror of lifecycle.py PROMOTION_THRESHOLDS (keep in sync). Used to
// tell the reviewer the correct per-class promotion floor for this strategy.
const PROMOTION_THRESHOLDS = {
  equity: { min_sharpe: 0.5,  max_drawdown: 0.20 },
  etp:    { min_sharpe: 0.5,  max_drawdown: 0.20 },
  option: { min_sharpe: 0.80, max_drawdown: 0.30 },
  crypto: { min_sharpe: 0.50, max_drawdown: 0.70 },
};

function buildStrategyPrompt(strategy, tradePack, counterfactuals) {
  const ic = strategy.instrument_class || 'equity';
  const thr = PROMOTION_THRESHOLDS[ic] || PROMOTION_THRESHOLDS.equity;
  const classLine = `Instrument class: ${ic} (promotion floor: Sharpe ≥ ${thr.min_sharpe.toFixed(2)}, MaxDD ≤ ${(thr.max_drawdown * 100).toFixed(0)}%)`;
  return `${MEMO_SYSTEM_PREAMBLE}

Strategy: ${strategy.id} (${strategy.name})
Status: ${strategy.status}
Tier: ${strategy.tier}
${classLine}
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

Lifetime OUE (Over / Under / Expected) classification across every closed
trade — invariant O + U + E = total closed. Use this to judge whether
the strategy is mis-priced for risk: a high (over+under) ratio means the
GBM expectation is off; a high under-only ratio means the bracket
geometry (stop / target / max-hold) needs work; a near-zero over rate
with healthy expected says the strategy under-delivers on its upside.
${JSON.stringify(tradePack.oue || {}, null, 2)}

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

  // SP-4: enrich with instrument_class from the manifest (top-level field).
  try {
    const manifestPath = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
    const mf = JSON.parse(require('fs').readFileSync(manifestPath, 'utf-8'));
    strategy.instrument_class = (mf.strategies || {})[strategy.id]?.instrument_class || 'equity';
  } catch (_) { strategy.instrument_class = strategy.instrument_class || 'equity'; }

  if (!tradePack.signals.length && !tradePack.pnl.length) {
    log('no trades yet — skipping');
    return { strategy_id: strategy.id, skipped: true, reason: 'no_trades' };
  }

  const counterfactuals = _counterfactuals(tradePack.pnl);
  const prompt = buildStrategyPrompt(strategy, tradePack, counterfactuals);
  log(`prompting Opus (signals=${tradePack.signals.length} pnl=${tradePack.pnl.length})`);

  const memoModel = resolveModel('mastermind', 'comprehensive-review', 'memo_writer');
  const out = await runOneShot({
    prompt,
    model: memoModel,
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

  // Regime-eligibility drift detection (added 2026-05-12)
  // Re-runs regime_performance_analyzer over last 90d of signal_pnl, compares
  // proposed eligible_regimes to current value in manifest.json. Surfaces drift
  // in memo under regime_eligibility_drift. Does NOT auto-modify manifest —
  // operator runs scripts/update_eligible_regimes.py to apply.
  try {
    // Read current eligible_regimes from manifest.json (top-level field per strategy).
    const manifestPath = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
    const manifest = JSON.parse(require('fs').readFileSync(manifestPath, 'utf-8'));
    const stratRecord = (manifest.strategies || {})[strategy.id] || {};
    const current = stratRecord.eligible_regimes || ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];

    const analyzeResult = spawnSync(PYTHON, [
      '-c',
      `import json, sys, os
sys.path.insert(0, '/root/openclaw/src')
from backtest.regime_performance_analyzer import (
    load_signal_pnl, load_thresholds_from_db, propose_eligible_regimes,
)
uri = os.environ['POSTGRES_URI']
sid = os.environ['STRATEGY_ID']
df = load_signal_pnl(uri, days=90)
thresh = load_thresholds_from_db(uri)
print(json.dumps(propose_eligible_regimes(df, sid, thresh)))`,
    ], {
      encoding: 'utf-8',
      env: {
        ...process.env,
        POSTGRES_URI: process.env.POSTGRES_URI || 'postgresql://openclaw:password@localhost:5432/openclaw',
        STRATEGY_ID: strategy.id,
      },
    });

    if (analyzeResult.status === 0 && analyzeResult.stdout) {
      const proposed = JSON.parse(analyzeResult.stdout.trim() || '[]');
      const added   = proposed.filter(r => !current.includes(r));
      const dropped = current.filter(r => !proposed.includes(r));

      if (added.length || dropped.length) {
        const drift = {
          current,
          proposed,
          added,
          dropped,
          note: 'Operator review required — run scripts/update_eligible_regimes.py to apply.',
        };
        memo.regime_eligibility_drift = drift;
        // Also append to markdown_body so drift surfaces in the Discord post.
        memo.markdown_body +=
          `\n\n---\n## ⚠ Regime-Eligibility Drift Detected\n` +
          `**Current:** ${current.join(', ') || '(none)'}\n` +
          `**Proposed (90d live data):** ${proposed.join(', ') || '(none)'}\n` +
          (added.length   ? `**Added:** ${added.join(', ')}\n`   : '') +
          (dropped.length ? `**Dropped:** ${dropped.join(', ')}\n` : '') +
          `\n_${drift.note}_`;
      }
    } else if (analyzeResult.stderr) {
      console.error(`[comprehensive_review] regime analyzer failed for ${strategy.id}:`, analyzeResult.stderr.slice(0, 200));
    }
  } catch (e) {
    console.error(`[comprehensive_review] regime drift check failed for ${strategy.id}:`, e.message);
  }

  if (dryRun) {
    log(`DRY — would persist memo (cost=$${out.costUsd.toFixed(3)})`);
    return { strategy_id: strategy.id, dry_run: true, memo };
  }

  // metadata column is preserved as an empty object; the addenda-tracking
  // field (addenda_ids_active) is no longer set because calibration addenda
  // were removed 2026-05-19. metadata stays available for future audit
  // fields without schema churn.
  const memoMetadata = {};

  const { rows } = await _query(
    `INSERT INTO strategy_memos
       (strategy_id, lifetime_summary, parameter_analysis,
        recommendations, markdown_body, cost_usd, metadata)
     VALUES ($1, $2::jsonb, $3::jsonb, $4::jsonb, $5, $6, $7::jsonb)
     RETURNING id`,
    [memo.strategy_id, JSON.stringify(memo.lifetime_summary),
     JSON.stringify(memo.parameter_analysis),
     JSON.stringify(memo.recommendations),
     memo.markdown_body, memo.cost_usd,
     JSON.stringify(memoMetadata)],
  );
  const memoId = rows[0].id;
  log(`persisted memo ${memoId.slice(0, 8)} (cost=$${out.costUsd.toFixed(3)})`);

  // Phase 2B: write regime_recommendations to strategy_regime_param_proposals.
  // Each valid entry → one proposal row. Pre-supersedes any still-pending
  // proposal for the same (strategy_id, regime_state). Malformed entries
  // (wrong regime name, non-numeric size_scalar, etc.) are dropped + logged.
  const regimeRecs = Array.isArray(json?.regime_recommendations)
    ? json.regime_recommendations : [];
  let proposalCount = 0;
  if (regimeRecs.length) {
    const REGIMES = new Set(['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']);
    const runId = `mastermind:review-${new Date().toISOString().slice(0, 10)}`;
    for (const rec of regimeRecs) {
      const regime = rec?.regime_state;
      if (!REGIMES.has(regime)) {
        log(`dropped malformed regime_recommendation: ${JSON.stringify(rec).slice(0, 120)}`);
        continue;
      }
      // Defensive: at least one of the proposed fields must be non-null,
      // otherwise the entry is informational only — skip.
      const fields = ['eligible', 'size_scalar', 'stop_pct', 'target_pct', 'max_hold_days'];
      if (fields.every(f => rec[f] === null || rec[f] === undefined)) {
        continue;
      }
      const insertResult = spawnSync(PYTHON, [
        '-c',
        `import json, sys, os
sys.path.insert(0, '/root/openclaw/src')
from strategies.proposal_manager import supersede_pending, insert_proposal
payload = json.loads(os.environ['PAYLOAD'])
supersede_pending(payload['strategy_id'], payload['regime_state'], payload['proposer'])
pid = insert_proposal(**payload)
print(pid)`,
      ], {
        encoding: 'utf-8',
        env: {
          ...process.env,
          POSTGRES_URI: process.env.POSTGRES_URI || 'postgresql://openclaw:password@localhost:5432/openclaw',
          PAYLOAD: JSON.stringify({
            proposer:              runId,
            strategy_id:           strategy.id,
            regime_state:          regime,
            current_row:           null,
            proposed_eligible:     typeof rec.eligible === 'boolean' ? rec.eligible : null,
            proposed_size_scalar:  typeof rec.size_scalar === 'number' ? rec.size_scalar : null,
            proposed_stop_pct:     typeof rec.stop_pct === 'number' ? rec.stop_pct : null,
            proposed_target_pct:   typeof rec.target_pct === 'number' ? rec.target_pct : null,
            proposed_max_hold_days: Number.isInteger(rec.max_hold_days) ? rec.max_hold_days : null,
            confidence:            typeof rec.confidence === 'number' ? rec.confidence : null,
            reasoning:             String(rec.reasoning_one_line || '').slice(0, 500),
            memo_id:               memoId,
          }),
        },
      });
      if (insertResult.status === 0) {
        proposalCount++;
      } else {
        log(`proposal insert failed for ${regime}: ${(insertResult.stderr || '').slice(0, 200)}`);
      }
    }
    if (proposalCount) {
      log(`wrote ${proposalCount} regime proposal(s) for operator approval`);
      memo.markdown_body += `\n\n---\n**${proposalCount} regime parameter proposal(s) awaiting operator approval** — review at /api/regime-proposals or run \`python3 -m strategies.proposal_manager --list\`.`;
      // Update memo body in DB to include the proposal note.
      await _query(
        `UPDATE strategy_memos SET markdown_body = $1 WHERE id = $2`,
        [memo.markdown_body, memoId],
      );
    }
  }

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

module.exports = { run, buildStrategyPrompt };
