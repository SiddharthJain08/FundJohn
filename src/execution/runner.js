/**
 * Execution Engine Runner
 * Spawns engine.py as a child process, captures output, and posts results to Discord.
 *
 * Called by:
 *   - workflow.js runDailyClose()
 *   - Discord relay /engine-run command
 *
 * Post-run flow:
 *   1. engine.py runs → signals written to DB
 *   2. buildStrategyMemo() queries signals → DataBot posts to #strategy-memos
 *   3. buildSignalSynthesis() analyses signals → ResearchDesk posts to #research-feed
 */

'use strict';

const { spawn }       = require('child_process');
const path            = require('path');
const { Pool }        = require('pg');
const { getClient }   = require('../database/redis');
const notifications   = require('../channels/discord/notifications');

const ROOT   = path.resolve(__dirname, '..', '..');
const ENGINE = path.join(ROOT, 'src', 'execution', 'engine.py');
const pool   = new Pool({ connectionString: process.env.POSTGRES_URI });

/**
 * Run the execution engine for a given workspaceId.
 * Returns the parsed JSON output from engine.py.
 */
async function runEngine(workspaceId = 'default') {
    return new Promise((resolve, reject) => {
        const env = {
            ...process.env,
            WORKSPACE_ID: workspaceId,
            POSTGRES_URI: process.env.POSTGRES_URI,
            PYTHONPATH:   ROOT,
        };

        const proc = spawn('python3', [ENGINE], {
            cwd:   ROOT,
            env,
            stdio: ['ignore', 'pipe', 'pipe'],
        });

        let stdout = '';
        let stderr = '';

        proc.stdout.on('data', d => { stdout += d.toString(); });
        proc.stderr.on('data', d => { stderr += d.toString(); });

        proc.on('close', code => {
            if (code !== 0) {
                const err = stderr.slice(0, 500) || stdout.slice(0, 500) || '(no output)';
                return reject(new Error(`engine.py exited ${code}: ${err}`));
            }

            // engine.py prints JSON on last line
            const lines  = stdout.trim().split('\n');
            const last   = lines[lines.length - 1];
            try {
                resolve(JSON.parse(last));
            } catch (e) {
                resolve({ status: 'ok', raw: last });
            }
        });

        proc.on('error', reject);
    });
}

/**
 * Full daily close run: engine → notify Discord → update Redis status key.
 */
async function runDailyClose(workspaceId = 'default') {
    const redis = getClient();
    const key   = `engine:last_run:${workspaceId}`;

    try {
        await redis.set(key, JSON.stringify({ status: 'running', started_at: new Date().toISOString() }));

        const result = await runEngine(workspaceId);

        await redis.set(key, JSON.stringify({ ...result, completed_at: new Date().toISOString() }), 'EX', 86400);

        // Always post compact engine summary to #trade-signals
        const msg = formatEngineReport(result);
        if (result.signals_generated > 0 || result.report_triggers > 0) {
            await notifications.notifyEngineSignals(msg).catch(() => {});
        } else {
            await notifications.notifyStrategistStatus(msg).catch(() => {});
        }

        // If signals exist: post detailed strategy memo → then trigger research synthesis
        const runDate = result.run_date || new Date().toISOString().slice(0, 10);
        const signals = await queryTodaySignals(workspaceId, runDate).catch(() => []);
        if (signals.length > 0) {
            const memo = buildStrategyMemo(result, signals, runDate);
            await notifications.notifyStrategyMemo(memo).catch(() => {});

            const synthesis = buildSignalSynthesis(result, signals, runDate);
            await notifications.notifySignalSynthesis(synthesis).catch(() => {});
        }

        return result;
    } catch (err) {
        const errPayload = { status: 'error', error: err.message, ts: new Date().toISOString() };
        await redis.set(key, JSON.stringify(errPayload), 'EX', 3600).catch(() => {});
        throw err;
    }
}

/**
 * Get status of the last engine run from Redis.
 */
async function getLastRunStatus(workspaceId = 'default') {
    const redis = getClient();
    const raw   = await redis.get(`engine:last_run:${workspaceId}`);
    return raw ? JSON.parse(raw) : null;
}

