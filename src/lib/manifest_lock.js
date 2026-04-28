'use strict';

/**
 * manifest_lock.js — cross-language file-locking + atomic-write helper for
 * src/strategies/manifest.json (and any other JSON file that gets written
 * by both Node and Python processes).
 *
 * Why this exists:
 *   manifest.json was getting corrupted in two distinct ways:
 *     1. Concurrent writers (lifecycle.py auto_backtest + saturday_brain.js
 *        _stage running in the same wall-clock second) producing interleaved
 *        bytes — the Python writer's `write_text()` was non-atomic, and even
 *        the JS writers' read-modify-write cycle suffered the lost-update
 *        problem when racing against Python writes.
 *     2. JSON nesting bugs from old code paths that did string-concat instead
 *        of object-merge — those are fixed but the corruption pattern looked
 *        the same as a torn write, so they were misdiagnosed.
 *
 * The fix has two parts (both required, both implemented here):
 *
 *   PART 1 — File-level mutex via O_EXCL lockfile.
 *     Both Node and Python use the SAME lockfile path and the SAME O_EXCL
 *     atomic-create semantics. Whichever process creates the lockfile first
 *     wins; others retry with backoff. Stale locks (holder process died) are
 *     detected via PID liveness check + a generous 60s lockfile mtime cutoff.
 *
 *   PART 2 — Atomic write via tmp + rename.
 *     Even with the lock held, the write itself uses `O_WRONLY | O_CREAT |
 *     O_TRUNC` on a sibling `.tmp` file, then `rename()` — which on POSIX
 *     filesystems is atomic. A crashed writer leaves a `.tmp` orphan but
 *     never a half-written manifest.json.
 *
 * Lock contract (between Node and Python):
 *   - Lockfile path = `<target>.lock`
 *   - Lockfile content = `<PID>\n<ISO timestamp>\n<actor tag>` (3 lines)
 *   - Acquire: `fs.openSync(lockfile, O_WRONLY | O_CREAT | O_EXCL)`
 *   - Release: `fs.unlinkSync(lockfile)`
 *   - Stale: lockfile mtime > 60s OR `kill -0 <pid>` returns ESRCH → force-unlink and retry
 *
 * Public API:
 *   await withManifestLock(targetPath, async (currentManifest) => {
 *     currentManifest.strategies['foo'] = bar;
 *     return currentManifest;        // helper writes back atomically
 *   }, { actor: 'saturday_brain' });
 *
 *   // Or use the lower-level acquire/release for fine-grained control:
 *   const release = await acquireLock(targetPath, { actor: '...' });
 *   try { ... } finally { release(); }
 */

const fs   = require('fs');
const path = require('path');

const LOCK_TIMEOUT_MS    = 60_000;   // stale-lock cutoff
const ACQUIRE_TIMEOUT_MS = 30_000;   // give up acquiring after 30s
const POLL_BASE_MS       = 25;       // initial poll interval
const POLL_MAX_MS        = 500;      // cap on backoff

function _lockPath(targetPath) {
  return targetPath + '.lock';
}

function _isProcessAlive(pid) {
  if (!pid || !Number.isFinite(pid) || pid <= 1) return false;
  try {
    // signal 0 = check existence + permissions, doesn't actually signal
    process.kill(pid, 0);
    return true;
  } catch (e) {
    if (e.code === 'EPERM') return true;   // exists, just not ours to signal
    return false;                          // ESRCH or anything else → dead
  }
}

function _readLockMeta(lockfile) {
  try {
    const stat = fs.statSync(lockfile);
    const txt  = fs.readFileSync(lockfile, 'utf8');
    const [pidStr, ts, actor] = txt.split('\n');
    return { pid: parseInt(pidStr, 10), ts, actor: actor || '?', mtimeMs: stat.mtimeMs };
  } catch (_) { return null; }
}

function _maybeClearStaleLock(lockfile) {
  const meta = _readLockMeta(lockfile);
  if (!meta) return false;     // lockfile vanished
  const ageMs = Date.now() - meta.mtimeMs;
  const stale = ageMs > LOCK_TIMEOUT_MS || !_isProcessAlive(meta.pid);
  if (!stale) return false;
  try {
    fs.unlinkSync(lockfile);
    process.stderr.write(
      `[manifest_lock] cleared stale lock at ${lockfile} ` +
      `(age=${Math.round(ageMs / 1000)}s, pid=${meta.pid}/${meta.actor}, ` +
      `alive=${_isProcessAlive(meta.pid)})\n`
    );
    return true;
  } catch (_) { return false; }
}

