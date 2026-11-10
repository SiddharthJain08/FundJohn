'use strict';
// Static wiring test for the dashboard Crypto page (Task 15).
// Reads server.js as TEXT — never require() it (that boots Express + DB).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

const SERVER_JS = path.join(__dirname, '..', 'src', 'channels', 'api', 'server.js');
const src = fs.readFileSync(SERVER_JS, 'utf8');

function fnBody(name) {
  const re = new RegExp('(?:async\\s+)?function ' + name + '\\s*\\([^)]*\\)\\s*\\{');
  const m = re.exec(src);
  assert.ok(m, name + ' must exist');
  const end = src.indexOf('\n}\n', m.index);
  assert.ok(end > m.index, name + ' must close');
  return src.slice(m.index, end + 2);
}

test('server.js passes node --check (template-literal escaping intact)', () => {
  const r = spawnSync(process.execPath, ['--check', SERVER_JS], { encoding: 'utf8' });
  assert.equal(r.status, 0, r.stderr);
});

test('nav: Crypto button sits right after Market and before Portfolio', () => {
  const iMarket = src.indexOf('id="nav-market"');
  const iCrypto = src.indexOf('id="nav-crypto"');
  const iPort = src.indexOf('id="nav-portfolio"');
  assert.ok(iMarket > 0 && iCrypto > 0 && iPort > 0);
  assert.ok(iMarket < iCrypto && iCrypto < iPort, 'order must be Market · Crypto · Portfolio');
  const between = src.slice(iMarket, iCrypto);
  assert.equal((between.match(/<button/g) || []).length, 1, 'no other button between Market and Crypto');
  assert.match(src, /<button class="nav-btn" id="nav-crypto" onclick="showCrypto\(\)">Crypto<\/button>/);
});

test('_setNavActive lists crypto right after market', () => {
  assert.match(fnBody('_setNavActive'), /\['market','crypto','portfolio','strategies','research','pipelines'\]/);
});

