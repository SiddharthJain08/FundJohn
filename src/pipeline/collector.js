'use strict';

/**
 * OpenClaw Background Data Collector
 *
 * Continuously collects OHLCV, snapshots, options Greeks, and fundamentals
 * for the S&P 100 universe using a rate-limited queue.
 *
 * Rate limits (Massive/Polygon — Options Starter tier):
 *   - Unlimited API calls; burst throttle at ~300 req/min sustained
 *   - Stock snapshots: NOT included (Options Starter only) — derived from stored prices
 *   - Historical OHLCV (/v2/aggs): included
 *   - Options chains (/v3/snapshot/options): included (primary path)
 *   - Technical indicators (/v1/indicators): included
 *
 * Strategy:
 *   Phase 1 (every 5 min during market hours): Multi-ticker snapshot (1 call)
 *   Phase 2 (daily, staggered): Historical prices (100 calls over ~20 min)
 *   Phase 3 (daily, after Phase 2): Options chains (100 calls over ~20 min)
 *   Phase 4 (daily, FMP): Fundamentals (limited to ~2 per ticker per day)
 *
 * NOTE: Technicals (RSI, SMA) were removed. They were computed from stored prices
 * but never consumed by any live strategy. They cost 4+ hours per night due to
 * Polygon 429 retries (Options Starter plan) in the compute fallback path.
 */

const https = require('https');
const store = require('./store');

const POLYGON_KEY = process.env.POLYGON_API_KEY;
const FMP_KEY     = process.env.FMP_API_KEY;
// AlphaVantage was removed 2026-04-28 — its capabilities are covered by
// Polygon (technical indicators, sector data) and FMP (macro / economic
// calendar). The previous `AV_KEY` constant was unused even before removal.

// Rate interval derived from DB config — reloaded each cycle
let POLYGON_INTERVAL_MS = 13_000; // default: ~4.6 req/min (free tier safe margin)

async function loadConfig() {
  const cfg = await store.getConfig().catch(() => ({}));
  const reqPerMin = parseInt(cfg.polygon_req_per_min || '5', 10);
  // Unlimited sentinel (9999) → no artificial delay. Otherwise use 90% of limit.
  if (reqPerMin >= 9999) {
    POLYGON_INTERVAL_MS = 0; // unlimited — no delay between calls
  } else {
    POLYGON_INTERVAL_MS = Math.max(100, Math.round((60 / (reqPerMin * 0.9)) * 1000));
  }
  return cfg;
}

async function getActiveTickers() {
  // Try DB universe first, fall back to hardcoded if table not yet populated
  const tickers = await store.getUniverseTickers().catch(() => []);
  if (tickers.length > 0) return tickers;
  // Fallback to static list
  const { getUniverse } = require('./universe');
  return getUniverse('SP100');
}

let _paused = false;
let _running = false;
let _sleeping = false;
let _nextRunAt = null;
let _wakeUpTimer = null;
let _stats = { snapshots: 0, prices: 0, options: 0, fundamentals: 0, errors: 0, lastRun: null };
let _broadcast        = null;
let _setPresence      = null;
let _alertPost        = null;
let _onComplete       = null;
let _completionFired  = false;

// Current phase progress — exposed for /pipeline status
let _progress = { phase: null, current: 0, total: 0, ticker: null, phaseStart: null, rowsThisPhase: 0 };

// ── Per-API daily quota counters (reset at UTC midnight) ──────────────────────
const _apiCalls = { fmp: 0, polygon: 0, date: null };

function _resetApiCountersIfNewDay() {
  const today = new Date().toISOString().slice(0, 10);
  if (_apiCalls.date !== today) {
    _apiCalls.fmp = 0;
    _apiCalls.polygon = 0;
    _apiCalls.date = today;
  }
}

function trackApiCall(api) {
  _resetApiCountersIfNewDay();
  _apiCalls[api] = (_apiCalls[api] || 0) + 1;
}

function apiQuotaRemaining(api, dailyLimit) {
  _resetApiCountersIfNewDay();
  return dailyLimit - (_apiCalls[api] || 0);
}

// ── Market-hours detection (ET, Mon–Fri 9:30–16:00) ─────────────────────────
function isMarketHours() {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    weekday: 'short', hour: 'numeric', minute: 'numeric', hour12: false,
  }).formatToParts(new Date());
  const p = {};
  for (const { type, value } of parts) p[type] = value;
  if (['Sat', 'Sun'].includes(p.weekday)) return false;
  const h = parseInt(p.hour, 10) % 24;
  const m = parseInt(p.minute, 10);
  return (h > 9 || (h === 9 && m >= 30)) && h < 16;
}

// ── Sleep / wake ──────────────────────────────────────────────────────────────
function enterSleepMode(nextRunAt) {
  _sleeping = true;
  _nextRunAt = nextRunAt || null;
  const etStr = nextRunAt
    ? nextRunAt.toLocaleString('en-US', { timeZone: 'America/New_York', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true, timeZoneName: 'short' })
    : 'next scheduled run';
  notify(`😴 All data collected — sleeping until ${etStr}`);
  if (_setPresence) _setPresence(`😴 Sleeping — next run: ${etStr}`);
  if (_alertPost) _alertPost(`😴 **BotJohn sleeping** — all data collected. Next collection: **${etStr}**\nResponds to commands normally. Use \`!john /pipeline status\` anytime.`);
}

function exitSleepMode() {
  _sleeping = false;
  notify('⏰ Waking up — daily collection starting shortly');
  if (_setPresence) _setPresence('⏰ Waking up...');
}

function isSleeping() { return _sleeping; }
function getNextRun() { return _nextRunAt; }

function pause()    { _paused = true;  console.log('[collector] Paused'); }
function resume()   { _paused = false; console.log('[collector] Resumed'); }
function isRunning()  { return _running; }
function getStats()   { return { ..._stats, paused: _paused, sleeping: _sleeping, nextRunAt: _nextRunAt, progress: { ..._progress }, apiCalls: { ..._apiCalls } }; }
function setBroadcast(fn) { _broadcast = fn; }
function setDiscordHooks({ presence, alertPost, onComplete }) {
  _setPresence = presence;
  _alertPost   = alertPost;
  if (onComplete) _onComplete = onComplete;
}

function notify(msg) {
  console.log(`[collector] ${msg}`);
  if (_broadcast) _broadcast({ type: 'pipeline', message: msg, stats: _stats });
}

// Progress bar string (10 chars)
function progressBar(current, total) {
  const filled = total > 0 ? Math.round((current / total) * 10) : 0;
  return '▓'.repeat(filled) + '░'.repeat(10 - filled);
}

// Update Discord presence + post periodic alerts
function tickProgress(phase, current, total, ticker, rowsWritten = 0) {
  _progress = { phase, current, total, ticker, phaseStart: _progress.phaseStart, rowsThisPhase: (_progress.rowsThisPhase || 0) + rowsWritten };

  const elapsed = _progress.phaseStart ? (Date.now() - _progress.phaseStart) / 1000 : 1;
  const rate    = elapsed > 0 ? (current / elapsed * 60).toFixed(1) : '?';
  const etaSec  = current > 0 ? Math.round(((total - current) / current) * elapsed) : null;
  const eta     = etaSec != null ? (etaSec > 60 ? `${Math.round(etaSec / 60)}m` : `${etaSec}s`) : '?';

  // Discord presence
  if (_setPresence) {
    _setPresence(`${phase} ${progressBar(current, total)} ${current}/${total} | ${rate}/min`);
  }

  // Post to #data-alerts every 10 tickers (but not on tick 0)
  if (_alertPost && current > 0 && current % 10 === 0) {
    const pct = Math.round((current / total) * 100);
    _alertPost([
      `**${phase}** — ${progressBar(current, total)} \`${current}/${total}\` (${pct}%)`,
      `Ticker: \`${ticker}\` | Speed: **${rate} tickers/min** | ETA: **${eta}**`,
      `Rows written this phase: **${(_progress.rowsThisPhase).toLocaleString()}** | Errors: ${_stats.errors}`,
    ].join('\n'));
  }
}

