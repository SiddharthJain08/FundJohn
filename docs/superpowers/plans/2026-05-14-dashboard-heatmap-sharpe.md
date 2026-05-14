# Dashboard heatmap polish + Sharpe-decomposition alpha bars + Playwright workaround — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the design in `docs/superpowers/specs/2026-05-14-dashboard-heatmap-sharpe-design.md` — heatmap tiles centered (option C) with live pct return + dollar P&L; alpha bars decomposed into per-strategy contributions that sum to the ticker's live Sharpe; size % normalized to 100%; a `tools/page-shot.js` Playwright workaround for visual verification.

**Architecture:** Single inline app in `src/channels/api/server.js`. One new API endpoint (`/api/portfolio/ticker-alpha/:ticker`) computes the ticker-level Sharpe decomposition on demand with a 5-min cache. The old `/api/portfolio/strategy-sharpe` endpoint is dropped. Frontend renderers gain `accountEquity` arg, do lazy fetch on tile click, render the new option-C layout. The Playwright workaround sits at `tools/page-shot.js` as developer tooling, not a runtime dependency.

**Tech Stack:** Node 22, Express, PostgreSQL, vanilla JS (inline app), `puppeteer-core` (dev-only).

---

## File structure

| File | Responsibility |
|---|---|
| `src/channels/api/server.js` | API + inline app — single file, all changes land here |
| `tools/page-shot.js` (new) | CLI: navigate to a URL, take a screenshot, save PNG to disk |
| `tools/verify_decomp.js` (new) | One-shot sanity script: prove Σ alpha = ticker Sharpe across all tickers |

`puppeteer-core` is installed with `--no-save` so it stays out of `package.json` (dashboard does not depend on it at runtime).

---

## Task 1: Playwright workaround — install + script

**Files:**
- Create: `tools/page-shot.js`

- [ ] **Step 1: Install puppeteer-core dev-only**

```bash
cd /root/openclaw && npm install --no-save puppeteer-core
```

Expected: installs into `node_modules/puppeteer-core` but does NOT modify `package.json`.

- [ ] **Step 2: Write the screenshot tool**

```javascript
#!/usr/bin/env node
// tools/page-shot.js — headless-chromium screenshot helper. Drives the
// already-installed chromium-headless-shell via puppeteer-core so I can
// visually verify dashboard changes without the MCP Playwright tool.
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
const HEADLESS = '/root/.cache/ms-playwright/chromium_headless_shell-1223/chrome-linux/headless_shell';

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
    executablePath: HEADLESS,
    args: ['--no-sandbox', '--disable-dev-shm-usage'],
    defaultViewport: { width: w, height: h },
  });
  try {
    const page = await browser.newPage();
    page.on('console', msg => console.log('[browser]', msg.type(), msg.text()));
    page.on('pageerror', err => console.log('[browser-error]', err.message));
    await page.goto(url, { waitUntil: 'networkidle2', timeout: 30000 });
    await new Promise(r => setTimeout(r, waitMs));
    if (click) {
      await page.click(click).catch(() => console.log('click failed:', click));
      await new Promise(r => setTimeout(r, 400));
    }
    if (clickText) {
      const found = await page.evaluate((t) => {
        const els = [...document.querySelectorAll('*')];
        const el = els.find(e => e.textContent && e.textContent.trim() === t && e.children.length === 0);
        if (el) { el.click(); return true; }
        return false;
      }, clickText);
      if (!found) console.log('click-text not found:', clickText);
      await new Promise(r => setTimeout(r, 400));
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
```

- [ ] **Step 3: Verify it can screenshot the current dashboard**

Run: `node /root/openclaw/tools/page-shot.js --url http://localhost:3000/ --output /tmp/before.png`
Expected: `saved: /tmp/before.png` printed, file size > 50KB.

Then `Read` the PNG to confirm it shows the current dashboard.

- [ ] **Step 4: Commit page-shot.js**

```bash
cd /root/openclaw && git add tools/page-shot.js && git commit -m "tools(page-shot): puppeteer-core wrapper around chromium-headless-shell

Works around the MCP Playwright server's hardcoded /opt/google/chrome
path by driving the playwright-installed headless shell directly.
Saves PNGs that can be opened with the Read tool for visual diffing."
```

---

