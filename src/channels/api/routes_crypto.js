/**
 * Crypto flow dashboard backend (Phase 1) — reads Postgres tables written by
 * src/ingestion/crypto_flow (migration 168).
 *
 *  GET /api/crypto/snapshot          latest per-ticker + __MARKET__ features, provider health
 *  GET /api/crypto/history/:ticker   hourly series (days clamped 1-30)
 *  GET /api/crypto/events            recent whale events (limit clamped 1-200)
 *  GET /api/crypto/providers         latest health per provider + degraded list
 *  GET /api/crypto/risk              env flags + literal breaker/sizing blocks
 *  GET /api/crypto/positions         Alpaca crypto positions (normalised tickers)
 *
 * Every endpoint answers HTTP 200; empty/erroring tables give {available:false, reason}.
 * startEventWatcher(broadcast) polls for new whale events and pushes SSE 'crypto_event'.
 */
const fs = require('fs');
const path = require('path');
const express = require('express');
const router = express.Router();

let dbQuery = require('../../database/postgres').query;
let { runAlpaca } = require('./alpaca_cli');  // shared wrapper: ALPACA_CLI_BIN, --quiet/--timeout

const UNIVERSE = JSON.parse(fs.readFileSync(
  path.join(__dirname, '../../ingestion/crypto_flow/crypto_flow_universe.json'), 'utf8'));
const ROLE = new Map(UNIVERSE.assets.map((a) => [a.ticker, a.role]));

const clamp = (v, lo, hi, dflt) => {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt;
};
const num = (x) => (x === null || x === undefined ? null : (Number.isFinite(Number(x)) ? Number(x) : null));
const fail = (res, e) => res.json({ available: false, reason: 'error', error: e && e.message ? e.message : String(e) });

// Same rule as universe.normalize_broker_symbol: strip '/' and '-', upper-case, must end USD.
function normalizeCryptoSymbol(sym) {
  const s = String(sym || '').replace(/[/-]/g, '').toUpperCase();
  if (!s.endsWith('USD') || s.length <= 3) return null;
  const t = `${s.slice(0, -3)}-USD`;
  return ROLE.has(t) ? t : null;
}

router.get('/snapshot', async (req, res) => {
  try {
    const snap = await dbQuery(
      `SELECT DISTINCT ON (ticker) ticker, ts_utc, features FROM crypto_flow_snapshot
        WHERE ts_utc >= NOW() - INTERVAL '2 days' ORDER BY ticker, ts_utc DESC`);
    if (!snap.rows.length) return res.json({ available: false, reason: 'no_ticks_yet' });
    const health = await dbQuery(
      `SELECT DISTINCT ON (provider) provider, ts_utc, ok, rows, latency_ms, err, skipped
         FROM crypto_flow_provider_health WHERE ts_utc >= NOW() - INTERVAL '1 day'
        ORDER BY provider, ts_utc DESC`);
    const mk = snap.rows.find((r) => r.ticker === '__MARKET__');
    const maxTs = snap.rows.reduce((m, r) => (new Date(r.ts_utc) > new Date(m) ? r.ts_utc : m), snap.rows[0].ts_utc);
    const tickers = snap.rows.filter((r) => r.ticker !== '__MARKET__')
      .map((r) => ({ ticker: r.ticker, role: ROLE.get(r.ticker) || null, ts_utc: r.ts_utc, ...r.features }))
      .sort((a, b) => ((b.role === 'benchmark') - (a.role === 'benchmark')) || a.ticker.localeCompare(b.ticker));
    res.json({
      available: true,
      ts_utc: mk ? mk.ts_utc : maxTs,
      market: mk ? mk.features : {},
      tickers,
      provider_health: health.rows,
    });
  } catch (e) { fail(res, e); }
});

router.get('/history/:ticker', async (req, res) => {
  const ticker = req.params.ticker;
  if (!/^[A-Z0-9]{2,10}-USD$/.test(ticker)) return res.json({ available: false, reason: 'bad_ticker' });
  const days = clamp(req.query.days, 1, 30, 7);
  try {
    const r = await dbQuery(
      `SELECT ts_utc, features->>'exch_netflow_usd_24h' AS exch_netflow_usd_24h,
              features->>'oi_usd' AS oi_usd, features->>'funding_8h' AS funding_8h,
              features->>'whale_score' AS whale_score, features->>'price_usd' AS price_usd
         FROM crypto_flow_snapshot
        WHERE ticker = $1 AND ts_utc >= NOW() - ($2 || ' days')::interval
        ORDER BY ts_utc ASC`, [ticker, days]);
    const rows = r.rows.map((x) => ({
      ts_utc: x.ts_utc,
      exch_netflow_usd_24h: num(x.exch_netflow_usd_24h), oi_usd: num(x.oi_usd),
      funding_8h: num(x.funding_8h), whale_score: num(x.whale_score), price_usd: num(x.price_usd),
    }));
    res.json({ available: rows.length > 0, ticker, rows });
  } catch (e) { fail(res, e); }
});

const EVENT_COLS = `event_key, ts_utc, source, chain, symbol, ticker, amount, amount_usd,
                    from_owner, to_owner, direction, tx_hash`;

router.get('/events', async (req, res) => {
  const limit = clamp(req.query.limit, 1, 200, 50);
  try {
    const r = await dbQuery(
      `SELECT ${EVENT_COLS} FROM crypto_whale_events_live ORDER BY ts_utc DESC LIMIT $1`, [limit]);
    res.json({ available: r.rows.length > 0, rows: r.rows });
  } catch (e) { fail(res, e); }
});