// Check if all tickers have full price coverage and fire _onComplete once
async function checkCompletionStatus(tickers, fromDate, toDate) {
  if (_completionFired || !_onComplete) return;
  const { query: dbQuery } = require('../database/postgres');
  // A ticker is "covered" if its coverage extends to within 7 days of today
  // (accounts for weekends, holidays, and new tickers being added)
  const cutoff = new Date(new Date(toDate).getTime() - 7 * 86400_000).toISOString().slice(0, 10);
  const res = await dbQuery(
    `SELECT COUNT(*) AS covered FROM data_coverage WHERE data_type='prices' AND date_to >= $1`,
    [cutoff]
  ).catch(() => null);
  const covered = parseInt(res?.rows?.[0]?.covered || '0', 10);
  if (covered >= tickers.length) {
    _completionFired = true;
    notify(`🎉 All ${covered}/${tickers.length} tickers fully covered — initial collection complete!`);
    try { await _onComplete({ covered, total: tickers.length, fromDate, toDate, stats: { ..._stats } }); } catch {}
  }
}

// ── HTTP helpers ───────────────────────────────────────────────────────────────

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// Hard request-deadline (ms) — used to wrap every Polygon/FMP call so a
// half-open / silently-stalled TCP stream cannot wedge the cycle. Node's
// `timeout` option is socket-idle only; if a server keeps sending one byte
// every <30s, or if the response stream emits an unhandled 'error' after
// the connection RSTs mid-body, the socket-idle timer is irrelevant. The
// 2026-04-29 cycle wedged in Phase 3 (Polygon options chain) on this exact
// shape — no error surfaced, no progress, ep_poll forever. See git log.
const HTTP_REQUEST_DEADLINE_MS = parseInt(process.env.HTTP_REQUEST_DEADLINE_MS || '60000', 10);

async function httpGet(url, _retryCount = 0) {
  const raw = await new Promise((resolve, reject) => {
    let settled = false;
    const settle = (fn, arg) => { if (settled) return; settled = true; fn(arg); };

    // Hard overall-deadline — covers the case where a response stream RSTs
    // mid-body and node never fires 'end' (the bug that wedged the
    // 2026-04-29 cycle).
    const deadline = setTimeout(() => {
      try { req.destroy(); } catch (_) {}
      settle(reject, new Error(`Request deadline exceeded (${HTTP_REQUEST_DEADLINE_MS}ms)`));
    }, HTTP_REQUEST_DEADLINE_MS);

    const req = https.get(url, { timeout: 30_000 }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        clearTimeout(deadline);
        settle(resolve, { status: res.statusCode, headers: res.headers, body: data });
      });
      // Without this handler, a connection RST after headers causes the
      // promise to pend forever (no end, no error path). Found 2026-04-29.
      res.on('error', (err) => {
        clearTimeout(deadline);
        settle(reject, err);
      });
    });
    req.on('error', (err) => { clearTimeout(deadline); settle(reject, err); });
    req.on('timeout', () => {
      clearTimeout(deadline);
      try { req.destroy(); } catch (_) {}
      settle(reject, new Error('Request timeout'));
    });
  });

  if (raw.status === 429) {
    const retryAfter = Math.min(parseFloat(raw.headers['retry-after'] || '60'), 120);
    const isPolygon = url.includes('polygon.io') || url.includes('massive.io');

    if (isPolygon) {
      // Polygon-specific handling: publish Redis alert, backoff, retry
      const backoffUntil = Date.now() / 1000 + retryAfter;
      try {
        const { getClient } = require('../database/redis');
        const r = getClient();
        await r.set('rate_backoff:polygon', String(backoffUntil), 'EX', Math.ceil(retryAfter) + 5);
        await r.set('rate_backoff:massive', String(backoffUntil), 'EX', Math.ceil(retryAfter) + 5);
        await r.publish('data:alerts', JSON.stringify({
          type:            'RATE_LIMIT_HIT',
          provider:        'polygon',
          backoff_seconds: retryAfter,
          timestamp:       Date.now(),
          message:         `Polygon/Massive 429 — backing off ${retryAfter}s, will resume automatically`,
        }));
      } catch (_) {}

      if (_alertPost) {
        _alertPost(`⚠️ **Polygon 429** — rate limit hit. Backing off **${retryAfter}s**. Will resume automatically.`);
      }

      console.warn(`[collector] 429 from Polygon — waiting ${retryAfter}s (retry #${_retryCount + 1})`);
      await sleep(retryAfter * 1000);

      if (_retryCount < 3) return httpGet(url, _retryCount + 1);
      throw new Error(`Rate limited (429) after ${_retryCount + 1} retries`);
    } else {
      // Non-Polygon 429 (FMP, etc.) — throw immediately for caller to handle
      throw new Error(`HTTP 429 — rate limited by ${new URL(url).hostname}`);
    }
  }

  if (raw.status === 403) throw new Error('Forbidden (403) — check API tier');
  if (raw.status !== 200) throw new Error(`HTTP ${raw.status}`);

  try { return JSON.parse(raw.body); }
  catch (e) { throw new Error('JSON parse error'); }
}

async function rateLimitedCall(fn) {
  while (_paused) await sleep(1000);
  const result = await fn();
  // With unlimited plan: no artificial delay between calls.
  // POLYGON_INTERVAL_MS is now 1ms (effectively 0) when rate = 9999.
  if (POLYGON_INTERVAL_MS > 100) await sleep(POLYGON_INTERVAL_MS);
  return result;
}

// ── Phase 1: Snapshot — retired (parquet-primary architecture) ──────────────
// Previously derived a 5-min rolling cache in the `snapshots` DB table from
// price_data. Parquet-primary makes the cache redundant: dashboard consumers
// now derive latest-per-ticker on-demand from prices.parquet via
// readParquet('latest_snapshots'), which is microseconds on a 12MB file.

async function runSnapshots() {
  const tickers = await getActiveTickers();
  notify(`📡 Snapshot (parquet-primary): ${tickers.length} tickers — no-op (latest derived on read)`);
  await store.logRun(null, 'snapshot', 'success', tickers.length, null, 0, 0);
}

// ── Phase 1b: Macro vol indices (VIX/VIX3M/VVIX) ─────────────────────────
// Wraps src/ingestion/cboe_vol_indices.py (SP-1: renamed from fetch_vol_indices
// to mark it as the sole sanctioned yfinance importer). Strategies that read
// aux_data['macro'] depend on these series being current. Fast (<10s wall-clock).
async function runVolIndices() {
  const { execSync } = require('child_process');
  const start = Date.now();
  notify('📈 Macro vol indices: VIX/VIX3M/VVIX (bounded cboe_vol_indices module)');
  try {
    const out = execSync(
      'python3 src/ingestion/cboe_vol_indices.py',
      { cwd: require('path').resolve(__dirname, '..', '..'), timeout: 60_000, stdio: ['ignore', 'pipe', 'pipe'] },
    ).toString();
    const elapsed = ((Date.now() - start) / 1000).toFixed(1);
    let inserted = 0;
    try {
      const m = out.match(/\{.*?inserted.*?\}/s);
      if (m) {
        const r = JSON.parse(m[0]);
        inserted = Object.values(r.inserted || {}).reduce((a, b) => a + Math.max(0, b), 0);
      }
    } catch (_) {}
    notify(`✅ Macro vol indices complete — ${inserted} rows in ${elapsed}s`);
    await store.logRun(null, 'macro', 'success', inserted, null, Date.now() - start, 0);
  } catch (err) {
    // Non-fatal — collector continues with prices/options/etc.
    notify(`[WARN] Macro vol indices failed: ${err.message.slice(0, 200)}`);
    await store.logRun(null, 'macro', 'error', 0, err.message.slice(0, 500), Date.now() - start, 0);
  }
}

// ── Daily-collection wrappers for the 4 ingest scripts. Each phase is non-fatal
// (matches runVolIndices): a script failure logs a [WARN] and continues. ──

const ROOT_DIR = require('path').resolve(__dirname, '..', '..');

async function _runIngestPhase(emoji, label, scriptArgs, dataset, timeoutMs = 5 * 60 * 1000) {
  const { execFile } = require('child_process');
  const { promisify } = require('util');
  const execFileP = promisify(execFile);
  const start = Date.now();
  notify(`${emoji} ${label}`);
  try {
    const { stdout } = await execFileP('python3', scriptArgs, {
      cwd: ROOT_DIR, timeout: timeoutMs, maxBuffer: 4 * 1024 * 1024,
    });
    const elapsed = ((Date.now() - start) / 1000).toFixed(1);
    const tail = stdout.trim().split('\n').slice(-2).join(' ').slice(0, 200);
    notify(`✅ ${label} complete in ${elapsed}s — ${tail}`);
    await store.logRun(null, dataset, 'success', 0, null, Date.now() - start, 0);
  } catch (err) {
    notify(`[WARN] ${label} failed: ${(err.message || '').slice(0, 200)}`);
    await store.logRun(null, dataset, 'error', 0, (err.message || '').slice(0, 500), Date.now() - start, 0);
  }
}