## Task 2: Remove the dropped Sharpe endpoint

**Files:**
- Modify: `src/channels/api/server.js` (drop the `/api/portfolio/strategy-sharpe` route and its in-memory cache)

- [ ] **Step 1: Delete the endpoint block**

Locate the block beginning with `// Per-strategy + portfolio Sharpe` and ending at the closing `});` of `app.get('/api/portfolio/strategy-sharpe', ...)`. Delete it including the `_sharpeCache` / `_SHARPE_TTL_MS` declarations.

- [ ] **Step 2: Remove the frontend fetch**

In `loadPortfolio`, remove the `_safeFetch('/api/portfolio/strategy-sharpe', ...)` line from the `Promise.all`. Also remove the `sharpe` destructured variable, the line `_sharpeData = sharpe || ...`, and the `let _sharpeData = ...` module-level declaration.

- [ ] **Step 3: Syntax check**

Run: `node -c /root/openclaw/src/channels/api/server.js && echo OK`
Expected: `OK`.

---

## Task 3: New on-demand endpoint `/api/portfolio/ticker-alpha/:ticker`

**Files:**
- Modify: `src/channels/api/server.js` (insert after `/api/portfolio/history`)

- [ ] **Step 1: Write the endpoint with LRU cache**

```javascript
// Per-ticker Sharpe decomposition. Returns the ticker's live Sharpe and
// each strategy's alpha contribution. By construction the alphas sum to
// the ticker Sharpe — see docs/superpowers/specs/2026-05-14-...-design.md.
//
// alpha(s) = mean_d(strategy_s_daily_pnl_on_ticker)
//          / std_d(combined_ticker_daily_pnl)
// ticker_sharpe = mean_d(combined) / std_d(combined)
// Σ alpha(s) = mean(Σ strats) / std(combined) = ticker_sharpe ✓
//
// Tickers with < 2 distinct closed-trade days return live_sharpe=null
// + reason="insufficient_days" — the client renders a fallback message.
const _tickerAlphaCache = new Map();   // ticker → { ts, payload }
const _TICKER_ALPHA_TTL_MS = 5 * 60 * 1000;
const _TICKER_ALPHA_CAP    = 256;       // soft LRU cap

app.get('/api/portfolio/ticker-alpha/:ticker', async (req, res) => {
  const ticker = String(req.params.ticker || '').trim().toUpperCase();
  if (!ticker) return res.status(400).json({ error: 'ticker required' });
  try {
    const now = Date.now();
    const hit = _tickerAlphaCache.get(ticker);
    if (hit && now - hit.ts < _TICKER_ALPHA_TTL_MS) return res.json(hit.payload);

    // Daily combined PnL on this ticker — used both for ticker_sharpe and
    // as the shared sigma denominator for every strategy's alpha.
    const combinedRes = await dbQuery(`
      SELECT sp.closed_at AS d, SUM(sp.pnl_pct) AS day_pnl
      FROM signal_performance sp
      JOIN execution_signals es ON es.id = sp.signal_id
      WHERE sp.status = 'closed' AND es.ticker = $1
      GROUP BY sp.closed_at
      ORDER BY sp.closed_at
    `, [ticker]);
    const series = combinedRes.rows.map(r => parseFloat(r.day_pnl));
    const days   = series.length;
    if (days < 2) {
      const payload = {
        ticker, live_sharpe: null, days, reason: 'insufficient_days',
        strategies: [], computed_at: new Date().toISOString(),
      };
      _tickerAlphaCache.set(ticker, { ts: now, payload });
      return res.json(payload);
    }
    const mu_combined = series.reduce((s, x) => s + x, 0) / days;
    const variance    = series.reduce((s, x) => s + (x - mu_combined) ** 2, 0) / (days - 1);
    const sigma       = Math.sqrt(variance);
    const live_sharpe = sigma > 0 ? mu_combined / sigma : null;
    if (live_sharpe == null) {
      const payload = {
        ticker, live_sharpe: null, days, reason: 'zero_variance',
        strategies: [], computed_at: new Date().toISOString(),
      };
      _tickerAlphaCache.set(ticker, { ts: now, payload });
      return res.json(payload);
    }

    // Per-strategy contribution: mean of that strategy's daily PnL on
    // the ticker, divided by the SHARED ticker sigma above. Means add
    // linearly → Σ alpha(s) = ticker_sharpe.
    const stratRes = await dbQuery(`
      SELECT es.strategy_id,
             es.direction,
             SUM(sp.pnl_pct)::float AS sum_pnl,
             COUNT(*)::int           AS n_trades
      FROM signal_performance sp
      JOIN execution_signals es ON es.id = sp.signal_id
      WHERE sp.status = 'closed' AND es.ticker = $1
      GROUP BY es.strategy_id, es.direction
    `, [ticker]);
    // Direction is stored per-signal; a strategy can hold both LONG and
    // SHORT on the same ticker. Roll up the contribution but tag the
    // dominant direction (largest n_trades) for display.
    const byStrat = new Map();
    for (const r of stratRes.rows) {
      const mean_strat = parseFloat(r.sum_pnl) / days;
      const alpha      = mean_strat / sigma;
      const cur = byStrat.get(r.strategy_id);
      if (!cur) {
        byStrat.set(r.strategy_id, {
          strategy_id: r.strategy_id, alpha, dir: r.direction, n_trades: r.n_trades,
        });
      } else {
        cur.alpha += alpha;
        cur.n_trades += r.n_trades;
        // Keep direction of the larger sub-group
        if (r.n_trades > cur.n_trades / 2) cur.dir = r.direction;
      }
    }
    const strategies = [...byStrat.values()].sort((a, b) => b.alpha - a.alpha);

    const payload = {
      ticker, live_sharpe, days,
      strategies, computed_at: new Date().toISOString(),
    };
    // Soft LRU cap.
    if (_tickerAlphaCache.size >= _TICKER_ALPHA_CAP) {
      const oldestKey = _tickerAlphaCache.keys().next().value;
      _tickerAlphaCache.delete(oldestKey);
    }
    _tickerAlphaCache.set(ticker, { ts: now, payload });
    res.json(payload);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});
```

