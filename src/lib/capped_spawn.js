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

function defaultMemoryMax() {
  const v = process.env.OPENCLAW_BACKTEST_MEMORY_MAX;
  return (v === undefined || v === null || String(v).trim() === '') ? DEFAULT_MEMORY_MAX : String(v).trim();
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

const _state = { probe: _realProbe, uid: () => process.getuid(), available: null, warned: false };

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
 * @param {{memoryMax?: string}} [opts]
 * @returns {{cmd: string, args: string[], capped: boolean, memoryMax: string|null}}
 */
function wrapCapped(cmd, args, opts = {}) {
  const cap = (opts.memoryMax === undefined) ? defaultMemoryMax() : String(opts.memoryMax || '').trim();
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
  },
};

module.exports = { wrapCapped, defaultMemoryMax, DEFAULT_MEMORY_MAX, _internals };