function _sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

/**
 * Acquire the lock. Returns a release function. Throws on timeout.
 *
 * @param {string} targetPath  the file being protected (lockfile is
 *                             targetPath + '.lock')
 * @param {object} opts
 *   - actor: string identifying who's holding (logs only, default 'unknown')
 *   - timeoutMs: max wait for acquire (default 30000)
 */
async function acquireLock(targetPath, opts = {}) {
  const lockfile = _lockPath(targetPath);
  const actor    = opts.actor    || 'unknown';
  const deadline = Date.now() + (opts.timeoutMs || ACQUIRE_TIMEOUT_MS);
  let backoff = POLL_BASE_MS;

  // Ensure target dir exists (lockfile dir == target dir).
  try { fs.mkdirSync(path.dirname(lockfile), { recursive: true }); } catch (_) {}

  while (true) {
    let fd;
    try {
      fd = fs.openSync(lockfile, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL, 0o600);
      const payload = `${process.pid}\n${new Date().toISOString()}\n${actor}\n`;
      fs.writeSync(fd, payload);
      fs.closeSync(fd);
      // Success.
      return function release() {
        try { fs.unlinkSync(lockfile); } catch (_) {}
      };
    } catch (e) {
      if (e.code !== 'EEXIST') throw e;
      // Held by someone else — check if stale.
      _maybeClearStaleLock(lockfile);
      if (Date.now() > deadline) {
        const meta = _readLockMeta(lockfile);
        const heldBy = meta ? `pid=${meta.pid} actor=${meta.actor} ts=${meta.ts}` : 'unknown';
        throw new Error(`manifest_lock acquire timed out after ${opts.timeoutMs || ACQUIRE_TIMEOUT_MS}ms; held by ${heldBy}`);
      }
      await _sleep(backoff);
      backoff = Math.min(backoff * 2, POLL_MAX_MS);
    }
  }
}

/**
 * High-level wrapper. Reads the manifest under lock, passes it to the
 * callback, writes the (possibly mutated) result back atomically. The
 * callback may return a NEW manifest object or mutate the passed one in
 * place (and return undefined / the same reference).
 *
 * @param {string}   targetPath
 * @param {function} cb         async (manifest) => manifest|undefined
 * @param {object}   opts       { actor, timeoutMs, encoding ('utf8') }
 * @returns whatever the callback returned (after the write completes)
 */
async function withManifestLock(targetPath, cb, opts = {}) {
  const release = await acquireLock(targetPath, opts);
  try {
    const encoding = opts.encoding || 'utf8';
    let raw;
    try {
      raw = fs.readFileSync(targetPath, encoding);
    } catch (e) {
      if (e.code === 'ENOENT') raw = '{}';   // first-write
      else throw e;
    }
    let manifest;
    try {
      manifest = JSON.parse(raw);
    } catch (e) {
      throw new Error(`manifest_lock: target ${targetPath} is not valid JSON — ${e.message}`);
    }
    const result = await cb(manifest);
    const toWrite = (result === undefined) ? manifest : result;

    const tmp = targetPath + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify(toWrite, null, 2), { encoding });
    fs.renameSync(tmp, targetPath);
    return toWrite;
  } finally {
    release();
  }
}

/**
 * Synchronous read under lock. Useful for callers that just want a coherent
 * snapshot without the read-modify-write cycle (e.g. server.js fetching the
 * manifest for an /api/strategies response — though that endpoint can also
 * just do a plain readFileSync since concurrent writes are atomic).
 */
async function readManifestUnderLock(targetPath, opts = {}) {
  const release = await acquireLock(targetPath, opts);
  try {
    return JSON.parse(fs.readFileSync(targetPath, opts.encoding || 'utf8'));
  } finally {
    release();
  }
}

module.exports = {
  acquireLock,
  withManifestLock,
  readManifestUnderLock,
  _internals: { _isProcessAlive, _readLockMeta, _maybeClearStaleLock, _lockPath },
};