- [ ] **Step 2: Restart + smoke test the endpoint**

```bash
systemctl restart johnbot.service && sleep 2 && \
curl -s http://localhost:3000/api/portfolio/ticker-alpha/WDC | \
  node -e 'let d="";process.stdin.on("data",c=>d+=c).on("end",()=>{
    const p=JSON.parse(d);
    console.log("WDC live_sharpe =", p.live_sharpe);
    console.log("days =", p.days, "n_strategies =", p.strategies.length);
    const sum = p.strategies.reduce((s,x) => s + x.alpha, 0);
    console.log("sum of alphas   =", sum.toFixed(6));
    console.log("decomp invariant:", Math.abs(sum - p.live_sharpe) < 1e-6 ? "PASS" : "FAIL");
  });'
```

Expected: `decomp invariant: PASS`, `live_sharpe ≈ 0.8426`, `sum of alphas ≈ 0.8426`.

- [ ] **Step 3: Smoke a ticker with insufficient data**

```bash
curl -s http://localhost:3000/api/portfolio/ticker-alpha/WBD | python3 -c 'import sys,json; p=json.load(sys.stdin); print("live_sharpe:", p["live_sharpe"], "reason:", p.get("reason"))'
```

Expected: `live_sharpe: None reason: insufficient_days` (WBD has 1 closed day).

---

## Task 4: Cross-ticker decomposition correctness probe

**Files:**
- Create: `tools/verify_decomp.js`

- [ ] **Step 1: Write the probe**

