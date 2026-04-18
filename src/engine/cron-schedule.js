/**
 * OpenClaw Cron Schedule
 *
 * Background heartbeat jobs — zero LLM tokens.
 * The signal pipeline (memos → research → trade) is event-driven via Discord
 * channel posts, not cron-triggered. See bot.js handleBotMessage().
 *
 * Jobs registered here:
 *   23:59 ET daily          — reset token budget counters in Redis
 *   every 30min off-hours  — check if strategist should activate (DEPLOY mode)
 *                            + resume any budget-paused pipeline
 *   every 30min weekends   — strategist eligible all day
 */

'use strict';

const cron         = require('node-cron');
const { execSync, spawn } = require('child_process');
const path         = require('path');
const { Pool }     = require('pg');
const { getClient: redisClient } = require('../database/redis');

const pool = new Pool({ connectionString: process.env.POSTGRES_URI });

const WORKSPACE_ID     = process.env.WORKSPACE_ID || 'cad1a456-0b65-40ae-8be6-3530e36c53c2';
const REPORT_TRIGGER_N = 30;   // completed trades before first auto-report
const PYTHON           = 'python3';
const ROOT             = path.resolve(__dirname, '..', '..');
const WORKSPACE_DIR    = path.join(ROOT, 'workspaces', 'default');


// ── Helpers ───────────────────────────────────────────────────────────────────

function runPython(script, args = '') {
    return execSync(`${PYTHON} ${script} ${args}`, {
        cwd: ROOT,
        env: { ...process.env },
        stdio: 'pipe',
        timeout: 600_000,
    }).toString();
}

function log(msg) {
    const ts = new Date().toISOString();
    console.log(`[${ts}] [Cron] ${msg}`);
}


// ── 17:30 ET — Signal Pipeline (after 17:00 data collection) ─────────────────

async function runMarketClosePipeline() {
    // Skip if Massive WS already fired this (publishes pipeline:market_close at close)
    try {
        const r = redisClient();
        const wsTriggered = await r.get('massive_ws:pipeline_fired_today');
        if (wsTriggered) {
            log('Market close pipeline already fired by Massive WS — skipping cron duplicate');
            return;
        }
    } catch (_e) { /* Redis unavailable — proceed with cron */ }

    log('Market close pipeline starting (0 LLM tokens — Kelly optimization only)');

    // 1. Market state: HMM, RORO, stress, write regime file
    log('Running market state...');
    try {
        runPython('scripts/run_market_state.py');
        log('Regime file updated');
    } catch (e) {
        log(`ERROR: market-state failed — ${e.message.slice(0, 200)}`);
        return;
    }

    // 2. Build signals cache from master dataset
    log('Building signals cache...');
    try {
        runPython(`${WORKSPACE_DIR}/tools/signals_cache.py`, '--build');
        log('Signals cache ready');
    } catch (e) {
        log(`ERROR: cache build failed — ${e.message.slice(0, 200)}`);
        return;
    }

    // 3. Run all hardcoded strategies (zero LLM tokens)
    log('Executing strategy signal runner...');
    try {
        const output = runPython(`${WORKSPACE_DIR}/tools/signal_runner.py`);
        log(`Signal runner complete: ${output.slice(0, 200)}`);
    } catch (e) {
        log(`ERROR: signal runner failed — ${e.message.slice(0, 200)}`);
        return;
    }

    // 4. Run execution engine → write signals to DB → post memos to Discord
    log('Running post_memos (engine + Discord memos)...');
    try {
        const memosOut = runPython('src/execution/post_memos.py');
        log(`post_memos complete: ${memosOut.slice(0, 200)}`);
    } catch (e) {
        log(`ERROR: post_memos failed — ${e.message.slice(0, 200)}`);
        // Non-fatal: continue to trade_agent so Alpaca execution isn't blocked
    }

    // 5. Kelly optimization + submit GREEN signals to Alpaca paper trading
    // Green = kelly_net > 0.5% (MIN_KELLY). Red signals are vetoed and logged.
    log('Running trade_agent (Kelly optimization + Alpaca paper orders)...');
    try {
        const tradeOut = runPython('src/execution/trade_agent.py');
        log(`trade_agent complete: ${tradeOut.slice(0, 200)}`);
    } catch (e) {
        log(`ERROR: trade_agent failed — ${e.message.slice(0, 200)}`);
    }

    // 6. Check report triggers
    await checkReportTriggers();

    log('Market close pipeline complete (post_memos + green-only Alpaca orders submitted)');

    // Notify dashboard clients that fresh data is available
    try {
        const http = require('http');
        const port = parseInt(process.env.DASHBOARD_PORT) || 3000;
        const req  = http.request({ hostname: 'localhost', port, path: '/api/events/data-updated', method: 'POST',
                                    headers: { 'Content-Type': 'application/json', 'Content-Length': '0' } });
        req.on('error', () => {});
        req.end();
    } catch (_) {}
}