router.get('/providers', async (req, res) => {
  try {
    const r = await dbQuery(
      `SELECT provider, ts_utc, ok, rows, latency_ms, err, skipped, rn FROM (
         SELECT *, ROW_NUMBER() OVER (PARTITION BY provider ORDER BY ts_utc DESC) AS rn
           FROM crypto_flow_provider_health WHERE ts_utc >= NOW() - INTERVAL '1 day') t
        WHERE rn <= 3 ORDER BY provider, rn`);
    const by = new Map();
    for (const row of r.rows) {
      if (!by.has(row.provider)) by.set(row.provider, []);
      by.get(row.provider).push(row);
    }
    const providers = [];
    const degraded = [];
    for (const [provider, rows] of by) {
      const { rn, ...latest } = rows[0];
      providers.push(latest);
      if (rows.length >= 3 && rows.every((x) => x.ok === false)) degraded.push(provider);
    }
    res.json({ available: providers.length > 0, providers, degraded });
  } catch (e) { fail(res, e); }
});

router.get('/risk', (req, res) => {
  const flag = (k) => (process.env[k] === '1' ? '1' : process.env[k] === '0' ? '0' : 'unset');
  res.json({
    flags: {
      CRYPTO_FLOWS: flag('OPENCLAW_CRYPTO_FLOWS'),
      CRYPTO_BREAKER: flag('OPENCLAW_CRYPTO_BREAKER'),
      CRYPTO_POOLED_SIZING: flag('OPENCLAW_CRYPTO_POOLED_SIZING'),
    },
    params: {
      whale_min_usd: Number(process.env.OPENCLAW_CRYPTO_WHALE_MIN_USD || 1000000),
      large_print_usd: Number(process.env.OPENCLAW_CRYPTO_LARGE_PRINT_USD || 250000),
      thresholds: { delev_oi_chg_1h_pct: -5, delev_ret_1h_pct: -3, netflow_z_shock: 3, coverage_floor: 0.25, score_alert: 70 },
    },
    breaker: { status: 'pending_phase_2', triggers: ['T4a sleeve −8 %', 'T4b position −15 %'] },
    sizing: { status: 'pending_phase_3', benchmark: 'BTC-USD', bench_cap_nav: 0.25, gross_cap_nav: 0.50 },
  });
});

router.get('/positions', async (req, res) => {
  try {
    const r = await runAlpaca(['position', 'list', '--jq', '[.[]|select(.asset_class=="crypto")]'],
      { timeout: 15000 });
    if (!r.ok) {
      const msg = (r.error && r.error.error) || r.stderr || `alpaca exit ${r.exit_code}`;
      return fail(res, new Error(String(msg)));
    }
    const list = typeof r.payload === 'string' ? JSON.parse(r.payload || '[]') : (r.payload || []);
    const rows = list.map((p) => ({
      symbol: p.symbol, ticker: normalizeCryptoSymbol(p.symbol), qty: num(p.qty),
      market_value: num(p.market_value), unrealized_pl: num(p.unrealized_pl),
      unrealized_plpc: num(p.unrealized_plpc),
    }));
    res.json({ available: true, rows });
  } catch (e) { fail(res, e); }
});

// ── SSE watcher ─────────────────────────────────────────────────────────────
let watcherTimer = null;

function startEventWatcher(broadcast, { intervalMs = 30000 } = {}) {
  stopEventWatcher();
  let watermark = null;       // primed by the first successful poll; no replay of old events
  let primed = false;         // distinct from watermark: an empty table primes to epoch
  let inFlight = false;       // a slow DB must not stack overlapping ticks
  let lastErr = null;
  const tick = async () => {
    if (inFlight) return;
    inFlight = true;
    try {
      const m = await dbQuery('SELECT MAX(ts_utc) AS ts FROM crypto_whale_events_live');
      const ts = m.rows[0] && m.rows[0].ts ? new Date(m.rows[0].ts) : null;
      lastErr = null;
      if (!primed) { watermark = ts || new Date(0); primed = true; return; }
      if (ts && ts > watermark) {
        const r = await dbQuery(
          `SELECT ${EVENT_COLS} FROM crypto_whale_events_live WHERE ts_utc > $1
            ORDER BY ts_utc DESC LIMIT 50`, [watermark]);
        watermark = ts;
        if (r.rows.length) broadcast({ type: 'crypto_event', rows: r.rows });
      }
    } catch (e) {
      if (e.message !== lastErr) console.warn('[crypto-watcher]', e.message);
      lastErr = e.message;
    } finally {
      inFlight = false;
    }
  };
  watcherTimer = setInterval(tick, intervalMs);
  if (watcherTimer.unref) watcherTimer.unref();
  tick();
  return watcherTimer;
}

function stopEventWatcher() {
  if (watcherTimer) clearInterval(watcherTimer);
  watcherTimer = null;
}

module.exports = router;
module.exports.startEventWatcher = startEventWatcher;
module.exports.stopEventWatcher = stopEventWatcher;
module.exports.normalizeCryptoSymbol = normalizeCryptoSymbol;
module.exports._setQueryForTests = (fn) => { dbQuery = fn; };
module.exports._setRunAlpacaForTests = (fn) => { runAlpaca = fn || require('./alpaca_cli').runAlpaca; };