function formatEngineReport(result) {
    const lines = [
        `**Execution Engine — ${result.run_date || 'today'}**`,
        `Regime: \`${result.regime || 'unknown'}\``,
        `Strategies run: ${result.strategies_run}`,
        `Signals generated: ${result.signals_generated}`,
        `Confluence signals: ${result.confluence_count}`,
        `P&L updates: ${result.pnl_updates}`,
        `Report triggers: ${result.report_triggers}`,
        `Duration: ${result.duration_s}s`,
    ];
    return lines.join('\n');
}

/**
 * Query today's execution signals from DB.
 */
async function queryTodaySignals(workspaceId, runDate) {
    const res = await pool.query(
        `SELECT strategy_id, ticker, direction, entry_price, stop_loss,
                target_1, target_2, position_size_pct, regime_state, signal_params
         FROM execution_signals
         WHERE workspace_id = $1 AND signal_date = $2
         ORDER BY strategy_id, ticker`,
        [workspaceId, runDate]
    );
    return res.rows;
}

/**
 * Build the full strategy execution memo posted by DataBot to #strategy-memos.
 * Detailed per-signal table with risk metrics.
 */
function buildStrategyMemo(result, signals, runDate) {
    const byStrategy = {};
    for (const s of signals) {
        if (!byStrategy[s.strategy_id]) byStrategy[s.strategy_id] = [];
        byStrategy[s.strategy_id].push(s);
    }

    const strategyLabels = {
        'S9_dual_momentum':          'S9 — Antonacci Dual Momentum',
        'S_custom_jt_momentum_12mo': 'JT — 12-Month Cross-Sectional Momentum',
        'S10_quality_value':         'S10 — Quality/Value',
        'S12_insider':               'S12 — Insider Cluster Buy',
        'S15_iv_rv_arb':             'S15 — IV/RV Arbitrage',
    };

    const lines = [
        `📡 **Strategy Execution Memo — ${runDate}**`,
        `Regime: **${result.regime || 'UNKNOWN'}** | Strategies: ${result.strategies_run} | Signals: ${signals.length} | Confluence: ${result.confluence_count}`,
        '',
    ];

    for (const [stratId, sigs] of Object.entries(byStrategy)) {
        const label = strategyLabels[stratId] || stratId;
        lines.push(`**${label}** (${sigs.length} signals)`);
        lines.push('```');
        lines.push('Ticker  Entry      Stop     Risk       T1    R:R   Size    12mo');
        lines.push('─'.repeat(62));
        for (const s of sigs) {
            const params = typeof s.signal_params === 'object' ? s.signal_params : JSON.parse(s.signal_params || '{}');
            const mom    = params.lookback_ret ?? params.momentum_12mo ?? 0;
            const risk   = ((s.entry_price - s.stop_loss) / s.entry_price * 100).toFixed(1);
            const rr     = ((s.target_1 - s.entry_price) / Math.max(s.entry_price - s.stop_loss, 0.01)).toFixed(1);
            const sz     = (s.position_size_pct * 100).toFixed(2);
            const momStr = (mom >= 0 ? '+' : '') + (mom * 100).toFixed(0) + '%';
            lines.push(
                `${s.ticker.padEnd(6)}  ${('$'+s.entry_price.toFixed(2)).padStart(8)}  ${('$'+s.stop_loss.toFixed(2)).padStart(8)}  ${(risk+'%').padStart(5)}  ${('$'+s.target_1.toFixed(2)).padStart(8)}  ${(rr+'x').padStart(4)}  ${(sz+'%').padStart(5)}  ${momStr.padStart(6)}`
            );
        }
        lines.push('```');
        lines.push('');
    }

    lines.push(`**Position sizing:** HIGH_VOL scale=0.35 | Gross exposure: ~${(signals.reduce((a, s) => a + s.position_size_pct, 0) * 100).toFixed(1)}%`);
    return lines.join('\n');
}

/**
 * Build the ResearchDesk signal synthesis posted to #research-feed.
 * Highlights top picks, flags risks, notes regime context.
 */