```javascript
#!/usr/bin/env node
// Iterate every ticker with >=2 closed-trade days and confirm the
// alpha-decomposition invariant |Σ alpha - live_sharpe| < 1e-6.
const BASE = 'http://localhost:3000';

(async () => {
  // Pull the union of tickers from positions + history so we cover both.
  const [pos, hist] = await Promise.all([
    fetch(BASE + '/api/portfolio/positions').then(r => r.json()),
    fetch(BASE + '/api/portfolio/history').then(r => r.json()),
  ]);
  const tickers = new Set();
  for (const r of pos)  if (r.ticker) tickers.add(r.ticker);
  for (const r of hist) if (r.ticker) tickers.add(r.ticker);
  console.log('Probing', tickers.size, 'tickers...');

  let pass = 0, skip = 0, fail = 0;
  const fails = [];
  for (const t of tickers) {
    try {
      const p = await (await fetch(`${BASE}/api/portfolio/ticker-alpha/${encodeURIComponent(t)}`)).json();
      if (p.live_sharpe == null) { skip += 1; continue; }
      const sum = p.strategies.reduce((s, x) => s + x.alpha, 0);
      const diff = Math.abs(sum - p.live_sharpe);
      if (diff < 1e-6) pass += 1;
      else { fail += 1; fails.push({ t, sum, sharpe: p.live_sharpe, diff }); }
    } catch (e) { fail += 1; fails.push({ t, error: e.message }); }
  }
  console.log(`pass=${pass}  skip(insufficient)=${skip}  fail=${fail}`);
  if (fails.length) console.log('failures:', fails.slice(0, 5));
  process.exit(fail === 0 ? 0 : 1);
})();
```

- [ ] **Step 2: Run it**

Run: `node /root/openclaw/tools/verify_decomp.js`
Expected: `fail=0`. The pass count should be ~76 (tickers with ≥ 2 days); skip count ~290.

- [ ] **Step 3: Commit both tools + the endpoint**

```bash
cd /root/openclaw && \
git add src/channels/api/server.js tools/verify_decomp.js && \
git commit -m "feat(api): /api/portfolio/ticker-alpha/:ticker with Sharpe decomposition

Replaces strategy-sharpe with on-demand per-ticker endpoint. Per-strategy
alpha = mean(strategy daily pnl on ticker) / std(combined ticker daily pnl).
Σ alpha = ticker live Sharpe by construction (shared denominator).

Tools: tools/verify_decomp.js iterates every ticker with >=2 closed-trade
days and asserts |Σ alpha - live_sharpe| < 1e-6."
```

---

## Task 5: Heatmap tile — option C with centered hero + $ P&L + 100% size

**Files:**
- Modify: `src/channels/api/server.js`
  - `_buildHeatmapHtml` (rewrite)
  - `.pf-tile` CSS block (rewrite)

- [ ] **Step 1: Replace `_buildHeatmapHtml` body**

Find the existing function. Replace its entire body with:

```javascript
function _buildHeatmapHtml(groups, selectedTicker, expanded, nav) {
  const sorted = [...groups].sort((a, b) => (b.total_size_pct || 0) - (a.total_size_pct || 0));
  const containerHeight = expanded ? 560 : 280;
  const tilesPerRow     = expanded ? 12 : 4;
  const shown           = expanded ? sorted : sorted.slice(0, 12);
  // Size denominator: sum across the WHOLE recent window, not just the
  // visible slice. Otherwise compact-view tiles inflate to 100% of the
  // top-12 and the expanded view rescales — visually inconsistent.
  // Showing share-of-total-book gives stable percentages.
  const sizeDenom = sorted.reduce((s, g) => s + (g.total_size_pct || 0), 0) || 1;
  const overall   = shown.reduce((s, g) => s + (g.total_size_pct || 0), 0) || 1;
  let rowsHtml = '';
  for (let i = 0; i < shown.length; i += tilesPerRow) {
    const slice  = shown.slice(i, i + tilesPerRow);
    const rowSum = slice.reduce((s, g) => s + (g.total_size_pct || 0), 0);
    if (rowSum <= 0) continue;
    const rowHeight = Math.max(56, (rowSum / overall) * containerHeight);
    const tiles = slice.map(g => {
      const pnl = (g.net_pnl != null && isFinite(g.net_pnl)) ? g.net_pnl * 100 : null;
      // Normalized size — share of total book exposure across ALL tickers.
      // Raw notional (which can exceed 100% due to 4× margin) goes to title=.
      const sharePct = ((g.total_size_pct || 0) / sizeDenom) * 100;
      const rawNotional = (g.total_size_pct || 0) * 100;
      // Dollar P&L = Σ(pnl_pct × size_pct) × NAV. g.contrib_pct already
      // holds Σ(pnl × size) as a fraction.
      const dollarPnl = (nav && isFinite(nav) && g.contrib_pct != null)
        ? g.contrib_pct * nav : null;
      const widthPct  = ((g.total_size_pct || 0) / rowSum) * 100;
      const isSel     = g.ticker === selectedTicker;
      const days      = g.avg_days != null ? g.avg_days.toFixed(0) + 'd' : '';
      const tip = \`\${g.ticker} · \${sharePct.toFixed(2)}% of book (notional \${rawNotional.toFixed(1)}%) · \${g.n} strateg\${g.n === 1 ? 'y' : 'ies'}\${pnl != null ? ' · pnl ' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '%' : ''}\${dollarPnl != null ? ' · ' + _fmtDollar(dollarPnl) : ''}\${g.avg_days != null ? ' · ' + days : ''}\`;
      return \`<div class="pf-tile \${isSel ? 'selected' : ''}"
                   data-ticker="\${g.ticker}"
                   style="flex-basis:\${widthPct.toFixed(3)}%; background:\${_pnlColor(pnl)};"
                   title="\${tip}">
        <div class="pf-tile-hero">
          <div class="tk-symbol">\${g.ticker}</div>
          <div class="tk-row">
            <span class="tk-pnl">\${pnl != null ? _fmtPct(pnl, true) : '—'}</span>
            <span class="tk-dollar">\${dollarPnl != null ? _fmtDollar(dollarPnl, true) : ''}</span>
          </div>
        </div>
        <div class="pf-tile-strip">
          <span>\${sharePct.toFixed(1)}%</span>
          <span>\${days}</span>
        </div>
      </div>\`;
    }).join('');
    rowsHtml += \`<div class="pf-heatmap-row" style="height:\${rowHeight.toFixed(0)}px">\${tiles}</div>\`;
  }
  return \`<div class="pf-heatmap \${expanded ? 'expanded' : ''}">\${rowsHtml}</div>\`;
}
```

