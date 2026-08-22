'use strict';

/**
 * alpaca_cli.js — thin Node wrapper around the alpaca CLI subprocess.
 *
 * Used by server.js portfolio + watchlist endpoints (Phase 1.2 / 2.5 of
 * the alpaca CLI integration) and by collector.js options + corporate
 * actions phases (Phase 2.1 / 2.3). All callers go through this so the
 * binary path, stdout-JSON-default convention, and stderr-error envelope
 * are decoded once.
 *
 * The CLI returns JSON on stdout for success (exit 0) and a JSON error
 * envelope on stderr for failures (non-zero exit). Common error fields
 * inside the envelope: `status` (HTTP code), `error` (string), `code`
 * (numeric), `path`, `request_id`. There is no `--json` flag — JSON is
 * the default output.
 */

const { spawn } = require('child_process');

const ALPACA_CLI = process.env.ALPACA_CLI_BIN || '/root/go/bin/alpaca';

/**
 * Global flags appended to every invocation (github.com/alpacahq/cli):
 *   --quiet    keeps stderr a pure JSON error document — without it the CLI
 *              prefixes "Rate limited, retrying in …" lines that break
 *              JSON.parse(stderr) and hide `error.status` from callers.
 *   --timeout  bounds the CLI's own HTTP timeout (default 30s) to just under
 *              our subprocess timeout, so a slow broker yields a structured
 *              error instead of a SIGKILL with empty stderr.
 * Callers that already pass either flag keep their value.
 */
function cliGlobalFlags(args, timeoutMs) {
  const flags = [];
  if (!args.includes('--quiet') && !args.includes('-q')) flags.push('--quiet');
  if (!args.includes('--timeout')) {
    flags.push('--timeout', String(Math.max(1, Math.floor(timeoutMs / 1000) - 1)));
  }
  return flags;
}

function runAlpaca(args, { timeout = 30_000, env } = {}) {
  return new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
    args = [...args, ...cliGlobalFlags(args, timeout)];
    const proc = spawn(ALPACA_CLI, args, {
      env: env || process.env,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    const timer = setTimeout(() => {
      try { proc.kill('SIGKILL'); } catch (_) {}
      resolve({
        ok: false, exit_code: -1,
        stdout: '', stderr: 'cli timeout',
        payload: null,
        error: { error: 'cli timeout', status: null },
      });
    }, timeout);
    proc.stdout.on('data', (c) => { stdout += c; });
    proc.stderr.on('data', (c) => { stderr += c; });
    proc.on('error', (err) => {
      clearTimeout(timer);
      resolve({
        ok: false, exit_code: -1,
        stdout, stderr: err.message,
        payload: null,
        error: { error: err.message, status: null },
      });
    });
    proc.on('close', (code) => {
      clearTimeout(timer);
      let payload = null;
      let errJson = null;
      if (code === 0 && stdout) {
        try { payload = JSON.parse(stdout); } catch (_) { payload = stdout; }
      } else if (code !== 0 && stderr) {
        try { errJson = JSON.parse(stderr); } catch (_) {}
      }
      resolve({
        ok: code === 0,
        exit_code: code,
        // CLI contract: 0 = success, 1 = API/general error, 2 = auth error.
        // rc=2 means credentials are missing/invalid — never retry, surface it.
        auth_error: code === 2,
        stdout, stderr,
        payload,
        error: errJson,
      });
    });
  });
}

module.exports = { runAlpaca, cliGlobalFlags, ALPACA_CLI };
