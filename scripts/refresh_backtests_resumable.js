#!/usr/bin/env node
/**
 * Resumable fleet re-backtest driver.
 *
 * WHY THIS EXISTS: src/maintenance/refresh_backtests.sh runs
 * `unified_backtest --all-live` as ONE python process under `timeout -k 60 21600`
 * (6h). Its own comment concedes the gap: "Durable fix = per-strategy
 * subprocess/watchdog resumable refresh (tracked separately)." That gap is now
 * load-bearing:
 *
 *   - MEASURED 2026-07-16: one heavy strategy (momentum_12_1, 22.8k trades)
 *     exceeds 10 MINUTES on the 5-year-backfilled panel. 126 strategies is
 *     ~8-21h — the 6h cap kills it mid-fleet, guaranteed.
 *   - A mid-fleet kill is the WORST outcome, not a neutral one: canonical is
 *     then HALF-REWRITTEN — some strategies measured on real 10y data, others
 *     still on the old ~5.5y window — and `weight = effective_sharpe` would
 *     size the book off two incomparable methodologies. --all-live is also
 *     not resumable, so a retry restarts from strategy #1.
 *   - One process for 126 strategies also never returns memory to the OS.
 *     load_prices_panels peaks ~4.0GB per call on a 2-core/8GB no-swap box;
 *     a fresh subprocess per strategy guarantees that peak is released.
 *
 * DESIGN
 *   - One subprocess per strategy (`unified_backtest --strategy-id`), niced.
 *     A crash/OOM kills THAT strategy only; the fleet continues.
 *   - Checkpoint file of completed strategy_ids → --resume skips them, so an
 *     interrupted run continues instead of restarting.
 *   - HARD DEADLINE: stops cleanly rather than being killed mid-strategy.
 *   - Per-strategy timeout so one pathological strategy can't eat the window.
 *   - COMPUTE ONLY. Writes canonical strategy_backtest_runs/_regimes. Does NOT
 *     rebuild weights and does NOT touch activation — actuation stays a
 *     separate, deliberate step (weekly_live_sharpe.js / backtest_panel).
 *
 * ⚠️ CANONICAL IS MIXED WHILE THIS RUNS. Readers must not actuate mid-run:
 *    weekend_saturday.sh step 4 (coupling) reads canonical as its baseline and
 *    step 7 (weekly_live_sharpe) rebuilds weights FROM it. Finish before
 *    Sat 12:00 UTC, or stamp-disable that timer for the cycle.
 *
 * Usage:
 *   node scripts/refresh_backtests_resumable.js --dry-run
 *   node scripts/refresh_backtests_resumable.js --resume --deadline 2026-07-18T06:00
 */
'use strict';

require('dotenv').config({ path: '/root/openclaw/.env' });
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');

const arg = (n, d) => { const i = process.argv.indexOf(`--${n}`); return i >= 0 ? (process.argv[i + 1] ?? true) : d; };
const DRY        = process.argv.includes('--dry-run');
const RESUME     = process.argv.includes('--resume');
const CKPT       = String(arg('checkpoint', path.join(ROOT, 'data/.refresh_backtests.done')));
const FAILED     = CKPT + '.failed';
const DEADLINE   = String(arg('deadline', '2026-07-18T06:00'));   // UTC, well clear of Sat 12:00
const PER_TIMEOUT_S = parseInt(arg('per-timeout', '3600'), 10);   // 1h/strategy
const LIMIT      = parseInt(arg('limit', '0'), 10);
// Memory-settle gate (2026-07-16). load_prices_panels peaks ~4.0GB per strategy
// on this 8GB no-swap box; spawning the NEXT strategy the instant the previous
// subprocess EXITS caused an OOM CASCADE — a heavy strategy OOMs, its pages
// aren't reclaimed yet, the next spawn collides and OOMs in seconds, and so on
// (observed: 1 ok / 5 fail in a row). Before each spawn, wait until the OS
// actually has room. Give each strategy the whole box: one process at a time.
const MEM_FLOOR_MB = parseInt(arg('mem-floor', '5000'), 10);      // require this avail before spawning
const MEM_WAIT_MAX_S = parseInt(arg('mem-wait', '180'), 10);      // cap the wait so a stuck box can't hang the fleet

