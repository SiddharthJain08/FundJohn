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

const _https = require('https');

// Bubble subprocess [WARN]/[ERROR]/[FAIL]/FATAL lines to #pipeline-feed so
// silent stdout failures don't go unnoticed. The 2026-04-28 LOW_VOL miss
// happened because run_market_state.py's `[WARN] DB write failed` line
// went to stdout, was captured by execSync, and then discarded by the
// caller — nothing surfaced for 7 days. This rescues the next instance.
const _ALERT_PATTERN = /^\s*(\[(WARN|ERROR|FAIL)\]|FATAL:)/i;
let   _alertWebhook = null;
async function _resolveAlertWebhook() {
    if (_alertWebhook !== null) return _alertWebhook;
    try {
        const r = await pool.query(
            "SELECT webhook_urls FROM agent_registry WHERE id='botjohn' LIMIT 1"
        );
        const urls = r.rows[0]?.webhook_urls || {};
        _alertWebhook = urls['pipeline-feed'] || urls['botjohn-log'] || '';
    } catch (_) { _alertWebhook = ''; }
    return _alertWebhook;
}
function _postAlert(content) {
    _resolveAlertWebhook().then((url) => {
        if (!url) return;
        const u = new URL(url);
        const body = JSON.stringify({ content: content.slice(0, 1900) });
        const req = _https.request({
            hostname: u.hostname, path: u.pathname + u.search, method: 'POST',
            headers: {
                'Content-Type':   'application/json',
                'Content-Length': Buffer.byteLength(body),
            },
        }, (res) => { res.on('data', () => {}); res.on('end', () => {}); });
        req.on('error', () => {});
        req.write(body); req.end();
    }).catch(() => {});
}

function _scanAlerts(scriptName, output) {
    if (!output) return;
    const lines = output.split('\n').filter(l => _ALERT_PATTERN.test(l));
    if (!lines.length) return;
    const head = lines.slice(0, 6).map(l => `  ${l.trim()}`).join('\n');
    const more = lines.length > 6 ? `\n  …+${lines.length - 6} more` : '';
    _postAlert(`⚠️ **cron[${scriptName}]** captured ${lines.length} alert line(s):\n${head}${more}`);
}