function buildSignalSynthesis(result, signals, runDate) {
    const regime = result.regime || 'UNKNOWN';

    // Score each signal: momentum strength × R:R × size
    const scored = signals.map(s => {
        const params = typeof s.signal_params === 'object' ? s.signal_params : JSON.parse(s.signal_params || '{}');
        const mom    = Math.abs(params.lookback_ret ?? params.momentum_12mo ?? 0);
        const rank   = params.momentum_rank ?? 0.5;
        const rr     = (s.target_1 - s.entry_price) / Math.max(s.entry_price - s.stop_loss, 0.01);
        const risk   = (s.entry_price - s.stop_loss) / s.entry_price;
        const score  = mom * rr * s.position_size_pct * (1 + rank);
        return { ...s, _mom: mom, _rank: rank, _rr: rr, _risk: risk, _score: score, _params: params };
    }).sort((a, b) => b._score - a._score);

    // Detect cross-strategy confluence
    const tickerStrategies = {};
    for (const s of signals) {
        if (!tickerStrategies[s.ticker]) tickerStrategies[s.ticker] = [];
        tickerStrategies[s.ticker].push(s.strategy_id);
    }
    const confluent = Object.entries(tickerStrategies)
        .filter(([, strats]) => strats.length >= 2)
        .map(([ticker, strats]) => ({ ticker, strategies: strats }));

    const lines = [
        `🔬 **ResearchDesk Signal Synthesis — ${runDate}**`,
        `Reading ${signals.length} signals from #strategy-memos | Regime: **${regime}**`,
        '',
    ];

    // Top picks (top 3 by composite score)
    lines.push('**Top Picks by Conviction**');
    lines.push('```');
    for (const s of scored.slice(0, 3)) {
        const risk = (s._risk * 100).toFixed(1);
        const rr   = s._rr.toFixed(1);
        const mom  = ((s._mom) * 100).toFixed(0);
        lines.push(`${s.ticker.padEnd(5)} ${s.strategy_id.padEnd(28)} 12mo +${mom}%  R:R ${rr}x  risk ${risk}%  sz ${(s.position_size_pct*100).toFixed(2)}%`);
    }
    lines.push('```');
    lines.push('');

    // Confluence
    if (confluent.length > 0) {
        lines.push('**Cross-Strategy Confluence** *(≥2 strategies agree)*');
        for (const { ticker, strategies } of confluent) {
            lines.push(`• **${ticker}** — ${strategies.join(' + ')}`);
        }
        lines.push('');
    }

    // Regime context
    const regimeNotes = {
        HIGH_VOL:     `⚠️ HIGH_VOL regime: position scale=0.35 (35% of normal). Stops are wider — monitor daily.`,
        TRANSITIONING: `⚡ TRANSITIONING regime: volatility expanding. Tighten stops if breaks fail.`,
        LOW_VOL:      `✅ LOW_VOL regime: full position scale=1.0. Trend-following strategies have edge.`,
    };
    lines.push(`**Regime Context:** ${regimeNotes[regime] || regime}`);
    lines.push('');

    // Flag any wide-stop signals (risk > 8%)
    const wideStop = scored.filter(s => s._risk > 0.08);
    if (wideStop.length > 0) {
        lines.push(`**Wide Stop Flags** *(risk >8% — consider half-size or skip)*`);
        lines.push(wideStop.map(s => `• ${s.ticker} — ${(s._risk*100).toFixed(1)}% stop distance`).join('\n'));
        lines.push('');
    }

    // Sizing summary by strategy
    lines.push('**Sizing by Strategy**');
    const byStrat = {};
    for (const s of signals) {
        if (!byStrat[s.strategy_id]) byStrat[s.strategy_id] = { count: 0, total: 0 };
        byStrat[s.strategy_id].count++;
        byStrat[s.strategy_id].total += s.position_size_pct;
    }
    for (const [sid, { count, total }] of Object.entries(byStrat)) {
        lines.push(`• ${sid}: ${count} positions × ${(total/count*100).toFixed(2)}% avg = **${(total*100).toFixed(2)}% total**`);
    }

    return lines.join('\n');
}

module.exports = {
    runEngine,
    runDailyClose,
    getLastRunStatus,
    formatEngineReport,
    buildStrategyMemo,
    buildSignalSynthesis,
};