test('_hideAllPages hides crypto-page; CSS hides it by default', () => {
  assert.match(fnBody('_hideAllPages'), /crypto-page/);
  assert.match(src, /#crypto-page\{display:none/);
});

test('page markup carries every crypto container id', () => {
  for (const id of ['crypto-page', 'crypto-inner', 'cr-hero', 'cr-regime', 'cr-tiles', 'cr-board', 'cr-detail', 'cr-feed', 'cr-risk']) {
    assert.match(src, new RegExp('<div id="' + id + '"'), id + ' missing');
  }
});

test('crypto regime panel moved off the Market overview', () => {
  assert.doesNotMatch(src, /cryptoRegimePlaceholder/);
  assert.doesNotMatch(fnBody('showOverview'), /loadCryptoRegime\(\)/);
  const lcr = fnBody('loadCryptoRegime');
  assert.match(lcr, /getElementById\('cr-regime'\)/);
  assert.match(lcr, /id="crypto-regime-panel"/);
});

test('advisory tiles appended to the crypto regime structure', () => {
  const body = fnBody('_renderCryptoRegimeStructure');
  for (const k of ['adv_btc_netflow_z_7d', 'adv_btc_funding_8h', 'adv_delev_market']) assert.match(body, new RegExp(k));
  for (const lbl of ['BTC netflow z', 'Funding 8h', 'Deleveraging']) assert.ok(body.includes(lbl), lbl);
});

test('loadMarket skips the overview repaint on crypto/research/pipeline pages', () => {
  const body = fnBody('loadMarket');
  assert.match(body, /crypto-page/);
  assert.match(body, /onMarket\s*=\s*!onPortfolio && !onStrategies && !onCrypto && !onResearch && !onPipelines/);
});

test('controller + SSE wiring exist', () => {
  for (const fn of ['showCrypto', '_initCrypto', '_refreshCrypto', '_renderCryptoHero', '_appendCryptoEvents']) fnBody(fn);
  assert.match(src, /d\.type === 'crypto_event'[^\n]*_appendCryptoEvents/);
  const refresh = fnBody('_refreshCrypto');
  for (const u of ['/api/crypto/snapshot', '/api/regime/crypto', '/api/crypto/providers', '/api/crypto/risk', '/api/crypto/positions']) {
    assert.ok(refresh.includes("'" + u + "'"), u);
  }
});

// ── Task 16: board, tiles, feed, risk, mobile ───────────────────────────────
const vm = require('vm');

// Render getDashboardHtml() WITHOUT requiring server.js (that boots Express +
// DB): slice the function out of the source and evaluate just it in a vm
// with DASHBOARD_BUILD stubbed.
function renderDashboardHtml() {
  const start = src.indexOf('function getDashboardHtml() {');
  const endMark = '\n</html>`;\n}\n';
  const end = src.indexOf(endMark, start);
  assert.ok(start > 0 && end > start, 'getDashboardHtml must be extractable');
  const fnSrc = src.slice(start, end + endMark.length);
  const ctx = vm.createContext({ DASHBOARD_BUILD: 'test-build' });
  vm.runInContext(fnSrc, ctx);
  return vm.runInContext('getDashboardHtml()', ctx);
}
const HTML = renderDashboardHtml();
const SCRIPTS = [...HTML.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].map(m => m[1]);

test('Task 15 stub placeholders are gone', () => {
  assert.doesNotMatch(src, /\(Task 16\)/);
});

test('board renders the thirteen columns in order', () => {
  const body = src.slice(src.indexOf('const _CR_BOARD_COLS'), src.indexOf('function _crBoardCell'));
  const labels = ['Ticker', 'Price', '24h', 'Whale score', 'Exch netflow 24h', 'z', 'OI', 'OI Δ24h %',
    'Funding ann. %', 'L/S', 'Taker', 'Print imb 1h', 'Coverage %'];
  let at = 0;
  for (const l of labels) {
    const i = body.indexOf("label: '" + l + "'", at);
    assert.ok(i >= at, 'column ' + l + ' missing or out of order');
    at = i + 1;
  }
  assert.match(fnBody('_renderCryptoBoard'), /_CR_BOARD_COLS/);
  assert.equal((body.match(/opt: true/g) || []).length, 7, 'columns 6–12 are optional');
});

test('_refreshCrypto isolates each render in try/catch and fetches events', () => {
  const body = fnBody('_refreshCrypto');
  assert.match(body, /try \{/);
  assert.match(body, /render error/);
  for (const r of ['loadCryptoRegime', '_renderCryptoHero', '_renderCryptoBoard', '_renderCryptoTiles', '_renderCryptoFeed', '_renderCryptoRisk']) {
    assert.ok(body.includes(r), r);
  }
  assert.ok(body.includes("'/api/crypto/events?limit=50'"));
});

test('boot 60 s poll no longer refreshes the crypto regime (the page interval does)', () => {
  const i = src.indexOf("if (document.getElementById('regime-panel')) loadRegime();");
  assert.ok(i > 0, 'loadRegime poll kept');
  const poll = src.slice(i, src.indexOf('}, 60_000);', i));
  assert.doesNotMatch(poll, /loadCryptoRegime/);
});

test('crypto CSS classes exist and the mobile block hides optional columns', () => {
  for (const c of ['.cr-table{', '.cr-charts{', '.cr-risk-grid{', '.cr-feed-row{', '.cr-row-sel{', '.cr-tag-in{', '.cr-tag-out{']) {
    assert.ok(src.includes(c), c);
  }
  const m = /@media \(max-width: ?768px\) ?\{/.exec(src);
  assert.ok(m, 'mobile block');
  const block = src.slice(m.index, src.indexOf('\n}\n', m.index));
  assert.ok(block.includes('.cr-table .cr-opt{display:none}'));
  assert.ok(block.includes('.cr-risk-grid{grid-template-columns:1fr}'));
});

test('every emitted <script> parses (template-literal escaping survives rendering)', () => {
  assert.ok(SCRIPTS.length >= 2);
  for (const s of SCRIPTS) new vm.Script(s);
  assert.ok(HTML.includes('function _renderCryptoBoard('));
});

// Behavioural: run the renderers from the RENDERED client script against a
// stub DOM — proves null-safety on empty and full payloads.
function crSandbox() {
  const main = SCRIPTS.find(s => s.includes('function _renderCryptoBoard('));
  const pick = (from, to) => { const a = main.indexOf(from); const b = main.indexOf(to, a); assert.ok(a >= 0 && b > a, from); return main.slice(a, b); };
  const code = [
    pick('const _STATE_COLOR', 'function _durationStr'),
    pick('function escapeHtml(', '\n}\n') + '\n}\n',
    pick('const _cryptoState = {', 'function _cryptoVisible'),
    pick('// ── Crypto page renderers: board', 'async function showPipelines'),
  ].join('\n') + '\nthis._api = { _cryptoState, _renderCryptoBoard, _renderCryptoTiles, _renderCryptoFeed, _appendCryptoEvents, _renderCryptoRisk, getEvents: () => _cryptoEvents };';
  const els = {};
  const document = { title: 'OpenClaw', getElementById: id => (els[id] = els[id] || { innerHTML: '' }), documentElement: {} };
  const ctx = vm.createContext({ document, console, setTimeout: () => 1, clearTimeout: () => {}, getComputedStyle: () => ({ getPropertyValue: () => '' }) });
  vm.runInContext(code, ctx);
  return { api: ctx._api, els, document };
}

test('renderers survive empty payloads and show placeholders', () => {
  const { api, els } = crSandbox();
  api._renderCryptoBoard({ available: false });
  api._renderCryptoBoard(null);
  api._renderCryptoTiles({}, null);
  api._renderCryptoFeed([]);
  api._renderCryptoRisk({ available: false }, { available: false });
  assert.match(els['cr-board'].innerHTML, /No flow ticks yet/);
  assert.match(els['cr-tiles'].innerHTML, /—/);
  assert.match(els['cr-feed'].innerHTML, /No events yet/);
  assert.match(els['cr-risk'].innerHTML, /No crypto positions/);
});

test('board: stables hidden, benchmark pinned, whale score desc, netflow colours', () => {
  const { api, els } = crSandbox();
  api._renderCryptoBoard({ tickers: [
    { ticker: 'SOL-USD', role: 'alt', whale_score: 80, exch_netflow_usd_24h: 1.2e6 },
    { ticker: 'USDT-USD', role: 'stable', whale_score: 99 },
    { ticker: 'BTC-USD', role: 'benchmark', whale_score: 10, exch_netflow_usd_24h: -3e8, funding_ann: 0.1234 },
    { ticker: 'ETH-USD', role: 'alt', whale_score: 50 },
    { ticker: 'DOGE-USD', role: 'alt', whale_score: '9' },
  ] });
  const h = els['cr-board'].innerHTML;
  assert.doesNotMatch(h, /USDT/);
  const order = ['BTC-USD', 'SOL-USD', 'ETH-USD', 'DOGE-USD'].map(t => h.indexOf('data-t="' + t + '"'));
  assert.ok(order[0] > 0 && order[0] < order[1] && order[1] < order[2] && order[2] < order[3], 'benchmark first, then score desc (string scores numeric)');
  assert.ok(h.includes('color:var(--red)">+$1.2M'));
  assert.ok(h.includes('color:var(--green)">-$300.0M'));
  assert.ok(h.includes('>12.34<'), 'funding_ann × 100, 2 dp');
});

test('feed: append dedupes by event_key, trims to 50, prefixes the title', () => {
  const { api, els, document } = crSandbox();
  const ev = i => ({ event_key: 'k' + i, ts_utc: new Date(Date.UTC(2026, 10, 6, 0, i)).toISOString(), chain: 'eth', symbol: 'ETH', amount_usd: 2e6, direction: i % 2 ? 'exchange_in' : 'exchange_out', tx_hash: '0xabcdef0123456789' });
  api._renderCryptoFeed(Array.from({ length: 50 }, (_, i) => ev(i)));
  api._appendCryptoEvents([ev(49), ev(60)]);
  const evs = api.getEvents();
  assert.equal(evs.length, 50);
  assert.equal(evs[0].event_key, 'k60');
  assert.equal(evs.filter(e => e.event_key === 'k49').length, 1);
  assert.ok(document.title.startsWith('• '));
  assert.match(els['cr-feed'].innerHTML, /unknown wallet → unknown wallet/);
  assert.match(els['cr-feed'].innerHTML, /title="0xabcdef0123456789">0xabcdef</);
});

test('risk panel reads caps and flags from the payload', () => {
  const { api, els } = crSandbox();
  api._renderCryptoRisk({
    flags: { CRYPTO_FLOWS: '1', CRYPTO_BREAKER: '0', CRYPTO_POOLED_SIZING: 'unset' },
    params: { whale_min_usd: 1e6, large_print_usd: 250000, thresholds: { score_alert: 70 } },
    breaker: { status: 'shadow', hold_flat: { since: '2026-11-10T10:00:00Z' }, thresholds: { sleeve_dd: -0.08, position_dd: -0.15, blackout_h: 3 },
      last_evals: [{ ts_utc: '2026-11-10T10:05:00Z', mode: 'shadow', sleeve_dd: -0.091, worst_ticker: 'SOL-USD', worst_plpc: -0.16, action: 'shadow' }] },
    sizing: { status: 'pending_phase_3', benchmark: 'BTC-USD', bench_cap_nav: 0.25, gross_cap_nav: 0.5 },
  }, { available: true, rows: [{ ticker: 'BTC-USD', qty: 0.5, market_value: 37000, unrealized_plpc: -0.021 }] });
  const h = els['cr-risk'].innerHTML;
  assert.ok(h.includes('CRYPTO_FLOWS: on') && h.includes('CRYPTO_BREAKER: off') && h.includes('CRYPTO_POOLED_SIZING: unset'));
  assert.ok(h.includes('sizing: pending_phase_3 — benchmark BTC-USD, BTC cap 25 %, crypto gross cap 50 %'));
  assert.ok(h.includes('breaker: shadow') && h.includes('HOLD FLAT since'));
  assert.ok(h.includes('sleeve DD -8.0 %') && h.includes('SOL-USD@-16.0%') && h.includes('-9.1%'));
  assert.ok(h.includes('-2.10 %'));
});

test('breaker: hero chip reads risk.breaker.status + HOLD FLAT; risk panel lists last_evals; null-safe', () => {
  const hero = fnBody('_renderCryptoHero');
  assert.ok(hero.includes('risk.breaker') && hero.includes('.status') && hero.includes('HOLD FLAT'));
  assert.ok(!hero.includes('pending phase 2') && !hero.includes('phase 2)'));
  assert.ok(fnBody('_renderCryptoRisk').includes('last_evals'));
  const { api, els } = crSandbox();
  api._renderCryptoRisk({ flags: {}, params: {}, sizing: {} }, { available: false });   // old payload, no breaker
  assert.ok(els['cr-risk'].innerHTML.includes('breaker: —'));
});

test('risk panel lists per-ticker holds (hold_tickers) and stays quiet when none', () => {
  const { api, els } = crSandbox();
  const base = { flags: {}, params: {}, sizing: {} };
  api._renderCryptoRisk(Object.assign({}, base, { breaker: { status: 'armed', hold_flat: null,
    hold_tickers: { 'SOL-USD': { since: '2026-11-10T10:00:00Z' }, 'ETH-USD': { since: '2026-11-10T09:00:00Z' } } } }), { available: false });
  let h = els['cr-risk'].innerHTML;
  assert.ok(h.includes('HOLD ETH-USD, SOL-USD') && !h.includes('HOLD FLAT'));
  api._renderCryptoRisk(Object.assign({}, base, { breaker: { status: 'armed', hold_flat: null, hold_tickers: {} } }), { available: false });
  h = els['cr-risk'].innerHTML;
  assert.ok(h.includes('breaker: armed') && !h.includes('HOLD'));
});