function availMB() {
  // MemAvailable is the honest "can allocate without swapping" figure; free's
  // "used" is not (it counts reclaimable cache). No swap here, so this is firm.
  try {
    const m = fs.readFileSync('/proc/meminfo', 'utf8').match(/MemAvailable:\s+(\d+) kB/);
    return m ? Math.round(parseInt(m[1], 10) / 1024) : null;
  } catch { return null; }
}
// Shell-free blocking sleep: spawnSync with an argument array (no command
// string, no injection surface). This is a deliberately synchronous driver.
const sleepSec = (s) => spawnSync('sleep', [String(s)]);
function waitForMemory(sid) {
  const t0 = Date.now();
  let waited = false;
  for (;;) {
    const a = availMB();
    if (a === null || a >= MEM_FLOOR_MB) {
      if (waited) console.log(`[rebt]   memory recovered to ${a}MB — spawning ${sid}`);
      return;
    }
    if ((Date.now() - t0) / 1000 >= MEM_WAIT_MAX_S) {
      console.log(`[rebt]   ⚠️ memory still ${a}MB < ${MEM_FLOOR_MB}MB after ${MEM_WAIT_MAX_S}s — spawning ${sid} anyway`);
      return;
    }
    if (!waited) { console.log(`[rebt]   waiting for memory: ${a}MB < ${MEM_FLOOR_MB}MB floor (let the prior peak reclaim)…`); waited = true; }
    sleepSec(3);
  }
}

// LIVE-FIRST ordering (2026-07-16). On this box the 10y re-backtest is a
// multi-day run, so front-load the strategies that drive the CURRENT book's
// sizing + the conviction-floor recheck (state='live'); candidates (needed only
// for NEW activation decisions) and staging trail. Alphabetical within each
// tier for a stable, resumable order. This only changes ORDER — every strategy
// still runs exactly once; the checkpoint is by strategy_id, order-independent.
const RANK = { live: 0, candidate: 1, staging: 2 };

function manifestEntries() {
  const m = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/strategies/manifest.json'), 'utf8'));
  return Object.entries(m.strategies || {})
    .filter(([, e]) => e.state in RANK)
    .sort(([aSid, aE], [bSid, bE]) =>
      (RANK[aE.state] - RANK[bE.state]) || aSid.localeCompare(bSid));
}

// Backtest quarantine (2026-07-21). A strategy whose backtest REPRODUCIBLY
// fails (e.g. a numerical bug) carries an additive manifest flag
// `backtest_quarantine` — this does NOT change lifecycle `state` — so it stops
// consuming a fleet slot every night until the strategy code is fixed.
function quarantinedStrategies() {
  return manifestEntries().filter(([, e]) => e.backtest_quarantine).map(([sid]) => sid);
}

// Emit the skip reasons ONCE per process. NEVER silent: the exclusion has to be
// visible in the run log. (Previously logged from inside allStrategies(), which
// is called several times per run — hence the duplicate QUARANTINED blocks.)
let _quarantineLogged = false;
function logQuarantined() {
  if (_quarantineLogged) return;
  _quarantineLogged = true;
  for (const [sid, e] of manifestEntries().filter(([, e]) => e.backtest_quarantine)) {
    const why = (e.backtest_quarantine && e.backtest_quarantine.reason) || 'flagged';
    console.log(`[rebt] QUARANTINED (skipped): ${sid} — ${why}`);
  }
}

// RUNNABLE strategies only — quarantined ones are excluded from the work queue.
// Callers measuring COMPLETION must not use this set; see the `remaining`
// computation below for why.
function allStrategies() {
  logQuarantined();
  return manifestEntries().filter(([, e]) => !e.backtest_quarantine).map(([sid]) => sid);
}