// vol_indices.parquet (wide-format vix/vvix/vix9d_close) — distinct from the
// long-format VIX/VIX3M/VVIX rows in macro.parquet that runVolIndices() refreshes.
async function runVolIndicesWide() {
  return _runIngestPhase(
    '📊', 'Vol indices (wide-format)',
    ['src/ingestion/ingest_vol_indices.py'],
    'vol_indices', 60_000,
  );
}

// 30-minute equity bars for the top-250 universe.
async function run30mPrices() {
  return _runIngestPhase(
    '⏱️', '30m prices (top-250)',
    ['src/ingestion/ingest_prices_30m.py'],
    'prices_30m', 5 * 60_000,
  );
}

// Per-ticker ATM-30d/90d IV history derived from options_eod.parquet.
// Guard: only run daily once the parquet has ≥ 30 days of history. The first
// run is performed by staging-approval backfill, which builds the seed
// history; daily collection just appends yesterday's slice on top.
async function runIvHistory() {
  const fs = require('fs');
  const path = require('path');
  const ivPath = path.join(ROOT_DIR, 'data', 'master', 'iv_history.parquet');
  if (!fs.existsSync(ivPath)) {
    notify('⏭️  iv_history skipped — no seed parquet (run staging-approval first)');
    return;
  }
  // Cheap depth probe — count distinct dates without loading all columns.
  let distinctDays = 0;
  try {
    const { execFileSync } = require('child_process');
    const out = execFileSync('python3', ['-c',
      "import pandas as pd, sys; df = pd.read_parquet('" + ivPath + "', columns=['date']); print(df['date'].astype(str).nunique())",
    ], { timeout: 30_000 }).toString().trim();
    distinctDays = parseInt(out, 10) || 0;
  } catch (_) {}
  if (distinctDays < 30) {
    notify(`⏭️  iv_history skipped — only ${distinctDays} day(s) of seed history; needs ≥ 30`);
    return;
  }
  return _runIngestPhase(
    '📐', 'iv_history (incremental)',
    ['src/ingestion/ingest_iv_history.py'],
    'iv_history', 5 * 60_000,
  );
}

// Forward earnings calendar via ingest_earnings_calendar.py (throttled per-ticker).
async function runEarningsCalendar() {
  return _runIngestPhase(
    '📅', 'Earnings calendar (per-ticker, 1s throttle)',
    ['src/ingestion/ingest_earnings_calendar.py', '--throttle', '1.0'],
    'earnings_calendar', 15 * 60_000,
  );
}

// ── Phase 2: Historical prices — gap-aware, never re-fetches known data ────────

async function runHistoricalPrices(daysBack = 3650, tickers = null) {
  if (!tickers) tickers = await getActiveTickers();
  const toDate   = new Date().toISOString().slice(0, 10);
  const fromDate = new Date(Date.now() - daysBack * 86400_000).toISOString().slice(0, 10);
  notify(`📊 Historical prices: checking gaps for ${tickers.length} tickers (target: ${fromDate} → ${toDate})`);
  _progress = { phase: '📊 Prices', current: 0, total: tickers.length, ticker: null, phaseStart: Date.now(), rowsThisPhase: 0 };

  let skipped = 0;
  let totalCalls = 0;
  // SP-1: equity OHLCV source is Alpaca P1 (yfinance removed; Task 15 wires the bars API)
  const usePolygonOHLCV = false;

  for (let i = 0; i < tickers.length; i++) {
    const ticker = tickers[i];
    if (_paused) { notify('⏸️ Paused — waiting...'); while (_paused) await sleep(1000); }

    // ── Gap detection: only fetch what we don't already have ──────────────────
    const gaps = await store.getGaps(ticker, 'prices', fromDate, toDate);

    if (gaps.length === 0) {
      skipped++;
      tickProgress('📊 Prices', i + 1, tickers.length, ticker, 0);
      continue; // fully covered — skip entirely
    }

    const start = Date.now();
    let totalWritten = 0;

    for (const gap of gaps) {
      try {
        const written = await fillPricesAlpaca(ticker, gap.from, gap.to);
        await store.updateCoverage(ticker, 'prices', gap.from, gap.to, written);
        totalWritten += written;
        totalCalls++;
      } catch (err) {
        _stats.errors++;
        await store.logRun(ticker, 'prices', 'error', 0, err.message, Date.now() - start, 1);
        notify(`⚠️ ${ticker} prices error: ${err.message}`);
      }
    }

    _stats.prices += totalWritten;
    tickProgress('📊 Prices', i + 1, tickers.length, ticker, totalWritten);
    if (totalWritten > 0) {
      await store.logRun(ticker, 'prices', 'success', totalWritten, null, Date.now() - start, gaps.length);
    }
  }

  // Flush buffered rows to prices.parquet in one atomic write.
  try {
    const flushed = await store.flushPrices();
    if (flushed && flushed.flushed) {
      console.log(`[collector] Prices flush: ${flushed.flushed} rows → prices.parquet (total ${flushed.total_after})`);
    }
  } catch (err) {
    console.error(`[collector] Prices flush FAILED: ${err.message}`);
  }

  const elapsed = Math.round((Date.now() - _progress.phaseStart) / 1000);
  notify(`✅ Historical prices complete — ${_progress.rowsThisPhase.toLocaleString()} new rows | ${skipped}/${tickers.length} tickers skipped (already complete) | ${totalCalls} API calls`);
  if (_alertPost) _alertPost(`✅ **Phase 2 complete** — ${_progress.rowsThisPhase.toLocaleString()} rows added | ${skipped} tickers skipped (no gaps) | ${totalCalls} API calls in ${Math.round(elapsed / 60)}m`);
  await checkCompletionStatus(tickers, fromDate, toDate);
}

// ── Equity price gap-fill — Alpaca daily bars (SP-1: yfinance removed) ────────
// TODO Task 15+: implement Alpaca /v2/stocks/{T}/bars OHLCV gap-fill here.
// yfinance path removed in Task 14 (SP-1 provider cutover).

async function fillPricesAlpaca(ticker, fromDate, toDate) {
  // NotImplementedError: Alpaca daily bars ingestion not yet wired.
  // Equity OHLCV gap-fill will be implemented in Task 15.
  // For now, log and return 0 so the collection phase continues without crashing.
  console.warn(`[collector] fillPricesAlpaca: NOT IMPLEMENTED — ${ticker} ${fromDate}→${toDate} (Task 15)`);
  return 0;
}


// ── Phase 3: Options chains — direct Alpaca CLI with greeks-validity filter ─

// Per-phase soft budget (default 30 min) — when exceeded, the options
// phase logs the partial result and exits cleanly so signals/handoff/
// trade/alpaca can still run on time. Override with OPTIONS_PHASE_BUDGET_S.
const OPTIONS_PHASE_BUDGET_S = parseInt(process.env.OPTIONS_PHASE_BUDGET_S || '1800', 10);

/**
 * Greeks-validity filter for Alpaca chain snapshots.
 *
 * Returns false (drop silently) for:
 *   - Expired contracts (expiry <= today)
 *   - Zero-volume, zero-greek contracts (0-DTE or illiquid deep OTM/ITM)
 *
 * Returns false (drop with warning) for:
 *   - Non-expired, non-zero-volume contracts with all-zero greeks
 *     (unexpected — logged as sp1.greeks_anomaly for monitoring)
 *
 * Returns true to keep all others.
 */
function _optionsGreeksValid(row) {
  const today = new Date().toISOString().slice(0, 10);
  const allZero = (row.delta || 0) === 0 && (row.gamma || 0) === 0
               && (row.theta || 0) === 0 && (row.vega  || 0) === 0;
  if (!allZero) return true;

  // All greeks are zero — determine whether to drop silently or warn.
  if (row.expiration && row.expiration <= today) return false;  // expired — silent drop
  if ((row.volume || 0) === 0) return false;                    // zero-volume — silent drop

  // Non-expired, non-zero-volume with all-zero greeks: unexpected anomaly — warn and drop.
  const label = row.underlying
    ? `${row.underlying} ${row.expiration} ${row.strike}${row.option_type === 'call' ? 'C' : 'P'}`
    : (row.contract_symbol || 'unknown');
  console.warn('sp1.greeks_anomaly', label);
  return false;
}

