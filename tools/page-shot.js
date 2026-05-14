#!/usr/bin/env node
// tools/page-shot.js — headless-chromium screenshot helper. Drives the
// already-installed chromium via puppeteer-core so we can visually verify
// dashboard changes without the MCP Playwright tool (which insists on
// /opt/google/chrome and won't fall back).
//
// Usage:
//   node tools/page-shot.js --url URL --output PATH [opts]
// Opts:
//   --viewport WxH       default 1440x900
//   --wait-ms N          additional sleep after load (default 1500)
//   --click "SELECTOR"   optional, click element before screenshot
//   --click-text "TEXT"  optional, click first element whose textContent === TEXT
//   --full-page          full scrollable page (default false)
//
// Saves a PNG. Exit 0 on success, 1 on error.

const puppeteer = require('puppeteer-core');
const CHROME    = '/root/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome';

function arg(name, fallback) {
  const i = process.argv.indexOf(name);
  if (i < 0) return fallback;
  return process.argv[i + 1];
}
function flag(name) { return process.argv.includes(name); }

(async () => {
  const url      = arg('--url');
  const output   = arg('--output');
  const viewport = arg('--viewport', '1440x900');
  const waitMs   = parseInt(arg('--wait-ms', '1500'), 10);
  const click    = arg('--click');
  const clickText = arg('--click-text');
  const fullPage = flag('--full-page');
  if (!url || !output) {
    console.error('usage: page-shot.js --url URL --output PATH [--viewport WxH] [--wait-ms N] [--click SEL] [--click-text TXT] [--full-page]');
    process.exit(1);
  }
  const [w, h] = viewport.split('x').map(Number);
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
    defaultViewport: { width: w, height: h },
  });
  try {
    const page = await browser.newPage();
    page.on('console', msg => {
      const t = msg.type();
      if (t === 'error' || t === 'warning') console.log('[browser]', t, msg.text());
    });
    page.on('pageerror', err => console.log('[browser-error]', err.message));
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
    await new Promise(r => setTimeout(r, waitMs));
    if (click) {
      await page.click(click).catch(e => console.log('click failed:', click, e.message));
      await new Promise(r => setTimeout(r, 600));
    }
    if (clickText) {
      const found = await page.evaluate((t) => {
        const els = [...document.querySelectorAll('*')];
        const el = els.find(e => e.textContent && e.textContent.trim() === t && e.children.length === 0);
        if (el) { el.click(); return true; }
        return false;
      }, clickText);
      if (!found) console.log('click-text not found:', clickText);
      await new Promise(r => setTimeout(r, 600));
    }
    await page.screenshot({ path: output, fullPage });
    console.log('saved:', output);
  } catch (e) {
    console.error('error:', e.message);
    process.exit(1);
  } finally {
    await browser.close();
  }
})();
