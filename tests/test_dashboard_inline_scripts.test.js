'use strict';
/**
 * Regression: the dashboard's inline <script> blocks must PARSE.
 *
 * `getDashboardHtml()` in src/channels/api/server.js is one giant backtick
 * template literal. A `\'` inside a single-quoted string nested in it becomes a
 * bare `'` in the served HTML and closes that string early — a SyntaxError in
 * the browser that runs NONE of the dashboard client JS. `node --check
 * server.js` cannot see it (the inner script is just a string to Node). This
 * happened at b56cc0cd (2026-09-25, "hasn\'t"); stray backticks inside the
 * template had happened before. The dashboard is the operator's only view.
 *
 * Hermetic: server.js is read as TEXT (never required — it boots Express and
 * DB pools); only the outer `${DASHBOARD_BUILD}` interpolation is stubbed. Any
 * NEW outer interpolation throws a ReferenceError here — that is intended.
 *
 * Run: node --test tests/test_dashboard_inline_scripts.test.js
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SERVER = path.join(__dirname, '..', 'src', 'channels', 'api', 'server.js');

function renderDashboardHtml() {
  const src = fs.readFileSync(SERVER, 'utf8');
  const start = src.indexOf('function getDashboardHtml() {');
  assert.ok(start >= 0, 'getDashboardHtml() not found in server.js');
  const endMarker = src.indexOf('</html>`;', start);
  assert.ok(endMarker > start, 'closing </html>`; not found after getDashboardHtml()');
  const closeBrace = src.indexOf('\n}', endMarker);
  assert.ok(closeBrace > endMarker, 'closing brace of getDashboardHtml() not found');
  const fnSrc = src.slice(start, closeBrace + 2);
  const ctx = vm.createContext({ DASHBOARD_BUILD: 'test-build' });
  return vm.runInContext(fnSrc + '\ngetDashboardHtml();', ctx, { filename: 'getDashboardHtml.js' });
}

function inlineScripts(html) {
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
  const out = [];
  let m;
  while ((m = re.exec(html))) out.push(m[1]);
  return out;
}

test('dashboard HTML renders and contains the activation card', () => {
  const html = renderDashboardHtml();
  assert.ok(html.length > 10_000 && html.includes('</html>'), 'rendered HTML looks truncated');
  assert.ok(html.includes('id="st-act-val"'), 'activation card anchor missing');
  assert.ok(html.includes('_actBenchStatus'), 'activation bench status renderer missing');
});

test('every inline <script> in the dashboard parses (no SyntaxError from template escapes)', () => {
  const scripts = inlineScripts(renderDashboardHtml());
  assert.ok(scripts.length >= 2, `expected >= 2 inline scripts, found ${scripts.length}`);
  scripts.forEach((body, i) => {
    try {
      new vm.Script(body, { filename: `dashboard-inline-${i + 1}` });
    } catch (e) {
      const where = (e.stack || '').split('\n').slice(0, 2).join(' | ');
      assert.fail(`inline script ${i + 1} (${body.length} chars) does not parse: ${e.message} @ ${where}`);
    }
  });
});