- [ ] **Step 2: Add the `_fmtDollar` helper**

Just before `_buildHeatmapHtml`, add:

```javascript
// Compact dollar formatter with optional sign. Examples:
//   _fmtDollar(234)         → "$234"
//   _fmtDollar(234, true)   → "+$234"
//   _fmtDollar(-1234)       → "-$1.2k"
//   _fmtDollar(12345, true) → "+$12.3k"
//   _fmtDollar(2_345_000)   → "$2.3M"
function _fmtDollar(v, withSign) {
  if (v == null || !isFinite(v)) return '';
  const sign = withSign ? (v > 0 ? '+' : (v < 0 ? '-' : '')) : (v < 0 ? '-' : '');
  const abs = Math.abs(v);
  let body;
  if (abs >= 1e6)      body = (abs / 1e6).toFixed(1).replace(/\.0$/, '') + 'M';
  else if (abs >= 1e3) body = (abs / 1e3).toFixed(1).replace(/\.0$/, '') + 'k';
  else                 body = Math.round(abs).toString();
  return sign + '$' + body;
}
```

- [ ] **Step 3: Replace the tile CSS**

Find the existing `.pf-tile {…}` rule and the `.pf-tile .tk-symbol`, `.tk-pnl`, `.tk-meta` rules. Replace the whole tile block with:

```css
/* ── Heatmap tiles — option C: centered hero + dark footer strip ───────── */
.pf-tile{position:relative;border:1px solid rgba(255,255,255,0.06);border-radius:6px;cursor:pointer;overflow:hidden;display:flex;flex-direction:column;min-width:0;color:#fff;transition:transform .12s,border-color .12s,box-shadow .12s;box-shadow:0 1px 3px rgba(0,0,0,0.35)}
.pf-tile:hover{border-color:rgba(88,166,255,0.6);transform:translateY(-1px);z-index:3;box-shadow:0 6px 14px rgba(0,0,0,0.6)}
.pf-tile.selected{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue),0 6px 14px rgba(88,166,255,0.25);z-index:4}
.pf-tile-hero{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:6px 6px 4px;text-align:center;gap:3px;min-height:0}
.pf-tile-hero .tk-symbol{font-weight:800;font-size:14px;letter-spacing:.08em;line-height:1.1;text-shadow:0 1px 2px rgba(0,0,0,0.65);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.pf-tile-hero .tk-row{display:flex;align-items:baseline;gap:8px;line-height:1.1}
.pf-tile-hero .tk-pnl{font-size:13px;font-weight:700;font-variant-numeric:tabular-nums;text-shadow:0 1px 2px rgba(0,0,0,0.55)}
.pf-tile-hero .tk-dollar{font-size:11px;font-weight:600;font-variant-numeric:tabular-nums;opacity:0.92;text-shadow:0 1px 2px rgba(0,0,0,0.55)}
.pf-tile-strip{display:flex;justify-content:space-between;align-items:center;padding:3px 8px;font-size:9.5px;font-weight:500;letter-spacing:.04em;background:rgba(0,0,0,0.32);border-top:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.85);flex-shrink:0}
.pf-tile-strip span{font-variant-numeric:tabular-nums}
```

