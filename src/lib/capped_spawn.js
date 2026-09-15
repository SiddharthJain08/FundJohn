'use strict';
/**
 * capped_spawn.js — run a child command inside a MemoryMax-capped transient
 * systemd scope (2026-08-30).
 *
 * Why: johnbot (user-scope service, uid 0) spawns python for approval-job
 * backtests (research-orchestrator._spawnPython), backfills (backfill_runner)
 * and universe-shrink runs (staging_approver) with NO memory limit. On
 * 2026-08-30 01:41/01:48 UTC two such backtests (3.1 GB and 5.4 GB anon-RSS)
 * tripped the kernel's GLOBAL OOM killer on the 8 GB box. A transient scope
 * turns that into a contained kill of the one child.
 *
 * `systemd-run --scope` execs the command in place (verified: the python pid
 * equals the pid spawn() returned), so child.kill(), pid registration and
 * stdio piping in the callers are unaffected. The wrapper only applies when
 * (a) the cap is non-zero, (b) the process is root (the system manager lets
 * root create scopes; the research finisher runs as claudebot with no user
 * bus and stays inside its own 5G unit cgroup), and (c) a one-time probe
 * scope succeeds — otherwise the command passes through untouched, logged once.
 *
 * Cap: OPENCLAW_BACKTEST_MEMORY_MAX (systemd size string; default 4500M —
 * the 8 GB box carries ~3 GB of resident services; '0' disables).
 */
const { spawnSync } = require('child_process');

const DEFAULT_MEMORY_MAX = '4500M';

// QD E2 fix round 1 (2026-09-14): the cycle-step cap (OPENCLAW_STEP_MEMORY_MAX)
// lives in this same module so there is exactly one resolver for it — the JS
// twin of `_resolve_env_cap`/`DEFAULT_MEMORY_MAX` in src/lib/capped_spawn.py.
// Deliberately a distinct env var from OPENCLAW_BACKTEST_MEMORY_MAX above:
// the daily-cycle steps and the research/backtest children are tuned
// independently.
const DEFAULT_STEP_MEMORY_MAX = '4500M';

// A well-formed systemd size string: the bare '0' disable-the-cap sentinel,
// or digits followed by a K/M/G/T unit (case-insensitive). A bare non-zero
// number (e.g. '4500') is the footgun this guards against: systemd-run reads
// an unsuffixed MemoryMax= as raw BYTES, which would OOM-kill every step at
// spawn. Mirrors capped_spawn.py's `_CAP_RE`.
const _CAP_RE = /^(?:0|\d+[KMGT])$/i;

function defaultMemoryMax() {
  const v = process.env.OPENCLAW_BACKTEST_MEMORY_MAX;
  return (v === undefined || v === null || String(v).trim() === '') ? DEFAULT_MEMORY_MAX : String(v).trim();
}

// Validate a non-empty, non-'0' cap against `_CAP_RE`; malformed input falls
// back to `fallback` with a warning logged ONCE per process (shared budget
// across every caller — stepMemoryMax() and wrapCapped()'s own re-validation
// — so a malformed OPENCLAW_STEP_MEMORY_MAX doesn't warn twice just because
// it's read once directly and once more when threaded through wrapCapped).
function _validateCap(cap, fallback) {
  if (_CAP_RE.test(cap)) return cap;
  if (!_state.capWarned) {
    _state.capWarned = true;
    try {
      console.warn(`[capped_spawn] malformed cap=${JSON.stringify(cap)} (no unit suffix — ` +
        `systemd-run would read this as raw bytes); falling back to default ${fallback}`);
    } catch (_) { /* never let a logging failure break the wrap */ }
  }
  return fallback;
}

/**
 * The single resolver for OPENCLAW_STEP_MEMORY_MAX: empty/whitespace → the
 * step default; '0' → '0' (disabled, unchanged); anything else is validated
 * against `_CAP_RE`, falling back to the step default (+ one warning) if
 * malformed. JS twin of capped_spawn.py's `_resolve_env_cap`.
 */
function stepMemoryMax() {
  const v = process.env.OPENCLAW_STEP_MEMORY_MAX;
  if (v === undefined || v === null || String(v).trim() === '') return DEFAULT_STEP_MEMORY_MAX;
  const trimmed = String(v).trim();
  if (trimmed === '0') return '0';
  return _validateCap(trimmed, DEFAULT_STEP_MEMORY_MAX);
}

function _realProbe() {
  try {
    const r = spawnSync('systemd-run', ['--scope', '--collect', '--quiet', '-p', 'MemoryMax=64M', '--', '/bin/true'],
                        { timeout: 15_000, stdio: 'ignore' });
    return r.status === 0;
  } catch (_) {
    return false;
  }
}

const _state = { probe: _realProbe, uid: () => process.getuid(), available: null, warned: false, capWarned: false };

function _available() {
  if (_state.available === null) {
    const uid = typeof _state.uid === 'function' ? _state.uid() : _state.uid;
    _state.available = uid === 0 && Boolean(_state.probe());
    if (!_state.available && !_state.warned) {
      _state.warned = true;
      try { console.warn('[capped_spawn] transient scopes unavailable (uid=%s) — children run uncapped', uid); } catch (_) {}
    }
  }
  return _state.available;
}

/**
 * @param {string} cmd
 * @param {string[]} args
 * @param {{memoryMax?: string, fallback?: string}} [opts] `fallback` is the
 *   value a malformed `memoryMax` falls back to (default `DEFAULT_MEMORY_MAX`
 *   — the backtest/research default this module originally served). Callers
 *   working the step cap (daily_cycle_helpers.js, cron-schedule.js) pass
 *   `fallback: DEFAULT_STEP_MEMORY_MAX` so a malformed step cap can never
 *   silently resolve to the backtest default — the two knobs stay tunable
 *   independently even on the fallback path, per this module's own header.
 * @returns {{cmd: string, args: string[], capped: boolean, memoryMax: string|null}}
 */
function wrapCapped(cmd, args, opts = {}) {
  const fallback = opts.fallback || DEFAULT_MEMORY_MAX;
  let cap = (opts.memoryMax === undefined) ? defaultMemoryMax() : String(opts.memoryMax || '').trim();
  // Re-validate whatever cap we ended up with — an explicit opts.memoryMax
  // has not necessarily been through stepMemoryMax()'s validation, and
  // defaultMemoryMax() (OPENCLAW_BACKTEST_MEMORY_MAX) never validates
  // either. Mirrors capped_spawn.py's wrap_capped. '' / '0' are the
  // disable sentinel, not malformed input, so they skip validation.
  if (cap && cap !== '0') cap = _validateCap(cap, fallback);
  const plain = { cmd, args: [...args], capped: false, memoryMax: null };
  if (!cap || cap === '0') return plain;
  if (!_available()) return plain;
  return { cmd: 'systemd-run',
           args: ['--scope', '--collect', '--quiet', '-p', `MemoryMax=${cap}`, '--', cmd, ...args],
           capped: true, memoryMax: cap };
}

const _internals = {
  /** Test hook: override the probe and/or uid; no args = real probe, re-evaluated. */
  reset({ probe, uid } = {}) {
    _state.probe = probe || _realProbe;
    _state.uid = (uid === undefined) ? (() => process.getuid()) : uid;
    _state.available = null;
    _state.warned = false;
    _state.capWarned = false;
  },
};

module.exports = {
  wrapCapped, defaultMemoryMax, DEFAULT_MEMORY_MAX,
  stepMemoryMax, DEFAULT_STEP_MEMORY_MAX,
  _internals,
};
