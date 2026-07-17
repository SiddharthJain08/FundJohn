#!/usr/bin/env node
/*
 * Backtest an explicit list of strategy ids, one fresh subprocess each.
 *
 * Fills the gap in refresh_backtests_resumable.js, which derives its own
 * fleet list and takes no ids. Reuses the same discipline: dotenv env,
 * PYTHONPATH=src, nice -n 19, per-strategy timeout, memory-floor wait before
 * each spawn (no-swap 8GB box), checkpoint file so --resume skips done ids.
 *
 * COMPUTE ONLY — writes canonical strategy_backtest_runs/_regimes; does not
 * rebuild weights or touch activation.
 *
 * Usage:
 *   node scripts/backtest_ids.js --ids a,b,c [--resume] [--wait-for-fleet]
 *        [--per-timeout 7200] [--mem-floor 4000] [--checkpoint PATH]
 *
 *   --wait-for-fleet  poll until no refresh_backtests_resumable.js process
 *                     remains before starting (used to queue behind a fleet).
 * First use: the 2026-07-17 Oxford re-hearing (14 reaped candidates restored
 * for re-evaluation on the 10y backfilled panel).
 */
'use strict';

require('dotenv').config({ path: '/root/openclaw/.env' });
const fs = require('fs');
const path = require('path');
const { spawnSync, execSync } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const arg = (n, d) => { const i = process.argv.indexOf(`--${n}`); return i >= 0 ? (process.argv[i + 1] ?? true) : d; };

const IDS = String(arg('ids', '')).split(',').map(s => s.trim()).filter(Boolean);
const RESUME = process.argv.includes('--resume');
const WAIT_FLEET = process.argv.includes('--wait-for-fleet');
const PER_TIMEOUT_S = parseInt(arg('per-timeout', '7200'), 10);
const MEM_FLOOR_MB = parseInt(arg('mem-floor', '4000'), 10);
const MEM_WAIT_MAX_S = parseInt(arg('mem-wait', '300'), 10);
const CKPT = String(arg('checkpoint', path.join(ROOT, 'data/.backtest_ids.done')));

if (!IDS.length) { console.error('need --ids a,b,c'); process.exit(2); }
const log = (m) => console.log(`[bt-ids] ${new Date().toISOString()} ${m}`);

const availMB = () => {
  try { return parseInt(execSync("awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo", { encoding: 'utf8' }), 10); }
  catch { return null; }
};
const sleepSec = (s) => spawnSync('sleep', [String(s)]);

function waitForFleet() {
  for (;;) {
    let out = '';
    try { out = execSync('pgrep -f refresh_backtests_resumable.js || true', { encoding: 'utf8' }).trim(); } catch {}
    const others = out.split('\n').filter(p => p && parseInt(p, 10) !== process.pid);
    if (!others.length) { log('fleet driver gone — starting'); return; }
    log(`fleet driver still running (pid ${others[0]}) — waiting 300s`);
    sleepSec(300);
  }
}

function waitForMemory(sid) {
  const t0 = Date.now();
  for (;;) {
    const a = availMB();
    if (a === null || a >= MEM_FLOOR_MB) return;
    if ((Date.now() - t0) / 1000 >= MEM_WAIT_MAX_S) { log(`⚠️ memory still ${a}MB < ${MEM_FLOOR_MB}MB — spawning ${sid} anyway`); return; }
    sleepSec(15);
  }
}

const done = new Set(RESUME && fs.existsSync(CKPT)
  ? fs.readFileSync(CKPT, 'utf8').split('\n').filter(Boolean) : []);

if (WAIT_FLEET) waitForFleet();

const env = { ...process.env, PYTHONPATH: 'src' };
let ok = 0, fail = 0;
for (const sid of IDS) {
  if (done.has(sid)) { log(`skip ${sid} (checkpointed)`); continue; }
  waitForMemory(sid);
  const s0 = Date.now();
  const r = spawnSync('nice', ['-n', '19', 'python3', '-m', 'backtest.unified_backtest', '--strategy-id', sid],
    { cwd: ROOT, env, timeout: PER_TIMEOUT_S * 1000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  const mins = ((Date.now() - s0) / 60000).toFixed(1);
  const tail = ((r.stdout || '') + (r.stderr || '')).trim().split('\n').pop() || '';
  if (r.status === 0) {
    ok++; fs.appendFileSync(CKPT, sid + '\n');
    log(`ok   ${sid} (${mins}m) ${ok + fail}/${IDS.length} :: ${tail.slice(0, 160)}`);
  } else {
    fail++;
    log(`FAIL ${sid} (${mins}m) status=${r.status} signal=${r.signal || ''} :: ${tail.slice(0, 160)}`);
  }
}
log(`DONE ok=${ok} fail=${fail} of ${IDS.length}`);
process.exit(fail ? 1 : 0);
