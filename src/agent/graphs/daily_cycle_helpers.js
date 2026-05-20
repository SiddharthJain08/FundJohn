/**
 * Shared helpers for the 11 daily-cycle step nodes.
 *
 *   skipForSubset(step, state) → true if state.requestedSteps excludes this step
 *   strictMode(env)            → boolean from OPENCLAW_STRICT_EXIT_CODES
 *   runSubprocess(argv, opts)  → Promise<{rc, stdout, stderrTail, durationMs, timedOut}>
 */
'use strict';

const { spawn } = require('node:child_process');

function skipForSubset(step, state) {
  if (!state || !state.requestedSteps) return false;
  const req = state.requestedSteps;
  // Accept Set or Array for hand-coded resilience
  if (req instanceof Set) return !req.has(step);
  if (Array.isArray(req)) return !req.includes(step);
  return false;
}

function strictMode(env) {
  return (env && env.OPENCLAW_STRICT_EXIT_CODES) === '1';
}

function runSubprocess(argv, { timeoutSec = 600, env = process.env, cwd } = {}) {
  return new Promise((resolve) => {
    const startedAt = Date.now();
    const [cmd, ...args] = argv;
    let stdout = '';
    let stderr = '';
    let timedOut = false;

    const proc = spawn(cmd, args, {
      env,
      cwd: cwd || process.cwd(),
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    proc.stdout.on('data', (b) => { stdout += b.toString(); });
    proc.stderr.on('data', (b) => { stderr += b.toString(); });

    const timer = setTimeout(() => {
      timedOut = true;
      try { proc.kill('SIGTERM'); } catch {}
      // Hard-kill after 5s if it doesn't exit
      setTimeout(() => { try { proc.kill('SIGKILL'); } catch {} }, 5000);
    }, timeoutSec * 1000);

    proc.on('close', (code, signal) => {
      clearTimeout(timer);
      const durationMs = Date.now() - startedAt;
      const rc = timedOut ? 124 : (code === null ? (signal ? 137 : 1) : code);
      resolve({
        rc,
        stdout,
        stderrTail: stderr.slice(-1000),
        durationMs,
        timedOut,
      });
    });

    proc.on('error', (e) => {
      clearTimeout(timer);
      resolve({
        rc: 127,
        stdout: '',
        stderrTail: `spawn failed: ${e.message}`,
        durationMs: Date.now() - startedAt,
        timedOut: false,
      });
    });
  });
}

module.exports = { skipForSubset, strictMode, runSubprocess };
