/**
 * OpenClaw Cron Schedule
 *
 * Background heartbeat jobs — zero LLM tokens.
 * The signal pipeline (memos → research → trade) is event-driven via Discord
 * channel posts, not cron-triggered. See bot.js handleBotMessage().
 *
 * Jobs registered here:
 *   23:59 ET daily          — reset token budget counters in Redis
 *   resume any budget-paused pipeline on schedule
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

    // 10:00 AM ET Mon–Fri — the new daily cycle (Phase 2 of the pipeline
    // restructure). Spawns pipeline_orchestrator.py which runs queue_drain →
    // collect → signals → handoff → trade → alpaca → report.
    // Orchestrator is idempotent; duplicate triggers return immediately.
    cron.schedule('0 10 * * 1-5', () => {
        log('10am cycle: spawning pipeline_orchestrator.py');
        try {
            const { spawn } = require('child_process');
            const fs = require('fs');
            const path = require('path');
            const today = new Date().toISOString().slice(0, 10);
            const logDir = path.join(ROOT, 'logs');
            try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
            const logPath = path.join(logDir, `pipeline_orchestrator_${today}.log`);
            const logFd = fs.openSync(logPath, 'a');
            const child = spawn(PYTHON, ['scripts/run_pipeline.py', '--date', today], {
                cwd: ROOT,
                env: { ...process.env },
                detached: true,
                stdio: ['ignore', logFd, logFd],
            });
            child.unref();
            log(`10am cycle: orchestrator spawned (pid ${child.pid}) for ${today} → ${logPath}`);
        } catch (e) {
            log(`10am cycle spawn error: ${e.message}`);
        }
    }, { timezone: 'America/New_York' });

    // 9:00 AM ET Mon–Fri: fresh regime at market open (run_market_state.py only — no signals/Alpaca)
    cron.schedule('0 9 * * 1-5', async () => {
        log('Morning regime refresh starting');
        try {
            runPython('scripts/run_market_state.py');
            log('Morning regime file updated');
        } catch (e) {
            log(`ERROR: morning market-state failed — ${e.message.slice(0, 200)}`);
            return;
        }
        try {
            const http = require('http');
            const port = parseInt(process.env.DASHBOARD_PORT) || 3000;
            const req  = http.request({ hostname: 'localhost', port,
                path: '/api/events/data-updated', method: 'POST',
                headers: { 'Content-Type': 'application/json', 'Content-Length': '0' } });
            req.on('error', () => {});
            req.end();
        } catch (_) {}
        log('Morning regime refresh complete');
    }, { timezone: 'America/New_York' });


    // Sunday 08:00 ET — weekly memory synthesis + reaper + universe sync
    cron.schedule('0 8 * * 0', async () => {
        log('Weekly maintenance starting — reaper, signatures, universe sync');

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

        // NOTE (2026-04-23): strategist-ideator subagent deleted as an artifact.
        // arXiv discovery + Saturday Mastermind corpus mode now fully own idea intake.

        // arXiv discovery: harvest recent q-fin papers
        log('arXiv discovery starting...');
        try {
            const arxivOut = runPython('src/ingestion/arxiv_discovery.py');
            log(`arXiv discovery complete: ${arxivOut.slice(0, 200)}`);
        } catch (e) {
            log(`arXiv discovery error: ${e.message.slice(0, 200)}`);
        }

        // Sunday backtest sweep: re-run gate on stale pending/failed strategies
        log('Sunday backtest sweep starting...');
        try {
            const { Pool: SweepPool } = require('pg');
            const sweepPool = new SweepPool({ connectionString: process.env.POSTGRES_URI });
            const { rows: stale } = await sweepPool.query(
                `SELECT iq.candidate_id, iq.strategy_spec
                 FROM implementation_queue iq
                 WHERE iq.status IN ('backtest_failed', 'pending_approval', 'done')
                   AND iq.queued_at < NOW() - INTERVAL '7 days'
                 LIMIT 10`
            );
            await sweepPool.end();

            if (stale.length === 0) {
                log('Sunday sweep: no stale strategies to re-check.');
            } else {
                log(`Sunday sweep: re-checking ${stale.length} stale strategy(ies)...`);
                const ResearchOrchestrator = require('../agent/research/research-orchestrator');
                const orch = new ResearchOrchestrator();
                for (const row of stale) {
                    const spec = typeof row.strategy_spec === 'string'
                        ? JSON.parse(row.strategy_spec)
                        : row.strategy_spec;
                    await orch._codeFromQueue(
                        { candidate_id: row.candidate_id, strategy_spec: spec },
                        (msg) => log(`[sweep] ${msg}`),
                        null
                    ).catch((e) => log(`[sweep] error for ${spec?.strategy_id}: ${e.message}`));
                }
            }
        } catch (e) {
            log(`Sunday sweep error: ${e.message}`);
        }
    }, { timezone: 'America/New_York' });

  // 8:15 AM ET Mon-Fri: daily health digest to Discord so regressions surface in one glance
  try {
    const digest = require('./daily-health-digest');
    digest.register(cron, (text) => notifyDiscord(text));
    log('Daily health digest registered (Mon–Fri 08:15 ET)');
  } catch (err) {
    log(`Health digest register failed: ${err.message}`);
  }

  // 3:05 AM ET daily: snapshot curator priors → time series for trend-aware calibration
  cron.schedule('5 3 * * *', async () => {
    try {
      const { snapshotAll } = require('../agent/curators/snapshot_priors');
      const result = await snapshotAll();
      log(`Curator priors snapshot: ${result.total} rows`);
    } catch (err) {
      log(`Curator priors snapshot failed: ${err.message}`);
    }
  }, { timezone: 'America/New_York' });

  // Sunday 09:00 ET — optimizer-john weekly self-tune-up (after the
  // 08:00 maintenance block so universe_sync + data_ledger refresh +
  // strategy_signatures have completed). Writes patch proposals to
  // workspaces/default/optimizer/queue/ and posts a memo to #ops.
  // Patches NEVER auto-apply — operator runs !john /optimizer apply.
  cron.schedule('0 9 * * 0', async () => {
    try {
      const optimizer = require('../agent/optimizers/optimizer_john');
      const res = await optimizer.run({ notify: (m) => log(`[optimizer] ${m}`) });
      log(`optimizer-john: ${res.patchesQueued?.length || 0} queued, ${res.patchesRejected?.length || 0} rejected, cost $${(res.cost || 0).toFixed(2)}`);
    } catch (err) {
      log(`optimizer-john failed: ${err.message}`);
    }
  }, { timezone: 'America/New_York' });

  log('Cron schedule registered. Zero-token pipeline active.');
    log('Agents will only activate for DEPLOY, REPORT, and weekly synthesis tasks.');
}

module.exports = { start, processReportQueue, resetTokenBudgets };
