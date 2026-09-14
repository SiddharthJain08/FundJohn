'use strict';
/**
 * run_lock.js — JS twin of src/lib/run_lock.py (QD spec §5 E1, 2026-09-12).
 *
 * Both files read src/lib/run_lock_key.json, so the key can never drift
 * between the Python orchestrator and the LangGraph daily cycle. Value
 * format is byte-identical: `host:pid:start_iso`.
 *
 * Takeover is an atomic Lua GET+SET compare-and-swap (`eval`, mirroring
 * run_lock.py's `_TAKEOVER_CAS_SCRIPT`), not a blind overwrite: it writes
 * the new value only if the key's current value still equals the exact
 * dead-holder value we observed. Without this, two same-host racers who
 * both see the same dead-PID holder could both "win" the takeover. A lock
 * may be taken over ONLY when its holder host equals ours AND the holder
 * PID is dead (same PID-liveness pattern as src/lib/manifest_lock.js
 * `_isProcessAlive`). A holder on another host is never touched.
 *
 * Every renew and release compares the stored value to ours first — a
 * process whose lock was taken over must not delete or renew the new
 * owner's key. `acquire` and `renew` propagate Redis errors (a lock
 * decision must fail closed); `release` swallows them (it typically runs
 * from a `finally` and must never mask the run's real exit status).
 */
const os  = require('node:os');
const CFG = require('./run_lock_key.json');

const KEY_PREFIX        = String(CFG.prefix);
const TTL_SLACK_SECONDS = Number(CFG.ttl_slack_seconds);
const MIN_TTL_SECONDS   = Number(CFG.min_ttl_seconds);
const LOCK_BUSY_RC      = 75;   // EX_TEMPFAIL — mirrors run_lock.py

// Compare-and-swap used only for takeover: write `new` (with `ttl`) iff the
// key's current value still equals the dead-PID value we observed via GET.
// Atomic in real Redis (single EVAL); the JS test suite's FakeRedis emulates
// this exact script (see tests/lib/run_lock.test.js), same as the Python
// twin's FakeRedis (tests/lib/test_run_lock.py).
const _TAKEOVER_CAS_SCRIPT = (
  "if redis.call('GET', KEYS[1]) == ARGV[1] then "
  + "return redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3]) "
  + 'else return nil end'
);

function lockKey(runDate) { return `${KEY_PREFIX}:${runDate}`; }

function _text(raw) {
  if (raw == null) return null;
  return Buffer.isBuffer(raw) ? raw.toString('utf8') : String(raw);
}

function makeValue({ pid, host, startedAt } = {}) {
  const h = host || os.hostname();
  const p = pid == null ? process.pid : Number(pid);
  const t = startedAt || new Date().toISOString();
  return `${h}:${p}:${t}`;
}

function parseValue(raw) {
  const s = _text(raw);
  if (!s) return null;
  // Split on the FIRST TWO colons only — the ISO timestamp contains colons.
  const i = s.indexOf(':');
  if (i < 1) return null;
  const j = s.indexOf(':', i + 1);
  if (j < 0) return null;
  const host = s.slice(0, i);
  const pid  = Number(s.slice(i + 1, j));
  const startedAt = s.slice(j + 1);
  if (!host || !startedAt || !Number.isInteger(pid)) return null;
  return { host, pid, startedAt };
}

function ttlFor(stepTimeoutSec) {
  const base = Number.isFinite(Number(stepTimeoutSec)) ? Number(stepTimeoutSec) : 0;
  return Math.max(MIN_TTL_SECONDS, Math.trunc(base) + TTL_SLACK_SECONDS);
}

function pidAlive(pid) {
  const p = Number(pid);
  if (!Number.isInteger(p) || p <= 1) return false;
  try { process.kill(p, 0); return true; }
  catch (e) { return e.code === 'EPERM'; }
}

/**
 * Try to own `runDate`. Returns `{ok, value, holder}`: `ok=true` on
 * acquire or takeover (`value` is what we now hold, `holder` is null);
 * `ok=false` means still busy (`holder` is the raw value still in Redis,
 * possibly null in the vanishingly unlikely case it disappeared again
 * after our one retry).
 *
 * Propagates whatever the Redis client raises: acquiring must fail closed.
 */
async function acquire(r, runDate, ttlSec, opts = {}) {
  const key   = lockKey(runDate);
  const host  = opts.host || os.hostname();
  const value = opts.value || makeValue({ host });
  const ttl   = Math.trunc(Number(ttlSec));
  const log   = opts.log || (() => {});

  if (await r.set(key, value, 'NX', 'EX', ttl)) return { ok: true, value, holder: null };

  let holderRaw = await r.get(key);
  if (holderRaw == null) {
    // The key vanished between our SET NX (which found it present) and
    // this GET — its TTL lapsed, or its owner released it. That is "free",
    // not "busy": retry the NX once rather than reporting a live holder
    // that no longer exists.
    if (await r.set(key, value, 'NX', 'EX', ttl)) return { ok: true, value, holder: null };
    holderRaw = await r.get(key);
  }

  const holder = _text(holderRaw);
  const parsed = parseValue(holder);
  if (!parsed) return { ok: false, value: null, holder };

  if (parsed.host === host && !pidAlive(parsed.pid)) {
    log(`[lock] stale lock from pid ${parsed.pid} (host ${parsed.host}, started ${parsed.startedAt}) — taking over ${key}`);
    const took = await r.eval(_TAKEOVER_CAS_SCRIPT, 1, key, holder, value, ttl);
    if (took) return { ok: true, value, holder: null };
    // Someone else's CAS (another same-host racer, or the real owner
    // renewing) won the gap between our GET and this eval. We lost the
    // race cleanly — report whoever holds it now.
    return { ok: false, value: null, holder: _text(await r.get(key)) };
  }
  return { ok: false, value: null, holder };
}

