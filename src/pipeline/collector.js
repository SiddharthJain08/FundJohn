'use strict';

/**
 * OpenClaw Background Data Collector
 *
 * Collects OHLCV (EOD + intraday snapshots) and fundamentals for the resolved
 * union universe using a rate-limited queue. Since the SP-1 provider cutover
 * (2026-05-22) the sources are:
 *   - Alpaca (AAT Plus): equity/ETF bars via fillPricesAlpaca (--adjustment
 *     all), crypto bars via fillPricesAlpacaCrypto, intraday snapshots.
 *     Symbol bridge: dash-form share classes (BRK-B) normalize to Alpaca's
 *     dot form at the API boundary — applied at BOTH the EOD and intraday
 *     fill sites (multi-char suffixes included; fixed 2026-07-15).
 *   - FMP: index/FX historical prices via fillPricesFmpHistorical, plus
 *     fundamentals (bounded per ticker per day).
 *   Polygon/Massive/Yahoo/AlphaVantage are GONE (SP-1 + AV purge); legacy
 *   polygon_* config knobs below survive only as inert defaults.
 *
 * Freshness gate: the EOD lane verifies equity freshness against the
 * strategy-CONSUMED scope (_signalsConsumedScope — union of live-strategy
 * universes ∩ fetchable), NOT the wide SP-7 resolver envelope; degraded
 * paths fall back to universe_config, never the envelope (2026-07-15
 * starvation fix). Coverage rows (data_coverage) advance only after parquet
 * writes durably commit (store._commitPriceCoverage).
 */

const https = require('https');
const store = require('./store');
const { runAlpaca } = require('../channels/api/alpaca_cli');

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
  return getUniverse('SP500');
}

// ── SP-2 Phase B Task 5: quarantine set ──────────────────────────────────────
// Bootstrapped once at start(). Downstream parquet-row consumers (e.g. any
// future JS-side prices.parquet reader) can consult _quarantineSet to drop
// "<ticker>|<date>" rows that were quarantined by the backfill audit.
// We seed the prices.parquet master_table here because that is the only
// JS-readable parquet today; extend to other tables by adding entries to
// QUARANTINE_TABLES below.
const QUARANTINE_TABLES = ['prices.parquet'];
const _quarantineSet = new Set();

function isQuarantined(ticker, date) {
  return _quarantineSet.has(`${ticker}|${date}`);
}

