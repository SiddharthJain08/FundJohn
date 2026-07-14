#!/usr/bin/env node
'use strict';

/**
 * auto_approval.js — the fully-automatic promotion stage of the research
 * system (operator directive 2026-07-13: "make the research system fully
 * automatic and inherently regime based").
 *
 * Two passes + a finale, all through the EXISTING gate-preserving HTTP
 * surface on :3000 (never systemTransition — that bypasses the gate):
 *
 *   A. CANDIDATE SWEEP — every manifest state='candidate' strategy is offered
 *      candidate→live via POST /api/strategies/:id/transition as
 *      actor 'system:sunday-auto-approval' with NO eligible_regimes named →
 *      the per-regime qualification gate (promotion_service, policy
 *      2026-07-13 v2: sharpe > 0 AND sleeve max-DD ≤ class ceiling AND
 *      ≥ 100 trades, per regime) computes the qualifying set itself and the
 *      route syncs strategy_regime_params to exactly those regimes.
 *      422 (no qualifying regime / no backtest) simply leaves it candidate —
 *      re-offered every Sunday as backtests refresh.
 *      skip_weights_rebuild=true — the finale owns the single rebuild.
 *
 *   B. STAGING BUILD — every manifest state='staging' strategy is pushed
 *      through the fused approval pipeline (POST /api/strategies/:id/approve:
 *      backfill → strategycoder → backtest → candidate), STRICTLY SERIALLY
 *      (2-core VPS — concurrent builds/backtests are forbidden) within a time
 *      budget; leftovers keep their place for the next Sunday. Each completed
 *      job auto-continues candidate→live via the staging_approver finalize
 *      hook, so late finishers (after this stage exits) still land.
 *
 *   C. FINALE — one activation_assigner --all (slider + qualification applied
 *      to strategy_regime_params) followed by ONE strategy_weights --rebuild.
 *      Never per-strategy fire-and-forget: racing rebuilds duplicated the
 *      is_current snapshot generation (2026-07-13 lesson, 247 dupe pairs).
 *
 * Called from saturday_brain_finisher.js as Phase 9 of the Sunday second
 * pass, and runnable standalone (backlog flush):
 *
 *   node src/agent/curators/auto_approval.js [--dry-run] [--candidates-only]
 *        [--staging-budget-mins N] [--per-job-timeout-mins N] [--api-base URL]
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR  = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
const API_BASE      = process.env.OPENCLAW_API_BASE || 'http://127.0.0.1:3000';

// .env so POSTGRES_URI etc. are populated when run standalone.
try {
  for (const line of fs.readFileSync(path.join(OPENCLAW_DIR, '.env'), 'utf8').split('\n')) {
    const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
} catch (_) {}

function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 2 });
  return _query._pool.query(sql, params);
}

function _manifestStrategies() {
  const m = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
  return m.strategies || {};
}

async function _post(apiBase, route, body) {
  const resp = await fetch(apiBase + route, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const json = await resp.json().catch(() => ({}));
  return { status: resp.status, body: json };
}

const _sleep = (ms) => new Promise(r => setTimeout(r, ms));

// Memory guard (2026-07-14 lesson: the first staging pass collided with the
// EOD window on the 8GB no-swap box — the OOM killer took the coder children
// AND johnbot itself). Don't kick a build unless MemAvailable clears the bar.
const MIN_FREE_GB = 2.5;
function _memAvailableGb() {
  try {
    const m = /MemAvailable:\s+(\d+) kB/.exec(fs.readFileSync('/proc/meminfo', 'utf8'));
    return m ? parseInt(m[1], 10) / 1048576 : null;
  } catch (_) { return null; }
}
async function _waitForMemory(log, maxWaitMs) {
  const t0 = Date.now();
  for (;;) {
    const gb = _memAvailableGb();
    if (gb == null || gb >= MIN_FREE_GB) return true;
    if (Date.now() - t0 > maxWaitMs) return false;
    log(`  memory guard: ${gb.toFixed(1)}GB available < ${MIN_FREE_GB}GB — waiting 2m`);
    await _sleep(120_000);
  }
}

// ── Pass A: candidate sweep ─────────────────────────────────────────────────
async function sweepCandidates({ log, dryRun, apiBase }) {
  const strategies = _manifestStrategies();
  const candidates = Object.keys(strategies)
    .filter(sid => strategies[sid].state === 'candidate').sort();
  log(`candidate sweep: ${candidates.length} candidates to offer the gate`);

  const promoted = [], blocked = [], errored = [];
  for (const sid of candidates) {
    if (dryRun) {
      // Read-only gate evaluation via the shared service — no writes at all.
      try {
        const { computeQualifyingRegimes } = require('../../lib/promotion_service');
        const q = await computeQualifyingRegimes({
          dbQuery: (sql, params) => _query(sql, params),
          sid, instrumentClass: strategies[sid].instrument_class || 'equity',
        });
        if (q.qualifying.length > 0) {
          promoted.push({ sid, qualifying: q.qualifying });
          log(`  WOULD PROMOTE ${sid} → live in [${q.qualifying.join(', ')}]`);
        } else {
          blocked.push({ sid, failed_gates: q.hasRun ? ['no_qualifying_regime'] : ['no_backtest'] });
        }
      } catch (e) { errored.push({ sid, error: e.message }); }
      continue;
    }
    try {
      const r = await _post(apiBase, `/api/strategies/${encodeURIComponent(sid)}/transition`, {
        to_state: 'live',
        actor:    'system:sunday-auto-approval',
        reason:   'auto-promotion: per-regime qualification gate (Sunday second pass)',
        skip_weights_rebuild: true,
      });
      if (r.status === 200 && r.body.ok) {
        promoted.push({ sid, qualifying: r.body.qualifying_regimes || [] });
        log(`  PROMOTED ${sid} → live in [${(r.body.qualifying_regimes || []).join(', ')}]`);
      } else if (r.status === 422) {
        blocked.push({ sid, failed_gates: r.body.failed_gates || [r.body.error] });
      } else {
        errored.push({ sid, error: `${r.status} ${r.body.error || ''}`.trim() });
        log(`  ERROR ${sid}: ${r.status} ${r.body.error || ''}`);
      }
    } catch (e) {
      errored.push({ sid, error: e.message });
      log(`  ERROR ${sid}: ${e.message}`);
    }
  }
  log(`candidate sweep done: ${promoted.length} promoted, ${blocked.length} gate-blocked, ${errored.length} errors`);
  return { promoted, blocked, errored };
}

// ── Pass B: staging build (strictly serial) ─────────────────────────────────
async function processStaging({ log, dryRun, apiBase, budgetMs, perJobTimeoutMs }) {
  const strategies = _manifestStrategies();
  const staging = Object.keys(strategies)
    .filter(sid => strategies[sid].state === 'staging').sort();
  log(`staging build: ${staging.length} staged strategies (budget ${Math.round(budgetMs / 60000)}m, serial)`);
  const completed = [], failed = [], deferred = [];
  if (dryRun) {
    staging.forEach(sid => deferred.push(sid));
    log(`  dry-run: would build ${staging.length} serially`);
    return { completed, failed, deferred };
  }
  const t0 = Date.now();
  for (const sid of staging) {
    if (Date.now() - t0 > budgetMs) { deferred.push(sid); continue; }
    // Never kick a coder/backfill/backtest chain into a memory-starved box —
    // the OOM killer takes the children (and once took johnbot itself).
    if (!(await _waitForMemory(log, 60 * 60_000))) {
      deferred.push(sid);
      log(`  ${sid}: deferred — memory never freed above ${MIN_FREE_GB}GB within 1h`);
      continue;
    }
    try {
      const kick = await _post(apiBase, `/api/strategies/${encodeURIComponent(sid)}/approve`, {
        actor: 'system:sunday-auto-approval',
      });
      if (kick.status !== 202 || !kick.body.job_id) {
        // 409 = already has an active job (fine — poll it) or wrong state.
        if (kick.status === 409 && /already/.test(kick.body.error || '')) {
          log(`  ${sid}: active job already running — waiting on it`);
        } else {
          failed.push({ sid, error: `${kick.status} ${kick.body.error || ''}`.trim() });
          log(`  ${sid}: approve refused — ${kick.status} ${kick.body.error || ''}`);
          continue;
        }
      }
      // Poll the job to terminal state — SERIAL by design (2-core VPS).
      // Table columns are started_at/finished_at (NOT created_at — the
      // 2026-07-13 flush failed every poll on that and burst-kicked jobs).
      const jt0 = Date.now();
      let terminal = null;
      while (Date.now() - jt0 < perJobTimeoutMs) {
        await _sleep(30_000);
        const { rows } = await _query(
          `SELECT status, phase FROM strategy_approval_jobs
            WHERE strategy_id = $1 ORDER BY started_at DESC LIMIT 1`, [sid]);
        const j = rows[0];
        if (!j) break;
        if (j.status === 'succeeded' || j.status === 'failed' || j.status === 'cancelled') {
          terminal = j.status; break;
        }
      }
      if (terminal === 'succeeded') {
        completed.push(sid);
        log(`  ${sid}: fused approval complete (auto-continues to the gate)`);
      } else if (terminal) {
        failed.push({ sid, error: `job ${terminal}` });
        log(`  ${sid}: job ${terminal}`);
      } else {
        deferred.push(sid);
        log(`  ${sid}: still running at per-job timeout — leaving it to finish in background`);
        // Do NOT kick the next one while this still runs — the box is 2-core.
        break;
      }
    } catch (e) {
      // Infrastructure error (DB poll, network) — the job may STILL be
      // running inside johnbot. Kicking the next one anyway would stack
      // concurrent builds on the 2-core box, so HALT the pass; leftovers
      // roll to the next Sunday.
      failed.push({ sid, error: e.message });
      log(`  ${sid}: ${e.message} — halting staging pass (job may still be running)`);
      break;
    }
  }
  // Anything we never reached.
  for (const sid of staging) {
    if (![...completed, ...failed.map(f => f.sid), ...deferred].includes(sid)) deferred.push(sid);
  }
  log(`staging build done: ${completed.length} completed, ${failed.length} failed, ${deferred.length} deferred to next pass`);
  return { completed, failed, deferred };
}

// ── Finale: slider apply + ONE weights rebuild ──────────────────────────────
function runFinale({ log, dryRun, trigger }) {
  if (dryRun) { log('finale: dry-run — skipping assigner + rebuild'); return false; }
  const { spawnSync } = require('child_process');
  const env = { ...process.env, PYTHONPATH: 'src' };
  log('finale: activation_assigner --all (qualification gate + slider) …');
  const a = spawnSync('/bin/bash',
    ['-c', 'nice -n 19 python3 -m backtest.activation_assigner --all --notify'],
    { cwd: OPENCLAW_DIR, env, encoding: 'utf8', timeout: 15 * 60_000 });
  log(`finale: assigner exit=${a.status} ${(a.stdout || '').split('\n').filter(l => /summary/.test(l)).join(' ')}`);
  log('finale: ONE strategy_weights --rebuild …');
  const w = spawnSync('/bin/bash',
    ['-c', `nice -n 19 python3 -m execution.strategy_weights --rebuild --trigger=${trigger}`],
    { cwd: OPENCLAW_DIR, env: { ...env, OPENCLAW_AUTO_DEMOTE: '0' }, encoding: 'utf8', timeout: 20 * 60_000 });
  log(`finale: weights rebuild exit=${w.status}`);
  return a.status === 0 && w.status === 0;
}

async function runAutoApproval(opts = {}) {
  const log = opts.log || ((m) => console.error(`[auto_approval] ${m}`));
  const dryRun          = !!opts.dryRun;
  const apiBase         = opts.apiBase || API_BASE;
  const candidatesOnly  = !!opts.candidatesOnly;
  const budgetMs        = opts.stagingBudgetMs  || 90 * 60_000;
  const perJobTimeoutMs = opts.perJobTimeoutMs || 100 * 60_000;
  const trigger         = opts.trigger || 'sunday_auto_approval';

  const sweep   = await sweepCandidates({ log, dryRun, apiBase });
  const staging = candidatesOnly
    ? { completed: [], failed: [], deferred: [] }
    : await processStaging({ log, dryRun, apiBase, budgetMs, perJobTimeoutMs });

  let finaleOk = null;
  if (sweep.promoted.length > 0 || staging.completed.length > 0) {
    finaleOk = runFinale({ log, dryRun, trigger });
  } else {
    log('finale: nothing promoted/completed — no assigner/rebuild needed');
  }
  const summary = {
    promoted:        sweep.promoted,
    gate_blocked:    sweep.blocked.length,
    sweep_errors:    sweep.errored,
    staging_completed: staging.completed,
    staging_failed:  staging.failed,
    staging_deferred: staging.deferred,
    finale_ok:       finaleOk,
    dry_run:         dryRun,
  };
  log(`summary: ${sweep.promoted.length} promoted, ${sweep.blocked.length} blocked, ` +
      `staging ${staging.completed.length}✓/${staging.failed.length}✗/${staging.deferred.length}→next, finale=${finaleOk}`);
  return summary;
}

module.exports = { runAutoApproval, sweepCandidates, processStaging, runFinale };

if (require.main === module) {
  const getArg = (name, fallback = null) => {
    const i = process.argv.indexOf(name);
    if (i < 0) return fallback;
    const next = process.argv[i + 1];
    if (!next || next.startsWith('--')) return true;
    return next;
  };
  runAutoApproval({
    dryRun:          !!getArg('--dry-run', false),
    candidatesOnly:  !!getArg('--candidates-only', false),
    apiBase:         getArg('--api-base', API_BASE),
    stagingBudgetMs:  parseInt(getArg('--staging-budget-mins', '90'), 10) * 60_000,
    perJobTimeoutMs: parseInt(getArg('--per-job-timeout-mins', '100'), 10) * 60_000,
    trigger:         getArg('--trigger', 'auto_approval_cli'),
  }).then((s) => {
    console.log(JSON.stringify(s, null, 2));
    process.exit(0);
  }).catch((e) => {
    console.error('[auto_approval] FATAL:', e.message);
    console.error(e.stack);
    process.exit(1);
  });
}