/**
 * Extend OUR lock's TTL. False (and no write) if someone else owns it.
 *
 * Propagates whatever the Redis client raises: renewing must fail closed —
 * the caller must treat an exception here as "we no longer know we hold
 * the lock," never assume the run is still exclusive just because renew
 * did not explicitly return false.
 */
async function renew(r, runDate, value, ttlSec, opts = {}) {
  if (!value) return false;
  const key = lockKey(runDate);
  const ttl = Math.trunc(Number(ttlSec));
  const log = opts.log || (() => {});

  let cur = _text(await r.get(key));
  if (cur == null) {
    // Expired under us (a step outran its own TTL). NX makes the re-take
    // conditional on the key still being absent: if another launcher's
    // acquire() won the same gap, its write stands and we fall through to
    // "held by someone else" below instead of clobbering it.
    if (await r.set(key, value, 'NX', 'EX', ttl)) {
      log(`[lock] ${key} had expired — re-taken by ${value} (ttl=${ttl}s)`);
      return true;
    }
    cur = _text(await r.get(key));
  }
  if (cur !== value) {
    log(`[lock] renew skipped — ${key} held by ${cur}, not us (${value})`);
    return false;
  }
  return Boolean(await r.expire(key, ttl));
}

/**
 * Delete OUR lock. Never deletes a lock another owner took over.
 *
 * Swallows any Redis error instead of raising: release runs on the way
 * out (typically a `finally`), and a network hiccup here must never mask
 * or replace the run's real exit status. Logs and reports false instead.
 */
async function release(r, runDate, value, opts = {}) {
  if (!value) return false;
  const key = lockKey(runDate);
  const log = opts.log || (() => {});
  try {
    const cur = _text(await r.get(key));
    if (cur == null) return false;
    if (cur !== value) {
      log(`[lock] release skipped — ${key} held by ${cur}, not us (${value})`);
      return false;
    }
    await r.del(key);
    return true;
  } catch (exc) {
    log(`[lock] release of ${key} failed: ${exc && exc.message ? exc.message : exc}`);
    return false;
  }
}

// ── Current-lock handle (QD E1 controller ruling, 2026-09-14) ────────────────
// cron-schedule.js routes PRODUCTION through this LangGraph twin whenever
// OPENCLAW_LANGGRAPH_ORCHESTRATOR=1 (the default in prod .env) — the plan's
// original premise that the Python orchestrator alone owns production, and
// that this twin need only acquire once at the longest-step TTL, was wrong.
// This twin must now renew before every step and treat a lost lock as fatal,
// exactly like pipeline_orchestrator.py's `_renew_or_lose` / `LockLost`.
//
// One process runs at most one daily-cycle lock at a time in practice (the
// lock itself prevents two overlapping runs for the same date, and
// cron-schedule.js only ever dispatches one daily-cycle run at a time) — a
// single module-level handle is the smallest mechanism that lets
// `daily_cycle_helpers.runSubprocess`, several call-frames away from
// `_acquireRunLock`, renew without the lock being threaded through
// LangGraph state.
let _current = null; // { r, runDate, value }

function setCurrent({ r, runDate, value }) {
  _current = { r, runDate, value };
}

function clearCurrent() {
  _current = null;
}

/**
 * Renew the CURRENT process-held lock (set via setCurrent) for one step.
 * Returns true (no-op) when no lock is held — preserves today's behavior
 * for tests and one-off runs that never call setCurrent().
 *
 * A clean `false` from `renew()` (value mismatch — another owner now holds
 * it) is definitive loss, reported immediately. A THROWN exception (a
 * transient Redis hiccup) gets exactly one retry after `retryDelayMs`
 * (default 2000ms; overridable only for tests) before also counting as
 * lost. This is deliberately stricter than pipeline_orchestrator.py, which
 * logs a renew *exception* and lets the step proceed anyway: that Python
 * path renews from inside run_step's own retry loop with a human operator
 * and `doctor.py` watching the same box, whereas this twin's renew happens
 * several call-frames away from the acquire, with no other safety net if a
 * step is allowed to run while we can no longer prove we hold the lock.
 */
async function renewCurrent(stepTimeoutSec, opts = {}) {
  if (!_current) return true;
  const { r, runDate, value } = _current;
  const ttl = ttlFor(stepTimeoutSec);
  const log = opts.log || (() => {});
  const retryDelayMs = opts.retryDelayMs == null ? 2000 : opts.retryDelayMs;
  try {
    return await renew(r, runDate, value, ttl, { log });
  } catch (e) {
    log(`[lock] renew failed (${e && e.message ? e.message : e}) — retrying once in ${retryDelayMs}ms`);
    await new Promise((resolve) => setTimeout(resolve, retryDelayMs));
    try {
      return await renew(r, runDate, value, ttl, { log });
    } catch (e2) {
      log(`[lock] renew failed again — treating the lock as lost: ${e2 && e2.message ? e2.message : e2}`);
      return false;
    }
  }
}

module.exports = {
  KEY_PREFIX, TTL_SLACK_SECONDS, MIN_TTL_SECONDS, LOCK_BUSY_RC,
  lockKey, makeValue, parseValue, ttlFor, pidAlive,
  acquire, renew, release,
  setCurrent, clearCurrent, renewCurrent,
};