function runPython(script, args = '') {
    let stdout = '';
    let exitCode = 0;
    try {
        stdout = execSync(`${PYTHON} ${script} ${args}`, {
            cwd: ROOT,
            env: { ...process.env },
            stdio: 'pipe',
            timeout: 600_000,
        }).toString();
    } catch (err) {
        exitCode = err.status || -1;
        stdout = (err.stdout && err.stdout.toString()) || '';
        const stderr = (err.stderr && err.stderr.toString()) || '';
        // Surface the failure even if the script never reached its
        // own `[WARN]` print. exit-code discipline (0/1/2) is repo-wide
        // per Tier 3.
        const tag = exitCode === 2 ? 'AUTH/CONFIG' : exitCode === 1 ? 'TRANSIENT' : 'EXCEPTION';
        _postAlert(`🚨 **cron[${script}] exit ${exitCode} (${tag})**\n` +
                    '```\n' + (stderr.slice(-500) || stdout.slice(-500) || err.message).slice(0, 1700) + '\n```');
        throw err;
    }
    _scanAlerts(script, stdout);
    return stdout;
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

    // Close-execution rollout gate (default OFF). When ON, the daily cycle
    // computes at ~3:10pm ET (close[t]-proxy signals) and executes market orders
    // into the ~3:55pm close to mirror the backtests' close[t] fill, plus a
    // 9:35am dashboard-only SOD refresh. When OFF, the legacy 10:00am cycle runs
    // unchanged. Spec: docs/archive/superpowers/specs/2026-05-27-open-execution-timing-and-action-label-design.md
    const closeExecLive       = process.env.OPENCLAW_CLOSE_EXEC_LIVE       === '1';
    const eodSignalRegister   = process.env.OPENCLAW_EOD_SIGNAL_REGISTER   === '1';
    // 2026-07-29 same-day pivot: signal[t] -> submission[t]. 15:00 ET compute
    // (close[t]-proxy snapshot; no full collect in the critical path) ->
    // 15:55 ET execute into the close (mirrors the backtests' same_close
    // fill) -> 16:15 ET official EOD collect. The premarket protective flow
    // (9:15 gate / 9:25 reconcile / 9:32 sweep) is shared with the EOD mode.
    const sameDayExec         = process.env.OPENCLAW_SAMEDAY_EXEC          === '1';

    // Mutual exclusion: exactly one timing mode may drive routed strategies.
    const _modes = [eodSignalRegister, closeExecLive, sameDayExec].filter(Boolean);
    if (_modes.length > 1) {
        throw new Error(
            'OPENCLAW_EOD_SIGNAL_REGISTER, OPENCLAW_CLOSE_EXEC_LIVE and ' +
            'OPENCLAW_SAMEDAY_EXEC are mutually exclusive — they drive routed ' +
            'strategies on conflicting timing models. Enable exactly one.'
        );
    }

    // 10:00 AM ET Mon–Fri — legacy daily cycle (registered only when NEITHER
    // close-exec NOR EOD-flow is ON). EOD-flow supersedes the legacy same-day
    // cycle (§4: entire live cycle re-timed to act-next-day).
    if (!closeExecLive && !eodSignalRegister && !sameDayExec) cron.schedule('0 10 * * 1-5', () => {
        const useLangGraph = process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR === '1';
        const today = new Date().toISOString().slice(0, 10);

        if (useLangGraph) {
            log('10am cycle: dispatching to LangGraph daily-cycle');
            try {
                const { runDailyCycleGraph } = require('../agent/graphs/daily-cycle');
                runDailyCycleGraph({ runDate: today, reason: 'scheduled' })
                    .then((out) => log(`daily-cycle finished: status=${out.status} aborted=${out.abortedAt || 'none'}`))
                    .catch((err) => log(`daily-cycle FAILED: ${err.message}`));
            } catch (e) {
                log(`daily-cycle dispatch error: ${e.message}`);
            }
            return;
        }

        // Legacy path — Python orchestrator (unchanged from before E1)
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

    // Close-execution crons (registered only when close-exec is ON): compute → execute → SOD.
    if (closeExecLive) {
        const dispatchCycle = (reason, steps) => {
            const today = new Date().toISOString().slice(0, 10);
            try {
                const { runDailyCycleGraph } = require('../agent/graphs/daily-cycle');
                runDailyCycleGraph({ runDate: today, reason, requestedSteps: steps })
                    .then((out) => log(`${reason} finished: status=${out.status} aborted=${out.abortedAt || 'none'}`))
                    .catch((err) => log(`${reason} FAILED: ${err.message}`));
            } catch (e) {
                log(`${reason} dispatch error: ${e.message}`);
            }
        };
        // 3:10 PM ET — compute phase (close[t]-proxy signals; produces sized handoff; no execution).
        cron.schedule('10 15 * * 1-5', () => {
            log('close-exec compute (3:10pm ET)');
            dispatchCycle('scheduled-compute', ['collect', 'sentiment', 'signals', 'ic_gate', 'handoff', 'trade']);
        }, { timezone: 'America/New_York' });
        // 3:55 PM ET — execute phase (market orders into the close).
        cron.schedule('55 15 * * 1-5', () => {
            log('close-exec execute (3:55pm ET)');
            dispatchCycle('scheduled-execute', ['alpaca', 'reconcile', 'report', 'pyportfolioopt_shadow', 'health']);
        }, { timezone: 'America/New_York' });
        // 9:35 AM ET — start-of-day price refresh (dashboard only; not a signal input).
        cron.schedule('35 9 * * 1-5', () => {
            log('SOD dashboard refresh (9:35am ET)');
            try { runPython('src/pipeline/run_sod_refresh.py'); }
            catch (e) { log(`SOD refresh error: ${e.message.slice(0, 200)}`); }
        }, { timezone: 'America/New_York' });
    }

    // ── SP-6 Phase A: EOD→Open Execution Flow ────────────────────────────────
    // Gated by OPENCLAW_EOD_SIGNAL_REGISTER (default OFF). When ON, the legacy
    // same-day close-exec cycle is superseded: signals are computed at 4 PM[T]
    // on real close[T] prices, carried overnight, gated at 9:15 AM[T+1], and
    // executed into close[T+1] at 3:55 PM[T+1].
    // Mutual exclusion with OPENCLAW_CLOSE_EXEC_LIVE is enforced at registration
    // (throw above). Gate-OFF = zero new crons registered (byte-identical to pre-SP6).
    // Spec: docs/archive/superpowers/specs/2026-05-31-sp6-phase-a-eod-open-execution-design.md §4/§12
    if (eodSignalRegister || sameDayExec) {
        const dispatchCycle = (reason, steps) => {
            const today = new Date().toISOString().slice(0, 10);
            try {
                const { runDailyCycleGraph } = require('../agent/graphs/daily-cycle');
                runDailyCycleGraph({ runDate: today, reason, requestedSteps: steps })
                    .then((out) => log(`${reason} finished: status=${out.status} aborted=${out.abortedAt || 'none'}`))
                    .catch((err) => log(`${reason} FAILED: ${err.message}`));
            } catch (e) {
                log(`${reason} dispatch error: ${e.message}`);
            }
        };

        // (a) 16:15 ET Mon–Fri — EOD compute: collect → sentiment → signals.
        // Fires after the ~4:05pm EOD price-append so close[T] is fully present.
        // engine.py:run_strategies → write_signals writes execution_signals at
        // lifecycle_state='COMPUTED' with target_date=T+1; also writes the
        // eod_compute_health sentinel and parity_mark (mark_entry_price) for
        // any positions filled that day.
        // EOD mode only — under same-day exec the 16:15 slot runs collect
        // alone (see the sameDayExec block below).
        if (eodSignalRegister) cron.schedule('15 16 * * 1-5', () => {
            log('EOD compute (4:15pm ET): collect → sentiment → signals → option_hedge');
            dispatchCycle('eod-signal-register', ['collect', 'sentiment', 'signals', 'option_hedge']);
        }, { timezone: 'America/New_York' });

        // (b) 09:15 ET Mon–Fri — pre-market carry-forward gate.
        // Reads COMPUTED signals, applies panic/sentiment verdict, transitions to
        // APPROVED or REJECTED, writes signal_gate_verdicts + __gate_ran__ sentinel.
        // Gated by OPENCLAW_EOD_PREMARKET_GATE; no-op if gate is OFF.
        // premarket_gate.py has no __main__ block → spawn via -c one-liner with
        // sys.path bootstrap (the module inserts ROOT/src itself via Path(__file__),
        // but -c has no __file__ so we inject the path explicitly).
        if (process.env.OPENCLAW_EOD_PREMARKET_GATE === '1') {
            cron.schedule('15 9 * * 1-5', () => {
                log('EOD pre-market carry-forward gate (9:15am ET)');
                try {
                    const fs    = require('fs');
                    const today = new Date().toISOString().slice(0, 10);
                    const logDir = path.join(ROOT, 'logs');
                    try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
                    const logFd = fs.openSync(path.join(logDir, `premarket_gate_${today}.log`), 'a');
                    const child = spawn(PYTHON, [
                        '-c',
                        'import sys; sys.path.insert(0, "src"); from execution.premarket_gate import run_gate; run_gate()',
                    ], {
                        cwd:      ROOT,
                        env:      { ...process.env },
                        detached: true,
                        stdio:    ['ignore', logFd, logFd],
                    });
                    child.unref();
                    log(`premarket-gate spawned (pid ${child.pid})`);
                } catch (e) {
                    log(`premarket-gate spawn error: ${e.message}`);
                }
            }, { timezone: 'America/New_York' });
        }

        // (c) 09:25 ET Mon–Fri — open-window reconcile: drops/flatten only, via OPG.
        // MUST fire PRE-9:28: Alpaca OPG window closes at 9:28 AM (not 9:30 AM);
        // firing at 9:28 lands AT the deadline and gets rejected (confirmed 2026-06-08).
        // 09:25 gives ~3 min buffer for order submission before the OPG cutoff.
        // Gated by OPENCLAW_EOD_RECONCILE.
        // open_reconcile.run_reconcile: diffs APPROVED set vs broker book →
        // close dropped/flattened positions at the open (OPG dual-path) +
        // strategy-ledger close; does NOT size or submit opens.
        if (process.env.OPENCLAW_EOD_RECONCILE === '1') {
            cron.schedule('25 9 * * 1-5', () => {
                log('EOD open-window reconcile (9:25am ET — premarket, OPG required)');
                try {
                    const fs    = require('fs');
                    const today = new Date().toISOString().slice(0, 10);
                    const logDir = path.join(ROOT, 'logs');
                    try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
                    const logFd = fs.openSync(path.join(logDir, `open_reconcile_${today}.log`), 'a');
                    const child = spawn(PYTHON, ['src/execution/open_reconcile.py'], {
                        cwd:      ROOT,
                        env:      { ...process.env },
                        detached: true,
                        stdio:    ['ignore', logFd, logFd],
                    });
                    child.unref();
                    log(`open-reconcile spawned (pid ${child.pid})`);
                } catch (e) {
                    log(`open-reconcile spawn error: ${e.message}`);
                }
            }, { timezone: 'America/New_York' });

            // (d) 09:32 ET Mon–Fri — post-open OPG day-sweep.
            // Day-closes any OPG drop/flatten orders that did not fill at the
            // auction (the 2026-05-18 214/224-expired failure pattern). Fires
            // after the 9:30 open so unfilled OPGs are visible as expired.
            cron.schedule('32 9 * * 1-5', () => {
                log('EOD post-open OPG day-sweep (9:32am ET)');
                try {
                    const fs    = require('fs');
                    const today = new Date().toISOString().slice(0, 10);
                    const logDir = path.join(ROOT, 'logs');
                    try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
                    const logFd = fs.openSync(path.join(logDir, `open_reconcile_sweep_${today}.log`), 'a');
                    const child = spawn(PYTHON, ['src/execution/open_reconcile.py', '--sweep'], {
                        cwd:      ROOT,
                        env:      { ...process.env },
                        detached: true,
                        stdio:    ['ignore', logFd, logFd],
                    });
                    child.unref();
                    log(`open-reconcile --sweep spawned (pid ${child.pid})`);
                } catch (e) {
                    log(`open-reconcile sweep spawn error: ${e.message}`);
                }
            }, { timezone: 'America/New_York' });
        }

        // (e) 15:55 ET Mon–Fri — EOD into-close fill: size + execute + report + health.
        // The production sizer (regime_blended_sizer_live, Task 8a SP-6-aware) runs
        // as the 'trade' step: loads the APPROVED carried set, sizes against current
        // NAV/BP, then 'alpaca' fills the sized opens/resizes into close[T+1].
        // CRITICAL: 'trade' (sizer) must precede 'alpaca' (executor) — without it
        // there is nothing sized to execute. Drops are already closed at 9:30;
        // the sizer's re-diff sees them gone, so no double-close.
        // EOD mode only — same-day exec has its own 15:55 execute leg below.
        if (eodSignalRegister) cron.schedule('55 15 * * 1-5', () => {
            log('EOD into-close fill (3:55pm ET): trade → alpaca → reconcile → report → health');
            dispatchCycle('eod-into-close-fill', ['trade', 'alpaca', 'reconcile', 'report', 'health']);
        }, { timezone: 'America/New_York' });

        // ── Same-day execution (2026-07-29 pivot): signal[t] → submission[t] ──
        // Spec lineage: 2026-05-27 close-exec design (close[t]-proxy snapshot,
        // execute into the close) + the 2026-07-29 three-tier ingestion
        // directive. Requires OPENCLAW_CLOSE_PROXY_SNAPSHOT=1 in .env so
        // engine.load_prices injects the same-day proxy row (hardened
        // close_proxy_snapshot: chunk-poison eviction, same-day stamp guard,
        // coverage floor). The premarket gate/reconcile/sweep above stay
        // active in this mode — they protect the carried book premarket.
        if (sameDayExec) {
            // 14:30 ET — TIER 1: fetch everything the ACTING strategies consume,
            // fresh, before the compute reads it (three-tier ingestion). Its own
            // job, not a step in the 15:00 chain: an overrun here leaves a
            // partial overlay and the engine falls back per-ticker to the EOD
            // panel, whereas an overrun inside the chain delays execution and
            // can trip the 15:55 "no sized handoff" abort — a silent no-trade
            // day. Measured 2026-07-30: 5,173 tickers in 294s, so --budget 900
            // still lands ~15 min before the compute.
            cron.schedule('30 14 * * 1-5', () => {
                log('tier-1 acting-set ingest (2:30pm ET): options chains for the acting universe');
                try {
                    const fs = require('fs');
                    const today = new Date().toISOString().slice(0, 10);
                    const logDir = path.join(ROOT, 'logs');
                    try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
                    const logFd = fs.openSync(path.join(logDir, `acting_ingest_${today}.log`), 'a');
                    const child = spawn(PYTHON, ['scripts/run_acting_ingest.py',
                                                 '--date', today, '--budget', '900'], {
                        cwd: ROOT, env: { ...process.env },
                        detached: true, stdio: ['ignore', logFd, logFd],
                    });
                    child.unref();
                    log(`tier-1 ingest spawned (pid ${child.pid})`);
                } catch (e) {
                    log(`tier-1 ingest spawn error: ${e.message}`);
                }
            }, { timezone: 'America/New_York' });

            // 15:00 ET — compute: sentiment → signals(proxy) → gates → sized
            // handoff. NO full collect in the critical path (67-min aux
            // collection is the 16:15 slot's job; the tier-1 overlay above is
            // what makes the aux the engine reads same-day).
            cron.schedule('0 15 * * 1-5', () => {
                log('same-day compute (3:00pm ET): sentiment → signals → ic_gate → handoff → trade');
                dispatchCycle('sameday-compute',
                              ['sentiment', 'signals', 'ic_gate', 'handoff', 'trade']);
            }, { timezone: 'America/New_York' });

            // 15:55 ET — execute into the close, ONLY on a handoff produced
            // TODAY (a stale/missing handoff means the 15:00 compute failed —
            // submitting yesterday's sizing would be exactly the staleness
            // this pivot removes).
            cron.schedule('55 15 * * 1-5', () => {
                const fs = require('fs');
                const today = new Date().toISOString().slice(0, 10);
                const handoff = path.join(ROOT, 'output', 'handoffs', `${today}_sized.json`);
                if (!fs.existsSync(handoff)) {
                    log(`same-day execute ABORT: no ${today}_sized.json — 15:00 compute did not complete; nothing submitted`);
                    return;
                }
                log('same-day execute (3:55pm ET): alpaca → reconcile → report → shadow → health');
                dispatchCycle('sameday-execute',
                              ['alpaca', 'reconcile', 'report', 'pyportfolioopt_shadow', 'health']);
            }, { timezone: 'America/New_York' });

            // 16:15 ET — official EOD collect (append real close[t] + aux
            // masters) + option_hedge. Signals are NOT recomputed here; the
            // 15:00 chain is the sole signal producer in this mode.
            cron.schedule('15 16 * * 1-5', () => {
                log('same-day EOD collect (4:15pm ET): collect → option_hedge');
                dispatchCycle('sameday-eod-collect', ['collect', 'option_hedge']);
            }, { timezone: 'America/New_York' });

            // 16:20 ET — evening measurement pass (parity marks, ledger,
            // live_days, stale trackers). These ride the engine's `signals`
            // step, which in this mode runs at 15:00 — BEFORE the 15:55 fills —
            // so without this the day's fills go unmarked until the next
            // cycle (verified 2026-07-29: signals stayed COMPUTED after the
            // first same-day execution). Runs after the fills, produces NO
            // signals. Spawned directly: it is a measurement job, not a
            // pipeline step.
            cron.schedule('20 16 * * 1-5', () => {
                log('same-day evening marks (4:20pm ET): parity → ledger → live_days → stale');
                try {
                    const fs = require('fs');
                    const today = new Date().toISOString().slice(0, 10);
                    const logDir = path.join(ROOT, 'logs');
                    try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
                    const logFd = fs.openSync(path.join(logDir, `evening_marks_${today}.log`), 'a');
                    const child = spawn(PYTHON, ['scripts/run_evening_marks.py', '--date', today], {
                        cwd: ROOT, env: { ...process.env },
                        detached: true, stdio: ['ignore', logFd, logFd],
                    });
                    child.unref();
                    log(`evening-marks spawned (pid ${child.pid})`);
                } catch (e) {
                    log(`evening-marks spawn error: ${e.message}`);
                }
            }, { timezone: 'America/New_York' });

            // 9:35 ET — start-of-day dashboard price refresh (close-exec
            // heritage; dashboard-only, not a signal input).
            cron.schedule('35 9 * * 1-5', () => {
                log('SOD dashboard refresh (9:35am ET)');
                try { runPython('src/pipeline/run_sod_refresh.py'); }
                catch (e) { log(`SOD refresh error: ${e.message.slice(0, 200)}`); }
            }, { timezone: 'America/New_York' });
        }
    }
    // ── End SP-6 Phase A EOD block ────────────────────────────────────────────

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
        // Auto-liquidation-on-regime-change removed 2026-05-19 (Phase 1 of the
        // redeploy-not-liquidate plan). Daily HMM is now a read-only regime
        // writer; transitions surface to the 10 AM pipeline via the
        // market_regime row and regime_latest.json. Phase 2 wires intraday
        // transitions to a pipeline redeploy instead.
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


    // 9:00-19:55 ET, every 5 min (legacy) or 15 min (OPENCLAW_INTRADAY_15MIN_PREFETCH=1),
    // Mon-Fri — intraday HMM regime detector.
    // Fire-and-forget detached spawn (matches the 10am pipeline pattern).
    // Each tick: collects 6 live features → appends parquet → scores HMM →
    // hysteresis(3)/confidence(<70%)/cooldown(60min) gates. Phase 2
    // (2026-05-19) wires confirmed transitions to scripts/redeploy_pipeline.py
    // (detached subprocess.Popen, never blocks this cron tick).
    //
    // When OPENCLAW_INTRADAY_15MIN_PREFETCH=1: cadence switches to */15 (uniform
    // 3-tick confirmation window aligns to the prefetch buffer), and the Python
    // feature collector floors ts_utc to the 15-min boundary instead of 5-min.
    //
    // Window rationale: SPY option market trades 9:30-16:15 ET. We start
    // the cron at 9:00 to score the open transition and extend through
    // 19:55 to cover Alpaca's after-hours session (16:00-20:00). Outside
    // 9:30-16:15 the chain returns prior-close quotes — the dashboard
    // CARRY-FWD QUOTES badge surfaces this — so those ticks reflect
    // carry-forward, not new signal. Premarket coverage (4:00-9:30) was
    // tried 2026-05-20 and reverted: no new info, just noise.
    //
    // After-hours redeploys (16:00-20:00) are gated by
    // OPENCLAW_REDEPLOY_EXTENDED_HOURS (default OFF). When ON, Phase 3
    // alpaca_executor submits limit orders w/ extended-hours flag.
    const _intradaySchedule = process.env.OPENCLAW_INTRADAY_15MIN_PREFETCH === '1'
        ? '*/15 9-19 * * 1-5'   // 15-min cadence (uniform 3-tick confirmation + prefetch)
        : '*/5 9-19 * * 1-5';   // legacy 5-min cadence
    cron.schedule(_intradaySchedule, () => {
        try {
            const fs = require('fs');
            const path = require('path');
            const today = new Date().toISOString().slice(0, 10);
            const logDir = path.join(ROOT, 'logs');
            try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
            const logPath = path.join(logDir, `intraday_hmm_${today}.log`);
            const logFd = fs.openSync(logPath, 'a');
            const child = spawn(PYTHON, ['scripts/run_intraday_market_state.py'], {
                cwd: ROOT,
                env: { ...process.env },
                detached: true,
                stdio: ['ignore', logFd, logFd],
            });
            child.unref();
        } catch (e) {
            log(`Intraday HMM tick spawn error: ${e.message}`);
        }
    }, { timezone: 'America/New_York' });

    // SP-3.1 Phase B: 24/7 crypto regime detector — hourly, gated.
    // Inert unless OPENCLAW_CRYPTO_REGIME=1. Spawns the driver detached,
    // mirroring the intraday-HMM spawn above. Produces a regime signal only
    // (crypto_regime_latest.json + crypto_regime_states); no redeploy (Phase C).
    cron.schedule('0 * * * *', () => {
        if (process.env.OPENCLAW_CRYPTO_REGIME !== '1') return;
        try {
            const fs = require('fs');
            const path = require('path');
            const today = new Date().toISOString().slice(0, 10);
            const logDir = path.join(ROOT, 'logs');
            try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
            const logFd = fs.openSync(path.join(logDir, `crypto_hmm_${today}.log`), 'a');
            const child = spawn(PYTHON, ['scripts/run_crypto_market_state.py'], {
                cwd: ROOT,
                env: { ...process.env },
                detached: true,
                stdio: ['ignore', logFd, logFd],
            });
            child.unref();
        } catch (e) {
            log(`Crypto regime tick spawn error: ${e.message}`);
        }
    }, { timezone: 'UTC' });

    // Daily 18:00 ET — intraday HMM refit attempt. Trains a fresh HMM on
    // the accumulated intraday_features.parquet; the detector reads
    // hmm_intraday_latest.pkl on its next tick. Bootstrap-safe (no-op if
    // < 500 rows accumulated). Daily (not weekly) so the model arrives
    // within 24h of crossing the training threshold rather than waiting
    // up to a week for the next Sunday.
    cron.schedule('0 18 * * *', () => {
        log('Intraday HMM refit starting');
        try {
            const out = runPython('scripts/train_intraday_hmm.py');
            log(`Intraday HMM refit: ${out.slice(0, 300)}`);
        } catch (e) {
            log(`Intraday HMM refit error: ${e.message.slice(0, 200)}`);
        }
    }, { timezone: 'America/New_York' });

    // Daily 23:55 ET — recompute per-strategy avg holding period + next_fire_date.
    cron.schedule('55 23 * * *', () => {
        log('cadence-recompute: spawning strategy_cadence_recompute.py');
        try {
            const fs = require('fs');
            const path = require('path');
            const today = new Date().toISOString().slice(0, 10);
            const logDir = path.join(ROOT, 'logs');
            try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
            const logPath = path.join(logDir, `strategy_cadence_recompute_${today}.log`);
            const logFd = fs.openSync(logPath, 'a');
            const child = spawn(PYTHON, ['src/execution/strategy_cadence_recompute.py'], {
                cwd: ROOT,
                env: { ...process.env },
                detached: true,
                stdio: ['ignore', logFd, logFd],
            });
            child.unref();
            log(`cadence-recompute: spawned (pid ${child.pid}) → ${logPath}`);
        } catch (e) {
            log(`cadence-recompute spawn error: ${e.message}`);
        }
    }, { timezone: 'America/New_York' });

    // Intraday every 5 min during RTH (9:00-16:00 ET) Mon–Fri — position circuit breaker for consolidate-mode positions.
    cron.schedule('*/5 9-16 * * 1-5', () => {
        try {
            const fs = require('fs');
            const path = require('path');
            const today = new Date().toISOString().slice(0, 10);
            const logDir = path.join(ROOT, 'logs');
            try { fs.mkdirSync(logDir, { recursive: true }); } catch (_) {}
            const logPath = path.join(logDir, `position_circuit_breaker_${today}.log`);
            const logFd = fs.openSync(logPath, 'a');
            const child = spawn(PYTHON, ['src/execution/position_circuit_breaker.py'], {
                cwd: ROOT,
                env: { ...process.env },
                detached: true,
                stdio: ['ignore', logFd, logFd],
            });
            child.unref();
        } catch (e) {
            log(`circuit-breaker spawn error: ${e.message}`);
        }
    }, { timezone: 'America/New_York' });

    // Sunday 08:00 ET — weekly memory synthesis + universe sync
    // (Reaper removed 2026-04-28 per CLAUDE.md NEVER-DELETE-DATA invariant —
    // orphan-column detection no longer feeds data_deprecation_queue.)
    cron.schedule('0 8 * * 0', async () => {
        log('Weekly maintenance starting — signatures, universe sync');

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

        // Sync full ticker universe from FMP into universe_config
        // (sync_universe_to_db is FMP-only; Polygon was removed in SP-1)
        log('Universe sync starting (FMP)...');
        try {
            const syncOut = runPython('src/ingestion/run_universe_sync.py');
            log(`Universe sync complete: ${syncOut.slice(0, 200)}`);
        } catch (e) {
            log(`Universe sync error: ${e.message.slice(0, 200)}`);
        }

        // strategist-ideator + arXiv discovery moved into Saturday brain
        // Phase 6.5 + Phase 2 respectively. Both now run once per week from
        // src/agent/curators/saturday_brain.js, sequenced *after* tier-A
        // synchronous coding so the ideator can react to today's data
        // gaps. The previous Sunday-morning duplication was a Phase-1
        // legacy that double-spent tokens.

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

  // Daily health digest moved into the 10am pipeline as the final `health`
  // step (see pipeline_orchestrator.py STEPS). Posts to #pipeline-feed via
  // DataBot webhook so the digest lands right after each cycle's trade
  // execution instead of the morning before.

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

  log('Cron schedule registered. Zero-token pipeline active.');
    log('Agents will only activate for DEPLOY, REPORT, and weekly synthesis tasks.');
}

module.exports = { start, processReportQueue, resetTokenBudgets };
