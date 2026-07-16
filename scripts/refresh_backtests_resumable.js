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

function allStrategies() {
  const m = JSON.parse(fs.readFileSync(path.join(ROOT, 'src/strategies/manifest.json'), 'utf8'));
  return Object.entries(m.strategies || {})
    .filter(([, e]) => ['live', 'candidate', 'staging'].includes(e.state))
    .map(([sid]) => sid).sort();
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
    const s0 = Date.now();
    // Fresh subprocess per strategy: releases the ~4GB panel peak back to the
    // OS and contains an OOM to this strategy instead of the whole fleet.
    const r = spawnSync('nice', ['-n', '19', 'python3', '-m', 'backtest.unified_backtest', '--strategy-id', sid],
      { cwd: ROOT, env, timeout: PER_TIMEOUT_S * 1000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    const mins = ((Date.now() - s0) / 60000).toFixed(1);
    if (r.status === 0) {
      fs.appendFileSync(CKPT, sid + '\n');   // checkpoint ONLY on success
      ok++;
      console.log(`[rebt] ok   ${sid} (${mins}m) ${ok + fail}/${work.length}`);
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
  const remaining = allStrategies().filter(s => !new Set(
    fs.existsSync(CKPT) ? fs.readFileSync(CKPT, 'utf8').split('\n').filter(Boolean) : []).has(s)).length;
  console.log(`[rebt] DONE ok=${ok} fail=${fail} in ${total}m | ${remaining} strategies still outstanding`);
  if (remaining > 0) {
    console.log(`[rebt] ⚠️ CANONICAL IS MIXED (${remaining} strategies still on the old window/methodology).`);
    console.log(`[rebt]    Do NOT rebuild weights or run weekend_saturday until this reaches 0.`);
  }
  if (fail > 0) console.log(`[rebt] failures logged to ${FAILED}`);
  process.exit(0);
})().catch(e => { console.error('[rebt] FATAL', e); process.exit(1); });