/**
 * Fetch the full option chain for a single underlying via `alpaca data option chain`.
 * Pages through next_page_token, applies the greeks-validity filter, and returns
 * a flat array of rows (one per contract) in the collector's canonical format.
 *
 * Exported for testing; also called by runOptions per ticker.
 */
async function fetchOptionsChain(underlying) {
  const { runOptionsAlpaca } = require('./alpaca_options');
  const raw = await runOptionsAlpaca(underlying, { limit: 100 });
  return raw.filter(_optionsGreeksValid);
}

async function runOptions(tickers = null) {
  if (!tickers) tickers = await getActiveTickers();
  const today = new Date().toISOString().slice(0, 10);
  notify(`🎲 Options chains: ${tickers.length} tickers (budget ${OPTIONS_PHASE_BUDGET_S}s, source=alpaca)`);
  if (_alertPost) _alertPost(`🎲 **Phase 3: Options** starting — ${tickers.length} tickers (alpaca)`);
  const phaseStart = Date.now();
  _progress = { phase: '🎲 Options', current: 0, total: tickers.length, ticker: null, phaseStart, rowsThisPhase: 0 };

  // Determine which tickers still need today's options data
  const needed = [];
  let skipped = 0;
  for (let i = 0; i < tickers.length; i++) {
    const gaps = await store.getGaps(tickers[i], 'options', today, today);
    if (gaps.length === 0) { skipped++; tickProgress('🎲 Options', i + 1, tickers.length, tickers[i], 0); }
    else needed.push(tickers[i]);
  }

  if (needed.length === 0) {
    notify(`✅ Options: all ${skipped} tickers already have today's data — skipped`);
    if (_alertPost) _alertPost(`✅ **Phase 3 complete** — all tickers already covered`);
    return;
  }

  let budgetExceeded = false;
  let processed = 0;
  for (let i = 0; i < needed.length; i++) {
    // Soft budget check — bail out cleanly so the rest of the daily
    // cycle (signals/handoff/trade/alpaca) doesn't starve while we
    // wait on a slow upstream. Strategies that need options use
    // whatever was collected today plus yesterday's snapshot.
    const elapsedS = (Date.now() - phaseStart) / 1000;
    if (elapsedS > OPTIONS_PHASE_BUDGET_S) {
      const remaining = needed.length - i;
      notify(`⏰ Options budget exceeded (${Math.round(elapsedS)}s > ${OPTIONS_PHASE_BUDGET_S}s) — `
           + `processed ${processed}/${needed.length}, deferring ${remaining} tickers to next cycle`);
      if (_alertPost) _alertPost(
        `⏰ **Options budget hit** — ${processed}/${needed.length} fetched, ` +
        `${remaining} deferred (will retry next cycle). Phase took ${Math.round(elapsedS)}s of ${OPTIONS_PHASE_BUDGET_S}s budget.`
      );
      budgetExceeded = true;
      break;
    }

    const ticker = needed[i];
    if (_paused) { while (_paused) await sleep(1000); }
    const start = Date.now();
    try {
      const rows = await fetchOptionsChain(ticker);
      // Adapt flat row schema → the nested shape store._optionRow expects.
      const contracts = rows.map(r => ({
        details: {
          expiration_date: r.expiration,
          strike_price:    r.strike,
          contract_type:   r.option_type,
        },
        greeks: {
          delta: r.delta, gamma: r.gamma, theta: r.theta,
          vega:  r.vega,  rho:   r.rho,
        },
        implied_volatility: r.implied_volatility,
        day:                { last_price: r.last_price, volume: r.volume },
        last_quote:         { bid: r.bid, ask: r.ask },
        open_interest:      null,
      }));
      const written = await store.upsertOptions(ticker, contracts, today);
      await store.updateCoverage(ticker, 'options', today, today, written);
      _stats.options += written;
      tickProgress('🎲 Options', skipped + i + 1, tickers.length, ticker, written);
      await store.logRun(ticker, 'options', 'success', written, null, Date.now() - start, 1);
      processed++;
      // Heartbeat to orchestrator stdout every 25 fetched tickers — lets the
      // orchestrator's stdout-idle watchdog distinguish "still working" from
      // "wedged". tickProgress only fires Discord webhooks; stdout is silent
      // through the entire phase otherwise.
      if (processed % 25 === 0) {
        const phaseElapsed = Math.round((Date.now() - phaseStart) / 1000);
        notify(`🎲 Options progress — ${processed}/${needed.length} fetched, ${_progress.rowsThisPhase.toLocaleString()} contracts in ${phaseElapsed}s`);
      }
    } catch (err) {
      _stats.errors++;
      tickProgress('🎲 Options', skipped + i + 1, tickers.length, ticker, 0);
      await store.logRun(ticker, 'options', 'error', 0, err.message, Date.now() - start, 1);
    }
  }

  // Flush the in-memory buffer to options_eod.parquet. Without flushOptions,
  // rows buffered by upsertOptions are dropped on collector exit while
  // data_coverage is still marked fresh (the 2026-04-29 silent drift bug).
  try {
    const flushed = await store.flushOptions();
    if (flushed && flushed.flushed) {
      console.log(`[collector] Options flush: ${flushed.flushed} rows → options_eod.parquet (total ${flushed.total_after})`);
    }
  } catch (err) {
    console.error(`[collector] Options flush FAILED: ${err.message}`);
    notify(`[ERROR] Options parquet flush failed: ${err.message}`);
  }

  const elapsed = Math.round((Date.now() - phaseStart) / 1000);
  if (budgetExceeded) {
    notify(`⚠️ Options chains partial — ${_progress.rowsThisPhase.toLocaleString()} contracts in ${elapsed}s (budget capped)`);
  } else {
    notify(`✅ Options chains complete — ${_progress.rowsThisPhase.toLocaleString()} contracts in ${elapsed}s`);
    if (_alertPost) _alertPost(`✅ **Phase 3 complete** — ${_progress.rowsThisPhase.toLocaleString()} option contracts stored`);
  }
}

// ── Phase 4: Fundamentals via FMP ────────────────────────────────────────────
// FMP free tier: 250 req/day. We spread across days via 30-day gap skip.
// On 402 (daily quota exhausted) we stop immediately to preserve remaining quota.
// On 429 (rate limited) we back off 60s and retry once.

