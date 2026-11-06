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