// ── Report Trigger Check ──────────────────────────────────────────────────────

async function checkReportTriggers() {
    try {
        const result = await pool.query(`
            SELECT
                sp.strategy_id,
                COUNT(*) FILTER (WHERE sp.pnl_pct IS NOT NULL) AS completed,
                COUNT(*) FILTER (WHERE sp.pnl_pct IS NOT NULL AND NOT sp.reported) AS unreported
            FROM signal_performance sp
            WHERE sp.workspace_id = $1
            GROUP BY sp.strategy_id
            HAVING COUNT(*) FILTER (WHERE sp.pnl_pct IS NOT NULL AND NOT sp.reported) >= $2
        `, [WORKSPACE_ID, REPORT_TRIGGER_N]);

        const r = redisClient();
        for (const row of result.rows) {
            log(`Report trigger: ${row.strategy_id} has ${row.unreported} unreported completed trades`);
            await r.rpush(
                `queue:report:${WORKSPACE_ID}`,
                JSON.stringify({
                    strategy_id: row.strategy_id,
                    completed:   parseInt(row.completed),
                    unreported:  parseInt(row.unreported),
                    mode:        'REPORT',
                    queued_at:   new Date().toISOString(),
                })
            );
            log(`Queued REPORT for ${row.strategy_id}`);
        }
    } catch (e) {
        log(`ERROR: report trigger check failed — ${e.message}`);
    }
}


// ── Midnight — Reset Token Budget Counters ────────────────────────────────────

async function resetTokenBudgets() {
    log('Resetting daily token budget counters');
    const today   = new Date().toISOString().slice(0, 10);
    const r       = redisClient();
    const pattern = `token_usage:${WORKSPACE_ID}:*`;
    const keys    = await r.keys(pattern);

    let deleted = 0;
    for (const key of keys) {
        if (!key.endsWith(today)) {
            await r.del(key);
            deleted++;
        }
    }
    log(`Token budget reset: ${deleted} stale keys cleared`);
}


// ── Report Queue Processor ────────────────────────────────────────────────────

async function processReportQueue(swarm, generateId) {
    const r        = redisClient();
    const queueKey = `queue:report:${WORKSPACE_ID}`;
    const items    = [];

    let raw;
    while ((raw = await r.lpop(queueKey))) {
        items.push(JSON.parse(raw));
    }

    if (items.length === 0) return;
    log(`Processing ${items.length} REPORT invocations`);

    for (const item of items) {
        await swarm.init({
            type:      'report-builder',
            mode:      'STRATEGY_PERFORMANCE',
            workspace: WORKSPACE_DIR,
            threadId:  generateId(),
            prompt:    `Generate strategy performance report for ${item.strategy_id}. ${item.unreported} completed trades to analyze.`,
        });
    }
}


// ── Pipeline Resume Check ──────────────────────────────────────────────────────

/**
 * Check if a paused pipeline needs to resume.
 * Called every 30 min during off-hours when budget may have recovered.
 */
async function checkPipelineResume() {
    const r = redisClient();
    try {
        const raw = await r.get('pipeline:resume_checkpoint');
        if (!raw) return;

        const checkpoint = JSON.parse(raw);
        const runDate    = checkpoint.run_date;
        if (!runDate) return;

        // Check if pipeline lock still active (already running)
        const locked = await r.get(`pipeline:running:${runDate}`);
        if (locked) return;

        // Check budget is OK before resuming
        const mode = await r.get('budget:mode') || 'GREEN';
        if (mode === 'RED') {
            log(`Pipeline resume deferred — budget still RED`);
            return;
        }

        log(`Budget recovered (${mode}) — resuming pipeline for ${runDate}`);

        const orchestrator = path.join(ROOT, 'src', 'execution', 'pipeline_orchestrator.py');
        const proc = spawn('python3', [orchestrator, '--date', runDate, '--force-resume'], {
            cwd:      ROOT,
            env:      { ...process.env, PYTHONPATH: ROOT },
            detached: true,
            stdio:    'ignore',
        });
        proc.unref();
        log(`Pipeline orchestrator resumed (pid ${proc.pid}) for ${runDate}`);
    } catch (e) {
        log(`Pipeline resume check error: ${e.message}`);
    }
}