async function runFundamentals(tickers = null) {
  if (!tickers) tickers = await getActiveTickers();
  const today = new Date().toISOString().slice(0, 10);
  notify(`💹 Fundamentals: ${tickers.length} tickers via FMP`);
  if (_alertPost) _alertPost(`💹 **Phase 5: Fundamentals** starting — quarterly income statements via FMP for ${tickers.length} tickers`);
  _progress = { phase: '💹 Fundamentals', current: 0, total: tickers.length, ticker: null, phaseStart: Date.now(), rowsThisPhase: 0 };

  // Read interval and daily quota from config
  const cfg = await store.getConfig().catch(() => ({}));
  const FMP_INTERVAL   = parseInt(cfg.fmp_interval_ms  || '2000', 10);
  const FMP_PER_DAY    = parseInt(cfg.fmp_req_per_day  || '250',  10);
  const remaining = apiQuotaRemaining('fmp', FMP_PER_DAY);
  notify(`💹 Fundamentals: FMP quota remaining today: ${remaining}/${FMP_PER_DAY}`);

  // Fundamentals are quarterly — skip if fetched within last 30 days
  const thirtyDaysAgo = new Date(Date.now() - 30 * 86400_000).toISOString().slice(0, 10);
  let skipped = 0;
  let quotaExhausted = false;

  for (let i = 0; i < tickers.length; i++) {
    const ticker = tickers[i];
    if (_paused) { while (_paused) await sleep(1000); }

    // Stop if quota exhausted OR we've used our daily allowance this session
    if (quotaExhausted || apiQuotaRemaining('fmp', FMP_PER_DAY) <= 0) {
      if (!quotaExhausted) { quotaExhausted = true; notify(`⚠️ FMP daily quota (${FMP_PER_DAY}) reached — skipping remaining tickers (SP-1: yfinance fallback removed)`); }
      tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
      continue;
    }

    const gaps = await store.getGaps(ticker, 'fundamentals', thirtyDaysAgo, today);
    if (gaps.length === 0) {
      skipped++;
      tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
      continue;
    }

    const start = Date.now();
    let data = null;
    try {
      const url = `https://financialmodelingprep.com/stable/income-statement?symbol=${ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}`;
      data = await httpGet(url);
      trackApiCall('fmp');
      await sleep(FMP_INTERVAL);
    } catch (err) {
      if (err.message.includes('402')) {
        // Daily quota exhausted — stop making FMP calls for the rest of this cycle
        quotaExhausted = true;
        notify(`⚠️ FMP daily quota exhausted (402) after ${i} tickers — resuming tomorrow`);
        if (_alertPost) _alertPost(`⚠️ **FMP quota exhausted** — fundamentals will resume tomorrow (${i}/${tickers.length} done this cycle)`);
        tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
        continue;
      }
      if (err.message.includes('429')) {
        // Rate limited — back off 30s and retry once; if still 429, treat as quota exhausted
        notify(`⏳ FMP rate limited (429) — backing off 30s then retrying`);
        await sleep(30_000);
        try {
          const url = `https://financialmodelingprep.com/stable/income-statement?symbol=${ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}`;
          data = await httpGet(url);
          await sleep(FMP_INTERVAL);
        } catch (retryErr) {
          // Persistent 429 or 402 = quota exhausted — stop FMP entirely
          quotaExhausted = true;
          notify(`⚠️ FMP persistently rate-limited — stopping fundamentals for this cycle (SP-1: yfinance fallback removed)`);
          if (_alertPost) _alertPost(`⚠️ **FMP quota/rate-limit exhausted** — fundamentals stopping for this cycle`);
          tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
          continue;
        }
      } else {
        _stats.errors++;
        tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
        await store.logRun(ticker, 'fundamentals', 'error', 0, err.message, Date.now() - start, 1);
        continue;
      }
    }

    if (!Array.isArray(data) || !data.length) {
      tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
      continue;
    }

    // Fetch FMP ratios for ROE, ROIC, D/E, P/FCF — used by S10_quality_value
    let ratiosData = null;
    try {
      const ratiosUrl = `https://financialmodelingprep.com/stable/ratios?symbol=${ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}`;
      ratiosData = await httpGet(ratiosUrl);
      trackApiCall('fmp');
      await sleep(FMP_INTERVAL);
    } catch { /* non-fatal — ratios are supplemental */ }

    // Build ratios lookup by period date
    const ratiosByDate = {};
    if (Array.isArray(ratiosData)) {
      for (const r of ratiosData) {
        if (r.date) ratiosByDate[r.date] = r;
      }
    }

    try {
      const records = data.map(q => {
        const ratio = ratiosByDate[q.date] || {};
        return {
          period:             `${q.calendarYear}Q${q.period?.replace('Q', '') || ''}`,
          period_end:         q.date,
          revenue:            q.revenue,
          gross_profit:       q.grossProfit,
          ebitda:             q.ebitda,
          net_income:         q.netIncome,
          eps:                q.eps,
          gross_margin:       q.grossProfitRatio,
          operating_margin:   q.operatingIncomeRatio,
          net_margin:         q.netIncomeRatio,
          revenue_growth_yoy: null,
          pe_ratio:           null,
          market_cap:         null,
          roe:                ratio.returnOnEquity              ?? null,
          roic:               ratio.returnOnInvestedCapital    ?? null,
          debt_equity_ratio:  ratio.debtEquityRatio            ?? null,
          p_fcf_ratio:        ratio.priceToFreeCashFlowsRatio  ?? null,
          source:             'fmp',
        };
      });

      await store.upsertFundamentals(ticker, records);
      await store.updateCoverage(ticker, 'fundamentals', records.at(-1)?.period_end || today, records[0]?.period_end || today, records.length);
      _stats.fundamentals += records.length;
      tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, records.length);
      await store.logRun(ticker, 'fundamentals', 'success', records.length, null, Date.now() - start, 1);
    } catch (err) {
      _stats.errors++;
      tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
      await store.logRun(ticker, 'fundamentals', 'error', 0, err.message, Date.now() - start, 1);
    }
  }
  // SP-1: yfinance fundamentals fallback removed. When FMP quota is exhausted,
  // uncovered tickers resume on the next daily cycle.
  if (quotaExhausted) {
    const stillNeeded = [];
    for (const tk of tickers) {
      const gaps = await store.getGaps(tk, 'fundamentals', thirtyDaysAgo, today).catch(() => []);
      if (gaps.length > 0) stillNeeded.push(tk);
    }
    if (stillNeeded.length > 0) {
      notify(`💹 FMP quota exhausted — ${stillNeeded.length} tickers deferred to next cycle (SP-1: no yfinance fallback)`);
    }
  }

  try {
    const flushed = await store.flushFundamentals();
    if (flushed && flushed.flushed) {
      console.log(`[collector] Fundamentals flush: ${flushed.flushed} rows → financials.parquet (total ${flushed.total_after})`);
    }
  } catch (err) {
    console.error(`[collector] Fundamentals flush FAILED: ${err.message}`);
  }

  const elapsed = Math.round((Date.now() - _progress.phaseStart) / 1000);
  notify(`✅ Fundamentals complete — ${_progress.rowsThisPhase} new records, ${skipped} skipped (fetched <30d ago) in ${elapsed}s`);
  if (_alertPost) _alertPost(`✅ **Phase 5 complete** — ${_progress.rowsThisPhase} fundamental records added | ${skipped} skipped`);
}

// ── Market price history — Alpaca/FMP stub (SP-1: yfinance removed) ──────────
// TODO Task 15+: implement Alpaca bars + FMP OHLCV for indices/ETFs/crypto/forex.

async function runMarketPricesYFinance(tickers, historyDays = 3650) {
  if (!tickers || tickers.length === 0) return;

  const toDate   = new Date().toISOString().slice(0, 10);
  const fromDate = new Date(Date.now() - historyDays * 86400_000).toISOString().slice(0, 10);
  // SP-1: yfinance removed. Market instrument bars (indices, ETFs, crypto, forex)
  // not yet migrated to Alpaca/FMP. Log and skip — Task 15 implements the replacement.
  notify(`🌐 Market prices: ${tickers.length} instruments deferred — SP-1 Task 14 stub (${fromDate}→${toDate}) — implement via Alpaca/FMP in Task 15`);
  _progress = { phase: '🌐 Market Prices', current: 0, total: tickers.length, ticker: null, phaseStart: Date.now(), rowsThisPhase: 0 };
  // NotImplementedError: yfinance path removed. Market instrument bars
  // (indices, ETFs, crypto, commodities, forex) will be wired via
  // Alpaca bars API + FMP in Task 15.
}

// ── News collection — Alpaca News API (SP-1: yfinance removed) ───────────────
// Note: Alpaca News (src/ingestion/alpaca_news.py) populates ticker_sentiment_daily.
// runNewsCollection (below) writes to market_news table — migration to Alpaca/FMP
// news API or RSS feeds is TODO Task 15+.

