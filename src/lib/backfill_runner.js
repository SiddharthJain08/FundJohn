'use strict';

/**
 * backfill_runner.js — thin Node wrapper that invokes the canonical Python
 * `backfill_one_request(request_id)` helper from
 * src/pipeline/backfillers/__init__.py.
 *
 * Used by the fused staging-approval worker (src/agent/approvals/staging_approver.js)
 * to perform inline backfill of a single data_ingestion_queue row without
 * having to wait for the next daily queue_drain cycle.
 *
 * The Python side handles all the row updates (status='running' →
 * 'complete' | 'failed', wired_at, rows_backfilled) and registers the column
 * in data_columns so the next collector cycle picks it up automatically.
 */

const { spawn } = require('child_process');
const path      = require('path');

const OPENCLAW_DIR = path.resolve(__dirname, '..', '..');

/**
 * Run one backfill. Returns the JSON result emitted by the Python helper.
 *
 * @param {string} requestId        UUID of the data_ingestion_queue row.
 * @param {object} [opts]
 * @param {boolean} [opts.dryRun]   pass --dry-run through.
 * @param {number}  [opts.timeoutMs] kill child after this many ms (default 30 min).
 * @param {(child:ChildProcess)=>void} [opts.onChild] register the spawned child for cancellation.
 */
function runBackfill(requestId, opts = {}) {
  const { dryRun = false, timeoutMs = 30 * 60 * 1000, onChild } = opts;

  return new Promise((resolve) => {
    const args = ['-m', 'src.pipeline.backfillers', String(requestId)];
    if (dryRun) args.push('--dry-run');

    const child = spawn('python3', args, {
      cwd: OPENCLAW_DIR,
      env: { ...process.env, PYTHONPATH: OPENCLAW_DIR },
    });
    if (typeof onChild === 'function') {
      try { onChild(child); } catch (_) {}
    }

    let stdout = '';
    let stderr = '';
    // Capture both streams AND forward them line-by-line to johnbot's
    // stderr so the FMP/yfinance/EDGAR backfillers' per-ticker progress
    // prints — `[fmp] backfilling financials: 475 tickers × 21 quarters`,
    // `[fmp] progress: 50/475 tickers, 1247 rows so far` — show up in
    // journalctl while the worker runs. Final-line JSON parse still works
    // because the Python helper emits exactly one JSON line at the end.
    const tag = `[backfill ${String(requestId).slice(0, 8)}]`;
    const lineForwarder = (prefix) => {
      let buf = '';
      return (chunk) => {
        buf += chunk.toString();
        let idx;
        while ((idx = buf.indexOf('\n')) !== -1) {
          const line = buf.slice(0, idx);
          if (line.length) process.stderr.write(`${prefix} ${tag} ${line}\n`);
          buf = buf.slice(idx + 1);
        }
      };
    };
    const onStdoutLine = lineForwarder('[bf-out]');
    const onStderrLine = lineForwarder('[bf-err]');
    child.stdout.on('data', (d) => { stdout += d.toString(); onStdoutLine(d); });
    child.stderr.on('data', (d) => { stderr += d.toString(); onStderrLine(d); });

    const killTimer = setTimeout(() => {
      try { child.kill('SIGTERM'); } catch (_) {}
      setTimeout(() => { try { child.kill('SIGKILL'); } catch (_) {} }, 3_000);
    }, timeoutMs);

    child.on('exit', (code) => {
      clearTimeout(killTimer);
      // Python helper prints exactly one JSON line on success or failure.
      // Take the LAST non-blank line so warning prints from imported modules
      // don't break parsing.
      const lines = stdout.split('\n').map(s => s.trim()).filter(Boolean);
      const lastLine = lines[lines.length - 1];
      let parsed = null;
      if (lastLine) {
        try { parsed = JSON.parse(lastLine); } catch (_) { /* not JSON */ }
      }
      if (parsed && typeof parsed === 'object') {
        resolve(parsed);
        return;
      }
      resolve({
        ok:           false,
        request_id:   String(requestId),
        column_name:  null,
        provider:     null,
        rows_written: 0,
        elapsed_s:    0,
        from_date:    null,
        to_date:      null,
        error:        `python exit=${code}; stderr: ${stderr.slice(0, 400)}; stdout: ${stdout.slice(0, 400)}`,
      });
    });

    child.on('error', (err) => {
      clearTimeout(killTimer);
      resolve({
        ok:           false,
        request_id:   String(requestId),
        column_name:  null,
        provider:     null,
        rows_written: 0,
        elapsed_s:    0,
        from_date:    null,
        to_date:      null,
        error:        `spawn error: ${err.message}`,
      });
    });
  });
}

module.exports = { runBackfill };