- [ ] **Step 4: Smoke test**

```bash
node -c /root/openclaw/src/channels/api/server.js && echo SYNTAX_OK
systemctl restart johnbot.service && sleep 2 && curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3000/
```

Expected: `SYNTAX_OK` and HTTP 200.

---

## Task 6: Alpha bars — fetch new endpoint, render sum-to-Sharpe header

**Files:**
- Modify: `src/channels/api/server.js`
  - `_buildAlphaBarsHtml` (rewrite)
  - `renderPositions` and `renderHistory` (thread `accountEquity`, do lazy fetch, cache)
  - `loadPortfolio` (pass `account.equity` into render calls)
  - CSS: tweak `.alpha-bars` / `.ab-row` for "= live Sharpe" badge

- [ ] **Step 1: Add a per-ticker alpha cache + fetcher (frontend)**

Near the existing `_sharpeData` declaration (now removed), add:

```javascript
// Per-ticker alpha decomposition — fetched lazily on tile click.
//   _alphaCache[ticker] = payload from /api/portfolio/ticker-alpha/:ticker
//   _alphaInflight[ticker] = Promise (de-dupe concurrent clicks)
const _alphaCache    = {};
const _alphaInflight = {};
function _fetchTickerAlpha(ticker, onReady) {
  if (_alphaCache[ticker]) { onReady(_alphaCache[ticker]); return; }
  if (_alphaInflight[ticker]) { _alphaInflight[ticker].then(onReady); return; }
  _alphaInflight[ticker] = fetch('/api/portfolio/ticker-alpha/' + encodeURIComponent(ticker))
    .then(r => r.json())
    .then(p => { _alphaCache[ticker] = p; delete _alphaInflight[ticker]; return p; })
    .catch(_ => { delete _alphaInflight[ticker]; return { live_sharpe: null, strategies: [], reason: 'fetch_error' }; });
  _alphaInflight[ticker].then(onReady);
}
```

- [ ] **Step 2: Replace `_buildAlphaBarsHtml`**

```javascript
function _buildAlphaBarsHtml(group) {
  const alpha = _alphaCache[group.ticker];
  if (!alpha) {
    return \`<div class="alpha-bars empty">Loading alpha decomposition for <b>\${group.ticker}</b>…</div>\`;
  }
  if (alpha.live_sharpe == null) {
    const reasonText = alpha.reason === 'insufficient_days'
      ? \`<b>\${group.ticker}</b> has \${alpha.days} distinct closed-trade day\${alpha.days === 1 ? '' : 's'} — need ≥ 2 to compute a live Sharpe.\`
      : \`Live Sharpe unavailable for <b>\${group.ticker}</b>\${alpha.reason ? ' (' + alpha.reason + ')' : ''}.\`;
    return \`<div class="alpha-bars empty">\${reasonText}</div>\`;
  }
  const strategies = alpha.strategies || [];
  if (!strategies.length) {
    return \`<div class="alpha-bars empty">No per-strategy contributions for <b>\${group.ticker}</b>.</div>\`;
  }
  const sum    = strategies.reduce((s, x) => s + (x.alpha || 0), 0);
  const maxAbs = Math.max(...strategies.map(s => Math.abs(s.alpha || 0))) || 1;
  const matches = Math.abs(sum - alpha.live_sharpe) < 1e-6;

  const rows = strategies.map(s => {
    const a = s.alpha;
    const widthPct = (Math.abs(a) / maxAbs) * 50;
    const sign = a >= 0 ? 'pos' : 'neg';
    const cls  = a >= 0 ? 'pf-pnl-pos' : 'pf-pnl-neg';
    const dirNorm = _normalizeDir(s.dir);
    return \`<div class="ab-row">
      <div class="ab-label" title="\${s.strategy_id} · \${s.n_trades} closed trades on \${group.ticker}">\${s.strategy_id}</div>
      <div class="ab-dir \${_dirCls(dirNorm)}">\${dirNorm || ''}</div>
      <div class="ab-track">
        <div class="ab-zero"></div>
        <div class="ab-fill \${sign}" style="width:\${widthPct.toFixed(2)}%"></div>
      </div>
      <div class="ab-value \${cls}">\${(a >= 0 ? '+' : '') + a.toFixed(3)}</div>
    </div>\`;
  }).join('');

  const sumCls    = sum >= 0 ? 'pf-pnl-pos' : 'pf-pnl-neg';
  const sumTxt    = (sum >= 0 ? '+' : '') + sum.toFixed(3);
  const sharpeTxt = (alpha.live_sharpe >= 0 ? '+' : '') + alpha.live_sharpe.toFixed(3);
  const badge     = matches
    ? \`<span class="ab-badge">= live Sharpe \${sharpeTxt}</span>\`
    : \`<span class="ab-badge ab-badge-warn">≠ live Sharpe \${sharpeTxt} (Δ \${(sum - alpha.live_sharpe).toFixed(4)})</span>\`;

  return \`<div class="alpha-bars">
    <div class="ab-title">
      <span class="ab-ticker">\${group.ticker}</span>
      <span class="ab-meta">\${strategies.length} strateg\${strategies.length === 1 ? 'y' : 'ies'} · alpha sum = <span class="\${sumCls}">\${sumTxt}</span> \${badge}</span>
    </div>
    <div class="ab-bars">\${rows}</div>
  </div>\`;
}
```