async function runInsiderTransactions(tickers = null) {
  if (!tickers) tickers = await getActiveTickers();
  const today = new Date().toISOString().slice(0, 10);
  notify(`📋 Insider: collecting Form 4 data for ${tickers.length} tickers via FMP`);

  const cfg = await store.getConfig().catch(() => ({}));
  const FMP_INTERVAL = parseInt(cfg.fmp_interval_ms || '2000', 10);
  const FMP_PER_DAY  = parseInt(cfg.fmp_req_per_day || '250',  10);

  const { query: dbQuery } = require('../database/postgres');
  let inserted = 0;
  let skipped  = 0;
  let quotaExhausted = false;

  for (let i = 0; i < tickers.length; i++) {
    const ticker = tickers[i];
    if (_paused) { while (_paused) await sleep(1000); }

    if (quotaExhausted || apiQuotaRemaining('fmp', FMP_PER_DAY) <= 0) {
      if (!quotaExhausted) { quotaExhausted = true; notify(`⚠️ FMP daily quota (${FMP_PER_DAY}) reached — insider phase stopping`); }
      continue;
    }

    // Insider filings change infrequently — skip if fetched within 7 days
    const sevenDaysAgo = new Date(Date.now() - 7 * 86400_000).toISOString().slice(0, 10);
    const gaps = await store.getGaps(ticker, 'insider', sevenDaysAgo, today).catch(() => [null]);
    if (gaps.length === 0) { skipped++; continue; }

    const start = Date.now();
    let data = null;
    try {
      const url = `https://financialmodelingprep.com/stable/insider-trading/search?symbol=${ticker}&limit=50&apikey=${FMP_KEY}`;
      data = await httpGet(url);
      trackApiCall('fmp');
      await sleep(FMP_INTERVAL);
    } catch (err) {
      if (err.message.includes('402') || err.message.includes('429')) {
        quotaExhausted = true;
        notify(`⚠️ FMP quota/rate-limit hit during insider phase — stopping`);
        continue;
      }
      // 404 = no insider data for this ticker (ETFs, small caps) — not a real error
      if (err.message.includes('404')) {
        await store.updateCoverage(ticker, 'insider', today, today, 0);
        continue;
      }
      _stats.errors++;
      await store.logRun(ticker, 'insider', 'error', 0, err.message, Date.now() - start, 1);
      continue;
    }

    if (!Array.isArray(data) || !data.length) {
      await store.updateCoverage(ticker, 'insider', today, today, 0);
      continue;
    }

    let rowsInserted = 0;
    for (const txn of data) {
      const shares      = txn.securitiesTransacted != null ? parseFloat(txn.securitiesTransacted) : null;
      const price       = txn.price != null ? parseFloat(txn.price) : null;
      const sharesAfter = txn.securitiesOwned != null ? parseFloat(txn.securitiesOwned) : null;
      await store.bufferInsider({
        ticker,
        filing_date:        txn.filingDate || today,
        date:               txn.filingDate || today,
        transaction_date:   txn.transactionDate || null,
        insider_name:       txn.reportingName || txn.insiderName || null,
        role:               txn.typeOfOwner || txn.role || null,
        transaction_type:   txn.transactionType || null,
        shares,
        price_per_share:    price,
        net_value:          (shares != null && price != null) ? shares * price : null,
        shares_owned_after: sharesAfter,
      });
      rowsInserted++;
    }

    inserted += rowsInserted;
    await store.updateCoverage(ticker, 'insider', today, today, rowsInserted);
    await store.logRun(ticker, 'insider', 'success', rowsInserted, null, Date.now() - start, 1);
  }

  // Flush buffered rows to insider.parquet in one atomic write.
  try {
    const flushed = await store.flushInsider();
    if (flushed && flushed.flushed) {
      console.log(`[collector] Insider flush: ${flushed.flushed} rows → insider.parquet (total ${flushed.total_after})`);
    }
  } catch (err) {
    console.error(`[collector] Insider flush FAILED: ${err.message}`);
  }
  notify(`📋 Insider: ${inserted} new transactions | ${skipped} tickers skipped (fresh)`);
}

async function runNewsCollection(equityTickers) {
  // SP-1 Task 14: yfinance news path removed. market_news ingest via
  // alternative provider (Alpaca News API or RSS) is TODO Task 15+.
  // Alpaca News already populates ticker_sentiment_daily via
  // src/ingestion/alpaca_news.py (different table, different call site).
  notify(`📰 News: collection deferred — SP-1 yfinance removed, Task 15 will wire Alpaca/FMP news to market_news table`);

  // Prune articles older than 30 days (maintenance still runs)
  try {
    const { query: dbQuery } = require('../database/postgres');
    await dbQuery(`DELETE FROM market_news WHERE published_at < NOW() - INTERVAL '30 days'`).catch(() => null);
  } catch (_) {}
}

// fillMissingWithYFinance removed in SP-1 Task 14 — function was never called externally.

// ── Main loop ─────────────────────────────────────────────────────────────────

async function start() {
  if (_running) { console.log('[collector] Already running'); return; }
  _running = true;

  // Integrity check on boot — log + SSE alert on mismatch, never blocks pipeline
  await runIntegrityCheck();

  // Data freshness contract — surface stale datasets loudly on boot
  await runFreshnessCheckOnBoot();

  // Interrupted run detection — surface for operator review, never auto-resume
  try {
    const ps = require('../database/pipeline-state');
    await ps.expireOldRuns();
    const interrupted = await ps.findInterruptedRuns();
    if (interrupted.length > 0) {
      const summary = interrupted.map(r =>
        `  run_id=${r.run_id.slice(0,8)} ticker=${r.ticker || 'N/A'} stage=${r.current_stage} started=${String(r.started_at).slice(0,16)}`
      ).join('\n');
      console.warn(`[INTERRUPTED_RUN] ${interrupted.length} incomplete pipeline run(s) detected at boot:\n${summary}`);
      notify(`⚠️ **${interrupted.length} interrupted pipeline run(s)** detected — use \`/status\` to review. Do not auto-resume.`);
    }
  } catch (err) {
    console.warn('[pipeline-state] Boot check failed:', err.message);
  }

  notify('🚀 Data pipeline started — S&P 100 universe');

  // Phase 2 of the pipeline restructure (2026-04-22): the 10:00 AM ET
  // cron in cron-schedule.js is now the single daily trigger. The
  // collector no longer schedules its own cycle — it only runs when the
  // orchestrator spawns run_collector_once.js, or manually via the
  // /collector command. We keep the 5-minute snapshot loop below for
  // live price refresh.
  // (Legacy: used to call runDailyCollection() immediately + scheduleDailyCollection().)

  // Snapshots every 5 min — only during market hours and when not sleeping
  setInterval(async () => {
    if (!_paused && !_sleeping && isMarketHours()) {
      await runSnapshots().catch(console.error);
    }
  }, 5 * 60_000);
}