// ── Register Cron Jobs ─────────────────────────────────────────────────────────

function start(swarm, generateId, notifyDiscord) {

    // NOTE: Signal pipeline (post_memos → research_report → trade_agent) is no longer
    // cron-triggered. It fires event-driven from Discord channel posts:
    //   DataBot #strategy-memos → ResearchDesk activates → TradeDesk activates
    // See bot.js handleBotMessage() + runResearchPipeline() + runTradePipeline().

    // 23:59 ET daily — reset token budget
    cron.schedule('59 23 * * *', resetTokenBudgets, { timezone: 'America/New_York' });





    // Sunday 08:00 ET — weekly memory synthesis
    // BotJohn reads all memory/*.md files and writes consolidated learnings to agent.md
    // and promotes high-value patterns to CLAUDE.md. Zero user interaction required.
    cron.schedule('0 8 * * 0', async () => {
        log('Weekly memory synthesis starting — consolidating agent learnings');
        await swarm.init({
            type:      'strategist',
            mode:      'REPORT',
            workspace: WORKSPACE_DIR,
            threadId:  generateId(),
            prompt: (
                `MEMORY SYNTHESIS SESSION. Read the entire workspace/memory/ directory ` +
                `(signal_patterns.md, trade_learnings.md, regime_context.md, fund_journal.md). ` +
                `Extract durable patterns — not one-off observations. ` +
                `Write a "## Lessons Learned (auto-synthesized YYYY-MM-DD)" section to agent.md. ` +
                `Promote the 3-5 most important cross-run patterns to the top of signal_patterns.md ` +
                `and trade_learnings.md under a "## Key Patterns" header. ` +
                `Be ruthlessly terse — only write what will change how a future agent acts. ` +
                `Log to /root/.learnings/LEARNINGS.md with area tag 'memory-synthesis'.`
            ),
        }).catch((e) => log(`Memory synthesis error: ${e.message}`));

        // Weekly reaper: detect orphaned data columns → data_deprecation_queue
        try {
            const ResearchOrchestrator = require('../agent/research/research-orchestrator');
            const orch = new ResearchOrchestrator();
            await orch.runReaperPass((msg) => log(`[reaper] ${msg}`));
        } catch (e) {
            log(`Reaper pass error: ${e.message}`);
        }

        // Refresh data_ledger materialized view
        await pool.query('REFRESH MATERIALIZED VIEW CONCURRENTLY data_ledger').catch((e) =>
            log(`data_ledger refresh error: ${e.message}`)
        );

        // Regenerate strategy_signatures.json
        try {
            const { execSync } = require('child_process');
            execSync(`python3 ${ROOT}/src/strategies/generate_signatures.py`, { cwd: ROOT, timeout: 30_000 });
            log('strategy_signatures.json regenerated');
        } catch (e) {
            log(`strategy_signatures regeneration error: ${e.message}`);
        }

        // Sync full ticker universe from FMP + Polygon into universe_config
        log('Universe sync starting (FMP + Polygon)...');
        try {
            const syncOut = runPython('src/ingestion/run_universe_sync.py');
            log(`Universe sync complete: ${syncOut.slice(0, 200)}`);
        } catch (e) {
            log(`Universe sync error: ${e.message.slice(0, 200)}`);
        }
    }, { timezone: 'America/New_York' });

    // 4:20 PM ET Mon-Fri: run full market-close pipeline (market state + signals cache + signal_runner.py)
  cron.schedule('20 16 * * 1-5', runMarketClosePipeline, { timezone: 'America/New_York' });

  log('Cron schedule registered. Zero-token pipeline active.');
    log('Agents will only activate for DEPLOY, REPORT, and weekly synthesis tasks.');
}

module.exports = { start, processReportQueue, resetTokenBudgets };
