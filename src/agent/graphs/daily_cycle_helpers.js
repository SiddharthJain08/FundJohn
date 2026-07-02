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
        stderrTail: stderr.slice(-4000),
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

// ── Abort alerting ───────────────────────────────────────────────────────────
// When the daily cycle aborts at a step, the `health` step (which posts the daily
// digest) never runs, so without this the failure is silent. Surface every abort to
// #botjohn-log; flag the SIGKILL/OOM (rc=137) class explicitly. The 2026-06-22/23
// EOD collect OOM (rc=137) went unnoticed for 2 days because of this gap.

const _RC_HINTS = {
  137: 'SIGKILL — almost always OOM (rc=137)',
  139: 'SIGSEGV (rc=139)',
  124: 'timed out (rc=124)',
};

function formatAbortAlert({ runDate, runId, abortedAt, lastError, reason } = {}) {
  const rc = lastError && lastError.rc != null ? lastError.rc : null;
  const rcHint = rc != null ? `\n• ${_RC_HINTS[rc] || `exit code ${rc}`}` : '';
  const message = lastError && lastError.message ? String(lastError.message).slice(0, 300) : '(no message)';
  return [
    `🚨 **Daily cycle ABORTED** — ${runDate} (step \`${abortedAt}\`)`,
    `• run \`${runId}\`${reason ? `  ·  reason: ${reason}` : ''}`,
    `• error: ${message}${rcHint}`,
    `• no signals/trades were produced for this run — book left unchanged.`,
  ].join('\n');
}

// Bounded DB lookup — connect/query time-capped so a memory-thrashing box (the very
// condition that triggers an rc=137 abort) can't hang the lookup indefinitely.
async function _getBotjohnLogWebhook() {
  const { Client } = require('pg');
  const client = new Client({
    connectionString: process.env.POSTGRES_URI,
    connectionTimeoutMillis: 4000,
    query_timeout: 4000,
  });
  await client.connect();
  try {
    const r = await client.query('SELECT webhook_urls FROM agent_registry WHERE id=$1', ['botjohn']);
    return ((r.rows[0] && r.rows[0].webhook_urls) || {})['botjohn-log'] || null;
  } finally {
    await client.end().catch(() => {});
  }
}

function _postWebhook(url, content) {
  const https = require('node:https');
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ content: String(content).slice(0, 1900) });
    const u = new URL(url);
    const req = https.request(
      { hostname: u.hostname, path: u.pathname + u.search, method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
          // Identify so Cloudflare's bot filter doesn't 403 us (matches _discord_webhook.js).
          'User-Agent': 'OpenClaw-Curator/1.0 (+botjohn)',
        } },
      (res) => { res.on('data', () => {}); res.on('end', () => resolve({ status: res.statusCode })); },
    );
    req.setTimeout(4000, () => req.destroy(new Error('webhook POST timeout')));
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

/**
 * Post an abort alert to #botjohn-log. Fail-soft AND non-blocking: never throws,
 * never masks the original abort, and always resolves within an overall timeout
 * (deps.timeoutMs, default 6s) even if the DB lookup or webhook POST hangs — the
 * abort fires because the box is under memory pressure, so a hung handler is a real
 * risk. `deps` allows injecting getWebhook/postWebhook/timeoutMs for testing.
 */
async function postAbortAlert(payload, deps = {}) {
  const getWebhook  = deps.getWebhook  || _getBotjohnLogWebhook;
  const postWebhook = deps.postWebhook || _postWebhook;
  const timeoutMs   = deps.timeoutMs   || 6000;

  const work = (async () => {
    try {
      const url = await getWebhook();
      if (!url) {
        console.warn('[daily-cycle] no #botjohn-log webhook in agent_registry — abort alert not sent');
        return { sent: false, reason: 'no_webhook' };
      }
      const r = await postWebhook(url, formatAbortAlert(payload));
      return { sent: true, status: r && r.status };
    } catch (e) {
      console.error(`[daily-cycle] abort alert failed (non-fatal): ${e.message}`);
      return { sent: false, reason: 'error', error: e.message };
    }
  })();

  let timer;
  const timeout = new Promise((resolve) => {
    timer = setTimeout(() => {
      console.error('[daily-cycle] abort alert timed out (non-fatal)');
      resolve({ sent: false, reason: 'timeout' });
    }, timeoutMs);
  });
  try {
    // work always settles within ~4s (per-op connect/request timeouts above); the
    // overall timeout is the backstop. Clear it when work wins so it doesn't hold
    // the event loop open for the rest of timeoutMs.
    return await Promise.race([work, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

module.exports = {
  skipForSubset, strictMode, runSubprocess,
  formatAbortAlert, postAbortAlert,
};
