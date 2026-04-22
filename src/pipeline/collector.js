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
const AV_KEY      = process.env.ALPHA_VANTAGE_API_KEY;

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

async function httpGet(url, _retryCount = 0) {
  const raw = await new Promise((resolve, reject) => {
    const req = https.get(url, { timeout: 30_000 }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: data }));
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(); reject(new Error('Request timeout')); });
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

// ── Phase 2: Historical prices — gap-aware, never re-fetches known data ────────

async function runHistoricalPrices(daysBack = 3650, tickers = null) {
  if (!tickers) tickers = await getActiveTickers();
  const toDate   = new Date().toISOString().slice(0, 10);
  const fromDate = new Date(Date.now() - daysBack * 86400_000).toISOString().slice(0, 10);
  notify(`📊 Historical prices: checking gaps for ${tickers.length} tickers (target: ${fromDate} → ${toDate})`);
  _progress = { phase: '📊 Prices', current: 0, total: tickers.length, ticker: null, phaseStart: Date.now(), rowsThisPhase: 0 };

  let skipped = 0;
  let totalCalls = 0;
  // Massive/Polygon is options-only — all stock OHLCV comes from Yahoo Finance
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
        const written = await fillPricesYFinance(ticker, gap.from, gap.to);
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

// ── YFinance price gap-fill (Polygon OHLCV not in Options Starter) ────────────

async function fillPricesYFinance(ticker, fromDate, toDate) {
  const { execSync: _ex } = require('child_process');
  const _fs = require('fs');
  const script = `/tmp/yf_prices_${Date.now()}.py`;
  _fs.writeFileSync(script, `
import yfinance as yf, json, sys
tk = yf.Ticker("${ticker}")
hist = tk.history(start="${fromDate}", end="${toDate}", auto_adjust=True)
bars = []
for dt, row in hist.iterrows():
    bars.append({
        "t": int(dt.timestamp() * 1000),
        "o": float(row["Open"]), "h": float(row["High"]),
        "l": float(row["Low"]),  "c": float(row["Close"]),
        "v": float(row["Volume"])
    })
print(json.dumps(bars))
`);
  const out  = _ex(`python3 ${script}`, { timeout: 30_000, stdio: 'pipe' }).toString().trim();
  _fs.unlinkSync(script);
  const bars   = JSON.parse(out);
  const written = bars.length > 0 ? await store.upsertPrices(ticker, bars) : 0;
  return written;
}


// ── Phase 3: Options chains — Polygon Options Starter or Yahoo Finance fallback ─

async function runOptions(tickers = null) {
  if (!tickers) tickers = await getActiveTickers();
  const today = new Date().toISOString().slice(0, 10);
  notify(`🎲 Options chains: ${tickers.length} tickers`);
  if (_alertPost) _alertPost(`🎲 **Phase 3: Options** starting — ${tickers.length} tickers`);
  _progress = { phase: '🎲 Options', current: 0, total: tickers.length, ticker: null, phaseStart: Date.now(), rowsThisPhase: 0 };

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

  // Polygon Options Starter tier — options snapshot endpoint confirmed authorised
  // Yahoo Finance fallback retained only for unexpected auth failures
  for (let i = 0; i < needed.length; i++) {
    const ticker = needed[i];
    if (_paused) { while (_paused) await sleep(1000); }
    const start = Date.now();
    try {
      const url = `https://api.polygon.io/v3/snapshot/options/${ticker}?limit=250&apiKey=${POLYGON_KEY}`;
      const data = await rateLimitedCall(() => httpGet(url));
      const contracts = data.results || [];
      const written = await store.upsertOptions(ticker, contracts, today);
      await store.updateCoverage(ticker, 'options', today, today, written);
      _stats.options += written;
      tickProgress('🎲 Options', skipped + i + 1, tickers.length, ticker, written);
      await store.logRun(ticker, 'options', 'success', written, null, Date.now() - start, 1);
    } catch (err) {
      // If we hit an auth error (plan changed / endpoint removed) fall back to YFinance for remainder
      if (err.message.includes('403') || err.message.includes('NOT_AUTHORIZED')) {
        notify(`🎲 Options: Polygon auth error on ${ticker} — switching to Yahoo Finance fallback`);
        await runOptionsYFinance(needed.slice(i), today, skipped + i);
        return;
      }
      _stats.errors++;
      tickProgress('🎲 Options', skipped + i + 1, tickers.length, ticker, 0);
      await store.logRun(ticker, 'options', 'error', 0, err.message, Date.now() - start, 1);
    }
  }
  const elapsed = Math.round((Date.now() - _progress.phaseStart) / 1000);
  notify(`✅ Options chains complete — ${_progress.rowsThisPhase.toLocaleString()} contracts in ${elapsed}s`);
  if (_alertPost) _alertPost(`✅ **Phase 3 complete** — ${_progress.rowsThisPhase.toLocaleString()} option contracts stored`);
}

// Yahoo Finance options fallback — fetches nearest 3 expiries per ticker via yfinance
async function runOptionsYFinance(tickers, today, alreadySkipped = 0) {
  const { execSync } = require('child_process');
  const fs = require('fs');
  const script = `/tmp/yf_options_${Date.now()}.py`;
  const tickerList = JSON.stringify(tickers);

  fs.writeFileSync(script, `
import yfinance as yf, json, math
from datetime import date as _date

tickers = ${tickerList}
today_str = "${today}"
today = _date.fromisoformat(today_str)
RISK_FREE_RATE = 0.05
results = []

def f(v):
    try:
        x = float(v)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except: return None

def i(v):
    x = f(v)
    return None if x is None else int(x)

def _ncdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def _npdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def bs_greeks(S, K, T, r, sigma, option_type):
    """Black-Scholes Greeks; returns dict or None on bad inputs."""
    if None in (S, K, T, sigma) or S <= 0 or K <= 0 or T <= 0 or sigma <= 0 or sigma > 10:
        return None
    try:
        d1  = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2  = d1 - sigma * math.sqrt(T)
        nd1 = _ncdf(d1)
        nd2 = _ncdf(d2)
        pdf1 = _npdf(d1)
        gamma = pdf1 / (S * sigma * math.sqrt(T))
        vega  = S * pdf1 * math.sqrt(T) / 100.0   # per 1% IV
        if option_type == 'call':
            delta = nd1
            theta = (-(S * pdf1 * sigma) / (2 * math.sqrt(T))
                     - r * K * math.exp(-r * T) * nd2) / 365.0
            rho   = K * T * math.exp(-r * T) * nd2 / 100.0
        else:
            delta = nd1 - 1.0
            theta = (-(S * pdf1 * sigma) / (2 * math.sqrt(T))
                     + r * K * math.exp(-r * T) * (1 - nd2)) / 365.0
            rho   = -K * T * math.exp(-r * T) * (1 - nd2) / 100.0
        return {
            'delta': round(delta, 6), 'gamma': round(gamma, 6),
            'theta': round(theta, 6), 'vega':  round(vega, 6),
            'rho':   round(rho,   6),
        }
    except Exception:
        return None

for ticker in tickers:
    try:
        t = yf.Ticker(ticker)
        # Underlying price for Greek computation
        S = None
        try:
            fi = t.fast_info
            S = f(getattr(fi, 'last_price', None) or getattr(fi, 'regularMarketPrice', None))
        except Exception:
            pass
        if S is None:
            try:
                hist = t.history(period='1d')
                if not hist.empty:
                    S = f(hist['Close'].iloc[-1])
            except Exception:
                pass

        exps = (t.options or [])[:3]
        contracts = []
        for exp in exps:
            try:
                T = (_date.fromisoformat(exp) - today).days / 365.0
                if T <= 0:
                    continue
                chain = t.option_chain(exp)
                for side, df in [('call', chain.calls), ('put', chain.puts)]:
                    for _, row in df.iterrows():
                        strike = f(row.get('strike'))
                        iv     = f(row.get('impliedVolatility'))
                        g      = bs_greeks(S, strike, T, RISK_FREE_RATE, iv, side)
                        contracts.append({
                            'expiry':        exp,
                            'strike':        strike,
                            'contract_type': side,
                            'iv':            iv,
                            'open_interest': i(row.get('openInterest')),
                            'volume':        i(row.get('volume')),
                            'last_price':    f(row.get('lastPrice')),
                            'bid':           f(row.get('bid')),
                            'ask':           f(row.get('ask')),
                            'delta':         g['delta'] if g else None,
                            'gamma':         g['gamma'] if g else None,
                            'theta':         g['theta'] if g else None,
                            'vega':          g['vega']  if g else None,
                            'rho':           g['rho']   if g else None,
                        })
            except Exception:
                pass
        results.append({'ticker': ticker, 'contracts': contracts})
    except Exception as e:
        results.append({'ticker': ticker, 'error': str(e)})

print(json.dumps(results))
`);

  notify(`🎲 Options (YFinance): fetching ${tickers.length} tickers — nearest 3 expiries each`);

  try {
    const output = execSync(`python3 ${script}`, { timeout: 600_000, maxBuffer: 64 * 1024 * 1024 }).toString();
    const results = JSON.parse(output);
    let totalWritten = 0;

    for (let i = 0; i < results.length; i++) {
      const r = results[i];
      if (_paused) { while (_paused) await sleep(1000); }
      if (r.error || !r.contracts?.length) {
        tickProgress('🎲 Options', alreadySkipped + i + 1, alreadySkipped + tickers.length, r.ticker, 0);
        if (r.error) await store.logRun(r.ticker, 'options', 'error', 0, r.error, 0, 0);
        continue;
      }

      // Flat format — upsertOptions handles both flat and Polygon-nested
      const contracts = r.contracts.map(c => ({
        expiry_date:        c.expiry,
        strike_price:       c.strike,
        contract_type:      c.contract_type,
        greeks:             { delta: c.delta, gamma: c.gamma, theta: c.theta, vega: c.vega, rho: c.rho },
        implied_volatility: c.iv,
        open_interest:      c.open_interest,
        day:                { volume: c.volume, last_price: c.last_price },
        bid:                c.bid,
        ask:                c.ask,
      }));

      const written = await store.upsertOptions(r.ticker, contracts, today);
      await store.updateCoverage(r.ticker, 'options', today, today, written);
      _stats.options += written;
      totalWritten += written;
      tickProgress('🎲 Options', alreadySkipped + i + 1, alreadySkipped + tickers.length, r.ticker, written);
      await store.logRun(r.ticker, 'options', 'success', written, null, 0, 0);
    }

    const elapsed = Math.round((Date.now() - _progress.phaseStart) / 1000);
    try {
      const flushed = await store.flushOptions();
      if (flushed && flushed.flushed) {
        console.log(`[collector] Options flush: ${flushed.flushed} rows → options_eod.parquet (total ${flushed.total_after})`);
      }
    } catch (err) {
      console.error(`[collector] Options flush FAILED: ${err.message}`);
    }
    notify(`✅ Options (YFinance) complete — ${totalWritten.toLocaleString()} contracts | ${alreadySkipped} skipped (already covered) in ${elapsed}s`);
    if (_alertPost) _alertPost(`✅ **Phase 3 complete** — ${totalWritten.toLocaleString()} option contracts stored via Yahoo Finance (IV, strike, bid/ask, OI)`);
  } catch (err) {
    notify(`⚠️ Options YFinance error: ${err.message}`);
    if (_alertPost) _alertPost(`⚠️ **Phase 3 error** — options fetch failed: ${err.message}`);
  } finally {
    try { fs.unlinkSync(script); } catch {}
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
      if (!quotaExhausted) { quotaExhausted = true; notify(`⚠️ FMP daily quota (${FMP_PER_DAY}) reached — switching to yfinance`); }
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
          // Persistent 429 or 402 = quota exhausted — stop FMP entirely, fall back to yfinance
          quotaExhausted = true;
          notify(`⚠️ FMP persistently rate-limited — switching to Yahoo Finance fallback`);
          if (_alertPost) _alertPost(`⚠️ **FMP quota/rate-limit exhausted** — switching to Yahoo Finance for remaining tickers`);
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
  // If FMP quota was exhausted and tickers remain uncovered, fall back to yfinance
  const uncovered = tickers.filter(async (tk) => {
    const gaps = await store.getGaps(tk, 'fundamentals', thirtyDaysAgo, today).catch(() => [null]);
    return gaps.length > 0;
  });
  if (quotaExhausted) {
    const stillNeeded = [];
    for (const tk of tickers) {
      const gaps = await store.getGaps(tk, 'fundamentals', thirtyDaysAgo, today).catch(() => []);
      if (gaps.length > 0) stillNeeded.push(tk);
    }
    if (stillNeeded.length > 0) {
      notify(`💹 FMP quota exhausted — falling back to Yahoo Finance for ${stillNeeded.length} remaining tickers`);
      await runFundamentalsYFinance(stillNeeded, today);
      return;
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

// Yahoo Finance fundamentals fallback — quarterly income statement via yfinance
async function runFundamentalsYFinance(tickers, today) {
  const { execSync } = require('child_process');
  const fs = require('fs');
  const script = `/tmp/yf_fundamentals_${Date.now()}.py`;
  const tickerList = JSON.stringify(tickers);

  fs.writeFileSync(script, `
import yfinance as yf, json, math

tickers = ${tickerList}
results = []

def f(v):
    try:
        x = float(v)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except: return None

for ticker in tickers:
    try:
        t = yf.Ticker(ticker)
        q = t.quarterly_income_stmt
        if q is None or q.empty:
            results.append({'ticker': ticker, 'error': 'no data'})
            continue

        records = []
        for col in q.columns[:4]:  # last 4 quarters
            period_end = col.strftime('%Y-%m-%d')
            rev   = f(q.loc['Total Revenue', col])    if 'Total Revenue'    in q.index else None
            gp    = f(q.loc['Gross Profit', col])     if 'Gross Profit'     in q.index else None
            ebit  = f(q.loc['EBITDA', col])           if 'EBITDA'           in q.index else None
            ni    = f(q.loc['Net Income', col])       if 'Net Income'       in q.index else None
            eps   = f(q.loc['Basic EPS', col])        if 'Basic EPS'        in q.index else None
            oi    = f(q.loc['Operating Income', col]) if 'Operating Income' in q.index else None
            gm    = (gp / rev) if (rev and gp) else None
            om    = (oi / rev) if (rev and oi) else None
            nm    = (ni / rev) if (rev and ni) else None
            # Period label: e.g. 2025Q4
            year  = col.year
            month = col.month
            qnum  = (month - 1) // 3 + 1
            period = f'{year}Q{qnum}'
            records.append({
                'period': period, 'period_end': period_end,
                'revenue': rev, 'gross_profit': gp, 'ebitda': ebit,
                'net_income': ni, 'eps': eps,
                'gross_margin': gm, 'operating_margin': om, 'net_margin': nm,
            })
        results.append({'ticker': ticker, 'records': records})
    except Exception as e:
        results.append({'ticker': ticker, 'error': str(e)})

print(json.dumps(results))
`);

  notify(`💹 Fundamentals (YFinance): fetching ${tickers.length} tickers`);
  if (_alertPost) _alertPost(`💹 **Fundamentals fallback** — fetching ${tickers.length} tickers via Yahoo Finance (quarterly income statements)`);

  try {
    const output = execSync(`python3 ${script}`, { timeout: 300_000, maxBuffer: 16 * 1024 * 1024 }).toString();
    const results = JSON.parse(output);
    let totalWritten = 0;

    for (let i = 0; i < results.length; i++) {
      const r = results[i];
      if (r.error || !r.records?.length) {
        if (r.error) await store.logRun(r.ticker, 'fundamentals', 'error', 0, `yfinance: ${r.error}`, 0, 0);
        continue;
      }
      const records = r.records.map(rec => ({ ...rec, revenue_growth_yoy: null, pe_ratio: null, market_cap: null, source: 'yfinance' }));
      await store.upsertFundamentals(r.ticker, records);
      await store.updateCoverage(r.ticker, 'fundamentals', records.at(-1)?.period_end || today, records[0]?.period_end || today, records.length);
      _stats.fundamentals += records.length;
      totalWritten += records.length;
      await store.logRun(r.ticker, 'fundamentals', 'success', records.length, null, 0, 0);
    }

    const elapsed = Math.round((Date.now() - _progress.phaseStart) / 1000);
    notify(`✅ Fundamentals (YFinance) complete — ${totalWritten} records for ${results.filter(r => !r.error).length} tickers in ${elapsed}s`);
    if (_alertPost) _alertPost(`✅ **Phase 5 complete** — ${totalWritten} fundamental records via Yahoo Finance | ${results.filter(r => r.error).length} tickers had no data`);
  } catch (err) {
    notify(`⚠️ Fundamentals YFinance error: ${err.message}`);
    if (_alertPost) _alertPost(`⚠️ **Phase 5 error** — yfinance fundamentals failed: ${err.message}`);
  } finally {
    try { fs.unlinkSync(script); } catch {}
  }
}

// ── Market price history via yfinance batch (indices, ETFs, crypto, commodities, forex) ──

async function runMarketPricesYFinance(tickers, historyDays = 3650) {
  if (!tickers || tickers.length === 0) return;
  const { execSync } = require('child_process');
  const fs = require('fs');

  const toDate   = new Date().toISOString().slice(0, 10);
  const fromDate = new Date(Date.now() - historyDays * 86400_000).toISOString().slice(0, 10);
  notify(`🌐 Market prices: checking gaps for ${tickers.length} instruments (${fromDate} → ${toDate})`);
  _progress = { phase: '🌐 Market Prices', current: 0, total: tickers.length, ticker: null, phaseStart: Date.now(), rowsThisPhase: 0 };

  // Gap detection: only fetch tickers that have gaps
  const needed = [];
  let skipped = 0;
  for (const ticker of tickers) {
    const gaps = await store.getGaps(ticker, 'prices', fromDate, toDate);
    if (gaps.length === 0) { skipped++; } else { needed.push(ticker); }
  }

  if (needed.length === 0) {
    notify(`✅ Market prices complete — all ${skipped} instruments already current`);
    return;
  }

  // Batch download via yfinance — one API call for all tickers
  const script = `/tmp/yf_market_${Date.now()}.py`;
  const tickerJson = JSON.stringify(needed);

  fs.writeFileSync(script, `
import yfinance as yf, pandas as pd, json, math

def f(v):
    try:
        x = float(v)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except: return None

tickers = ${tickerJson}
from_date = "${fromDate}"
to_date   = "${toDate}"
results = {}

try:
    raw = yf.download(tickers, start=from_date, end=to_date,
                      auto_adjust=True, progress=False, group_by='ticker')
    for ticker in tickers:
        try:
            if len(tickers) == 1:
                df = raw
            elif isinstance(raw.columns, pd.MultiIndex):
                df = raw[ticker] if ticker in raw.columns.get_level_values(0) else pd.DataFrame()
            else:
                df = raw
            df = df.dropna(subset=['Close']) if not df.empty else df
            if df.empty:
                results[ticker] = {"error": "no data"}
                continue
            rows = []
            for date_idx, row in df.iterrows():
                vol = row.get('Volume')
                rows.append({
                    "date":   str(date_idx.date()),
                    "open":   f(row.get('Open')),
                    "high":   f(row.get('High')),
                    "low":    f(row.get('Low')),
                    "close":  f(row.get('Close')),
                    "volume": int(vol) if f(vol) is not None else None
                })
            results[ticker] = {"bars": rows}
        except Exception as e:
            results[ticker] = {"error": str(e)}
except Exception as e:
    for t in tickers:
        results[t] = {"error": str(e)}

print(json.dumps(results))
`);

  try {
    const output = execSync(`python3 ${script}`, {
      timeout: 300_000, maxBuffer: 128 * 1024 * 1024
    }).toString();
    const results = JSON.parse(output);

    let totalWritten = 0;
    for (const ticker of needed) {
      const r = results[ticker];
      if (!r || r.error) {
        _stats.errors++;
        notify(`⚠️ Market prices: ${ticker} — ${r?.error || 'no result'}`);
        continue;
      }
      const written = await store.upsertPrices(ticker, r.bars, 'yfinance');
      if (written > 0) {
        const dates = r.bars.filter(b => b.date).map(b => b.date).sort();
        await store.updateCoverage(ticker, 'prices', dates[0], dates[dates.length - 1], written);
      }
      _stats.prices += written;
      totalWritten += written;
      tickProgress('🌐 Market Prices', needed.indexOf(ticker) + 1, needed.length, ticker, written);
    }
    try {
      const flushed = await store.flushPrices();
      if (flushed && flushed.flushed) {
        console.log(`[collector] Market prices flush: ${flushed.flushed} rows → prices.parquet (total ${flushed.total_after})`);
      }
    } catch (err) {
      console.error(`[collector] Market prices flush FAILED: ${err.message}`);
    }
    notify(`✅ Market prices complete — ${totalWritten.toLocaleString()} rows for ${needed.length} instruments (${skipped} already current)`);
    if (_alertPost) _alertPost(`✅ **Market Prices** — ${totalWritten.toLocaleString()} rows | ${skipped} skipped | ${needed.length} fetched`);
  } catch (err) {
    _stats.errors++;
    notify(`⚠️ Market prices batch error: ${err.message}`);
  } finally {
    try { fs.unlinkSync(script); } catch {}
  }
}

// ── News collection via yfinance ──────────────────────────────────────────────
// Fetches articles for key market instruments + all SP100 equities.
// Deduped by UUID in DB — safe to run on every cycle.

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
  const { execSync } = require('child_process');
  const fs = require('fs');

  // Broad market tickers to anchor global/macro news
  const marketSeed = [
    '^GSPC','^DJI','^IXIC','^RUT','^VIX','^TNX','^TYX',
    'SPY','QQQ','TLT','GLD','GC=F','CL=F','BTC-USD','ETH-USD',
    '^STOXX50E','^N225','^HSI','^FTSE','EFA','EEM','USO','HYG'
  ];
  const allTickers = [...new Set([...marketSeed, ...equityTickers])];
  notify(`📰 News: fetching for ${allTickers.length} instruments`);

  const script = `/tmp/yf_news_${Date.now()}.py`;
  const tickerJson = JSON.stringify(allTickers);

  fs.writeFileSync(script, `
import yfinance as yf, json
from concurrent.futures import ThreadPoolExecutor, as_completed

tickers = ${tickerJson}

def parse(article, primary):
    c = article.get('content', {})
    if not c:
        return None
    uid     = c.get('id') or article.get('id', '')
    if not uid:
        return None
    title   = c.get('title', '').strip()
    if not title:
        return None
    summary = c.get('summary', '').strip()[:600]
    pub_obj = c.get('provider', {})
    publisher = pub_obj.get('displayName', '') if isinstance(pub_obj, dict) else ''
    url_obj = c.get('canonicalUrl') or c.get('clickThroughUrl', {})
    url     = url_obj.get('url', '') if isinstance(url_obj, dict) else ''
    pub_date = c.get('pubDate') or c.get('displayTime', '')
    # Normalise related tickers
    rel_raw = c.get('relatedTickers', [])
    related = []
    if isinstance(rel_raw, list):
        for r in rel_raw:
            sym = r.get('symbol','') if isinstance(r, dict) else str(r)
            if sym: related.append(sym.upper())
    return {
        'uuid': uid, 'primary_ticker': primary,
        'title': title, 'summary': summary,
        'publisher': publisher, 'url': url,
        'published_at': pub_date, 'related_tickers': related
    }

def score(a):
    # Higher = more informative: reward long summary, related tickers, real URL
    return (len(a.get('summary') or '') * 2
            + len(a.get('related_tickers') or []) * 10
            + (50 if a.get('url') else 0))

def best_for_ticker(candidates):
    if not candidates:
        return None
    return max(candidates, key=score)

def fetch(ticker):
    candidates = []
    try:
        t = yf.Ticker(ticker)
        for a in (t.news or [])[:12]:
            parsed = parse(a, ticker)
            if parsed:
                candidates.append(parsed)
    except:
        pass
    if not candidates:
        return None
    # Sort by score descending — best article first, rest stored compactly
    ranked = sorted(candidates, key=score, reverse=True)
    primary = ranked[0]
    # Pack remaining articles as compact JSONB (drop primary_ticker — redundant)
    rest = [{'uuid': a['uuid'], 'title': a['title'], 'publisher': a['publisher'],
              'url': a['url'], 'published_at': a['published_at'], 'summary': a['summary']}
            for a in ranked[1:] if a.get('title')]
    primary['related_articles'] = rest
    return primary

articles = []
with ThreadPoolExecutor(max_workers=8) as ex:
    futures = {ex.submit(fetch, t): t for t in tickers}
    for fut in futures:
        item = fut.result()
        if item:
            articles.append(item)

print(json.dumps(articles))
`);

  try {
    const output = execSync(`python3 ${script}`, {
      timeout: 180_000, maxBuffer: 32 * 1024 * 1024
    }).toString();
    const articles = JSON.parse(output);
    const inserted = await store.upsertNews(articles);
    notify(`✅ News: ${inserted} new articles (${articles.length} fetched, deduped)`);

    // Prune articles older than 30 days
    const { query: dbQuery } = require('../database/postgres');
    await dbQuery(`DELETE FROM market_news WHERE published_at < NOW() - INTERVAL '30 days'`).catch(() => null);
  } catch (err) {
    _stats.errors++;
    notify(`⚠️ News collection error: ${err.message?.slice(0,120)}`);
  } finally {
    try { fs.unlinkSync(script); } catch {}
  }
}

// ── YFinance fallback for missing data ────────────────────────────────────────

async function fillMissingWithYFinance(tickers) {
  notify(`🔄 YFinance fallback for ${tickers.length} tickers`);
  // This writes a Python script and executes it via shell
  const { execSync } = require('child_process');
  const script = `/tmp/yf_fill_${Date.now()}.py`;
  const tickerList = JSON.stringify(tickers);

  require('fs').writeFileSync(script, `
import yfinance as yf, json, math

def f(v):
    try:
        x = float(v)
        return None if (math.isnan(x) or math.isinf(x)) else x
    except: return None

tickers = ${tickerList}
results = []
for ticker in tickers:
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="1y")
        if hist.empty:
            continue
        rows = []
        for date, row in hist.iterrows():
            rows.append({
                "date": str(date.date()),
                "open": f(row["Open"]), "high": f(row["High"]),
                "low": f(row["Low"]), "close": f(row["Close"]),
                "volume": int(row["Volume"]) if not math.isnan(float(row["Volume"])) else None
            })
        results.append({"ticker": ticker, "bars": rows})
    except Exception as e:
        results.append({"ticker": ticker, "error": str(e)})

print(json.dumps(results))
`);

  try {
    const output = execSync(`python3 ${script}`, { timeout: 300_000 }).toString();
    const results = JSON.parse(output);
    for (const r of results) {
      if (r.error) continue;
      await store.upsertPrices(r.ticker, r.bars, 'yfinance');
    }
    notify(`✅ YFinance fill complete: ${results.filter(r => !r.error).length}/${tickers.length}`);
  } catch (err) {
    notify(`⚠️ YFinance fill error: ${err.message}`);
  } finally {
    require('fs').unlinkSync(script);
  }
}

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

  // Immediate first run
  await runDailyCollection();

  // Schedule daily collection — also enters sleep after each run and wakes 5min before next
  scheduleDailyCollection(async () => {
    exitSleepMode();
    await runDailyCollection();
  });

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

  // Phase 2a: S&P 100 equity prices — only tickers with gaps (yfinance fallback if Polygon 403)
  const priceEquityNeeded = gaps?.prices.tickers.filter(t => equityTickers.includes(t)) ?? equityTickers;
  const priceMarketNeeded = gaps?.prices.tickers.filter(t => marketTickers.includes(t)) ?? marketTickers;
  if (cfg.collect_prices !== 'false' && priceEquityNeeded.length > 0) {
    await runHistoricalPrices(historyDays, priceEquityNeeded);
  } else if (priceEquityNeeded.length === 0) {
    notify('✅ Prices (equity): all current — skipped');
  }

  // Phase 2b: Market instrument prices — yfinance batch
  if (cfg.collect_market_prices !== 'false' && priceMarketNeeded.length > 0) {
    await runMarketPricesYFinance(priceMarketNeeded, historyDays);
  } else if (priceMarketNeeded.length === 0) {
    notify('✅ Prices (market): all current — skipped');
  }

  // Phase 3: Options — only tickers without today's data
  const optionsNeeded = gaps?.options.tickers ?? optionsTickers;
  if (cfg.collect_options !== 'false' && optionsNeeded.length > 0) {
    await runOptions(optionsNeeded);
  } else if (optionsNeeded.length === 0) {
    notify('✅ Options: all current — skipped');
    if (_alertPost) _alertPost('✅ **Phase 3 complete** — all tickers already covered');
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

  // yfinance call count: market prices batch (1) + options fallback + fundamentals fallback
  const yfinanceCalls = (marketTickers.length > 0 ? 1 : 0)
                      + (_stats.options - before.options > 0 ? 1 : 0)
                      + (_stats.fundamentals - before.fundamentals > 0 && (_apiCalls.fmp || 0) - apiAtStart.fmp === 0 ? 1 : 0);

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
      `Polygon: **${fmtN(r?.polygon_calls)}** · FMP: **${fmtN(r?.fmp_calls)}** · YFinance: **${fmtN(r?.yfinance_calls)}**`,
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

module.exports = { start, pause, resume, isRunning, isSleeping, getNextRun, getStats, setBroadcast, setDiscordHooks, loadConfig, runSnapshots, runHistoricalPrices, runOptions, runFundamentals, runInsiderTransactions, runNewsCollection, runIntegrityCheck };