(async () => {
  const stopAt = Date.parse(DEADLINE + ':00Z');
  if (!Number.isFinite(stopAt)) { console.error(`bad --deadline ${DEADLINE} (want UTC YYYY-MM-DDTHH:MM)`); process.exit(1); }

  const done = new Set(RESUME && fs.existsSync(CKPT)
    ? fs.readFileSync(CKPT, 'utf8').split('\n').filter(Boolean) : []);
  let work = allStrategies().filter(s => !done.has(s));
  if (LIMIT > 0) work = work.slice(0, LIMIT);

  console.log(`[rebt] fleet=${allStrategies().length} done=${done.size} TO RUN=${work.length}`);
  console.log(`[rebt] per-strategy timeout ${PER_TIMEOUT_S}s | hard stop ${DEADLINE}Z | checkpoint ${CKPT}`);
  console.log(`[rebt] COMPUTE ONLY — canonical only; no weights rebuild, no activation.`);
  if (DRY) {
    console.log(`[rebt] DRY RUN — would run ${work.length} strategies. est ${(work.length * 10 / 60).toFixed(1)}-${(work.length * 20 / 60).toFixed(1)}h. No writes.`);
    console.log(`[rebt] first 5: ${work.slice(0, 5).join(', ')}`);
    process.exit(0);
  }

  const env = { ...process.env, PYTHONPATH: 'src' };
  let ok = 0, fail = 0, t0 = Date.now();
  for (const sid of work) {
    if (Date.now() >= stopAt) {
      console.log(`[rebt] DEADLINE ${DEADLINE}Z reached — stopping cleanly. Re-run with --resume to continue.`);
      break;
    }
    // Don't spawn into a memory-pressured box — this prevents the OOM cascade
    // where one heavy strategy's death takes the next few with it.
    waitForMemory(sid);
    const s0 = Date.now();
    // Fresh subprocess per strategy: releases the ~4GB panel peak back to the
    // OS and contains an OOM to this strategy instead of the whole fleet.
    // fleet_oom_victim_exec.sh marks ONLY this child oom_score_adj=1000 (the
    // driver stays at normal priority — unit-level OOMScoreAdjust killed the
    // driver itself at the 2026-07-27 13:30Z market-open surge).
    const r = spawnSync('bash', [path.join(ROOT, 'scripts/fleet_oom_victim_exec.sh'),
      'python3', '-m', 'backtest.unified_backtest', '--strategy-id', sid],
      { cwd: ROOT, env, timeout: PER_TIMEOUT_S * 1000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    const mins = ((Date.now() - s0) / 60000).toFixed(1);
    if (r.status === 0) {
      fs.appendFileSync(CKPT, sid + '\n');   // checkpoint ONLY on success
      ok++;
      console.log(`[rebt] ok   ${sid} (${mins}m) ${ok + fail}/${work.length}`);
    } else if ((r.stdout || '').includes('wrote run_id=')) {
      // Post-write kill (2026-07-27: daytime OOM during teardown/panel
      // rebuild). unified_backtest prints 'wrote run_id=' only AFTER
      // conn.commit() — the canonical row is durable, so checkpoint instead
      // of burning a full re-run. The skipped panel rebuild self-heals on the
      // strategy's next dashboard rebuild.
      fs.appendFileSync(CKPT, sid + '\n');
      ok++;
      const how = r.signal ? `signal ${r.signal}` : `rc=${r.status}`;
      console.log(`[rebt] ok   ${sid} (${mins}m) SALVAGED — write committed, killed post-write (${how}) ${ok + fail}/${work.length}`);
    } else {
      fail++;
      // rc=137 is the OOM signature on this box; surface it distinctly so a
      // memory regression is never mistaken for a strategy-logic failure.
      const why = r.status === 137 ? 'OOM (rc=137)'
                : r.signal            ? `signal ${r.signal}`
                : `rc=${r.status}`;
      const tail = ((r.stderr || '') + (r.stdout || '')).trim().split('\n').slice(-1)[0] || '';
      fs.appendFileSync(FAILED, `${sid}\t${why}\t${tail.slice(0, 160)}\n`);
      console.log(`[rebt] FAIL ${sid} (${mins}m) ${why} :: ${tail.slice(0, 110)}`);
    }
  }
  const total = ((Date.now() - t0) / 60000).toFixed(1);
  const doneSet = new Set(fs.existsSync(CKPT)
    ? fs.readFileSync(CKPT, 'utf8').split('\n').filter(Boolean) : []);
  const runnableLeft = allStrategies().filter(s => !doneSet.has(s)).length;
  // COUNT QUARANTINED STRATEGIES AS OUTSTANDING (2026-08-02). They are excluded
  // from allStrategies(), so counting only that set made them vanish from the
  // completion metric: `remaining` could reach 0 — printing a clean line with no
  // CANONICAL IS MIXED warning — while they sat on the OLD window/methodology.
  // That is not cosmetic. fleet_overnight_resume.sh greps this exact line and,
  // at 0, runs `run_universe_shrink.py --adopt --reassign` (real actuation:
  // --adopt commits universe-ladder verdicts, --reassign re-derives per-regime
  // sleeve eligibility) AND disables its own timer. Neither is covered by the
  // masked OPENCLAW_ACTIVATION_ASSIGNER / AUTO_DEMOTE guards. The S_pairs
  // quarantine had this armed already. They ARE outstanding — they simply
  // aren't runnable tonight — so uniform must not be declared while any remain.
  const quarantinedLeft = quarantinedStrategies().filter(s => !doneSet.has(s));
  const remaining = runnableLeft + quarantinedLeft.length;
  const split = quarantinedLeft.length
    ? ` (${runnableLeft} runnable + ${quarantinedLeft.length} quarantined)`
    : '';
  console.log(`[rebt] DONE ok=${ok} fail=${fail} in ${total}m | ${remaining} strategies still outstanding${split}`);
  if (remaining > 0) {
    console.log(`[rebt] ⚠️ CANONICAL IS MIXED (${remaining} strategies still on the old window/methodology).`);
    console.log(`[rebt]    Do NOT rebuild weights or run weekend_saturday until this reaches 0.`);
  }
  if (quarantinedLeft.length) {
    console.log(`[rebt] ⛔ UNIFORM UNREACHABLE while quarantined: ${quarantinedLeft.join(', ')}`);
    console.log(`[rebt]    Fix + re-backtest, or consciously accept a canonical that excludes them.`);
  }
  if (fail > 0) console.log(`[rebt] failures logged to ${FAILED}`);
  process.exit(0);
})().catch(e => { console.error('[rebt] FATAL', e); process.exit(1); });
