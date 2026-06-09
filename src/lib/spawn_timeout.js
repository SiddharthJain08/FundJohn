'use strict';

/**
 * spawnWithTimeout — spawn a child process that CANNOT hang forever.
 *
 * The weekend research pipeline (saturday_brain) spawns paperhunter `claude-bin`
 * children (Phase 4 hunt) and python ingestors (Phase 2 sweep) with no upper
 * time bound. On 2026-06-06/07 a single wedged child blocked the whole run until
 * systemd SIGKILLed it at the 6h `TimeoutStartSec` ceiling (stalled at
 * phase=hunt). This helper bounds every spawn: if the child outlives `timeoutMs`
 * it is SIGKILLed and the promise settles with `timedOut: true`.
 *
 * Always resolves (never rejects) with a structured result so callers can apply
 * their own success/failure semantics:
 *   { code, signal, stdout, stderr, timedOut, error }
 *
 * @param {string} cmd
 * @param {string[]} args
 * @param {object} spawnOpts  passed straight to child_process.spawn
 * @param {{timeoutMs?: number, onTimeout?: function}} [ctl]
 */
function spawnWithTimeout(cmd, args, spawnOpts = {}, ctl = {}) {
  const { spawn } = require('child_process');
  const { timeoutMs, onTimeout } = ctl;
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(cmd, args, spawnOpts);
    } catch (err) {
      return resolve({ code: -1, signal: null, stdout: '', stderr: '', timedOut: false, error: err.message });
    }

    let stdout = '';
    let stderr = '';
    let timedOut = false;
    let settled = false;

    const timer = (timeoutMs && timeoutMs > 0)
      ? setTimeout(() => {
          timedOut = true;
          try { child.kill('SIGKILL'); } catch (_) { /* already gone */ }
          if (typeof onTimeout === 'function') { try { onTimeout(); } catch (_) {} }
        }, timeoutMs)
      : null;

    if (child.stdout) child.stdout.on('data', (d) => { stdout += d.toString(); });
    if (child.stderr) child.stderr.on('data', (d) => { stderr += d.toString(); });

    const finish = (code, signal, err) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      resolve({ code, signal, stdout, stderr, timedOut, error: err ? err.message : null });
    };

    child.on('close', (code, signal) => finish(code, signal, null));
    child.on('error', (err) => finish(-1, null, err));
  });
}

module.exports = { spawnWithTimeout };
