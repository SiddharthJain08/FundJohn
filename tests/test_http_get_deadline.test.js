'use strict';

/**
 * tests/test_http_get_deadline.test.js
 *
 * Verifies that the collector's `httpGet` (src/pipeline/collector.js)
 * cannot wedge forever on the two failure modes that caused the
 * 2026-04-29 cycle to stall in Phase 3 (options) for 30+ minutes:
 *
 *   1. Mid-stream RST → response stream emits 'error' → must reject.
 *   2. Server holds connection open silently → hard request-deadline
 *      must fire and reject.
 *
 * The test spins up a tiny HTTPS server on a random port using a
 * self-signed cert and points httpGet at it via NODE_TLS_REJECT_UNAUTHORIZED.
 *
 * Run:
 *   node --test tests/test_http_get_deadline.test.js
 */

process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';
process.env.HTTP_REQUEST_DEADLINE_MS = '1500';   // tight deadline for test speed
process.env.OPENCLAW_NO_HTTP_LISTEN = '1';
process.env.POSTGRES_URI = process.env.POSTGRES_URI || 'postgresql://x:y@localhost:5432/x';

const { test } = require('node:test');
const assert    = require('node:assert/strict');
const https     = require('https');
const tls       = require('tls');
const { execSync } = require('child_process');
const fs        = require('fs');
const path      = require('path');
const os        = require('os');

// Generate a one-shot self-signed cert in /tmp so we don't need openssl
// pre-installed in CI envs that already have it.
function makeSelfSignedCert() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'httptest-'));
  const key = path.join(dir, 'k.pem');
  const crt = path.join(dir, 'c.pem');
  execSync(
    `openssl req -x509 -newkey rsa:2048 -keyout ${key} -out ${crt} ` +
    `-days 1 -nodes -subj "/CN=localhost" -batch`,
    { stdio: 'ignore' },
  );
  return { key: fs.readFileSync(key), cert: fs.readFileSync(crt) };
}

function startServer(handler) {
  const { key, cert } = makeSelfSignedCert();
  return new Promise((resolve) => {
    const srv = https.createServer({ key, cert }, handler);
    srv.listen(0, '127.0.0.1', () => {
      resolve({ srv, port: srv.address().port });
    });
  });
}

// Pull the (private) httpGet out of collector.js by requiring the module
// — it's not exported, so we re-implement minimal harness via the
// snapshot endpoint shape. Easiest path: monkey-patch require cache so
// `httpGet` is reachable. Instead, just exercise the same behavior with
// a thin re-import of the function via VM.
const collectorSrc = fs.readFileSync(
  path.join(__dirname, '..', 'src', 'pipeline', 'collector.js'), 'utf8',
);
// Extract just the httpGet block + its dependency (HTTP_REQUEST_DEADLINE_MS).
// The function is ~50 lines; we wrap it in a sandbox to test in isolation.
const m = collectorSrc.match(
  /const HTTP_REQUEST_DEADLINE_MS[\s\S]*?async function httpGet[\s\S]*?\n\}\n/m,
);
assert.ok(m, 'failed to extract httpGet from collector.js');
// eslint-disable-next-line no-new-func
const factory = new Function('https', 'process', `${m[0]}; return httpGet;`);
const httpGet = factory(https, process);

test('httpGet rejects when server RSTs the response mid-stream', async () => {
  const { srv, port } = await startServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.write('{"results":');
    // Half the JSON, then violently kill the socket — simulates a
    // Polygon connection RST mid-body. Without `res.on('error', reject)`
    // in httpGet, the await pends forever.
    setTimeout(() => req.socket.destroy(), 50);
  });
  try {
    const t0 = Date.now();
    await assert.rejects(
      httpGet(`https://127.0.0.1:${port}/`),
      /(?:socket hang up|ECONNRESET|aborted|JSON parse error|Request deadline)/,
    );
    const elapsed = Date.now() - t0;
    assert.ok(elapsed < 3000, `should reject quickly, took ${elapsed}ms`);
  } finally {
    srv.close();
  }
});

test('httpGet rejects via deadline when server holds the response open silently', async () => {
  const { srv, port } = await startServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.write('{');
    // Never write anything else, never close — would wedge forever
    // without HTTP_REQUEST_DEADLINE_MS.
  });
  try {
    const t0 = Date.now();
    await assert.rejects(
      httpGet(`https://127.0.0.1:${port}/`),
      /Request deadline/,
    );
    const elapsed = Date.now() - t0;
    // deadline is 1500ms in test; allow 500ms slop for setup
    assert.ok(elapsed >= 1400 && elapsed < 4000,
              `should fire deadline near 1500ms, took ${elapsed}ms`);
  } finally {
    srv.close();
  }
});