function loadQuarantineSet() {
  // Synchronous spawn so the set is populated before the snapshot loop
  // and runDailyCollection can begin. The Python CLI prints a JSON array
  // of [symbol, date_iso] pairs. Failures are logged + non-fatal — we
  // prefer to run with an empty set than block collection on a transient
  // PG hiccup at boot.
  const { spawnSync } = require('child_process');
  for (const table of QUARANTINE_TABLES) {
    try {
      const proc = spawnSync('python3', [
        '-m', 'src.pipeline.quarantine_filter',
        '--master-table', table, '--json',
      ], {
        cwd: '/root/openclaw',
        encoding: 'utf8',
        timeout: 30_000,
        env: process.env,
      });
      if (proc.status !== 0) {
        console.warn(`[collector] quarantine_filter ${table} exited ${proc.status}: ${proc.stderr || '(no stderr)'}`);
        continue;
      }
      const pairs = JSON.parse(proc.stdout || '[]');
      for (const [sym, dt] of pairs) _quarantineSet.add(`${sym}|${dt}`);
      console.log(`[collector] Loaded ${pairs.length} quarantined (ticker,date) pairs for ${table}`);
    } catch (err) {
      console.warn(`[collector] quarantine_filter ${table} bootstrap failed: ${err.message}`);
    }
  }
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

// SP-2 Phase A: read the resolved union from Redis (populated by the daily
// cycle) or shell out to the resolver CLI as a fallback.
// SP-7 Phase C: generalized to support kind='envelope' (no-floor fetch list).
async function readUnionUniverseFromRedis(redis, dateStr, states = 'live', kind = 'union') {
  const key = `universe:${kind}:${dateStr}:${states}`;
  try {
    const cached = await redis.get(key);
    if (cached) return JSON.parse(cached);
  } catch (e) {
    console.warn(`[collector] redis universe read failed: ${e.message}`);
  }
  try {
    const { execSync } = require('child_process');
    const envelopeFlag = kind === 'envelope' ? ' --envelope' : '';
    const out = execSync(
      `python3 -m src.strategies.universe_resolver --as-of ${dateStr} --states ${states} --json${envelopeFlag}`,
      { encoding: 'utf8', timeout: 120000, cwd: '/root/openclaw' },
    );
    const tickers = JSON.parse(out);
    // ioredis takes positional EX args; the old `{ EX: 14400 }` object form is
    // node-redis-v4 syntax that ioredis ignores (latent bug — this function
    // had zero callers until SP-7 Phase C).
    try { await redis.set(key, JSON.stringify(tickers), 'EX', 14400); } catch {}
    return tickers;
  } catch (e) {
    console.warn(`[collector] universe ${kind} resolution failed: ${e.message}`);
    // Caller decides the fallback. NEVER substitute a narrower set here —
    // the previous getUniverse('SP500') fallback was a silent-shrink risk.
    return null;
  }
}

// SP-7 Phase C (C2): widen the PRICE fetch list to the resolver's no-floor
// envelope (spec §4). universe_config demotes to operator overlay: its
// active=true equities stay in (union); active=false rows are a HARD
// exclusion applied after the union. Fail-open = keep the config list; the
// fetch envelope must never silently shrink.
async function applyResolverEnvelope(equityTickers, dateStr) {
  if (process.env.OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE !== '1') return equityTickers;
  try {
    const { getClient } = require('../database/redis');
    const envelope = await readUnionUniverseFromRedis(getClient(), dateStr, 'live', 'envelope');
    if (!envelope || envelope.length === 0) {
      console.warn('[collector] envelope unavailable — keeping universe_config list');
      return equityTickers;
    }
    // anchored single-letter bridge (BRK.B→BRK-B; also .U units) — mirrors :637; never touches multi-letter .WS/.RT/.NYB forms
    const envDash = envelope.map(t => t.replace(/^([A-Z]+)\.([A-Z])$/, '$1-$2'));
    const inactive = new Set(await store.getInactiveTickers());
    const merged = [...new Set([...equityTickers, ...envDash])]
      .filter(t => !inactive.has(t)).sort();
    if (merged.length < equityTickers.length) {
      console.warn(`[collector] envelope would SHRINK ${equityTickers.length}→${merged.length} — keeping universe_config list`);
      return equityTickers;
    }
    console.log(`[collector] envelope: resolver=${envelope.length} config=${equityTickers.length} excluded=${inactive.size} final=${merged.length}`);
    return merged;
  } catch (e) {
    console.warn(`[collector] envelope merge failed — keeping universe_config list: ${e.message}`);
    return equityTickers;
  }
}

// SP-7 Phase C (C3 / parent decision 4): expensive per-ticker fetchers
// (fundamentals FMP, insider EDGAR) scope to the FLOORED adopted-union —
// what strategies actually resolve — not the wide fetch envelope.
// Expansion-only: union with the config list, so never-shrink holds.
async function adoptedUnionScope(configTickers, dateStr) {
  if (process.env.OPENCLAW_COLLECTOR_RESOLVER_ENVELOPE !== '1') return configTickers;
  try {
    const { getClient } = require('../database/redis');
    const union = await readUnionUniverseFromRedis(getClient(), dateStr, 'live', 'union');
    if (!union || union.length === 0) return configTickers;
    // anchored single-letter bridge (BRK.B→BRK-B; also .U units) — mirrors :637; never touches multi-letter .WS/.RT/.NYB forms
    const unionDash = union.map(t => t.replace(/^([A-Z]+)\.([A-Z])$/, '$1-$2'));
    const inactive = new Set(await store.getInactiveTickers());
    return [...new Set([...configTickers, ...unionDash])]
      .filter(t => !inactive.has(t)).sort();
  } catch (e) {
    console.warn(`[collector] adopted-union scope failed — config scope kept: ${e.message}`);
    return configTickers;
  }
}

// ── EOD freshness SCOPE (2026-07-15 incident) ────────────────────────────────
// The freshness gate asks one question: "is the panel `signals` reads fresh?"
// It is NOT "is every ticker we opportunistically fetch fresh?". Those are
// different sets, and conflating them halted the fund for five days.
//
// The price fetch runs on the wide resolver envelope BY DESIGN (SP-7 C2) so
// history keeps accruing for names a strategy may adopt later. Reusing that same
// wide list as the gate's denominator drags in thousands of instruments no
// strategy trades — dead OTC ADRs Alpaca's SIP feed cannot carry, delisted
// names, sporadic SPAC units — which can never be fresh at 16:15 ET. Measured on
// the real 07-14 cohort:
//
//   priceEquityTickers (envelope) 12,699  stale 706  94.44%  FAIL  <- the halt
//   universe_config equity         5,082  stale 174  96.58%  pass
//   live-strategy union            7,004  stale  56  99.20%  pass  <- 4.2pp room
//
// 650 of the 706 stale names were consumed by no live strategy. Fetch wide, gate
// narrow.
async function _signalsConsumedScope(configEquityTickers, priceEquityTickers, dateStr, {
  unionFn = null,
} = {}) {
  const fetchable = new Set(priceEquityTickers);
  // Intersect with what we actually fetch: demanding freshness for a ticker no
  // phase ever fetches is an unsatisfiable gate that would halt every cycle.
  // The resolver emits dot-form share classes (BRK.B) while universe_config
  // stores dash-form (BRK-B) — bridge with the same anchored single-letter rule
  // used at :190/:217 so the name doesn't silently drop out of the scope.
  const scope = (list) => [...new Set(list.map(t => t.replace(/^([A-Z]+)\.([A-Z])$/, '$1-$2')))]
    .filter(t => fetchable.has(t)).sort();

  try {
    const readUnion = unionFn || (async () => {
      const { getClient } = require('../database/redis');
      return readUnionUniverseFromRedis(getClient(), dateStr, 'live', 'union');
    });
    const union = await readUnion();
    if (union && union.length) {
      const scoped = scope(union);
      if (scoped.length) return scoped;
      console.warn('[collector] freshness scope: union resolved but nothing intersects the fetch set — falling back to universe_config');
    } else {
      console.warn('[collector] freshness scope: live-strategy union unavailable — falling back to universe_config');
    }
  } catch (e) {
    console.warn(`[collector] freshness scope: union read failed (${e.message}) — falling back to universe_config`);
  }

  // Degraded fallback is universe_config — NEVER priceEquityTickers. The wide
  // envelope is the exact denominator this gate exists to avoid; falling back to
  // it would silently reinstate the halt on the one day the resolver is down.
  // Config still reflects panel health (96.58% on the 07-14 cohort, i.e. pass).
  return scope(configEquityTickers);
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

// Build the error httpGet throws for a non-200 response. The message keeps the
// legacy `HTTP <status>` shape (callers string-match on it), and the status +
// a truncated body ride along so callers can tell FMP's two 402s apart.
function _httpError(status, body, message = null) {
  const err = new Error(message || `HTTP ${status}`);
  err.status = status;
  err.body = String(body || '').slice(0, 300);
  return err;
}

// FMP error taxonomy (D1, 2026-08-23). On the Starter tier a 402 means EITHER
// "daily quota exhausted" OR "this SYMBOL is gated behind a higher tier"
// (preferreds / warrants / units: body "Premium Query Parameter: 'Special
// Endpoint : This value set for 'symbol' is not available under your current
// subscription'"). The fundamentals phase used to read every 402 as quota and
// stop — at ticker ~30, every day, because the gated `.PR*`/`.WS` forms sort
// right after the first "A…" names.
const _FMP_SYMBOL_GATE_RE = /special endpoint|not available under your current subscription|premium query parameter/i;
function _classifyFmpError(err) {
  const msg = String(err?.message || '');
  const status = err?.status ?? (msg.match(/\b(4\d\d|5\d\d)\b/) || [])[1];
  const code = status != null ? Number(status) : null;
  if (code === 402) return _FMP_SYMBOL_GATE_RE.test(err?.body || '') ? 'symbol_gated' : 'quota';
  if (code === 429) return 'rate_limited';
  if (code === 404) return 'not_found';
  return 'other';
}

// D3 (2026-08-23): the 16:30 ET openclaw-options-archive.timer owns the daily
// chain (one merge-write, Redis checkpoints, and — since D3 — data_coverage).
// The in-cycle Phase 3 used to re-fetch the same ~390k contracts ten minutes
// earlier, then SIGKILL its own 2.27 GB flush at the 30 s parquet timeout
// (9/9 daily logs 08-11→08-21, 2.8 GB of orphan *.tmp). It is now opt-in —
// a manual fallback for a day the archive timer did not run.
function _inCycleOptionsEnabled(env = process.env) {
  return String(env.OPENCLAW_COLLECT_OPTIONS_INCYCLE || '') === '1';
}

// Per-cycle scope cap: a freshness-driven scope can be the whole universe the
// first time the freshness check actually works; cap it so one cycle never
// spends the full daily FMP quota. 0 / negative = no cap.
function _capScope(tickers, cap) {
  const n = Number(cap);
  if (!Number.isFinite(n) || n <= 0 || tickers.length <= n) return { tickers, deferred: 0 };
  return { tickers: tickers.slice(0, n), deferred: tickers.length - n };
}

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

  if (raw.status === 403) throw _httpError(403, raw.body, 'Forbidden (403) — check API tier');
  if (raw.status !== 200) throw _httpError(raw.status, raw.body);

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

// 30-minute equity bars — self-sustaining: universe derived from every ticker
// already in prices_30m.parquet (append-only, Alpaca source).
async function run30mPrices() {
  return _runIngestPhase(
    '⏱️', '30m prices (alpaca)',
    ['src/ingestion/ingest_prices_30m_daily.py'],
    'prices_30m', 5 * 60_000,
  );
}

// Per-ticker ATM-30d/90d IV history derived from options_eod.parquet.
// Guard history (2026-08-07): the original ≥30-day guard referenced a
// "staging-approval backfill" seed builder that NEVER EXISTED, so the
// incremental path was deadlocked from birth (15 days < 30, forever). The
// seed was built manually on 2026-08-07 (--rebuild with the new bounded
// pyarrow read) and yields 26 distinct dates — the honest maximum, because
// pre-2026-06-29 options_eod snapshots carry zero/absent greeks (no ATM-band
// deltas). Guard lowered to 20 so daily incrementals accrue from here; the
// original 30 would have re-deadlocked on a ceiling the data cannot reach yet.
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
  if (distinctDays < 20) {
    notify(`⏭️  iv_history skipped — only ${distinctDays} day(s) of seed history; needs ≥ 20`);
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

// Earnings MASTER merge: calendar rows + reported actuals → earnings.parquet
// (append-only). Bounded work — actuals fetch only covers tickers whose event
// just passed unreported.
async function runEarningsMasterMerge() {
  return _runIngestPhase(
    '📅', 'Earnings master merge (calendar + actuals)',
    ['src/ingestion/ingest_earnings_master.py'],
    'earnings', 10 * 60_000,
  );
}

async function runCorporateActions() {
  // 2026-08-07: corporate_actions.parquet had NO caller since its 04-28
  // authoring run (25 rows; split DETECTION moved to split_watcher v2, which
  // queries Alpaca directly). Keep the master itself current too — trailing
  // 60-day window per run, merge dedups on id, so daily runs are idempotent.
  // NOTE: the read-side adjuster stays OFF (OPENCLAW_APPLY_SPLIT_ADJUSTMENT
  // unset) — prices are already split-adjusted at ingest; adjusting on top
  // would double-adjust. This phase only maintains the master.
  const start = new Date(Date.now() - 60 * 86_400_000).toISOString().slice(0, 10);
  return _runIngestPhase(
    '🏢', 'Corporate actions (Alpaca)',
    ['src/pipeline/alpaca_corporate_actions.py', '--start', start],
    'corporate_actions', 10 * 60_000,
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

  // 2026-08-22: the daily fill is batched through `alpaca data multi-bars`
  // (one request per ~200 tickers sharing a gap range) instead of one CLI
  // process per ticker (~5 180 × ~65 ms of pure TLS/process overhead ≈ 6 min
  // per day). OPENCLAW_PRICES_MULTI_BARS=0 restores the per-ticker loop below
  // verbatim. Both paths share gap detection, per-ticker logRun/coverage
  // semantics and the 1000-ticker mid-flush.
  const useMultiBars = process.env.OPENCLAW_PRICES_MULTI_BARS !== '0';
  if (useMultiBars) {
    ({ skipped, totalCalls } = await _runHistoricalPricesBatched(tickers, fromDate, toDate));
  } else {
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
        // NO updateCoverage here. `written` counts rows BUFFERED, not stored, and
        // this passed `gap.to` (the range REQUESTED) rather than the max bar
        // returned — so coverage claimed data the parquet never received. Coverage
        // is now derived from durable rows inside store.flushPrices().
        const written = await fillPricesAlpaca(ticker, gap.from, gap.to);
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

    // Periodic flush every 1000 tickers to bound in-memory buffer size.
    // On large universes (12k+ tickers) the buffer can grow to several
    // hundred MB before the end-of-phase flush; a SIGKILL from the OS OOM
    // killer loses everything.  Flushing mid-loop writes partial progress to
    // disk and keeps peak heap modest.  Non-fatal — a flush error here is
    // logged but does not abort the loop (the final flush below still runs).
    if ((i + 1) % 1000 === 0) {
      try {
        const mid = await store.flushPrices();
        if (mid && mid.flushed) {
          console.log(`[collector] Prices mid-flush @ticker ${i + 1}: ${mid.flushed} rows → prices.parquet`);
        }
      } catch (e) {
        console.error(`[collector] Prices mid-flush FAILED (non-fatal): ${e.message}`);
      }
    }
  }
  } // end per-ticker path (OPENCLAW_PRICES_MULTI_BARS=0)

  // Final flush — picks up any rows written since the last mid-flush.
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

// ── Equity / ETF price gap-fill — Alpaca daily bars (SP-1 Task 15) ────────────
// SP-7 A3: canonical price convention = SPLIT-ADJUSTED ONLY (matches the
// Phase-B backfiller). Dividend adjustment restates all history on every
// dividend — incompatible with the append-only master store. Dividends are
// explicit in corporate_actions.parquet; see docs/reference/sp7-adjustment-convention.md.
// Caller (runHistoricalPrices) owns the flush via store.flushPrices() — we
// only buffer via store.upsertPrices.
//
// Symbols Alpaca's stocks endpoint can't serve (indices ^X, futures =F,
// crypto -USD, forex =X) get an HTTP 400 "invalid symbol" — those are warned
// and skipped with rows=0 so the phase keeps going. They flow through
// runMarketPricesNonEquity which fans the ETF subset back here.

async function fillPricesAlpaca(ticker, fromDate, toDate) {
  // Share-class separator: universe convention is `BRK-B` (yfinance style);
  // Alpaca expects `BRK.B`. Translate the API symbol only — rows stay keyed
  // by the universe ticker so downstream consumers don't drift.
  //
  // This was anchored to a SINGLE trailing letter (`/^[A-Z]+-[A-Z]$/`), which
  // silently 400'd every multi-char class suffix. 72 dash-form names that Alpaca
  // reports `active`+`tradable` on a lit exchange — mostly preferred (`AGM-PRI`,
  // `ALL-PRH`, `BA-PRA`, `BAC-PRQ`, `C-PRN`) — never received a bar and sat
  // permanently stale in data_coverage, dragging the EOD freshness gate down.
  // The failure was invisible: the 400 hit fillPricesAlpaca's invalid-symbol
  // branch (warn + return 0), and updateCoverage then CORRECTLY refused to
  // advance on a zero-row fetch — so the fetch "succeeded", coverage stayed
  // honest, and nothing ever alerted. Measured 2026-07-15 — every suffix class
  // resolves as dot-form and 400s as dash-form:
  //   BRK-B / AGM-PRI / AKO-A / AIIA-U / ALUB-WS / OXY-WS / AHT-PRF
  //   → dash = "invalid symbol", dot = real bars (AGM.PRI current through today)
  // so the bridge is unconditional on the first separator. Crypto (`BTC-USD`)
  // never reaches here — it routes to fillPricesAlpacaCrypto.
  const apiSymbol = ticker.replace('-', '.');

  const baseArgs = [
    'data', 'bars',
    '--symbol',     apiSymbol,
    '--start',      fromDate,
    '--end',        toDate,
    '--timeframe',  '1Day',
    '--adjustment', 'split',
    '--feed',       'sip',
    '--limit',      '1000',
  ];

  let written  = 0;
  let pageToken = null;
  let pages = 0;

  while (true) {
    const args = pageToken ? [...baseArgs, '--page-token', pageToken] : baseArgs;
    const r = await runAlpaca(args, { timeout: 30_000 });

    if (!r.ok) {
      const status = r.error?.status;
      const errMsg = r.error?.error || r.stderr?.slice(0, 200) || `exit ${r.exit_code}`;
      // "invalid symbol" 400s — non-stock tickers (indices/futures/crypto/forex)
      // routed here by mistake. Warn once and return 0; don't blow the phase.
      if (status === 400 && /invalid symbol/i.test(errMsg)) {
        console.warn(`[collector] fillPricesAlpaca: skip non-stock symbol ${ticker}${apiSymbol !== ticker ? `→${apiSymbol}` : ''} (${errMsg})`);
        return written;
      }
      throw new Error(`alpaca data bars ${ticker}${apiSymbol !== ticker ? `→${apiSymbol}` : ''} (${fromDate}→${toDate}): ${errMsg}`);
    }

    const bars = r.payload?.bars;
    if (Array.isArray(bars) && bars.length > 0) {
      written += await store.upsertPrices(ticker, bars, 'alpaca');
    }

    pageToken = r.payload?.next_page_token || null;
    pages++;
    if (!pageToken) break;
    // Hard cap so a malformed pagination token can't pin us.
    if (pages >= 20) {
      console.warn(`[collector] fillPricesAlpaca: ${ticker} hit 20-page cap, stopping early`);
      break;
    }
  }

  return written;
}

// ── Batched equity price fill — `alpaca data multi-bars` (2026-08-22) ─────────
// Live contract of the endpoint (probed on the CLI, see
// docs/reference/alpaca-cli-integration.md §5/§8):
//   * well-formed symbols Alpaca doesn't know are silently ABSENT from `bars`
//     (rc=0) — that ticker simply gets 0 rows, exactly like the per-ticker
//     "null bars" case, and coverage stays honest (flushPrices derives it
//     from durable rows);
//   * a MALFORMED symbol (`BRK-B`, `^GSPC`, `ES=F`, `BTC-USD`) fails the WHOLE
//     request with rc=1 `invalid symbol: X` — so symbols are pre-filtered by
//     Alpaca's stock grammar and routed to the per-ticker path (which already
//     warns + skips them), and if one still slips through the named symbol is
//     dropped and the chunk retried;
//   * `--limit 10000` counts data points across ALL symbols, so the chunk
//     size shrinks with the gap range (200 for a 1-day gap, ~3 for a 10-year
//     new-ticker backfill) to keep each request ≈ one page;
//   * any other chunk failure (5xx, timeout) falls back to the per-ticker
//     path for that chunk only, so a transient error degrades to today's
//     behaviour instead of losing 200 tickers.

const MULTI_BARS_MAX_SYMBOLS = 200;    // URL-length headroom; 200×1 day = 42 KB, 0.3 s
const MULTI_BARS_TARGET_POINTS = 8000; // keep a request under the 10 000-point page
const MULTI_BARS_MAX_PAGES = 200;

function _isAlpacaStockSymbol(apiSymbol) {
  return /^[A-Z][A-Z0-9]*(\.[A-Z]+)?$/.test(apiSymbol || '');
}

// Group gap items by identical (from,to) so one request never widens a
// ticker's range (a re-fetch of known rows is wasted quota; a narrower one is
// a coverage hole). Insertion order is preserved.
function _groupGapItems(items) {
  const groups = new Map();
  for (const it of items) {
    const key = `${it.from}|${it.to}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(it);
  }
  return groups;
}

function _multiBarsChunkSize(fromDate, toDate) {
  const days = Math.round((Date.parse(toDate) - Date.parse(fromDate)) / 86_400_000);
  if (!Number.isFinite(days) || days < 0) return 1;
  const tradingDays = Math.max(1, Math.round(days * 5 / 7) + 1);
  return Math.max(1, Math.min(MULTI_BARS_MAX_SYMBOLS, Math.floor(MULTI_BARS_TARGET_POINTS / tradingDays)));
}

// One multi-bars chunk: pages through next_page_token, fans `bars[api]` back
// to store.upsertPrices keyed by the UNIVERSE ticker. Returns
// { written: Map<api, rows>, calls, error } — `error` set means the caller
// should fall back to the per-ticker path for this chunk.
async function _fetchMultiBarsChunk(chunk, fromDate, toDate) {
  const byApi = new Map(chunk.map((c) => [c.api, c.ticker]));
  let symbols = chunk.map((c) => c.api);
  const written = new Map();
  let calls = 0;
  let pageToken = null;
  let pages = 0;
  let invalidDrops = 0;

  while (symbols.length) {
    const args = [
      'data', 'multi-bars',
      '--symbols',    symbols.join(','),
      '--start',      fromDate,
      '--end',        toDate,
      '--timeframe',  '1Day',
      '--adjustment', 'split',
      '--feed',       'sip',
      '--limit',      '10000',
    ];
    if (pageToken) args.push('--page-token', pageToken);
    const r = await runAlpaca(args, { timeout: 60_000 });
    calls++;

    if (!r.ok) {
      const errMsg = r.error?.error || r.stderr?.slice(0, 200) || `exit ${r.exit_code}`;
      const m = /invalid symbol:\s*([^\s,"]+)/i.exec(errMsg);
      // Validation rejects happen before any page is served; drop the named
      // symbol (warn + 0 rows, same as the per-ticker invalid-symbol branch)
      // and retry the chunk. Bounded so a pathological chunk can't loop.
      if (m && pages === 0 && invalidDrops < 3 && symbols.includes(m[1])) {
        const bad = m[1];
        console.warn(`[collector] multi-bars: skip non-stock symbol ${byApi.get(bad) || bad}${byApi.get(bad) && byApi.get(bad) !== bad ? `→${bad}` : ''} (${errMsg})`);
        written.set(bad, 0);
        symbols = symbols.filter((x) => x !== bad);
        invalidDrops++;
        continue;
      }
      return { written, calls, error: new Error(`alpaca data multi-bars (${fromDate}→${toDate}, ${symbols.length} symbols): ${errMsg}`) };
    }

    const bars = r.payload?.bars || {};
    for (const [api, rows] of Object.entries(bars)) {
      const ticker = byApi.get(api);
      if (!ticker || !Array.isArray(rows) || rows.length === 0) continue;
      const n = await store.upsertPrices(ticker, rows, 'alpaca');
      written.set(api, (written.get(api) || 0) + n);
    }

    pageToken = r.payload?.next_page_token || null;
    pages++;
    if (!pageToken) break;
    if (pages >= MULTI_BARS_MAX_PAGES) {
      console.warn(`[collector] multi-bars: ${fromDate}→${toDate} chunk hit ${MULTI_BARS_MAX_PAGES}-page cap, stopping early`);
      break;
    }
  }
  return { written, calls, error: null };
}

/**
 * Fill price gaps for many tickers with as few CLI invocations as possible.
 * items: [{ticker, from, to}] — one per gap. onTicker(ticker, written, err)
 * fires once per ITEM as soon as its rows are buffered (err !== null only
 * on the per-ticker fallback path, mirroring fillPricesAlpaca's throw).
 * Returns { calls }.
 */
async function fillPricesAlpacaBatch(items, { onTicker = async () => {} } = {}) {
  let calls = 0;
  const perTicker = async (ticker, fromDate, toDate) => {
    calls++;
    try {
      const w = await fillPricesAlpaca(ticker, fromDate, toDate);
      await onTicker(ticker, w, null);
    } catch (err) {
      await onTicker(ticker, 0, err);
    }
  };

  for (const [, group] of _groupGapItems(items)) {
    const { from: fromDate, to: toDate } = group[0];
    const batchable = [];
    const stragglers = [];
    for (const it of group) {
      // Same share-class bridge as fillPricesAlpaca (BRK-B → BRK.B); rows stay
      // keyed by the universe ticker.
      const api = it.ticker.replace('-', '.');
      if (_isAlpacaStockSymbol(api)) batchable.push({ ticker: it.ticker, api });
      else stragglers.push(it.ticker);
    }

    const size = _multiBarsChunkSize(fromDate, toDate);
    for (let i = 0; i < batchable.length; i += size) {
      const chunk = batchable.slice(i, i + size);
      const r = await _fetchMultiBarsChunk(chunk, fromDate, toDate);
      calls += r.calls;
      if (r.error) {
        console.warn(`[collector] multi-bars chunk failed — falling back to per-ticker for ${chunk.length} tickers: ${r.error.message}`);
        for (const it of chunk) await perTicker(it.ticker, fromDate, toDate);
        continue;
      }
      for (const it of chunk) await onTicker(it.ticker, r.written.get(it.api) || 0, null);
    }
    // Malformed symbols (index/futures/crypto/forex forms) after the batch:
    // the per-ticker path already warns + skips them on the 400.
    for (const ticker of stragglers) await perTicker(ticker, fromDate, toDate);
  }
  return { calls };
}

// runHistoricalPrices body for the batched path: gap detection for every
// ticker first (local DB/parquet work), then one batched fill with the same
// per-ticker logRun / progress / mid-flush bookkeeping as the per-ticker loop.
async function _runHistoricalPricesBatched(tickers, fromDate, toDate) {
  let skipped = 0;
  let done = 0;
  const items = [];
  const pending = new Map(); // ticker → { remaining, written, err, gaps, t0 }

  for (const ticker of tickers) {
    if (_paused) { notify('⏸️ Paused — waiting...'); while (_paused) await sleep(1000); }
    const gaps = await store.getGaps(ticker, 'prices', fromDate, toDate);
    if (gaps.length === 0) {
      skipped++;
      done++;
      tickProgress('📊 Prices', done, tickers.length, ticker, 0);
      continue;
    }
    pending.set(ticker, { remaining: gaps.length, written: 0, err: null, gaps: gaps.length, t0: Date.now() });
    for (const gap of gaps) items.push({ ticker, from: gap.from, to: gap.to });
  }
  notify(`📊 Historical prices: ${pending.size} tickers with gaps → ${items.length} ranges via multi-bars`);

  let completedSinceFlush = 0;
  const finalize = async (ticker) => {
    const p = pending.get(ticker);
    pending.delete(ticker);
    done++;
    const ms = Date.now() - p.t0;
    if (p.err) {
      _stats.errors++;
      await store.logRun(ticker, 'prices', 'error', 0, p.err.message, ms, 1);
      notify(`⚠️ ${ticker} prices error: ${p.err.message}`);
    }
    _stats.prices += p.written;
    tickProgress('📊 Prices', done, tickers.length, ticker, p.written);
    if (p.written > 0) {
      await store.logRun(ticker, 'prices', 'success', p.written, null, ms, p.gaps);
    }
    // Same bound as the per-ticker loop: flush every 1000 completed tickers so
    // the in-memory buffer can't grow to an OOM-sized blob on big universes.
    if (++completedSinceFlush % 1000 === 0) {
      try {
        const mid = await store.flushPrices();
        if (mid && mid.flushed) console.log(`[collector] Prices mid-flush @ticker ${done}: ${mid.flushed} rows → prices.parquet`);
      } catch (e) {
        console.error(`[collector] Prices mid-flush FAILED (non-fatal): ${e.message}`);
      }
    }
  };

  const { calls } = await fillPricesAlpacaBatch(items, {
    onTicker: async (ticker, written, err) => {
      const p = pending.get(ticker);
      if (!p) return;
      p.written += written;
      if (err && !p.err) p.err = err;
      if (--p.remaining <= 0) await finalize(ticker);
    },
  });
  return { skipped, totalCalls: calls };
}

// ── Crypto bars (SP-1 Task 15a) — `alpaca data crypto bars` ──────────────────
// Universe convention is `BTC-USD` (yfinance/coinbase style); Alpaca's crypto
// endpoint wants `BTC/USD`. Translate at the API boundary only — parquet rows
// stay keyed by the universe ticker. Response shape is bars-by-symbol object
// (`{bars: {BTC/USD: [...]}}`) instead of the flat array returned by stocks.

function _alpacaCryptoSymbol(ticker) {
  // BTC-USD → BTC/USD, ETH-USD → ETH/USD, etc.
  return ticker.replace(/-/g, '/');
}

async function fillPricesAlpacaCrypto(ticker, fromDate, toDate) {
  const apiSymbol = _alpacaCryptoSymbol(ticker);
  const baseArgs = [
    'data', 'crypto', 'bars',
    '--symbols',    apiSymbol,
    '--start',      fromDate,
    '--end',        toDate,
    '--timeframe',  '1Day',
    '--limit',      '1000',
  ];

  let written   = 0;
  let pageToken = null;
  let pages     = 0;

  while (true) {
    const args = pageToken ? [...baseArgs, '--page-token', pageToken] : baseArgs;
    const r = await runAlpaca(args, { timeout: 30_000 });

    if (!r.ok) {
      const status = r.error?.status;
      const errMsg = r.error?.error || r.stderr?.slice(0, 200) || `exit ${r.exit_code}`;
      if (status === 400 && /invalid symbol/i.test(errMsg)) {
        console.warn(`[collector] fillPricesAlpacaCrypto: skip ${ticker}→${apiSymbol} (${errMsg})`);
        return written;
      }
      throw new Error(`alpaca data crypto bars ${ticker}→${apiSymbol} (${fromDate}→${toDate}): ${errMsg}`);
    }

    // Crypto endpoint returns bars-by-symbol object; pick the requested key.
    const barsBySym = r.payload?.bars || {};
    const bars      = barsBySym[apiSymbol];
    if (Array.isArray(bars) && bars.length > 0) {
      written += await store.upsertPrices(ticker, bars, 'alpaca-crypto');
    }

    pageToken = r.payload?.next_page_token || null;
    pages++;
    if (!pageToken) break;
    if (pages >= 20) {
      console.warn(`[collector] fillPricesAlpacaCrypto: ${ticker} hit 20-page cap`);
      break;
    }
  }

  return written;
}

// ── FMP historical-price-eod (SP-1 Task 15a) — indices + forex ───────────────
// Alpaca's stocks endpoint can't serve indices (^GSPC, ^DJI, …) and the
// account isn't authorized for FX (403). FMP Starter still has both at the
// `stable/historical-price-eod/full` endpoint. Futures (CL=F, GC=F) require
// FMP Premium and are NOT covered — those stay deferred at the dispatch layer.

const _FMP_KEY = process.env.FMP_API_KEY || '';

function _httpsGetJson(url, timeoutMs = 15_000) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const req = https.request(
      {
        hostname: u.hostname,
        path:     u.pathname + u.search,
        method:   'GET',
        timeout:  timeoutMs,
        headers:  { Accept: 'application/json' },
      },
      (res) => {
        let chunks = '';
        res.on('data', (c) => { chunks += c; });
        res.on('end',  () => {
          // FMP returns plain text starting with "Premium" or "Special Endpoint"
          // when a symbol is gated behind a higher subscription tier — surface
          // this distinctly so callers can warn-and-skip instead of throwing on
          // parse-error.
          const body = chunks || '';
          try {
            resolve({ ok: true, status: res.statusCode, json: body ? JSON.parse(body) : null });
          } catch (_) {
            const head = body.slice(0, 200);
            const tier = /^(Premium|Special)/i.test(body.trim());
            resolve({ ok: !tier, status: res.statusCode, body: head, tierGated: tier });
          }
        });
      },
    );
    req.on('error',   (e) => resolve({ ok: false, error: e.message }));
    req.on('timeout', () => { req.destroy(); resolve({ ok: false, error: 'timeout' }); });
    req.end();
  });
}

function _fmpSymbol(ticker) {
  // yfinance forex (`EURUSD=X`, `USDJPY=X`) → FMP's bare `EURUSD`, `USDJPY`.
  // Indices (`^GSPC`, `^DJI`) carry through unchanged.
  if (/=X$/.test(ticker)) return ticker.replace(/=X$/, '');
  return ticker;
}

async function fillPricesFmpHistorical(ticker, fromDate, toDate) {
  if (!_FMP_KEY) {
    console.warn(`[collector] fillPricesFmpHistorical: FMP_API_KEY unset; skip ${ticker}`);
    return 0;
  }
  const apiSymbol = _fmpSymbol(ticker);
  const url = `https://financialmodelingprep.com/stable/historical-price-eod/full`
    + `?symbol=${encodeURIComponent(apiSymbol)}`
    + `&from=${fromDate}&to=${toDate}`
    + `&apikey=${encodeURIComponent(_FMP_KEY)}`;

  const r = await _httpsGetJson(url);
  if (r.tierGated) {
    // FMP returns "Premium Query Parameter: …" plain-text for symbols gated
    // behind a higher subscription tier (^TNX, ^GDAXI, DX-Y.NYB on Starter).
    // Warn and skip — same posture as Alpaca's 400-invalid-symbol path.
    console.warn(`[collector] fillPricesFmpHistorical: skip ${ticker}→${apiSymbol} (tier-gated on current FMP subscription)`);
    return 0;
  }
  if (!r.ok) {
    throw new Error(`fmp historical-price-eod ${ticker}→${apiSymbol}: ${r.error || 'parse: ' + (r.body || '').slice(0, 80)}`);
  }
  if (r.status !== 200) {
    throw new Error(`fmp historical-price-eod ${ticker}→${apiSymbol}: HTTP ${r.status}`);
  }
  if (r.json && typeof r.json === 'object' && !Array.isArray(r.json) && r.json['Error Message']) {
    // FMP encodes auth/quota/symbol errors as a JSON object with "Error Message".
    console.warn(`[collector] fillPricesFmpHistorical: skip ${ticker}→${apiSymbol} (${r.json['Error Message'].slice(0, 120)})`);
    return 0;
  }
  if (!Array.isArray(r.json) || r.json.length === 0) return 0;

  // FMP rows already use the long-key form _priceRow accepts (date/open/...).
  // upsertPrices handles missing vwap/transactions gracefully.
  return await store.upsertPrices(ticker, r.json, 'fmp');
}


// ── Intraday snapshot prices (Task 3.5) — TODAY's partial bar, all classes ───
// On a candidate 15-min regime transition the redeploy needs TODAY's live
// price, but re-running the daily collect can't get it (Alpaca finalizes the
// daily bar post-close). So we fetch each asset class's INTRADAY snapshot,
// whose `dailyBar` is today's partial OHLCV (`c` = current price), and write
// it through the SAME append-dedup keep-last path runHistoricalPrices uses
// (store.upsertPrices → store.flushPrices; updateCoverage advances date_to to
// today because rows>0). NEVER deletes — the (ticker,date) keep-last dedup in
// writePrices overwrites today's partial row on a later snapshot/EOD fetch.

// Pure: snapshot.dailyBar (today's partial OHLCV) → prices.parquet row, or null.
function _snapshotToPriceRow(ticker, snap, dateStr) {
  const db = snap && snap.dailyBar;
  if (!db || db.c == null) return null;
  return { ticker, date: dateStr, open: db.o, high: db.h, low: db.l, close: db.c, volume: db.v };
}

// FMP real-time quote (stable/quote) → today's price row for indices/forex.
// Distinct from fillPricesFmpHistorical (EOD bars): the quote carries the
// live intraday print (`price`) plus today's open/dayHigh/dayLow/volume.
async function _fmpIntradayRow(ticker, dateStr) {
  if (!_FMP_KEY) {
    console.warn(`[collector] _fmpIntradayRow: FMP_API_KEY unset; skip ${ticker}`);
    return null;
  }
  const apiSymbol = _fmpSymbol(ticker);
  const url = `https://financialmodelingprep.com/stable/quote`
    + `?symbol=${encodeURIComponent(apiSymbol)}`
    + `&apikey=${encodeURIComponent(_FMP_KEY)}`;
  const r = await _httpsGetJson(url);
  if (r.tierGated) {
    console.warn(`[collector] _fmpIntradayRow: skip ${ticker}→${apiSymbol} (tier-gated)`);
    return null;
  }
  if (!r.ok || r.status !== 200) {
    console.warn(`[collector] _fmpIntradayRow: skip ${ticker}→${apiSymbol} (${r.error || 'HTTP ' + r.status})`);
    return null;
  }
  const q = Array.isArray(r.json) ? r.json[0] : null;
  if (!q || q.price == null) return null;
  return {
    ticker,
    date:   dateStr,
    open:   q.open    ?? null,
    high:   q.dayHigh ?? null,
    low:    q.dayLow  ?? null,
    close:  q.price,
    volume: q.volume  ?? null,
  };
}

// Fetch TODAY's intraday snapshot for ALL asset classes and write it through
// the collector's append-dedup keep-last path. Mirrors runHistoricalPrices for
// universe resolution (getActiveTickers) + flush/dedup; _classifyMarketTicker
// for per-class routing. Each class is wrapped so an unavailable/erroring
// source logs a warning and skips THAT class (non-fatal). Returns rows written.
async function runIntradaySnapshotPrices(tickers = null) {
  // Universe resolution — identical to runHistoricalPrices (caller may pass a
  // subset; default is the full active universe). The resolver envelope is the
  // daily-cycle caller's concern, not this hot-path prefetch.
  if (!tickers) tickers = await getActiveTickers();
  const dateStr = _etParts().date; // today's ET trading date

  // Bucket by class. Equities fall through _classifyMarketTicker's `etf` branch
  // (same as the equity/ETF multi-snapshots path below), so 'etf' = equity+ETF.
  const buckets = { etf: [], crypto: [], index: [], forex: [], futures: [] };
  for (const t of tickers) buckets[_classifyMarketTicker(t)].push(t);

  if (buckets.futures.length > 0) {
    console.warn(`[collector] runIntradaySnapshotPrices: ${buckets.futures.length} futures deferred — no intraday provider`);
  }

  const rows = [];

  // ── Equity + ETF: alpaca data multi-snapshots, ~150 symbols/chunk ──────────
  // Response is a bare {API_SYMBOL: {dailyBar,...}} map keyed by the API symbol
  // (BRK.B), so we build a universe→api forward map per chunk and invert it to
  // re-key rows back to the universe ticker (BRK-B) — else no consumer reads
  // them and coverage never advances for share-class names.
  if (buckets.etf.length > 0) {
    try {
      const CHUNK = 150;
      for (let i = 0; i < buckets.etf.length; i += CHUNK) {
        const chunk = buckets.etf.slice(i, i + CHUNK);
        // Mirror fillPricesAlpaca's share-class normalization (BRK-B → BRK.B).
        // Unconditional on the first separator, for the same reason as :721 —
        // the old `/^[A-Z]+-[A-Z]$/` anchor only converted single-letter classes,
        // so every multi-char suffix (-PRI, -PRH, -WS, -U) was sent verbatim and
        // 400'd. Alpaca accepts ONLY the dot form for all of them.
        const fwd = new Map(); // apiSymbol → universe ticker
        for (const t of chunk) {
          fwd.set(t.replace('-', '.'), t);
        }
        const symbolsArg = [...fwd.keys()].join(',');
        const r = await runAlpaca(
          ['data', 'multi-snapshots', '--symbols', symbolsArg],
          { timeout: 30_000 },
        );
        if (!r.ok || !r.payload || typeof r.payload !== 'object') {
          const errMsg = r.error?.error || r.stderr?.slice(0, 200) || `exit ${r.exit_code}`;
          console.warn(`[collector] runIntradaySnapshotPrices: equity chunk ${i / CHUNK} failed (${errMsg}) — skipping chunk`);
          continue;
        }
        for (const [apiSym, snap] of Object.entries(r.payload)) {
          const ticker = fwd.get(apiSym) || apiSym;
          const row = _snapshotToPriceRow(ticker, snap, dateStr);
          if (row) rows.push(row);
        }
      }
    } catch (err) {
      console.warn(`[collector] runIntradaySnapshotPrices: equity/ETF class skipped (${err.message})`);
    }
  }

  // ── Crypto: alpaca data crypto snapshots (BTC-USD → BTC/USD) ───────────────
  // Response nests under `snapshots` keyed by the API symbol (BTC/USD); same
  // dailyBar shape so _snapshotToPriceRow is reused. Re-key back to BTC-USD.
  if (buckets.crypto.length > 0) {
    try {
      const fwd = new Map(); // apiSymbol (BTC/USD) → universe ticker (BTC-USD)
      for (const t of buckets.crypto) fwd.set(_alpacaCryptoSymbol(t), t);
      const r = await runAlpaca(
        ['data', 'crypto', 'snapshots', '--symbols', [...fwd.keys()].join(',')],
        { timeout: 30_000 },
      );
      const snaps = r.ok && r.payload ? r.payload.snapshots : null;
      if (!snaps || typeof snaps !== 'object') {
        const errMsg = r.error?.error || r.stderr?.slice(0, 200) || `exit ${r.exit_code}`;
        console.warn(`[collector] runIntradaySnapshotPrices: crypto class skipped (${errMsg})`);
      } else {
        for (const [apiSym, snap] of Object.entries(snaps)) {
          const ticker = fwd.get(apiSym) || apiSym.replace(/\//g, '-');
          const row = _snapshotToPriceRow(ticker, snap, dateStr);
          if (row) rows.push(row);
        }
      }
    } catch (err) {
      console.warn(`[collector] runIntradaySnapshotPrices: crypto class skipped (${err.message})`);
    }
  }

  // ── Indices + forex: FMP real-time quote (mirrors fillPricesFmpHistorical's
  // provider + _fmpSymbol normalization, but quote endpoint for today's print).
  for (const cls of ['index', 'forex']) {
    for (const ticker of buckets[cls]) {
      try {
        const row = await _fmpIntradayRow(ticker, dateStr);
        if (row) rows.push(row);
      } catch (err) {
        console.warn(`[collector] runIntradaySnapshotPrices: ${ticker} (${cls}) skipped (${err.message})`);
      }
    }
  }

  // ── Write through the SAME flush/dedup path runHistoricalPrices uses ───────
  let written = 0;
  for (const row of rows) {
    try {
      // Coverage intentionally NOT written here — upsertPrices only buffers, so
      // claiming coverage now would lie if the flush below never lands.
      // store.flushPrices() commits it from the rows it actually wrote.
      const n = await store.upsertPrices(row.ticker, [row], 'alpaca-snapshot');
      written += n;
    } catch (err) {
      _stats.errors++;
      console.warn(`[collector] runIntradaySnapshotPrices: buffer ${row.ticker} failed (${err.message})`);
    }
  }
  try {
    const flushed = await store.flushPrices();
    if (flushed && flushed.flushed) {
      console.log(`[collector] Intraday-snapshot flush: ${flushed.flushed} rows → prices.parquet (total ${flushed.total_after})`);
    }
  } catch (err) {
    console.error(`[collector] Intraday-snapshot flush FAILED: ${err.message}`);
    throw err;
  }

  console.log(`[collector] ✅ Intraday snapshot prices: ${written} rows written for ${dateStr} `
    + `(${buckets.etf.length} equity/ETF, ${buckets.crypto.length} crypto, ${buckets.index.length} index, ${buckets.forex.length} forex)`);
  return written;
}


// ── Phase 3: Options chains — direct Alpaca CLI with greeks-validity filter ─

// Per-phase soft budget (default 30 min) — when exceeded, the options
// phase logs the partial result and exits cleanly so signals/handoff/
// trade/alpaca can still run on time. Override with OPTIONS_PHASE_BUDGET_S.
const OPTIONS_PHASE_BUDGET_S = parseInt(process.env.OPTIONS_PHASE_BUDGET_S || '1800', 10);

// Incremental-flush threshold (rows). runOptions buffers each ticker's contracts
// in memory; without periodic flushing the whole day's chain (~405k contracts)
// piles up in the node process before the single end-of-phase flush. Flushing when
// the buffer crosses this threshold bounds node-side memory. The parquet WRITE
// itself is memory-bounded in parquet_store (DuckDB streaming merge) so each flush
// is cheap on RAM. 0 disables (single end-of-phase flush). Override via env.
function _optionsFlushThreshold(env = process.env) {
  return parseInt(env.OPTIONS_FLUSH_ROW_THRESHOLD || '250000', 10);
}
function _shouldFlushOptions(bufferedRows, threshold) {
  return threshold > 0 && bufferedRows >= threshold;
}
const OPTIONS_FLUSH_ROW_THRESHOLD = _optionsFlushThreshold();

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
  let bufferedRows = 0;   // rows in store's options buffer since the last flush
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
      bufferedRows += written;
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
      // Incremental flush once the buffer crosses the threshold — bounds node-side
      // memory so the whole day's chain never piles up before the end-of-phase flush.
      if (_shouldFlushOptions(bufferedRows, OPTIONS_FLUSH_ROW_THRESHOLD)) {
        bufferedRows = 0;
        try {
          const f = await store.flushOptions();
          if (f && f.flushed) {
            console.log(`[collector] Options incremental flush: ${f.flushed} rows → options_eod.parquet (total ${f.total_after})`);
          }
        } catch (err) {
          // store._flush returns the rows to the buffer on failure; they ride the
          // end-of-phase flush. Surface but don't abort the phase.
          notify(`[WARN] Options incremental flush failed (rows retained for end-of-phase flush): ${err.message}`);
        }
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
  let gated = 0;
  let empty = 0;
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
      const kind = _classifyFmpError(err);
      if (kind === 'symbol_gated') {
        // Tier-gated SYMBOL (preferred/warrant/unit on Starter) — skip just this
        // name, record the check so the 30-day freshness scope stops re-asking,
        // and keep going. This is the same posture fillPricesFmpHistorical takes.
        gated++;
        console.warn(`[collector] fundamentals: skip ${ticker} (tier-gated on current FMP subscription)`);
        await store.logRun(ticker, 'fundamentals', 'skipped', 0, `tier-gated: ${err.body || err.message}`.slice(0, 200), Date.now() - start, 1);
        tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
        continue;
      }
      if (kind === 'not_found') {
        empty++;
        await store.logRun(ticker, 'fundamentals', 'empty', 0, 'HTTP 404', Date.now() - start, 1);
        tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
        continue;
      }
      if (kind === 'quota') {
        // Daily quota exhausted — stop making FMP calls for the rest of this cycle
        quotaExhausted = true;
        notify(`⚠️ FMP daily quota exhausted (402) after ${i} tickers — resuming tomorrow`);
        if (_alertPost) _alertPost(`⚠️ **FMP quota exhausted** — fundamentals will resume tomorrow (${i}/${tickers.length} done this cycle)`);
        tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
        continue;
      }
      if (kind === 'rate_limited') {
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
      // No statements (ETFs, funds, SPAC shells) — record the check so the
      // freshness scope doesn't re-ask every cycle. updateCoverage refuses
      // zero-row writes by design, so this lives in pipeline_runs instead.
      empty++;
      await store.logRun(ticker, 'fundamentals', 'empty', 0, null, Date.now() - start, 1);
      tickProgress('💹 Fundamentals', i + 1, tickers.length, ticker, 0);
      continue;
    }

    // Fetch FMP ratios + key-metrics + balance-sheet — S10_quality_value
    // (ROE/ROIC/D-E/EV-mult/P-FCF) and S_bankruptcy_risk_anomaly (Altman-Z
    // balance-sheet block). NOTE the /stable/ field renames: the old v3
    // reads (returnOnEquity from ratios, debtEquityRatio,
    // priceToFreeCashFlowsRatio) matched NOTHING in the stable payloads and
    // left these columns 0% populated parquet-wide (found 2026-07-03).
    let ratiosData = null, metricsData = null, balanceData = null;
    try {
      const ratiosUrl = `https://financialmodelingprep.com/stable/ratios?symbol=${ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}`;
      ratiosData = await httpGet(ratiosUrl);
      trackApiCall('fmp');
      await sleep(FMP_INTERVAL);
    } catch { /* non-fatal — ratios are supplemental */ }
    try {
      const metricsUrl = `https://financialmodelingprep.com/stable/key-metrics?symbol=${ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}`;
      metricsData = await httpGet(metricsUrl);
      trackApiCall('fmp');
      await sleep(FMP_INTERVAL);
    } catch { /* non-fatal */ }
    try {
      const balanceUrl = `https://financialmodelingprep.com/stable/balance-sheet-statement?symbol=${ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}`;
      balanceData = await httpGet(balanceUrl);
      trackApiCall('fmp');
      await sleep(FMP_INTERVAL);
    } catch { /* non-fatal */ }

    // Per-period lookups by date
    const byDate = (arr) => {
      const out = {};
      if (Array.isArray(arr)) for (const r of arr) { if (r.date) out[r.date] = r; }
      return out;
    };
    const ratiosByDate  = byDate(ratiosData);
    const metricsByDate = byDate(metricsData);
    const balanceByDate = byDate(balanceData);
    const first = (...vals) => vals.find(v => v !== null && v !== undefined) ?? null;

    try {
      const records = data.map(q => {
        const ratio = ratiosByDate[q.date]  || {};
        const km    = metricsByDate[q.date] || {};
        const bs    = balanceByDate[q.date] || {};
        const wcDerived = (bs.totalCurrentAssets != null && bs.totalCurrentLiabilities != null)
          ? bs.totalCurrentAssets - bs.totalCurrentLiabilities : null;
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
          pe_ratio:           first(ratio.priceToEarningsRatio, ratio.priceEarningsRatio),
          market_cap:         km.marketCap ?? null,
          ev_ebitda:          first(km.evToEBITDA, ratio.enterpriseValueMultiple),
          ev_revenue:         km.evToSales ?? null,
          roe:                first(km.returnOnEquity, ratio.returnOnEquity),
          roic:               first(km.returnOnInvestedCapital, km.returnOnCapitalEmployed),
          debt_equity_ratio:  first(ratio.debtToEquityRatio, ratio.debtEquityRatio),
          p_fcf_ratio:        first(ratio.priceToFreeCashFlowRatio, ratio.priceToFreeCashFlowsRatio),
          total_assets:       bs.totalAssets ?? null,
          total_liabilities:  bs.totalLiabilities ?? null,
          retained_earnings:  bs.retainedEarnings ?? null,
          working_capital:    first(km.workingCapital, wcDerived),
          operating_income:   q.operatingIncome ?? null,
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
  notify(`✅ Fundamentals complete — ${_progress.rowsThisPhase} new records, ${skipped} skipped (fetched <30d ago), ${gated} tier-gated, ${empty} no-statement in ${elapsed}s`);
  if (_alertPost) _alertPost(`✅ **Phase 5 complete** — ${_progress.rowsThisPhase} fundamental records added | ${skipped} skipped | ${gated} tier-gated | ${empty} no-statement`);
}

// ── Market price history — ETFs/crypto via Alpaca; indices/forex via FMP ─────
// Universe is mixed-asset: ETFs (SPY/QQQ/…), indices (^GSPC/^DJI/…), crypto
// (BTC-USD/…), forex (EURUSD=X/…), futures (CL=F/…). Each goes to its own
// provider/endpoint. Futures stay deferred — FMP Starter doesn't serve them
// and Alpaca has no futures feed; resolve once a paid provider is added.
//
// Dispatch (suffix-based, no DB roundtrip needed):
//   ^X     → index    → fillPricesFmpHistorical   (source='fmp')
//   -USD   → crypto   → fillPricesAlpacaCrypto    (source='alpaca-crypto')
//   =X|NYB → forex    → fillPricesFmpHistorical   (source='fmp', `=X` stripped)
//   =F     → futures  → deferred (no provider)
//   else   → ETF      → fillPricesAlpaca          (source='alpaca')

function _classifyMarketTicker(ticker) {
  if (/^\^/.test(ticker))             return 'index';
  if (/=F$/.test(ticker))             return 'futures';
  if (/-USD$/.test(ticker))           return 'crypto';
  if (/=X$|\.NYB$/.test(ticker))      return 'forex';
  return 'etf';
}

async function _fillMarketTicker(category, ticker, fromDate, toDate) {
  switch (category) {
    case 'etf':     return fillPricesAlpaca(ticker, fromDate, toDate);
    case 'crypto':  return fillPricesAlpacaCrypto(ticker, fromDate, toDate);
    case 'index':
    case 'forex':   return fillPricesFmpHistorical(ticker, fromDate, toDate);
    case 'futures': return 0; // no provider; caller logs the deferral once
    default:        return 0;
  }
}

async function runMarketPricesNonEquity(tickers, historyDays = 3650) {
  if (!tickers || tickers.length === 0) return;

  const toDate   = new Date().toISOString().slice(0, 10);
  const fromDate = new Date(Date.now() - historyDays * 86400_000).toISOString().slice(0, 10);

  // Bucket by category so we can log one summary per-class up front.
  const buckets = { etf: [], crypto: [], index: [], forex: [], futures: [] };
  for (const t of tickers) buckets[_classifyMarketTicker(t)].push(t);

  const futuresCount = buckets.futures.length;
  if (futuresCount > 0) {
    notify(`🌐 Market prices: ${futuresCount} futures contract(s) deferred — no provider available (FMP Premium not subscribed; Alpaca has no futures feed)`);
  }

  // Tickers we will actually fetch.
  const fetchable = [...buckets.etf, ...buckets.crypto, ...buckets.index, ...buckets.forex];
  if (fetchable.length === 0) return;

  notify(`🌐 Market prices: ${buckets.etf.length} ETF + ${buckets.crypto.length} crypto + ${buckets.index.length} index + ${buckets.forex.length} forex (${fromDate}→${toDate})`);
  _progress = { phase: '🌐 Market Prices', current: 0, total: fetchable.length, ticker: null, phaseStart: Date.now(), rowsThisPhase: 0 };

  let skipped = 0;
  let totalCalls = 0;
  for (let i = 0; i < fetchable.length; i++) {
    const ticker = fetchable[i];
    const category = _classifyMarketTicker(ticker);
    if (_paused) { notify('⏸️ Paused — waiting...'); while (_paused) await sleep(1000); }

    const gaps = await store.getGaps(ticker, 'prices', fromDate, toDate);
    if (gaps.length === 0) {
      skipped++;
      tickProgress('🌐 Market Prices', i + 1, fetchable.length, ticker, 0);
      continue;
    }
    const start = Date.now();
    let totalWritten = 0;
    for (const gap of gaps) {
      try {
        // Coverage NOT written here — same buffered-vs-durable lie as the equity
        // path; these writers share _buffers.prices, so store.flushPrices()
        // commits coverage from the rows it actually wrote.
        const written = await _fillMarketTicker(category, ticker, gap.from, gap.to);
        totalWritten += written;
        totalCalls++;
      } catch (err) {
        _stats.errors++;
        await store.logRun(ticker, 'prices', 'error', 0, err.message, Date.now() - start, 1);
        notify(`⚠️ ${ticker} (${category}) market-prices error: ${err.message}`);
      }
    }
    _stats.prices += totalWritten;
    tickProgress('🌐 Market Prices', i + 1, fetchable.length, ticker, totalWritten);
    if (totalWritten > 0) {
      await store.logRun(ticker, 'prices', 'success', totalWritten, null, Date.now() - start, gaps.length);
    }
  }

  try {
    const flushed = await store.flushPrices();
    if (flushed && flushed.flushed) {
      console.log(`[collector] Market-prices flush: ${flushed.flushed} rows → prices.parquet (total ${flushed.total_after})`);
    }
  } catch (err) {
    console.error(`[collector] Market-prices flush FAILED: ${err.message}`);
  }

  notify(`✅ Market prices complete — ${_progress.rowsThisPhase.toLocaleString()} new rows | ${skipped}/${fetchable.length} skipped (no gaps) | ${totalCalls} API calls`);
}


// ── News collection — Alpaca News API (SP-1 Task 15a, 2026-05-22) ────────────
// alpaca_news.py dual-writes ticker_sentiment_daily.alpaca_news_* + market_news
// from one fetch. runNewsCollection (below) is now a 30-day prune-only phase.

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
  // SP-1 Task 15a (2026-05-22): market_news ingestion now happens in the
  // sentiment step via src/ingestion/alpaca_news.py, which dual-writes to
  // both ticker_sentiment_daily.alpaca_news_* AND market_news. One Alpaca
  // fetch feeds both consumers. This phase is now a pure maintenance prune
  // so the daily-cycle phase ordering + Discord progress posts don't change.
  notify(`📰 News: ingestion runs in sentiment step (alpaca_news.py dual-write); pruning market_news older than 30d`);
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

  // SP-2 Phase B Task 5: bootstrap the quarantine set so downstream
  // parquet-row consumers can drop unsuperseded bad (ticker,date) rows.
  loadQuarantineSet();

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

  notify('🚀 Data pipeline started — resolved equity universe');

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

// ── EOD same-day close capture (fix B, 2026-06-04) ───────────────────────────
// The 16:15 ET EOD cycle must run signals on close[T]. Historically the gap
// pre-scan marked a ticker `covered` at date_to >= yesterday, so the in-cycle
// collect could never fetch today's close — the EOD compute structurally ran
// on close[T−1] while the 16:30 ET eod-refresh timer captured the closes four
// minutes AFTER the compute finished (sp6 diagnosis 2026-06-04 §10).
// Post-close on a trading day, the pre-scan now requires date_to >= today and
// _verifyEquityFreshness blocks/aborts so signals never run on a stale panel.

const _ALPACA_BIN_EOD = process.env.ALPACA_BIN || '/root/go/bin/alpaca';

function _etParts(now = new Date()) {
  const date = new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(now);
  const fmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York', hour12: false,
    weekday: 'short', hour: '2-digit', minute: '2-digit',
  });
  const parts = Object.fromEntries(fmt.formatToParts(now).map(p => [p.type, p.value]));
  return { date, weekday: parts.weekday, hhmm: `${parts.hour}:${parts.minute}` };
}

async function _alpacaCalendarProbe(dateStr) {
  // → true (trading session) / false (market closed that date) / null (probe failed)
  try {
    const { execFile } = require('child_process');
    const { promisify } = require('util');
    const out = await promisify(execFile)(
      _ALPACA_BIN_EOD, ['calendar', '--start', dateStr, '--end', dateStr],
      { timeout: 5000 },
    );
    const sessions = JSON.parse((out.stdout || '').trim() || '[]');
    // Non-array (e.g. an {error: ...} object on rc=0) = probe failure → null
    // (fail-loud), NOT false — false means "authenticated answer: market
    // closed" and silently downgrades the freshness requirement.
    if (!Array.isArray(sessions)) return null;
    return sessions.some(s => s && s.date === dateStr);
  } catch (_) {
    return null;
  }
}

async function _eodFreshnessContext({ now = new Date(), calendarProbe = _alpacaCalendarProbe } = {}) {
  const { date, weekday, hhmm } = _etParts(now);
  const isWeekday = !['Sat', 'Sun'].includes(weekday);
  const postClose = hhmm >= '16:05';
  if (!isWeekday || !postClose) return { requireToday: false, todayEt: date };
  const trading = await calendarProbe(date);
  // trading === false → market holiday → legacy rule (latest session is the
  // previous one; nothing new to capture). trading === null → probe FAILED →
  // fail-loud: require today. Worst case on an undetected holiday is an abort
  // + Discord alert; silently running signals on a stale panel is the exact
  // bug this gate exists to prevent.
  return { requireToday: trading !== false, todayEt: date };
}

async function _queryFreshCoverage(tickers, todayEt) {
  const { query: dbQuery } = require('../database/postgres');
  const res = await dbQuery(
    `SELECT ticker FROM data_coverage
      WHERE data_type='prices' AND date_to >= $2::date AND ticker = ANY($1::text[])`,
    [tickers, todayEt]
  );
  // data_coverage is an honest oracle here: updateCoverage refuses to advance
  // date_to on zero-row fetches, so date_to >= today ⟺ today's bar was stored.
  return (res?.rows || []).map(r => r.ticker);
}

async function _verifyEquityFreshness(equityTickers, todayEt, {
  queryFreshFn = _queryFreshCoverage,
  refetchFn = null,
  sleepFn = sleep,
  maxAttempts = parseInt(process.env.OPENCLAW_EOD_FRESHNESS_MAX_ATTEMPTS || '3', 10),
  minFrac = parseFloat(process.env.OPENCLAW_EOD_FRESHNESS_MIN_FRAC || '0.95'),
  retryDelayMs = 45_000,
} = {}) {
  if (!equityTickers || equityTickers.length === 0) return { fresh: [], stale: [] };
  let fresh = await queryFreshFn(equityTickers, todayEt);
  let stale = equityTickers.filter(t => !fresh.includes(t));
  let attempt = 1;
  while (stale.length > 0 && attempt < maxAttempts) {
    notify(`⏳ EOD freshness: ${stale.length}/${equityTickers.length} equity tickers missing ${todayEt} bars — retry ${attempt}/${maxAttempts - 1}`);
    if (refetchFn) await refetchFn(stale);
    await sleepFn(retryDelayMs);
    fresh = await queryFreshFn(equityTickers, todayEt);
    stale = equityTickers.filter(t => !fresh.includes(t));
    attempt += 1;
  }
  const frac = equityTickers.length ? fresh.length / equityTickers.length : 1;
  if (frac < minFrac) {
    throw new Error(
      `EOD freshness gate FAILED: only ${fresh.length}/${equityTickers.length} equity tickers have ` +
      `${todayEt} bars after ${attempt} attempt(s) — refusing to let signals run on a stale panel`
    );
  }
  if (stale.length > 0) {
    notify(`⚠️ EOD freshness: proceeding with ${stale.length} stale straggler(s): ${stale.slice(0, 10).join(', ')}`);
  }
  return { fresh, stale };
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
  // SP-7 C2: PRICES fetch on the wide no-floor envelope; news/insider scope
  // decided separately (Task 11 / spec §5 envelope hierarchy).
  const priceEquityTickers = await applyResolverEnvelope(
    equityTickers, new Date().toISOString().slice(0, 10));
  // SP-7 C2/C3: fundamentals + insider follow the adopted-union (spec §5
  // envelope hierarchy). Computed here alongside priceEquityTickers so the
  // adopted-union scope feeds the gap scan — else adopted names never surface
  // in gaps (mirror of priceEquityTickers pattern).
  const fundamentalScope = await adoptedUnionScope(fundamentalTickers, new Date().toISOString().slice(0, 10));
  const insiderScope     = await adoptedUnionScope(equityTickers,      new Date().toISOString().slice(0, 10));
  // Use priceEquityTickers (post-envelope) so the banner reflects what's
  // actually price-fetched, not the wider universe_config envelope.
  const universeLabel     = `Equities (${priceEquityTickers.length}) + Market (${marketTickers.length})`;

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

  // Post-close on a trading day the prices requirement tightens to TODAY so
  // this cycle's collect captures the just-completed session's closes itself
  // (fix B — see _eodFreshnessContext above).
  const eodCtx = await _eodFreshnessContext().catch(() => ({ requireToday: false, todayEt: today }));
  if (eodCtx.requireToday) {
    notify(`🕓 Post-close cycle: requiring today's (${eodCtx.todayEt}) close bars for the equity envelope`);
  }

  notify('🔍 Scanning data coverage gaps...');
  const gaps = await store.getGapSummary({
    priceTickers:   [...priceEquityTickers, ...marketTickers],
    optionsTickers,
    fundTickers:    fundamentalScope, // adopted-union scope feeds the gap scan (mirror of priceEquityTickers) — else adopted names never surface in gaps
    fromDate,
    toDate:         today,
    fundStaleDays:  45,
    pricesRequiredThrough: eodCtx.requireToday ? eodCtx.todayEt : null,
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
    // 1b': re-derive historical_regimes.parquet from the fresh VIX rows.
    // rebuild_cache() had ZERO scheduled callers and the master silently
    // froze 2026-07-22 → 08-06 (caught by the master_freshness probe) —
    // starving S_regime_age_momentum + S_mvgarch_nig_crra_portfolio live
    // and serving stale regime labels to every backtest partition. Cheap:
    // one VIX classify pass over ~9k macro rows.
    await _runIngestPhase(
      '🧭', 'Historical regimes rebuild',
      ['src/strategies/historical_regimes.py', '--rebuild'],
      'historical_regimes', 3 * 60_000,
    );
  }

  // Phase 1c: Wide-format vol_indices.parquet (vix_close, vvix_close, vix9d_close).
  // Distinct from macro.parquet (long-format) — strategies str01_vvix_early_warning
  // et al read this file directly. Default-on.
  if (cfg.collect_vol_indices_wide !== 'false') {
    await runVolIndicesWide();
  }

  // Phase 2a: equity prices — only tickers with gaps (Alpaca P1; Task 15 wires bars API)
  const priceEquityNeeded = gaps?.prices.tickers.filter(t => priceEquityTickers.includes(t)) ?? priceEquityTickers;
  const priceMarketNeeded = gaps?.prices.tickers.filter(t => marketTickers.includes(t)) ?? marketTickers;
  if (cfg.collect_prices !== 'false' && priceEquityNeeded.length > 0) {
    await runHistoricalPrices(historyDays, priceEquityNeeded);
  } else if (priceEquityNeeded.length === 0) {
    notify('✅ Prices (equity): all current — skipped');
  }

  // Phase 2a-verify (fix B): post-close, the equity panel MUST hold today's
  // closes before signals run. Bounded retries for bar-latency stragglers;
  // a substantially stale panel THROWS → run_collector_once exits non-zero →
  // the cycle aborts BEFORE the signals step (strict exit codes).
  if (eodCtx.requireToday && cfg.collect_prices !== 'false') {
    // Gate on what `signals` CONSUMES, not on everything we fetched. See
    // _signalsConsumedScope: the wide fetch envelope is deliberate (history for
    // future adoptions) but as a health denominator it drags in ~5.7k names no
    // strategy trades and halts the fund over them.
    const gateScope = await _signalsConsumedScope(
      equityTickers, priceEquityTickers, eodCtx.todayEt);
    notify(`🔎 EOD freshness scope: ${gateScope.length} consumed (of ${priceEquityTickers.length} fetched)`);
    await _verifyEquityFreshness(gateScope, eodCtx.todayEt, {
      refetchFn: (stale) => runHistoricalPrices(historyDays, stale),
    });
    notify(`✅ EOD freshness gate passed — equity panel current through ${eodCtx.todayEt}`);
  }

  // Phase 2b: Market instrument prices (stub — SP-1 yfinance removed; Task 15 wires Alpaca/FMP)
  if (cfg.collect_market_prices !== 'false' && priceMarketNeeded.length > 0) {
    await runMarketPricesNonEquity(priceMarketNeeded, historyDays);
  } else if (priceMarketNeeded.length === 0) {
    notify('✅ Prices (market): all current — skipped');
  }

  // Phase 2c: 30-minute equity bars (top-250 by avg_dollar_volume_30d)
  // via Polygon REST aggregates. ~1-2 min wall-clock at 4 calls/sec.
  // Strategies str04 (intraday SPY) + str06 (EOD reversal) read this file.
  if (cfg.collect_prices_30m !== 'false') {
    await run30mPrices();
  }

  // Phase 3: Options — only tickers without today's data. Opt-in since D3:
  // the 16:30 ET archive timer is the daily chain's owner (see
  // _inCycleOptionsEnabled); OPENCLAW_COLLECT_OPTIONS_INCYCLE=1 re-enables
  // the in-cycle fetch as a manual fallback.
  const optionsNeeded = gaps?.options.tickers ?? optionsTickers;
  if (cfg.collect_options !== 'false' && optionsNeeded.length > 0 && !_inCycleOptionsEnabled()) {
    notify(`⏭️  Options: in-cycle fetch disabled — openclaw-options-archive.timer (16:30 ET) owns the chain (${optionsNeeded.length} tickers pending its run; set OPENCLAW_COLLECT_OPTIONS_INCYCLE=1 to fetch here)`);
  } else if (cfg.collect_options !== 'false' && optionsNeeded.length > 0) {
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

  // Phase 4: Fundamentals — only stale tickers, budget-aware.
  // D1 (2026-08-23): the scope is freshness-driven (data_coverage.last_updated
  // OR a pipeline_runs check within fmp_fundamentals_stale_days), ordered
  // oldest-first, and capped per cycle so the first cycle after the 402 fix
  // doesn't try the whole universe (4 calls/ticker × 11k = the daily quota).
  const fundCap = parseInt(cfg.fmp_fundamentals_max_per_cycle || '1500', 10);
  const fundScoped = _capScope(gaps?.fundamentals.tickers ?? fundamentalScope, fundCap);
  const fundNeeded = fundScoped.tickers;
  if (fundScoped.deferred > 0) notify(`💹 Fundamentals: ${fundNeeded.length} oldest-first this cycle, ${fundScoped.deferred} deferred (cap fmp_fundamentals_max_per_cycle=${fundCap})`);
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
    // 4c: fold the fresh calendar + reported actuals into the earnings
    // MASTER — the file engine.py and the earnings strategies actually read.
    // Its previous refresh path was orphaned FMP dead code and the master
    // silently froze 2026-04-30 → 08-06 (remediation spec §3). Runs right
    // after the calendar so the master can never lag it by more than a run.
    await runEarningsMasterMerge();

    // 4d: corporate-actions master refresh (2026-08-07 un-orphaning — the
    // file had no caller at all; see runCorporateActions).
    await runCorporateActions();
  }

  // Phase 6: News — skipped in YELLOW/RED
  if (cfg.collect_news !== 'false' && !budgetConstraints.skipNews) {
    await runNewsCollection(equityTickers);
  } else if (budgetConstraints.skipNews) {
    notify(`💰 News skipped — budget ${budgetStatus.mode}`);
  }

  // Phase 7: Form 4 Insider Transactions
  if (cfg.collect_insider !== 'false') {
    await runInsiderTransactions(insiderScope);
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
  // SKIPPED when OPENCLAW_LANGGRAPH_ORCHESTRATOR=1: under LangGraph, the
  // `collect` node is just ONE step of the graph — spawning the Python
  // orchestrator here races it against the same daily-cycle thread and
  // corrupts the trade step (incident 2026-05-22: dual execution caused
  // bracket-on-cover rejections + LangGraph trade abort at 14:12:39).
  //
  // Legacy path retained for environments without LangGraph: parquet-primary
  // collector phases write to master parquets directly, so this spawn drives
  // the downstream signals→trade→alpaca cycle. Idempotent per day via Redis
  // key pipeline:completed:{date}.
  if (process.env.OPENCLAW_LANGGRAPH_ORCHESTRATOR === '1') {
    console.log('[collector] LangGraph orchestrator gate ON — skipping legacy pipeline_orchestrator.py spawn');
  } else {
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
  }

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
  const priceEquityTickers = await applyResolverEnvelope(
    equityTickers, new Date().toISOString().slice(0, 10));
  const tickerSet     = new Set([...priceEquityTickers, ...marketTickers]);
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

  await runHistoricalPrices(historyDays, priceEquityTickers);
  await runMarketPricesNonEquity(marketTickers, historyDays);

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
    equity_tickers:   priceEquityTickers,
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

module.exports = { start, pause, resume, isRunning, isSleeping, getNextRun, getStats, setBroadcast, setDiscordHooks, loadConfig, runSnapshots, runHistoricalPrices, runOptions, fetchOptionsChain, runFundamentals, runInsiderTransactions, runNewsCollection, runIntegrityCheck, runDailyCollection, runEodRefresh, readUnionUniverseFromRedis, applyResolverEnvelope, adoptedUnionScope, _signalsConsumedScope, fillPricesAlpaca, fillPricesAlpacaBatch, _groupGapItems, _multiBarsChunkSize, _isAlpacaStockSymbol, fillPricesAlpacaCrypto, fillPricesFmpHistorical, runIntradaySnapshotPrices, _snapshotToPriceRow, loadQuarantineSet, isQuarantined, _quarantineSet, _eodFreshnessContext, _verifyEquityFreshness, _etParts, _optionsFlushThreshold, _shouldFlushOptions, _httpError, _classifyFmpError, _capScope, _inCycleOptionsEnabled };