async function runDailyCollection() {
  const cycleStart = Date.now();

  // Load config fresh at the start of each cycle
  const cfg = await loadConfig();

  if (cfg.collection_enabled === 'false') {
    notify('⏸️ Collection disabled via pipeline_config — skipping cycle');
    return;
  }

  // ── Budget check (deterministic — zero token cost) ────────────────────────
  const { checkBudget, enforceBudget } = require('../budget/enforcer');
  const budgetStatus     = await checkBudget().catch(() => ({ mode: 'GREEN' }));
  const budgetConstraints = enforceBudget(budgetStatus.mode);
  if (budgetStatus.mode !== 'GREEN') {
    notify(`⚠️ Budget ${budgetStatus.mode} — $${budgetStatus.dailyUsd.toFixed(2)}/day, $${budgetStatus.monthlyUsd.toFixed(2)}/mo (${budgetStatus.pctUsed?.toFixed(0)}% of $${budgetStatus.budgetUsd})`);
  }

  const historyDays = parseInt(cfg.history_days || '3650', 10);

  // ── Universe split ─────────────────────────────────────────────────────────
  // Fetch full universe with category metadata — route each phase accordingly
  const fullUniverse      = await store.getActiveUniverse();
  const equityTickers     = fullUniverse.filter(u => u.category === 'equity').map(u => u.ticker);
  const marketTickers     = fullUniverse.filter(u => u.category !== 'equity').map(u => u.ticker);
  const optionsTickers    = fullUniverse.filter(u => u.has_options).map(u => u.ticker);
  const fundamentalTickers = fullUniverse.filter(u => u.has_fundamentals).map(u => u.ticker);
  const universeLabel     = `SP100 (${equityTickers.length}) + Market (${marketTickers.length})`;

  // Start cycle record
  const cycleId = await store.startCycle().catch(() => null);

  // Snapshot the pre-cycle API call counts so we can diff per-source at the end
  _resetApiCountersIfNewDay();
  const apiAtStart = { fmp: _apiCalls.fmp || 0, polygon: _apiCalls.polygon || 0 };

  notify('📅 Daily collection cycle starting...');
  _stats.lastRun = new Date().toISOString();
  if (_setPresence) _setPresence('📅 Scanning gaps...');
  if (_alertPost) _alertPost(`📅 **Daily collection cycle started** — ${universeLabel} | ${new Date().toUTCString()}`);

  // ── Pre-cycle gap scan (3 SQL queries — replaces per-ticker gap checks per phase) ─
  const today    = new Date().toISOString().slice(0, 10);
  const fromDate = new Date(Date.now() - historyDays * 86400_000).toISOString().slice(0, 10);

  notify('🔍 Scanning data coverage gaps...');
  const gaps = await store.getGapSummary({
    priceTickers:   [...equityTickers, ...marketTickers],
    optionsTickers,
    fundTickers:    fundamentalTickers,
    fromDate,
    toDate:         today,
    fundStaleDays:  45,
  }).catch(() => null);

  if (gaps) {
    const bar = (covered, needed) => {
      const total = covered + needed;
      if (total === 0) return '—';
      const pct = Math.round((covered / total) * 10);
      return `[${'█'.repeat(pct)}${'░'.repeat(10 - pct)}] ${covered}/${total}`;
    };
    const scanLines = [
      `🔍 **Pre-cycle gap scan** — ${today}`,
      '```',
      `Phase  Data type     Coverage                  Need update`,
      `──────────────────────────────────────────────────────────`,
      `P2     Prices        ${bar(gaps.prices.covered,      gaps.prices.needed).padEnd(28)} ${gaps.prices.needed > 0      ? gaps.prices.needed      + ' tickers' : '✅ current'}`,
      `P3     Options       ${bar(gaps.options.covered,     gaps.options.needed).padEnd(28)} ${gaps.options.needed > 0     ? gaps.options.needed     + ' tickers' : '✅ current'}`,
      `P4     Fundamentals  ${bar(gaps.fundamentals.covered,gaps.fundamentals.needed).padEnd(28)} ${gaps.fundamentals.needed > 0? gaps.fundamentals.needed + ' tickers' : '✅ current'}`,
      '```',
    ];
    const allCurrent = gaps.prices.needed === 0 && gaps.options.needed === 0
                    && gaps.fundamentals.needed === 0;
    if (allCurrent) {
      scanLines.push('All data is current — running snapshot refresh only.');
    }
    const scanMsg = scanLines.join('\n');
    notify(scanMsg);
    if (_alertPost) _alertPost(scanMsg);

    // Early exit if nothing to do (snapshots always refresh for live prices)
    if (allCurrent && cfg.collect_prices === 'false' && cfg.collect_options === 'false') {
      notify('⏭️ All data current — skipping collection phases');
      if (_alertPost) _alertPost('⏭️ All data current — cycle complete (snapshot refresh only)');
      await runSnapshots();
      return;
    }
  }

  // Capture per-phase deltas
  const before = { prices: _stats.prices, options: _stats.options, fundamentals: _stats.fundamentals, snapshots: _stats.snapshots };

  // Phase 1: Snapshots — always runs (live price refresh)
  await runSnapshots();

  // Phase 1b: Macro / vol indices — VIX, VIX3M, VVIX via bounded cboe_vol_indices module.
  // (incremental fetch keyed off macro.parquet max_date per series).
  if (cfg.collect_macro !== 'false') {
    await runVolIndices();
  }

  // Phase 1c: Wide-format vol_indices.parquet (vix_close, vvix_close, vix9d_close).
  // Distinct from macro.parquet (long-format) — strategies str01_vvix_early_warning
  // et al read this file directly. Default-on.
  if (cfg.collect_vol_indices_wide !== 'false') {
    await runVolIndicesWide();
  }

  // Phase 2a: S&P 100 equity prices — only tickers with gaps (Alpaca P1; Task 15 wires bars API)
  const priceEquityNeeded = gaps?.prices.tickers.filter(t => equityTickers.includes(t)) ?? equityTickers;
  const priceMarketNeeded = gaps?.prices.tickers.filter(t => marketTickers.includes(t)) ?? marketTickers;
  if (cfg.collect_prices !== 'false' && priceEquityNeeded.length > 0) {
    await runHistoricalPrices(historyDays, priceEquityNeeded);
  } else if (priceEquityNeeded.length === 0) {
    notify('✅ Prices (equity): all current — skipped');
  }

  // Phase 2b: Market instrument prices (stub — SP-1 yfinance removed; Task 15 wires Alpaca/FMP)
  if (cfg.collect_market_prices !== 'false' && priceMarketNeeded.length > 0) {
    await runMarketPricesYFinance(priceMarketNeeded, historyDays);
  } else if (priceMarketNeeded.length === 0) {
    notify('✅ Prices (market): all current — skipped');
  }

  // Phase 2c: 30-minute equity bars (top-250 by avg_dollar_volume_30d)
  // via Polygon REST aggregates. ~1-2 min wall-clock at 4 calls/sec.
  // Strategies str04 (intraday SPY) + str06 (EOD reversal) read this file.
  if (cfg.collect_prices_30m !== 'false') {
    await run30mPrices();
  }

  // Phase 3: Options — only tickers without today's data
  const optionsNeeded = gaps?.options.tickers ?? optionsTickers;
  if (cfg.collect_options !== 'false' && optionsNeeded.length > 0) {
    await runOptions(optionsNeeded);
  } else if (optionsNeeded.length === 0) {
    notify('✅ Options: all current — skipped');
    if (_alertPost) _alertPost('✅ **Phase 3 complete** — all tickers already covered');
  }

  // Phase 3b: iv_history derivation — depends on fresh options_eod, so runs
  // immediately after Phase 3. Skipped automatically until iv_history.parquet
  // has ≥ 30 days of seed history (the staging-approval backfill builds the
  // seed; daily collection just appends yesterday's slice).
  if (cfg.collect_iv_history !== 'false') {
    await runIvHistory();
  }

  // Phase 4: Fundamentals — only stale tickers, budget-aware
  const fundNeeded = gaps?.fundamentals.tickers ?? fundamentalTickers;
  if (cfg.collect_fundamentals !== 'false' && !budgetConstraints.skipFundamentals && fundNeeded.length > 0) {
    await runFundamentals(fundNeeded);
  } else if (budgetConstraints.skipFundamentals) {
    notify(`💰 Fundamentals skipped — budget ${budgetStatus.mode}`);
  } else if (fundNeeded.length === 0) {
    notify('✅ Fundamentals: all current — skipped');
  }

  // Phase 4b: Forward earnings calendar (per-ticker, 1s throttle).
  // Runs after fundamentals because they share the same provider universe.
  if (cfg.collect_earnings_calendar !== 'false') {
    await runEarningsCalendar();
  }

  // Phase 6: News — skipped in YELLOW/RED
  if (cfg.collect_news !== 'false' && !budgetConstraints.skipNews) {
    await runNewsCollection(equityTickers);
  } else if (budgetConstraints.skipNews) {
    notify(`💰 News skipped — budget ${budgetStatus.mode}`);
  }

  // Phase 7: Form 4 Insider Transactions
  if (cfg.collect_insider !== 'false') {
    await runInsiderTransactions(equityTickers);
  }

  // Prune pipeline_runs audit log — market data tables are never deleted
  const { query: dbQuery } = require('../database/postgres');
  await dbQuery(
    `DELETE FROM pipeline_runs WHERE created_at < NOW() - INTERVAL '90 days'`
  ).catch(() => null);

  const durationMs = Date.now() - cycleStart;
  const cycleMins  = Math.round(durationMs / 60000);

  // SP-1: yfinance paths removed from collector — yfinanceCalls is always 0 after Task 14.
  // Vol indices still use cboe_vol_indices.py (bounded, separate counter) — not tracked here.
  const yfinanceCalls = 0;

  // Save completed cycle record and get back the full row for the notification
  let cycleRow = null;
  if (cycleId) {
    cycleRow = await store.completeCycle(cycleId, {
      durationMs,
      snapshotTickers:     _stats.snapshots    - before.snapshots,
      priceRows:           _stats.prices       - before.prices,
      optionsContracts:    _stats.options      - before.options,
      technicalRows:       0,
      fundamentalRecords:  _stats.fundamentals - before.fundamentals,
      polygonCalls:        (_apiCalls.polygon  || 0) - apiAtStart.polygon,
      fmpCalls:            (_apiCalls.fmp      || 0) - apiAtStart.fmp,
      yfinanceCalls,
      errors:              _stats.errors,
    });
  }

  const fmtN = (n) => Number(n || 0).toLocaleString();
  notify(`✅ Daily collection complete — ${cycleMins}m | errors: ${_stats.errors}`);

  // Legacy DB→parquet sync step removed 2026-04-22: parquet-primary collector
  // already writes directly to master parquets in each phase (Phase 2 prices,
  // Phase 3 options, Phase 4 fundamentals, insider, macro). The referenced
  // scripts/sync_master_parquets.py was deleted in the 2026-04-21 migration.

  if (_alertPost) {
    const r = cycleRow;
    const lines = [
      `✅ **Daily cycle complete** — cycle #${r?.id ?? cycleId ?? '?'}`,
      `⏱️ Duration: **${cycleMins}m** | Status: ${(r?.errors || 0) > 0 ? '⚠️ complete-with-errors' : '✅ complete'}`,
      ``,
      `**Rows collected**`,
      `Snapshots: **${fmtN(r?.snapshot_tickers)}** · Prices: **${fmtN(r?.price_rows)}** · Options: **${fmtN(r?.options_contracts)}** · Tech: **${fmtN(r?.technical_rows)}** · Fund: **${fmtN(r?.fundamental_records)}**`,
      `Total rows: **${fmtN(r?.total_rows)}** | Errors: **${r?.errors ?? _stats.errors}**`,
      ``,
      `**API calls this cycle**`,
      `Polygon: **${fmtN(r?.polygon_calls)}** · FMP: **${fmtN(r?.fmp_calls)}** · Alpaca: (quotes via QuoteMonitor)`,
      ``,
      `*Full history: \`!john /pipeline cycles\`*`,
    ];
    _alertPost(lines.join('\n'));
  }

  // ── Post-collection: spawn orchestrator ────────────────────────────────────
  // In the parquet-primary architecture each collector phase already writes
  // directly to master parquets (prices/options/fundamentals/insider/macro),
  // so the legacy DB→parquet sync step is gone. The orchestrator is idempotent
  // per day via the Redis key pipeline:completed:{date} so repeat triggers
  // (external cron, boot-time catch-up) exit cleanly.
  (() => {
    const { spawn: _spawn } = require('child_process');
    const _rootDir          = require('path').resolve(__dirname, '..', '..');
    const _orchestratorPath = require('path').join(__dirname, '..', 'execution', 'pipeline_orchestrator.py');
    const _runDate          = new Date().toISOString().slice(0, 10);

    const _proc = _spawn('python3', [_orchestratorPath, '--date', _runDate], {
      cwd:      _rootDir,
      env:      { ...process.env, PYTHONPATH: _rootDir },
      detached: true,
      stdio:    'ignore',
    });
    _proc.unref();
    console.log(`[collector] Pipeline orchestrator spawned (pid ${_proc.pid}) for ${_runDate}`);
  })();

  enterSleepMode(_nextRunAt);
}