- [ ] **Step 3: Thread `accountEquity` through renderers**

Change `renderPositions(rows)` signature to `renderPositions(rows, accountEquity)`. Inside, store `_navCache = accountEquity` at module scope, OR pass it everywhere. Use the module-scope approach for simplicity:

Add at the top, near `_rawDataCache`:
```javascript
let _navCache = null;
```

In `renderPositions`, near the existing `_rawDataCache['pf-positions'] = rows;`, add:
```javascript
if (accountEquity != null && isFinite(accountEquity)) _navCache = accountEquity;
```

Then inside the function, change every call `_buildHeatmapHtml(groups, ..., expanded)` to `_buildHeatmapHtml(groups, selectedGroup ? selectedGroup.ticker : null, expanded, _navCache)`.

Modify the tile click handler — replace:
```javascript
el.querySelectorAll('.pf-tile').forEach(tile => {
  tile.addEventListener('click', () => {
    const tk = tile.dataset.ticker;
    const cur = _heatmapSelected['pf-positions'];
    _heatmapSelected['pf-positions'] = (cur === tk) ? null : tk;
    renderPositions(_rawDataCache['pf-positions']);
  });
});
```
With:
```javascript
el.querySelectorAll('.pf-tile').forEach(tile => {
  tile.addEventListener('click', () => {
    const tk = tile.dataset.ticker;
    const cur = _heatmapSelected['pf-positions'];
    if (cur === tk) {
      _heatmapSelected['pf-positions'] = null;
      renderPositions(_rawDataCache['pf-positions']);
      return;
    }
    _heatmapSelected['pf-positions'] = tk;
    renderPositions(_rawDataCache['pf-positions']);
    _fetchTickerAlpha(tk, () => {
      if (_heatmapSelected['pf-positions'] === tk) renderPositions(_rawDataCache['pf-positions']);
    });
  });
});
```

Do the equivalent change in `renderHistory` (`accountEquity` arg, store on module scope, and wherever `_buildAlphaBarsHtml(g)` is called for an expanded row, ensure `_fetchTickerAlpha` is kicked off in `_bindGroupClicks` — see Step 4).

- [ ] **Step 4: Trigger lazy-fetch from closed-history group clicks**

Find `_bindGroupClicks` and inside its click handler, immediately after `_toggleExpanded(...)` is called, add:

```javascript
if (_isExpanded(tableId, tk)) {
  _fetchTickerAlpha(tk, () => {
    const rows = _rawDataCache[tableId] || [];
    if (tableId === 'pf-positions') renderPositions(rows);
    else if (tableId === 'pf-history') renderHistory(rows);
  });
}
```

