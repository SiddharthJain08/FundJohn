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

function runAlpaca(args, { timeout = 30_000, env } = {}) {
  return new Promise((resolve) => {
    let stdout = '';
    let stderr = '';
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
        stdout, stderr,
        payload,
        error: errJson,
      });
    });
  });
}

module.exports = { runAlpaca, ALPACA_CLI };