// Compute ms until HH:MM in the given timezone — handles DST automatically
function msUntilNextTrigger(hour, minute, tz) {
  const now = new Date();
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour: 'numeric', minute: 'numeric', second: 'numeric', hour12: false,
  }).formatToParts(now);
  const p = {};
  for (const { type, value } of parts) p[type] = parseInt(value, 10);
  const currentMins = (p.hour % 24) * 60 + p.minute;
  const targetMins  = hour * 60 + minute;
  let minutesUntil  = targetMins - currentMins;
  if (minutesUntil <= 1) minutesUntil += 24 * 60; // never fire within 1 min of last run
  return minutesUntil * 60_000 - (p.second || 0) * 1000;
}

// Reads trigger time from pipeline_config each cycle — change takes effect next schedule
// Also sets _nextRunAt and schedules a wake-up 5min before the trigger
function scheduleDailyCollection(fn) {
  async function schedule() {
    const cfg     = await store.getConfig().catch(() => ({}));
    const timeStr = cfg.daily_trigger_time || '16:30';
    const tz      = cfg.daily_trigger_tz   || 'America/New_York';
    const [hour, minute] = timeStr.split(':').map(Number);
    const delay   = msUntilNextTrigger(hour, minute, tz);
    _nextRunAt    = new Date(Date.now() + delay);
    const nextRun = _nextRunAt.toLocaleString('en-US', { timeZone: tz, timeZoneName: 'short' });
    console.log(`[collector] Next daily collection: ${nextRun} (in ${Math.round(delay / 60_000)}m)`);

    // Wake up 5 minutes before collection so Discord shows the status change
    if (_wakeUpTimer) clearTimeout(_wakeUpTimer);
    const wakeDelay = Math.max(0, delay - 5 * 60_000);
    _wakeUpTimer = setTimeout(() => exitSleepMode(), wakeDelay);

    setTimeout(async () => {
      await fn().catch(console.error);
      schedule(); // re-read config and reschedule for next day
    }, delay);
  }
  schedule();
}

async function runFreshnessCheckOnBoot() {
  try {
    const { runFreshnessCheck } = require('./freshness');
    const result = await runFreshnessCheck();
    if (!result.skipped && result.alerts.length > 0) {
      try {
        const { broadcast: sseB } = require('../channels/api/server');
        sseB({ type: 'data-staleness', alerts: result.alerts, ts: new Date().toISOString() });
      } catch {}
    }
  } catch (err) {
    console.warn('[freshness] Check skipped:', err.message);
  }
}

// ── Boot integrity check ───────────────────────────────────────────────────────
// ── Evening EOD refresh ───────────────────────────────────────────────────────
// Slimmed-down cycle that runs after market close to capture today's EOD
// bars. Only Phase 2 (equity prices + market prices) — no options,
// fundamentals, news, insider, or orchestrator spawn. Force-fetches even
// when getGapSummary says "covered" because the morning cycle's
// updateCoverage may have advanced past today's date_to incorrectly
// (now corrected, but the EOD pass is the durable safety net).
async function runEodRefresh() {
  const cycleStart = Date.now();
  notify('🌅 EOD refresh — fetching today\'s closing bars');

  const fullUniverse  = await store.getActiveUniverse();
  const equityTickers = fullUniverse.filter(u => u.category === 'equity').map(u => u.ticker);
  const marketTickers = fullUniverse.filter(u => u.category !== 'equity').map(u => u.ticker);
  const tickerSet     = new Set([...equityTickers, ...marketTickers]);
  const historyDays   = 7;  // small lookback for EOD gap detection

  // Snapshot coverage state BEFORE the run so we can diff per-ticker after
  // and build an accurate "gaps filled" report. Map: ticker → 'YYYY-MM-DD'.
  const toIso = (v) => {
    if (!v) return null;
    if (v instanceof Date) return v.toISOString().slice(0, 10);
    const s = String(v);
    return s.length >= 10 ? s.slice(0, 10) : null;
  };
  const beforeRows = await store.getAllCoverage('prices').catch(() => []);
  const before = new Map();
  for (const r of beforeRows) {
    if (tickerSet.has(r.ticker)) before.set(r.ticker, toIso(r.date_to));
  }

  await runHistoricalPrices(historyDays, equityTickers);
  await runMarketPricesYFinance(marketTickers, historyDays);

  // Diff coverage AFTER the run.
  const afterRows = await store.getAllCoverage('prices').catch(() => []);
  const after = new Map();
  for (const r of afterRows) {
    if (tickerSet.has(r.ticker)) after.set(r.ticker, toIso(r.date_to));
  }

  // Group: per "new date_to" how many tickers landed there?
  // Also: tickers whose date_to didn't advance (no new bars fetched).
  const advancedByDate = new Map();   // newDate → ticker[]
  const stagnant       = [];           // [{ticker, date}]
  let advancedCount = 0;
  for (const tk of tickerSet) {
    const b = before.get(tk) || null;
    const a = after.get(tk)  || null;
    if (a && a !== b) {
      advancedCount++;
      const list = advancedByDate.get(a) || [];
      list.push(tk);
      advancedByDate.set(a, list);
    } else {
      stagnant.push({ ticker: tk, date: a });
    }
  }

  const elapsed = Math.round((Date.now() - cycleStart) / 1000);
  notify(`✅ EOD refresh complete — ${elapsed}s | advanced: ${advancedCount}/${tickerSet.size}`);

  // Return a structured summary so run_collector_once.js (which owns the
  // webhook plumbing for this path) can format and post a proper alert.
  // Convert advancedByDate Map → plain object so callers can JSON it.
  const advancedByDateObj = {};
  for (const [d, list] of advancedByDate) advancedByDateObj[d] = list;
  return {
    elapsed_s:        elapsed,
    today:            new Date().toISOString().slice(0, 10),
    total_tickers:    tickerSet.size,
    equity_tickers:   equityTickers,
    market_tickers:   marketTickers,
    advanced_count:   advancedCount,
    advanced_by_date: advancedByDateObj,
    stagnant,         // [{ticker, date}]
  };
}

async function runIntegrityCheck() {
  try {
    const { verifyManifest } = require('../security/integrity');
    const result = verifyManifest();
    if (!result.skipped && !result.valid) {
      const { broadcast: sseB } = require('../channels/api/server');
      sseB({ type: 'integrity-violation', failures: result.failures, ts: new Date().toISOString() });
    }
  } catch (err) {
    console.warn('[integrity] Check skipped:', err.message);
  }
}

module.exports = { start, pause, resume, isRunning, isSleeping, getNextRun, getStats, setBroadcast, setDiscordHooks, loadConfig, runSnapshots, runHistoricalPrices, runOptions, fetchOptionsChain, runFundamentals, runInsiderTransactions, runNewsCollection, runIntegrityCheck, runDailyCollection, runEodRefresh };