- [ ] **Step 5: Update `loadPortfolio` callers**

In `loadPortfolio`, change the two render calls:

```javascript
renderPositions(positions, account && account.equity);
renderHistory(history, account && account.equity);
```

- [ ] **Step 6: Add badge CSS**

Find `.ab-row.ab-unranked` and right above it, add:

```css
.ab-badge{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:10px;font-size:9px;font-weight:600;letter-spacing:.05em;background:rgba(63,185,80,0.16);color:var(--green);border:1px solid rgba(63,185,80,0.35)}
.ab-badge-warn{background:rgba(248,81,73,0.16);color:var(--red);border-color:rgba(248,81,73,0.35)}
```

- [ ] **Step 7: Restart + first visual smoke**

```bash
node -c /root/openclaw/src/channels/api/server.js && echo SYNTAX_OK && \
systemctl restart johnbot.service && sleep 2 && \
node /root/openclaw/tools/page-shot.js --url http://localhost:3000/ --output /tmp/after-heatmap.png --wait-ms 2500
```

Then `Read` `/tmp/after-heatmap.png` — confirm the new tile layout is centered with the bottom strip.

- [ ] **Step 8: Click expansion screenshot**

Navigate to the dashboard via the workaround, click into Portfolio tab, click a known-rich ticker (WDC), screenshot:

```bash
node /root/openclaw/tools/page-shot.js \
  --url http://localhost:3000/ \
  --output /tmp/after-expand.png \
  --wait-ms 2500 \
  --click-text "WDC" \
  --viewport 1440x1200
```

Read `/tmp/after-expand.png` — confirm the alpha bars panel appears with the "= live Sharpe" badge and the bars sum to the displayed Sharpe.

(If the Portfolio tab isn't open by default, take a screenshot of the navigation chrome first to find the correct button, then add a second `--click "#nav-portfolio"` call.)

---

## Task 7: Final verification + commit

**Files:**
- (verification only)

- [ ] **Step 1: Run the decomposition probe again**

Run: `node /root/openclaw/tools/verify_decomp.js`
Expected: `fail=0`.

- [ ] **Step 2: Run system_checks**

Run: `cd /root/openclaw && python3 -m system_checks`
Expected: exit code ≤ 1 (FAIL/ERROR=0; pre-existing WARNs allowed).

- [ ] **Step 3: Commit the frontend changes**

```bash
cd /root/openclaw && git add src/channels/api/server.js && git commit -m "feat(dashboard): centered hero tiles + Sharpe-decomposition alpha bars

- Heatmap tile redesign (option C): centered ticker + pct return + $ P&L
  hero; dark footer strip with size% (normalized to 100% of book) + days.
- Alpha bars now fetch /api/portfolio/ticker-alpha/:ticker on click,
  show per-strategy alpha that sums to ticker live Sharpe; header badge
  proves the invariant.
- Account equity threaded into render path so tiles display dollar P&L.
- /api/portfolio/strategy-sharpe removed; superseded."
```

- [ ] **Step 4: Push**

```bash
git push 2>&1 | tail -3
```

---

## Self-review

- **Spec coverage:**
  - Math (A) → Task 3 endpoint, Task 4 probe, Task 6 alpha-bars header.
  - Size 100% (B) → Task 5 `sizeDenom` + strip.
  - Tile option C + pct return + $ P&L (C) → Task 5 `_buildHeatmapHtml` rewrite + `_fmtDollar`.
  - Alpha bars sum-to-Sharpe (D) → Task 6 `_buildAlphaBarsHtml` rewrite + badge.
  - Playwright workaround (E) → Task 1.
  - Drop strategy-sharpe → Task 2.
  All spec sections accounted for.

- **Placeholder scan:** No "TBD" / "fill in later" / "add appropriate". All code blocks present.

- **Type consistency:** `accountEquity` arg added to both renderers; `_navCache` module-scope holds it; `nav` arg threaded into `_buildHeatmapHtml`. Field names in `_alphaCache[ticker]` payload (`live_sharpe`, `strategies`, `reason`, `days`) are consistent between Task 3 backend and Task 6 frontend consumer.

- **Scope:** Single dashboard iteration, single file, plus two dev-tool scripts. Tractable inline.
