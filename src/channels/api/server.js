'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../../.env') });

const express = require('express');
const { getAllSubagentStatuses, getBucketStatus } = require('../../database/redis');
const { verdictCache, query: dbQuery } = require('../../database/postgres');
const fs = require('fs');
const REGIME_FILE = require('path').join(__dirname, '../../../.agents/market-state/regime_latest.json');

const app  = express();
app.use(express.json());
const PORT = process.env.DASHBOARD_PORT || 3000;

// SSE clients
const sseClients = new Set();

function broadcast(data) {
  const msg = `data: ${JSON.stringify(data)}\n\n`;
  for (const res of sseClients) {
    try { res.write(msg); } catch { sseClients.delete(res); }
  }
}
module.exports.broadcast = broadcast;
// Wire SSE broadcast into the pipeline collector so all data collection events push live updates
require("../../pipeline/collector").setBroadcast(broadcast);

// ── DB-backed API routes ────────────────────────────────────────────────────────

// Full universe grouped by category
app.get('/api/db/universe', async (req, res) => {
  try {
    const result = await dbQuery(
      `SELECT ticker, name, category, has_options, has_fundamentals, snapshot_24h
       FROM universe_config WHERE active=true ORDER BY category, ticker`
    );
    const grouped = {};
    for (const row of result.rows) {
      if (!grouped[row.category]) grouped[row.category] = [];
      grouped[row.category].push(row);
    }
    res.json(grouped);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Market overview — latest price + change for all instruments
app.get('/api/db/market-overview', async (req, res) => {
  try {
    const result = await dbQuery(`
      WITH latest AS (
        SELECT DISTINCT ON (ticker) ticker, date, open, high, low, close, volume
        FROM price_data ORDER BY ticker, date DESC
      ), prev AS (
        SELECT DISTINCT ON (p.ticker) p.ticker, p.close AS prev_close
        FROM price_data p
        JOIN latest l ON l.ticker = p.ticker AND p.date < l.date
        ORDER BY p.ticker, p.date DESC
      )
      SELECT l.ticker, l.date, l.close, l.open, l.high, l.low, l.volume,
             pr.prev_close,
             CASE WHEN pr.prev_close > 0
               THEN ROUND(((l.close - pr.prev_close) / pr.prev_close * 100)::numeric, 2)
             END AS change_pct,
             u.name, u.category
      FROM latest l
      LEFT JOIN prev pr ON pr.ticker = l.ticker
      JOIN universe_config u ON u.ticker = l.ticker
      WHERE u.active = true
      ORDER BY u.category, l.ticker
    `);
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Price history from DB — no external calls
app.get('/api/db/prices/:ticker', async (req, res) => {
  const ticker = req.params.ticker.toUpperCase();
  try {
    let result;
    if (req.query.limit) {
      // ?limit=N  → last N trading days (row count, chronological order)
      const n = Math.min(parseInt(req.query.limit) || 5, 3650);
      result = await dbQuery(
        `SELECT date, open, high, low, close, volume FROM price_data
         WHERE ticker=$1 ORDER BY date DESC LIMIT $2`,
        [ticker, n]
      );
      result = { rows: result.rows.reverse() }; // back to chronological
    } else {
      // ?days=N → calendar day window
      const days = Math.min(parseInt(req.query.days) || 365, 3650);
      result = await dbQuery(
        `SELECT date, open, high, low, close, volume FROM price_data
         WHERE ticker=$1 AND date >= CURRENT_DATE - ($2 * INTERVAL '1 day')
         ORDER BY date ASC`,
        [ticker, days]
      );
    }
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Options contracts from DB (top by open interest)
app.get('/api/db/options/:ticker', async (req, res) => {
  const ticker   = req.params.ticker.toUpperCase();
  const limit    = Math.min(parseInt(req.query.limit) || 30, 100);
  const type     = req.query.type; // 'call' or 'put'
  try {
    const result = await dbQuery(
      `SELECT expiry, strike, contract_type, delta, gamma, theta, vega, iv,
              open_interest, volume, last_price, bid, ask
       FROM options_data WHERE ticker=$1
         ${type ? `AND contract_type = '${type === 'call' ? 'call' : 'put'}'` : ''}
       ORDER BY snapshot_date DESC, open_interest DESC NULLS LAST LIMIT $2`,
      [ticker, limit]
    );
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Fundamentals from DB (last 4 quarters)
app.get('/api/db/fundamentals/:ticker', async (req, res) => {
  const ticker = req.params.ticker.toUpperCase();
  try {
    const result = await dbQuery(
      `SELECT period, period_end, revenue, gross_profit, ebitda, net_income, eps,
              gross_margin, operating_margin, net_margin, revenue_growth_yoy, source
       FROM fundamentals WHERE ticker=$1 ORDER BY period_end DESC LIMIT 4`,
      [ticker]
    );
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Pipeline + agent status (existing)
app.get('/api/status', async (req, res) => {
  const [agents, rateLimits] = await Promise.all([
    getAllSubagentStatuses().catch(() => []),
    getBucketStatus().catch(() => ({})),
  ]);
  res.json({ agents, rateLimits, timestamp: new Date().toISOString() });
});

app.get('/api/pipeline/status', async (req, res) => {
  try {
    const store    = require('../../pipeline/store');
    const coverage = await store.getCoverageStats();
    res.json({ coverage, timestamp: new Date().toISOString() });
  } catch (err) { res.json({ error: err.message, coverage: null }); }
});

app.get('/api/verdicts', async (req, res) => {
  try {
    const result = await dbQuery(
      `SELECT ticker, analysis_date, verdict, score, signals, stale_after
       FROM verdict_cache WHERE stale_after > NOW()
       ORDER BY analysis_date DESC LIMIT 50`
    );
    res.json(result.rows);
  } catch { res.json([]); }
});

// News feed from DB — supports ?ticker=X, ?tickers=X,Y,Z, ?q=keyword, ?since=ISO, ?limit=N
app.get('/api/db/news', async (req, res) => {
  try {
    const store   = require('../../pipeline/store');
    const ticker  = req.query.ticker;
    const tickers = req.query.tickers ? req.query.tickers.split(',').map(t => t.trim()).filter(Boolean) : null;
    const q       = req.query.q;
    const since   = req.query.since;
    const limit   = Math.min(parseInt(req.query.limit) || 30, 200);
    const news    = await store.getNews({ ticker, tickers, q, since, limit });
    res.json(news);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Trigger news collection on demand (runs in background, streams status via SSE)
app.post('/api/trigger/news', async (req, res) => {
  try {
    const collector = require('../../pipeline/collector');
    const store     = require('../../pipeline/store');
    res.json({ ok: true, message: 'News collection started' });
    // Run in background after response sent
    const fullUniverse  = await store.getActiveUniverse();
    const equityTickers = fullUniverse.filter(u => u.category === 'equity').map(u => u.ticker);
    collector.runNewsCollection(equityTickers).then(async () => {
      const { query: dbQuery } = require('../../database/postgres');
      const r = await dbQuery('SELECT COUNT(*) FROM market_news');
      broadcast({ type: 'news', total: parseInt(r.rows[0].count) });
    }).catch(err => broadcast({ type: 'news_error', error: err.message }));
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Recent collection cycles
app.get('/api/db/cycles', async (req, res) => {
  const n = Math.min(parseInt(req.query.n) || 10, 50);
  try {
    const result = await dbQuery(
      `SELECT id, started_at, completed_at, duration_ms, snapshot_tickers,
              price_rows, options_contracts, technical_rows, fundamental_records,
              polygon_calls, fmp_calls, yfinance_calls, total_rows, errors, status
       FROM collection_cycles ORDER BY id DESC LIMIT $1`,
      [n]
    );
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── Portfolio API ──────────────────────────────────────────────────────────────

app.get('/api/portfolio/positions', async (req, res) => {
  try {
    const result = await dbQuery(`
      SELECT es.id, es.strategy_id, es.ticker, es.direction,
             es.entry_price, es.stop_loss, es.target_1, es.position_size_pct,
             es.signal_date, es.status,
             sp.close_price AS current_price,
             sp.unrealized_pnl_pct, sp.days_held
      FROM execution_signals es
      LEFT JOIN LATERAL (
        SELECT close_price, unrealized_pnl_pct, days_held
        FROM signal_pnl WHERE signal_id = es.id ORDER BY pnl_date DESC LIMIT 1
      ) sp ON true
      WHERE es.status = 'open'
      ORDER BY es.signal_date DESC
    `);
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.get('/api/portfolio/history', async (req, res) => {
  try {
    const result = await dbQuery(`
      SELECT es.strategy_id, es.ticker, es.direction, es.entry_price,
             sp.closed_price, sp.realized_pnl_pct, sp.days_held,
             sp.close_reason, sp.closed_at
      FROM signal_pnl sp
      JOIN execution_signals es ON es.id = sp.signal_id
      WHERE sp.status = 'closed'
      ORDER BY sp.closed_at DESC LIMIT 100
    `);
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.get('/api/portfolio/summary', async (req, res) => {
  try {
    const [openRes, closedRes] = await Promise.all([
      dbQuery(`SELECT COUNT(*) AS open_count FROM execution_signals WHERE status = 'open'`),
      dbQuery(`
        SELECT COUNT(*) AS closed_count,
               COUNT(*) FILTER (WHERE realized_pnl_pct > 0) AS wins,
               ROUND(AVG(realized_pnl_pct)::numeric, 2) AS avg_pnl,
               ROUND(MAX(realized_pnl_pct)::numeric, 2) AS best,
               ROUND(MIN(realized_pnl_pct)::numeric, 2) AS worst
        FROM signal_pnl WHERE status = 'closed'
      `),
    ]);
    const open        = openRes.rows[0];
    const closed      = closedRes.rows[0];
    const closedCount = parseInt(closed.closed_count) || 0;
    const wins        = parseInt(closed.wins) || 0;
    res.json({
      open_count:   parseInt(open.open_count) || 0,
      closed_count: closedCount,
      win_rate:     closedCount > 0 ? Math.round(wins / closedCount * 100) : null,
      avg_realized: closed.avg_pnl,
      best_trade:   closed.best,
      worst_trade:  closed.worst,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.get('/api/portfolio/pnl-curve', async (req, res) => {
  const days = Math.min(parseInt(req.query.days) || 90, 365);
  try {
    const result = await dbQuery(`
      SELECT pnl_date,
             ROUND(AVG(unrealized_pnl_pct)::numeric, 2) AS avg_unrealized,
             COUNT(*) AS open_count
      FROM signal_pnl
      WHERE pnl_date >= CURRENT_DATE - ($1 * INTERVAL '1 day')
      GROUP BY pnl_date ORDER BY pnl_date
    `, [days]);
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.get('/api/data/freshness', async (req, res) => {
  try {
    const { getDataFreshness } = require('../../pipeline/freshness');
    res.json(await getDataFreshness());
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Strategies page: manifest + per-strategy stats join.
// Rows from manifest that have no signals yet land with null stats (just-promoted);
// strategy_ids in stats that aren't in the manifest (legacy/decommissioned) land with state='orphan'.
app.get('/api/strategies', async (req, res) => {
  try {
    const fs    = require('fs');
    const path  = require('path');
    const manifestPath = path.join(__dirname, '../../strategies/manifest.json');
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    const statsRows = (await dbQuery(`SELECT * FROM strategy_stats`)).rows;
    const statsById = Object.fromEntries(statsRows.map(r => [r.strategy_id, r]));

    const rows = [];
    const seen = new Set();
    for (const [sid, rec] of Object.entries(manifest.strategies || {})) {
      seen.add(sid);
      const s = statsById[sid] || {};
      rows.push({
        strategy_id:        sid,
        state:              rec.state || 'unknown',
        description:        rec.metadata?.description || '',
        state_since:        rec.state_since || null,
        open_count:         s.open_count || 0,
        closed_count:       s.closed_count || 0,
        total_count:        s.total_count || 0,
        wins:               s.wins || 0,
        losses:             s.losses || 0,
        win_rate:           s.win_rate,
        avg_realized_pct:   s.avg_realized_pct,
        avg_unrealized_pct: s.avg_unrealized_pct,
        best_trade_pct:     s.best_trade_pct,
        worst_trade_pct:    s.worst_trade_pct,
        avg_days_held:      s.avg_days_held,
        last_signal_date:   s.last_signal_date,
        dominant_regime:    s.dominant_regime,
      });
    }
    // Orphans: strategy_ids with signals but no manifest entry
    for (const s of statsRows) {
      if (seen.has(s.strategy_id)) continue;
      rows.push({
        strategy_id:        s.strategy_id,
        state:              'orphan',
        description:        '',
        state_since:        null,
        open_count:         s.open_count || 0,
        closed_count:       s.closed_count || 0,
        total_count:        s.total_count || 0,
        wins:               s.wins || 0,
        losses:             s.losses || 0,
        win_rate:           s.win_rate,
        avg_realized_pct:   s.avg_realized_pct,
        avg_unrealized_pct: s.avg_unrealized_pct,
        best_trade_pct:     s.best_trade_pct,
        worst_trade_pct:    s.worst_trade_pct,
        avg_days_held:      s.avg_days_held,
        last_signal_date:   s.last_signal_date,
        dominant_regime:    s.dominant_regime,
      });
    }
    res.json(rows);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/portfolio/account', async (req, res) => {
  const key    = process.env.ALPACA_API_KEY    || '';
  const secret = process.env.ALPACA_SECRET_KEY || '';
  const base   = process.env.ALPACA_BASE_URL   || 'https://paper-api.alpaca.markets';
  try {
    const https = require('https');
    const fetch = (url) => new Promise((resolve, reject) => {
      const u = new URL(url);
      const opts = {
        hostname: u.hostname, path: u.pathname + u.search, method: 'GET',
        headers: { 'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret, 'Accept': 'application/json' },
      };
      const req2 = https.request(opts, r => {
        let body = '';
        r.on('data', c => body += c);
        r.on('end', () => resolve({ status: r.statusCode, body }));
      });
      req2.on('error', reject);
      req2.end();
    });

    const acct = await fetch(`${base}/v2/account`);
    if (acct.status !== 200) return res.status(acct.status).json({ error: `Alpaca: ${acct.body}` });
    const a = JSON.parse(acct.body);

    res.json({
      equity:          parseFloat(a.equity)          || 0,
      cash:            parseFloat(a.cash)            || 0,
      buying_power:    parseFloat(a.buying_power)    || 0,
      last_equity:     parseFloat(a.last_equity)     || 0,
      long_market_value:  parseFloat(a.long_market_value)  || 0,
      short_market_value: parseFloat(a.short_market_value) || 0,
      day_pnl:         (parseFloat(a.equity) - parseFloat(a.last_equity)) || 0,
      day_pnl_pct:     parseFloat(a.last_equity) > 0
                         ? ((parseFloat(a.equity) - parseFloat(a.last_equity)) / parseFloat(a.last_equity) * 100)
                         : 0,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.get('/api/portfolio/value-curve', async (req, res) => {
  const key    = process.env.ALPACA_API_KEY    || '';
  const secret = process.env.ALPACA_SECRET_KEY || '';
  const base   = process.env.ALPACA_BASE_URL   || 'https://paper-api.alpaca.markets';
  const period = req.query.period || '1M';
  try {
    const https = require('https');
    const url   = `${base}/v2/account/portfolio/history?period=${period}&timeframe=1D&extended_hours=false`;
    const u     = new URL(url);
    const data  = await new Promise((resolve, reject) => {
      const opts = {
        hostname: u.hostname, path: u.pathname + u.search, method: 'GET',
        headers: { 'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret, 'Accept': 'application/json' },
      };
      const req2 = require('https').request(opts, r => {
        let body = '';
        r.on('data', c => body += c);
        r.on('end', () => resolve({ status: r.statusCode, body }));
      });
      req2.on('error', reject);
      req2.end();
    });
    if (data.status !== 200) return res.status(data.status).json({ error: `Alpaca: ${data.body}` });
    const h = JSON.parse(data.body);
    // Zip timestamps + equity into [{date, equity, profit_loss, profit_loss_pct}]
    const rows = (h.timestamp || []).map((ts, i) => ({
      date:             new Date(ts * 1000).toISOString().slice(0, 10),
      equity:           h.equity?.[i]          ?? null,
      profit_loss:      h.profit_loss?.[i]     ?? null,
      profit_loss_pct:  h.profit_loss_pct?.[i] ?? null,
    })).filter(r => r.equity !== null && r.equity > 0);
    res.json({ rows, base_value: h.base_value ?? null });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Called by cron after pipeline completes — pushes market_update to all SSE clients
app.post('/api/events/data-updated', (req, res) => {
  broadcast({ type: 'market_update' });
  res.json({ ok: true });
});

// Volatility regime — reads regime_latest.json written by run_market_state.py
app.get('/api/regime', async (req, res) => {
  try {
    const raw = await fs.promises.readFile(REGIME_FILE, 'utf8');
    res.json({ available: true, ...JSON.parse(raw) });
  } catch (err) {
    if (err.code === 'ENOENT' || err instanceof SyntaxError)
      return res.json({ available: false, state: 'NO_DATA' });
    res.status(500).json({ error: err.message });
  }
});

// SSE stream
app.get('/events', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.write('data: {"type":"connected"}\n\n');
  sseClients.add(res);
  req.on('close', () => sseClients.delete(res));
});

// ── Dashboard ──────────────────────────────────────────────────────────────────
app.get('/', (req, res) => res.send(getDashboardHtml()));

function getDashboardHtml() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenClaw</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--panel:#161b22;--border:#30363d;--border2:#21262d;
  --text:#e6edf3;--muted:#8b949e;--dim:#484f58;
  --blue:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--purple:#bc8cff;--orange:#f0883e;
}
html,body{height:100%;overflow:hidden}
body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',monospace;font-size:13px;display:flex;flex-direction:column}

/* Header */
#header{background:var(--panel);border-bottom:1px solid var(--border);padding:0 20px;height:44px;display:flex;align-items:center;gap:14px;flex-shrink:0}
#header h1{font-size:15px;font-weight:700;color:var(--blue);letter-spacing:-0.02em}
.dot{width:7px;height:7px;border-radius:50%;background:var(--green);animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
#clock{color:var(--dim);font-size:11px;margin-left:auto}
#pipeline-badge{font-size:10px;color:var(--muted);background:var(--border2);padding:2px 8px;border-radius:10px;cursor:pointer}
#pipeline-badge:hover{background:var(--border)}

/* Market Strip */
#strip{background:#0a0e14;border-bottom:1px solid var(--border2);height:32px;display:flex;align-items:center;overflow:hidden;flex-shrink:0;position:relative}
#strip-inner{display:flex;gap:0;white-space:nowrap;animation:scroll 60s linear infinite}
#strip-inner:hover{animation-play-state:paused}
@keyframes scroll{0%{transform:translateX(0)}100%{transform:translateX(-50%)}}
.strip-item{display:inline-flex;align-items:center;gap:6px;padding:0 16px;font-size:11px;border-right:1px solid var(--border2);cursor:pointer;height:32px}
.strip-item:hover{background:var(--panel)}
.strip-item .s-ticker{color:var(--muted);font-weight:600}
.strip-item .s-price{color:var(--text)}
.strip-item .s-chg{font-size:10px}

/* Body layout */
#view-wrap{flex:1;position:relative;overflow:hidden;min-height:0}
#body{position:absolute;inset:0;display:flex;overflow:hidden}

/* Sidebar */
#sidebar{width:230px;min-width:230px;border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
#search-wrap{padding:10px 12px 6px;border-bottom:1px solid var(--border2)}
#search{width:100%;background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:5px 10px;color:var(--text);font-size:12px;font-family:inherit;outline:none}
#search:focus{border-color:var(--blue)}
#cat-tabs{display:flex;gap:0;padding:6px 12px;flex-wrap:wrap;gap:4px;border-bottom:1px solid var(--border2)}
.cat-tab{font-size:10px;padding:2px 7px;border-radius:10px;cursor:pointer;color:var(--muted);background:var(--border2);border:none;font-family:inherit}
.cat-tab.active{background:var(--blue);color:#fff}
#ticker-list{flex:1;overflow-y:auto;padding:4px 0}
.t-item{display:flex;justify-content:space-between;align-items:center;padding:5px 14px;cursor:pointer;border-left:2px solid transparent}
.t-item:hover{background:var(--panel)}
.t-item.active{background:var(--panel);border-left-color:var(--blue)}
.t-item .t-sym{font-size:12px;font-weight:600;color:var(--text)}
.t-item .t-name{font-size:10px;color:var(--dim);max-width:90px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.t-item .t-chg{font-size:11px;text-align:right}
.t-sep{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);padding:8px 14px 3px;margin-top:4px}
#sidebar-footer{padding:10px 12px;border-top:1px solid var(--border2);font-size:10px;color:var(--dim)}

/* Main */
#main{flex:1;overflow-y:auto;display:flex;flex-direction:column}

/* Market overview */
#overview{padding:16px}
.ov-group{margin-bottom:20px}
.ov-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px;display:flex;align-items:center;gap:6px}
.ov-label::after{content:'';flex:1;height:1px;background:var(--border2)}
.ov-cards{display:flex;flex-wrap:wrap;gap:8px}
.ov-card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px;min-width:130px;cursor:pointer;transition:border-color .15s}
.ov-card:hover{border-color:var(--blue)}
.ov-card .ov-sym{font-size:11px;font-weight:700;color:var(--text)}
.ov-card .ov-name{font-size:10px;color:var(--dim);margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px}
.ov-card .ov-price{font-size:15px;font-weight:700}
.ov-card .ov-chg{font-size:11px;margin-top:2px}
.ov-card .ov-date{font-size:9px;color:var(--dim);margin-top:4px}

/* Ticker detail */
#detail{flex:1;display:flex;flex-direction:column;padding:0}
#detail-header{padding:14px 20px 10px;border-bottom:1px solid var(--border2);display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap}
#detail-sym{font-size:22px;font-weight:700;color:var(--text)}
#detail-name{font-size:12px;color:var(--muted);margin-top:2px}
#detail-price{font-size:28px;font-weight:700;margin-left:auto}
#detail-chg{font-size:13px;margin-top:4px;text-align:right}
#detail-date{font-size:10px;color:var(--dim);margin-top:2px;text-align:right}

/* Range + type selectors */
#chart-controls{display:flex;gap:6px;padding:10px 20px;align-items:center;border-bottom:1px solid var(--border2)}
.range-btn{background:var(--border2);border:1px solid var(--border);border-radius:4px;padding:3px 9px;font-size:11px;cursor:pointer;color:var(--muted);font-family:inherit}
.range-btn.active,.range-btn:hover{background:var(--blue);border-color:var(--blue);color:#fff}
#chart-type-toggle{margin-left:auto;display:flex;gap:4px}
.type-btn{background:var(--border2);border:1px solid var(--border);border-radius:4px;padding:3px 9px;font-size:11px;cursor:pointer;color:var(--muted);font-family:inherit}
.type-btn.active{background:var(--panel);border-color:var(--blue);color:var(--blue)}

/* Chart */
#chart-wrap{position:relative;padding:16px 20px 8px;flex:0 0 300px}
#priceChart{width:100%!important}

/* Technicals */
#tech-bar{display:flex;gap:12px;padding:10px 20px;border-top:1px solid var(--border2);border-bottom:1px solid var(--border2);flex-wrap:wrap;align-items:center}
.tech-item{display:flex;flex-direction:column;gap:2px;min-width:80px}
.tech-label{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.tech-val{font-size:13px;font-weight:600}
.rsi-wrap{flex:1;min-width:200px;max-width:300px}
.rsi-track{background:var(--border2);border-radius:4px;height:6px;position:relative;margin-top:4px}
.rsi-fill{height:100%;border-radius:4px;transition:width .4s}
.rsi-zones{display:flex;font-size:8px;color:var(--dim);justify-content:space-between;margin-top:2px}

/* Data tabs */
#data-tabs{display:flex;gap:0;padding:0 20px;border-bottom:1px solid var(--border2)}
.data-tab{padding:8px 14px;font-size:11px;cursor:pointer;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px}
.data-tab.active{color:var(--blue);border-bottom-color:var(--blue)}
.data-tab:hover{color:var(--text)}
#tab-content{padding:16px 20px;overflow-y:auto}

/* Tables */
.db-table{width:100%;border-collapse:collapse;font-size:11px}
.db-table th{color:var(--dim);font-weight:600;text-align:left;padding:4px 8px;border-bottom:1px solid var(--border2);font-size:10px;text-transform:uppercase;letter-spacing:.04em}
.db-table td{padding:5px 8px;border-bottom:1px solid var(--border2)}
.db-table tr:last-child td{border:none}
.db-table .call{color:#58a6ff}.db-table .put{color:#f0883e}
.num{text-align:right;font-variant-numeric:tabular-nums}

/* Cycles table */
.cycle-row td:first-child{color:var(--muted)}
.status-complete{color:var(--green)}.status-running{color:var(--blue)}.status-abandoned{color:var(--dim)}.status-complete-with-errors{color:var(--yellow)}

/* News */
.news-feed{display:flex;flex-direction:column;gap:8px}
.news-card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:11px 14px;transition:border-color .15s}
.news-card:hover{border-color:var(--blue)}
.news-card a{text-decoration:none;color:inherit}
.news-meta{display:flex;align-items:center;gap:8px;margin-bottom:5px;flex-wrap:wrap}
.news-source{font-size:10px;font-weight:600;color:var(--blue);text-transform:uppercase;letter-spacing:.04em}
.news-time{font-size:10px;color:var(--dim)}
.news-ticker-badge{font-size:9px;background:var(--border2);color:var(--muted);padding:1px 5px;border-radius:3px;cursor:pointer}
.news-ticker-badge:hover{color:var(--blue)}
.news-title{font-size:12px;font-weight:600;color:var(--text);line-height:1.4;margin-bottom:4px}
.news-summary{font-size:11px;color:var(--muted);line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.news-read-more{display:inline-block;margin-top:7px;font-size:10px;color:var(--blue);text-decoration:none;opacity:.75}
.news-read-more:hover{opacity:1;text-decoration:underline}
.news-section-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);padding:14px 0 8px;display:flex;align-items:center;gap:8px}
.news-section-label::after{content:'';flex:1;height:1px;background:var(--border2)}

/* Misc */
.positive{color:var(--green)}.negative{color:var(--red)}.neutral{color:var(--muted)}
.empty{color:var(--dim);text-align:center;padding:30px;font-size:11px}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border)}

/* Nav buttons */
.nav-btn{background:none;border:none;color:var(--muted);font-size:12px;cursor:pointer;font-family:inherit;padding:4px 11px;border-radius:4px}
.nav-btn.active,.nav-btn:hover{color:var(--blue);background:var(--border2)}
.refresh-btn{background:none;border:1px solid var(--border);color:var(--muted);font-size:11px;cursor:pointer;font-family:inherit;padding:3px 9px;border-radius:4px}
.refresh-btn:hover{color:var(--text);border-color:var(--blue)}

/* Portfolio page */
#portfolio-page{display:none;position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;background:var(--bg)}
#strategies-page{display:none;position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;background:var(--bg)}
#strategies-inner{max-width:1400px;margin:0 auto;padding:20px}
.st-tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
.st-tile{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.st-tile-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px}
.st-tile-value{font-size:22px;font-weight:700}
.st-filters{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;align-items:center}
.st-pill{background:var(--border2);border:1px solid var(--border);border-radius:4px;padding:3px 10px;font-size:11px;cursor:pointer;color:var(--muted);font-family:inherit}
.st-pill.active{background:var(--blue);border-color:var(--blue);color:#fff}
.st-pill-label{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);padding:0 6px 0 2px}
.state-live,.state-paper,.state-active{color:#fff;background:var(--green);border-color:var(--green);padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.state-candidate{color:var(--muted);background:var(--border2);border-color:var(--border);padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.state-staging{color:#fff;background:var(--orange);border-color:var(--orange);padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.state-orphan{color:var(--dim);background:transparent;border:1px solid var(--border);padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
th.st-sortable{cursor:pointer;user-select:none}
th.st-sortable:hover{color:var(--blue)}
th.st-sortable.st-sorted::after{content:' ▾';color:var(--blue)}
th.st-sortable.st-sorted-asc::after{content:' ▴';color:var(--blue)}
#pf-inner{display:flex;flex-direction:column;gap:16px;padding:20px 24px}
.pf-summary-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.pf-stat-card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.pf-stat-label{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
.pf-stat-value{font-size:22px;font-weight:700;color:var(--text)}
.pf-stat-sub{font-size:10px;color:var(--dim);margin-top:3px}
.pf-chart-wrap{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px;position:relative;height:180px}
.pf-chart-label{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:8px}
.pf-section{background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.pf-section-header{padding:11px 16px;border-bottom:1px solid var(--border2);font-size:11px;font-weight:600;color:var(--text);display:flex;align-items:center;justify-content:space-between}
.pf-section-body{overflow-x:auto}
.pf-pnl-pos{color:var(--green)}.pf-pnl-neg{color:var(--red)}.pf-pnl-flat{color:var(--muted)}
.dir-long{color:var(--green);font-weight:600}.dir-short{color:var(--red);font-weight:600}
/* ── Regime Panel ── */
.regime-panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:20px}
.regime-panel-header{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px;display:flex;align-items:center;gap:6px}
.regime-panel-header::after{content:'';flex:1;height:1px;background:var(--border2)}
.regime-top-row{display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:12px}
.regime-state-badge{font-size:13px;font-weight:700;padding:4px 12px;border-radius:6px;letter-spacing:.04em;border:1px solid transparent}
.regime-state-LOW_VOL{color:#fff;background:var(--green);border-color:var(--green)}
.regime-state-TRANSITIONING{color:#000;background:var(--yellow);border-color:var(--yellow)}
.regime-state-HIGH_VOL{color:#fff;background:var(--orange);border-color:var(--orange)}
.regime-state-CRISIS{color:#fff;background:var(--red);border-color:var(--red)}
.regime-state-NO_DATA{color:var(--dim);background:var(--border2);border-color:var(--border)}
.regime-meta-item{display:flex;flex-direction:column;gap:2px}
.regime-meta-label{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.regime-meta-val{font-size:13px;font-weight:600;color:var(--text)}
.regime-bar-group{flex:1;min-width:160px;max-width:260px}
.regime-bar-label{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin-bottom:3px;display:flex;justify-content:space-between}
.regime-bar-track{background:var(--border2);border-radius:4px;height:8px;overflow:hidden}
.regime-bar-fill{height:100%;border-radius:4px;transition:width .4s}
.regime-roro-track{background:var(--border2);border-radius:4px;height:8px;overflow:hidden;position:relative}
.regime-roro-center{position:absolute;left:50%;top:0;width:1px;height:100%;background:var(--border)}
.regime-roro-fill{position:absolute;top:0;height:100%;border-radius:4px;transition:all .4s}
.regime-bottom-row{display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start;margin-top:4px}
.regime-feat-grid{display:flex;gap:12px;flex-wrap:wrap;flex:1}
.regime-feat{display:flex;flex-direction:column;gap:2px;min-width:90px}
.regime-feat-label{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim)}
.regime-feat-val{font-size:12px;font-weight:600;color:var(--text)}
.regime-prob-section{min-width:180px}
.regime-prob-label{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin-bottom:6px}
.regime-prob-row{display:flex;align-items:center;gap:6px;margin-bottom:4px}
.regime-prob-name{font-size:10px;color:var(--muted);width:80px;flex-shrink:0}
.regime-prob-track{flex:1;background:var(--border2);border-radius:3px;height:6px;overflow:hidden}
.regime-prob-bar{height:100%;border-radius:3px;transition:width .4s}
.regime-prob-pct{font-size:10px;color:var(--muted);width:34px;text-align:right;flex-shrink:0}
.regime-alert-badge{font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(248,81,73,.15);color:var(--red);border:1px solid rgba(248,81,73,.3)}
</style>
</head>
<body>

<div id="header">
  <span class="dot" id="dot"></span>
  <h1>🦞 OpenClaw</h1>
  <button class="nav-btn active" id="nav-market" onclick="showMarket()">Market</button>
  <button class="nav-btn" id="nav-portfolio" onclick="showPortfolio()">Portfolio</button>
  <button class="nav-btn" id="nav-strategies" onclick="showStrategies()">Strategies</button>
  <span id="pipeline-badge">Loading pipeline...</span>
  <button class="refresh-btn" onclick="loadMarket();refreshPipeline()" title="Refresh data">↺ Refresh</button>
  <span id="clock"></span>
</div>

<div id="strip"><div id="strip-inner"></div></div>

<div id="view-wrap">
<div id="body">
  <div id="sidebar">
    <div id="search-wrap">
      <input id="search" placeholder="Search ticker or name..." oninput="filterTickers()" />
    </div>
    <div id="cat-tabs">
      <button class="cat-tab active" onclick="setCat('all')">All</button>
      <button class="cat-tab" onclick="setCat('equity')">SP500</button>
      <button class="cat-tab" onclick="setCat('index')">Index</button>
      <button class="cat-tab" onclick="setCat('etf')">ETF</button>
      <button class="cat-tab" onclick="setCat('crypto')">Crypto</button>
      <button class="cat-tab" onclick="setCat('commodity')">Cmdty</button>
      <button class="cat-tab" onclick="setCat('forex')">FX</button>
    </div>
    <div id="ticker-list"></div>
    <div id="sidebar-footer" id="sidebar-footer">— instruments</div>
  </div>

  <div id="main">
    <div id="overview" style="display:none"></div>
    <div id="detail" style="display:none"></div>
    <div id="loading" style="color:var(--dim);text-align:center;padding:60px;font-size:12px;">Loading market data...</div>
  </div>
</div>

<div id="portfolio-page">
<div id="pf-inner">
  <div id="pf-account-row" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px">
    <div class="pf-stat-card"><div class="pf-stat-label">Portfolio Value</div><div class="pf-stat-value" id="pf-equity">—</div><div class="pf-stat-sub" id="pf-equity-sub"></div></div>
    <div class="pf-stat-card"><div class="pf-stat-label">Cash</div><div class="pf-stat-value" id="pf-cash">—</div><div class="pf-stat-sub" id="pf-cash-sub"></div></div>
    <div class="pf-stat-card"><div class="pf-stat-label">Day P&amp;L</div><div class="pf-stat-value" id="pf-daypnl">—</div><div class="pf-stat-sub" id="pf-daypnl-sub"></div></div>
    <div class="pf-stat-card"><div class="pf-stat-label">Invested</div><div class="pf-stat-value" id="pf-invested">—</div><div class="pf-stat-sub" id="pf-invested-sub"></div></div>
  </div>
  <div class="pf-summary-row" id="pf-summary">
    <div class="pf-stat-card"><div class="pf-stat-label">Open Positions</div><div class="pf-stat-value" id="pf-open">—</div></div>
    <div class="pf-stat-card"><div class="pf-stat-label">Closed Trades</div><div class="pf-stat-value" id="pf-closed">—</div></div>
    <div class="pf-stat-card"><div class="pf-stat-label">Win Rate</div><div class="pf-stat-value" id="pf-winrate">—</div><div class="pf-stat-sub" id="pf-winrate-sub"></div></div>
    <div class="pf-stat-card"><div class="pf-stat-label">Avg Realized P&amp;L</div><div class="pf-stat-value" id="pf-avgpnl">—</div><div class="pf-stat-sub" id="pf-pnl-sub"></div></div>
  </div>
  <div class="pf-chart-wrap">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <div class="pf-chart-label" style="margin-bottom:0" id="pf-chart-title">Portfolio P&amp;L Curve (90d)</div>
      <div style="display:flex;gap:4px">
        <button class="range-btn active" id="btn-pnl-mode" onclick="setPnlMode('pnl')" style="font-size:10px;padding:2px 8px">P&amp;L %</button>
        <button class="range-btn" id="btn-value-mode" onclick="setPnlMode('value')" style="font-size:10px;padding:2px 8px">Value $</button>
      </div>
    </div>
    <canvas id="pnlChart" style="width:100%;height:130px"></canvas>
  </div>
  <div class="pf-section">
    <div class="pf-section-header"><span>Active Positions</span><span id="pf-pos-count" style="color:var(--muted);font-weight:400;font-size:10px"></span></div>
    <div class="pf-section-body"><div id="pf-positions"><div class="empty">Loading...</div></div></div>
  </div>
  <div class="pf-section">
    <div class="pf-section-header"><span>Closed Trades</span><span id="pf-hist-count" style="color:var(--muted);font-weight:400;font-size:10px"></span></div>
    <div class="pf-section-body"><div id="pf-history"><div class="empty">Loading...</div></div></div>
  </div>
</div><!-- #pf-inner -->
</div><!-- #portfolio-page -->

<div id="strategies-page">
  <div id="strategies-inner">
    <div class="st-tiles">
      <div class="st-tile"><div class="st-tile-label">Total Strategies</div><div class="st-tile-value" id="st-total">—</div></div>
      <div class="st-tile"><div class="st-tile-label">Active Signals</div><div class="st-tile-value" id="st-open">—</div></div>
      <div class="st-tile"><div class="st-tile-label">Lifetime Closed</div><div class="st-tile-value" id="st-closed">—</div></div>
      <div class="st-tile"><div class="st-tile-label">Stack Win Rate</div><div class="st-tile-value" id="st-winrate">—</div></div>
    </div>

    <div class="st-filters">
      <span class="st-pill-label">State:</span>
      <button class="st-pill active" data-state="all" onclick="setStateFilter('all')">All</button>
      <button class="st-pill" data-state="active" onclick="setStateFilter('active')">Active</button>
      <button class="st-pill" data-state="candidate" onclick="setStateFilter('candidate')">Candidate</button>
      <button class="st-pill" data-state="staging" onclick="setStateFilter('staging')">Staging</button>
      <span style="width:16px"></span>
      <span class="st-pill-label">Regime:</span>
      <button class="st-pill active" data-regime="all" onclick="setRegimeFilter('all')">All</button>
      <button class="st-pill" data-regime="LOW_VOL" onclick="setRegimeFilter('LOW_VOL')">Low Vol</button>
      <button class="st-pill" data-regime="TRANSITIONING" onclick="setRegimeFilter('TRANSITIONING')">Transitioning</button>
      <button class="st-pill" data-regime="HIGH_VOL" onclick="setRegimeFilter('HIGH_VOL')">High Vol</button>
      <button class="st-pill" data-regime="CRISIS" onclick="setRegimeFilter('CRISIS')">Crisis</button>
    </div>

    <div class="pf-section">
      <div class="pf-section-header">
        <span>Strategies</span>
        <span id="st-shown-count" style="color:var(--muted);font-weight:400;font-size:10px"></span>
      </div>
      <div class="pf-section-body">
        <div id="st-table-wrap"><div class="empty">Loading...</div></div>
      </div>
    </div>
  </div>
</div><!-- #strategies-page -->
</div><!-- #view-wrap -->

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let universeData = {};     // {category: [{ticker, name, ...}]}
let marketData   = {};     // {ticker: {close, change_pct, name, category, date, ...}}
let currentTicker = null;
let currentRange  = 365;
let currentCat    = 'all';
let priceChart    = null;

const CAT_LABELS = {equity:'S&P 100',index:'Indices',etf:'ETFs',crypto:'Crypto',commodity:'Commodities',forex:'Forex'};
const CAT_ICONS  = {equity:'📊',index:'📈',etf:'🏦',crypto:'₿',commodity:'🛢️',forex:'💱'};

// ── Market data refresh ───────────────────────────────────────────────────────
async function loadMarket() {
  const mkt = await fetch('/api/db/market-overview').then(r=>r.json()).catch(()=>[]);
  for (const row of mkt) marketData[row.ticker] = row;
  buildStrip();
  buildSidebar();
  const st = document.getElementById('strategies-page');
  const onStrategies = st && st.style.display === 'block';
  const onPortfolio  = document.getElementById('portfolio-page').style.display === 'block';
  const onMarket     = !onPortfolio && !onStrategies;
  if (onMarket && !currentTicker) showOverview();
}

// ── Boot ──────────────────────────────────────────────────────────────────────
(async () => {
  setInterval(() => {
    document.getElementById('clock').textContent =
      new Date().toLocaleTimeString('en-US',{timeZone:'America/New_York',hour12:true}) + ' ET';
  }, 1000);

  const [univ] = await Promise.all([
    fetch('/api/db/universe').then(r=>r.json()).catch(()=>({})),
  ]);
  universeData = univ;

  await loadMarket();
  document.getElementById('loading').style.display = 'none';

  refreshPipeline();
  setInterval(refreshPipeline, 60000);
  setInterval(loadMarket, 300000);

  // SSE
  const es = new EventSource('/events');
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (d.type === 'pipeline') refreshPipeline();
    if (d.type === 'market_update') {
      loadMarket();
      refreshPipeline();
      const st = document.getElementById('strategies-page');
      if (st && st.style.display === 'block') loadStrategies();
      if (document.getElementById('portfolio-page').style.display === 'block') loadPortfolio();
    }
  };
  es.onerror = () => { document.getElementById('dot').style.background = 'var(--red)'; };
})();

// ── Formatters ────────────────────────────────────────────────────────────────
function fmtPrice(ticker, price) {
  if (price == null) return '—';
  const n = parseFloat(price);
  if (ticker.includes('=X')) return n.toFixed(4);          // Forex
  if (ticker.startsWith('^')) return n.toLocaleString('en-US',{maximumFractionDigits:2}); // Index
  if (n > 1000) return '$' + n.toLocaleString('en-US',{maximumFractionDigits:0});
  if (n < 1)    return '$' + n.toFixed(4);
  return '$' + n.toFixed(2);
}
function pnlCls(n, posCls='pf-pnl-pos', negCls='pf-pnl-neg', flatCls='pf-pnl-flat') {
  if (n == null || isNaN(n)) return '';
  if (Math.abs(n) < 0.005) return flatCls;
  return n > 0 ? posCls : negCls;
}
function fmtChg(chg) {
  if (chg == null) return {text:'—',cls:'neutral'};
  const n = parseFloat(chg);
  const sign = n > 0 ? '+' : '';
  const cls = Math.abs(n) < 0.005 ? 'neutral' : (n > 0 ? 'positive' : 'negative');
  return {text: sign + n.toFixed(2) + '%', cls};
}
function fmtNum(n, decimals=2) {
  if (n == null) return '—';
  const v = parseFloat(n);
  if (Math.abs(v) >= 1e9) return '$' + (v/1e9).toFixed(1) + 'B';
  if (Math.abs(v) >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
  return v.toFixed(decimals);
}
function fmtVol(v) {
  if (!v) return '—';
  const n = parseInt(v);
  if (n >= 1e9) return (n/1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n/1e3).toFixed(0) + 'K';
  return n.toString();
}

// ── Market Strip ──────────────────────────────────────────────────────────────
const STRIP_TICKERS = ['^GSPC','^DJI','^IXIC','^RUT','^VIX','^TNX',
  'BTC-USD','ETH-USD','SOL-USD','GC=F','CL=F','EURUSD=X','GBPUSD=X',
  'SPY','QQQ','GLD','TLT','XLF','XLK','XLE'];

function buildStrip() {
  const items = STRIP_TICKERS
    .filter(t => marketData[t])
    .map(t => {
      const d = marketData[t];
      const c = fmtChg(d.change_pct);
      const name = d.name || t;
      const shortName = name.length > 14 ? name.slice(0,14) : name;
      return \`<div class="strip-item" onclick="selectTicker('\${t}')">
        <span class="s-ticker">\${t}</span>
        <span class="s-price">\${fmtPrice(t, d.close)}</span>
        <span class="s-chg \${c.cls}">\${c.text}</span>
      </div>\`;
    }).join('');
  const inner = document.getElementById('strip-inner');
  inner.innerHTML = items + items; // duplicate for seamless loop
}

// ── Sidebar ───────────────────────────────────────────────────────────────────
function buildSidebar() {
  const search = (document.getElementById('search').value || '').toUpperCase();
  const el = document.getElementById('ticker-list');
  const frag = [];
  let total = 0;

  const catOrder = ['equity','index','etf','crypto','commodity','forex'];
  for (const cat of catOrder) {
    if (currentCat !== 'all' && currentCat !== cat) continue;
    const tickers = (universeData[cat] || []).filter(t => {
      if (!search) return true;
      return t.ticker.includes(search) || (t.name || '').toUpperCase().includes(search);
    });
    if (!tickers.length) continue;

    if (currentCat === 'all') {
      frag.push(\`<div class="t-sep">\${CAT_ICONS[cat] || ''} \${CAT_LABELS[cat] || cat}</div>\`);
    }

    for (const t of tickers) {
      const d = marketData[t.ticker];
      const c = d ? fmtChg(d.change_pct) : {text:'—',cls:'neutral'};
      const isActive = t.ticker === currentTicker ? ' active' : '';
      frag.push(\`<div class="t-item\${isActive}" onclick="selectTicker('\${t.ticker}')">
        <div>
          <div class="t-sym">\${t.ticker}</div>
          <div class="t-name">\${t.name || ''}</div>
        </div>
        <div class="t-chg \${c.cls}">\${c.text}</div>
      </div>\`);
      total++;
    }
  }

  el.innerHTML = frag.join('');
  document.getElementById('sidebar-footer').textContent = total + ' instruments';
}

function setCat(cat) {
  currentCat = cat;
  document.querySelectorAll('.cat-tab').forEach(b => b.classList.toggle('active', b.textContent === {
    all:'All',equity:'SP500',index:'Index',etf:'ETF',crypto:'Crypto',commodity:'Cmdty',forex:'FX'
  }[cat]));
  buildSidebar();
}

function filterTickers() { buildSidebar(); }

// ── Market Overview (default view) ────────────────────────────────────────────
function showOverview() {
  currentTicker = null;
  document.getElementById('detail').style.display = 'none';
  const ov = document.getElementById('overview');
  ov.style.display = 'block';
  document.querySelectorAll('.t-item').forEach(el => el.classList.remove('active'));

  const groups = [
    { label:'US Indices',   tickers:['^GSPC','^DJI','^IXIC','^RUT','^VIX'] },
    { label:'Bond Yields',  tickers:['^TNX','^TYX','^FVX'] },
    { label:'Global Indices',tickers:['^STOXX50E','^N225','^FTSE','^GDAXI','^HSI'] },
    { label:'Major ETFs',   tickers:['SPY','QQQ','IWM','DIA','TLT','GLD'] },
    { label:'Sectors',      tickers:['XLF','XLK','XLE','XLV','XLI','XLY','XLP','XLU'] },
    { label:'Crypto',       tickers:['BTC-USD','ETH-USD','SOL-USD','BNB-USD','XRP-USD','AVAX-USD'] },
    { label:'Commodities',  tickers:['GC=F','SI=F','CL=F','NG=F','HG=F'] },
    { label:'Forex',        tickers:['EURUSD=X','GBPUSD=X','USDJPY=X','AUDUSD=X','DX-Y.NYB'] },
  ];

  const html = groups.map(g => {
    const cards = g.tickers.filter(t => marketData[t]).map(t => {
      const d = marketData[t];
      const c = fmtChg(d.change_pct);
      const name = (d.name || t).replace(' ETF','').replace(' Select Sector','').replace(' Futures','');
      return \`<div class="ov-card" onclick="selectTicker('\${t}')">
        <div class="ov-sym">\${t}</div>
        <div class="ov-name">\${name}</div>
        <div class="ov-price \${c.cls}">\${fmtPrice(t, d.close)}</div>
        <div class="ov-chg \${c.cls}">\${c.text}</div>
        <div class="ov-date">\${d.date ? String(d.date).slice(0,10) : ''}</div>
      </div>\`;
    }).join('');
    return cards ? \`<div class="ov-group">
      <div class="ov-label">\${g.label}</div>
      <div class="ov-cards">\${cards}</div>
    </div>\` : '';
  }).join('');

  const newsHtml = \`<div class="ov-group">
    <div class="ov-label">Market News</div>
    <div id="overview-news" style="padding:4px 0"><div class="empty">Loading news...</div></div>
  </div>\`;
  const regimePlaceholder = \`<div id="regime-panel" class="regime-panel">
    <div class="regime-panel-header">Volatility Regime</div>
    <div class="empty" style="padding:10px 0;font-size:11px">Loading regime...</div>
  </div>\`;
  ov.innerHTML = regimePlaceholder + (html || '<div class="empty">No market data yet — pipeline runs at 9:00 AM ET</div>') + newsHtml;
  loadNewsSection('overview-news', null);
  loadRegime();
}

// ── Regime Panel ─────────────────────────────────────────────────────────────
async function loadRegime() {
  const el = document.getElementById('regime-panel');
  if (!el) return;

  const d = await fetch('/api/regime').then(r => r.json())
    .catch(() => ({ available: false, state: 'NO_DATA' }));

  if (!d.available) {
    el.innerHTML = \`<div class="regime-panel-header">Volatility Regime</div>
      <div class="empty" style="padding:10px 0;font-size:11px">No regime data — runs at 9:00 AM and 4:20 PM ET on market days</div>\`;
    return;
  }

  const stateClass = {LOW_VOL:'regime-state-LOW_VOL',TRANSITIONING:'regime-state-TRANSITIONING',
    HIGH_VOL:'regime-state-HIGH_VOL',CRISIS:'regime-state-CRISIS'}[d.state]||'regime-state-NO_DATA';
  const posScale   = d.position_scale != null ? Math.round(d.position_scale*100)+'%' : '—';
  const conf       = d.confidence     != null ? Math.round(d.confidence*100)+'%'     : '—';
  const days       = d.days_in_current_state != null ? d.days_in_current_state+'d in state' : '';
  const alertBadge = d.regime_change_alert
    ? \`<span class="regime-alert-badge">⚠ REGIME CHANGE ALERT</span>\` : '';

  const stress    = Math.min(100, Math.max(0, d.stress_score ?? 0));
  const stressClr = stress >= 70 ? 'var(--red)' : stress >= 40 ? 'var(--orange)' : 'var(--green)';

  const roro     = Math.min(50, Math.max(-50, d.roro_score ?? 0));
  const roroPct  = (Math.abs(roro)/50*50).toFixed(1);
  const roroLeft = roro < 0 ? \`left:\${50-roroPct}%;width:\${roroPct}%\` : \`left:50%;width:\${roroPct}%\`;
  const roroClr  = roro >= 0 ? 'var(--green)' : 'var(--red)';
  const roroLbl  = roro >= 0 ? \`+\${d.roro_score.toFixed(1)} risk-on\` : \`\${d.roro_score.toFixed(1)} risk-off\`;

  const f = d.features || {};
  const featHtml = [
    ['VIX',          f.vix           != null ? f.vix.toFixed(2)                                              : '—'],
    ['VIX 5d Δ',     f.vix_5d_chg    != null ? (f.vix_5d_chg>=0?'+':'')+f.vix_5d_chg.toFixed(2)            : '—'],
    ['SPX 5d Ret',   f.spx_5d_return != null ? (f.spx_5d_return>=0?'+':'')+(f.spx_5d_return*100).toFixed(2)+'%' : '—'],
    ['SPX 20d RV',   f.spx_rv_20d    != null ? f.spx_rv_20d.toFixed(2)+'%'                                  : '—'],
    ['HY/IG Spread', f.hy_ig_spread  != null ? f.hy_ig_spread.toFixed(4)                                    : '—'],
    ['VIX Term',     f.vix_term_slope!= null ? f.vix_term_slope.toFixed(4)                                  : '—'],
  ].map(([lbl,val])=>\`<div class="regime-feat"><div class="regime-feat-label">\${lbl}</div><div class="regime-feat-val">\${val}</div></div>\`).join('');

  const tp = d.transition_probs_tomorrow || {};
  const SC = {LOW_VOL:'var(--green)',TRANSITIONING:'var(--yellow)',HIGH_VOL:'var(--orange)',CRISIS:'var(--red)'};
  const SN = {LOW_VOL:'Low Vol',TRANSITIONING:'Transit.',HIGH_VOL:'High Vol',CRISIS:'Crisis'};
  const probHtml = ['LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'].map(s => {
    const pct = tp[s]!=null ? Math.round(tp[s]*100) : 0;
    return \`<div class="regime-prob-row">
      <div class="regime-prob-name">\${SN[s]}</div>
      <div class="regime-prob-track"><div class="regime-prob-bar" style="width:\${pct}%;background:\${SC[s]}"></div></div>
      <div class="regime-prob-pct">\${pct}%</div></div>\`;
  }).join('');

  el.innerHTML = \`
    <div class="regime-panel-header">Volatility Regime <span style="font-size:9px;color:var(--dim);text-transform:none;letter-spacing:0">as of \${d.date||'—'}</span></div>
    <div class="regime-top-row">
      <span class="regime-state-badge \${stateClass}">\${d.state}</span>
      <div class="regime-meta-item"><div class="regime-meta-label">Position Scale</div><div class="regime-meta-val">\${posScale}</div></div>
      <div class="regime-meta-item"><div class="regime-meta-label">Confidence</div><div class="regime-meta-val">\${conf}</div></div>
      <div class="regime-meta-item"><div class="regime-meta-label">Duration</div><div class="regime-meta-val">\${days||'—'}</div></div>
      <div class="regime-bar-group">
        <div class="regime-bar-label"><span>Stress</span><span>\${stress}/100</span></div>
        <div class="regime-bar-track"><div class="regime-bar-fill" style="width:\${stress}%;background:\${stressClr}"></div></div>
      </div>
      <div class="regime-bar-group">
        <div class="regime-bar-label"><span>RORO</span><span>\${roroLbl}</span></div>
        <div class="regime-roro-track">
          <div class="regime-roro-center"></div>
          <div class="regime-roro-fill" style="\${roroLeft};background:\${roroClr}"></div>
        </div>
      </div>
      \${alertBadge}
    </div>
    <div class="regime-bottom-row">
      <div class="regime-feat-grid">\${featHtml}</div>
      <div class="regime-prob-section">
        <div class="regime-prob-label">Tomorrow's Probabilities</div>\${probHtml}
      </div>
    </div>\`;
}

// ── Ticker Detail ─────────────────────────────────────────────────────────────
async function selectTicker(ticker) {
  currentTicker = ticker;
  buildSidebar();
  document.getElementById('overview').style.display = 'none';
  const det = document.getElementById('detail');
  det.style.display = 'flex';
  det.innerHTML = '<div class="empty">Loading \${ticker}...</div>';
  await loadDetail(ticker);
}

async function loadDetail(ticker) {
  const det = document.getElementById('detail');
  const d   = marketData[ticker] || {};
  const c   = fmtChg(d.change_pct);

  det.innerHTML = \`
    <div id="detail-header">
      <div style="display:flex;flex-direction:column;gap:4px">
        <button onclick="showOverview()" style="background:none;border:none;color:var(--muted);font-size:11px;cursor:pointer;text-align:left;padding:0;font-family:inherit;display:flex;align-items:center;gap:4px;width:fit-content" onmouseover="this.style.color='var(--blue)'" onmouseout="this.style.color='var(--muted)'">← Market Overview</button>
        <div id="detail-sym">\${ticker}</div>
        <div id="detail-name">\${d.name || ''}</div>
      </div>
      <div style="margin-left:auto;text-align:right">
        <div id="detail-price" class="\${c.cls}">\${fmtPrice(ticker, d.close)}</div>
        <div id="detail-chg" class="\${c.cls}">\${c.text}</div>
        <div id="detail-date">\${d.date ? String(d.date).slice(0,10) : ''}</div>
      </div>
    </div>
    <div id="chart-controls">
      \${['5D','1M','3M','6M','1Y','5Y','10Y'].map(r =>
        \`<button class="range-btn\${r==='1Y'?' active':''}" onclick="setRange('\${r}',this)">\${r}</button>\`
      ).join('')}
    </div>
    <div id="chart-wrap"><canvas id="priceChart"></canvas></div>
    <div id="data-tabs">
      <div class="data-tab active" onclick="showTab('overview',this)">Overview</div>
      <div class="data-tab" onclick="showTab('options',this)">Options</div>
      <div class="data-tab" onclick="showTab('fundamentals',this)">Fundamentals</div>
      <div class="data-tab" onclick="showTab('cycles',this)">Cycles</div>
      <div class="data-tab" onclick="showTab('news',this)">News</div>
    </div>
    <div id="tab-content"></div>
  \`;

  await Promise.all([
    loadChart(ticker, currentRangeLabel),
    showTab('overview', null),
  ]);
}

// Maps range label → query string for /api/db/prices/:ticker
const RANGE_PARAMS = {
  '5D':  'limit=5',     // last 5 trading day rows (calendar days miss weekends)
  '1M':  'days=30',
  '3M':  'days=90',
  '6M':  'days=180',
  '1Y':  'days=365',
  '5Y':  'days=1825',
  '10Y': 'days=3650',
};
let currentRangeLabel = '1Y';

async function setRange(label, btn) {
  currentRangeLabel = label;
  document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  if (currentTicker) await loadChart(currentTicker, label);
}

async function loadChart(ticker, rangeLabel) {
  const qs = RANGE_PARAMS[rangeLabel] || 'days=365';
  const prices = await fetch(\`/api/db/prices/\${ticker}?\${qs}\`).then(r=>r.json()).catch(()=>[]);
  if (!prices || !prices.length) {
    document.getElementById('chart-wrap').innerHTML = '<div class="empty">No price data in DB yet for \${ticker}</div>';
    return;
  }

  const labels = prices.map(p => String(p.date).slice(0,10));
  const closes = prices.map(p => parseFloat(p.close));
  const isIndex = ticker.startsWith('^');

  if (priceChart) { priceChart.destroy(); priceChart = null; }
  const wrap = document.getElementById('chart-wrap');
  wrap.innerHTML = '<canvas id="priceChart"></canvas>';
  const ctx = document.getElementById('priceChart').getContext('2d');

  const startPrice = closes[0];
  const color = closes[closes.length-1] >= startPrice ? '#3fb950' : '#f85149';
  const colorAlpha = closes[closes.length-1] >= startPrice ? 'rgba(63,185,80,0.08)' : 'rgba(248,81,73,0.08)';

  priceChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: ticker,
        data: closes,
        borderColor: color,
        backgroundColor: colorAlpha,
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        tension: 0.1
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#161b22',
          borderColor: '#30363d',
          borderWidth: 1,
          titleColor: '#8b949e',
          bodyColor: '#e6edf3',
          callbacks: {
            label: ctx => (isIndex ? '' : '$') + parseFloat(ctx.parsed.y).toLocaleString('en-US',{maximumFractionDigits:isIndex?2:2})
          }
        }
      },
      scales: {
        x: {
          ticks: { color: '#484f58', maxTicksLimit: 8, font:{size:10} },
          grid: { color: '#21262d' }
        },
        y: {
          position: 'right',
          ticks: {
            color: '#484f58',
            font: { size:10 },
            callback: v => isIndex ? v.toLocaleString('en-US',{maximumFractionDigits:0}) : '$'+parseFloat(v).toLocaleString('en-US',{maximumFractionDigits:ticker.includes('=X')?4:0})
          },
          grid: { color: '#21262d' }
        }
      }
    }
  });
}

// ── Data Tabs ─────────────────────────────────────────────────────────────────
async function showTab(name, btn) {
  if (btn) {
    document.querySelectorAll('.data-tab').forEach(t => t.classList.remove('active'));
    btn.classList.add('active');
  }
  const el = document.getElementById('tab-content');
  if (!el) return;

  if (name === 'overview') {
    const d = marketData[currentTicker] || {};
    el.innerHTML = \`<table class="db-table">
      <tr><th>Field</th><th>Value</th></tr>
      <tr><td>Open</td><td class="num">\${fmtPrice(currentTicker, d.open)}</td></tr>
      <tr><td>High</td><td class="num">\${fmtPrice(currentTicker, d.high)}</td></tr>
      <tr><td>Low</td><td class="num">\${fmtPrice(currentTicker, d.low)}</td></tr>
      <tr><td>Close</td><td class="num">\${fmtPrice(currentTicker, d.close)}</td></tr>
      <tr><td>Prev Close</td><td class="num">\${fmtPrice(currentTicker, d.prev_close)}</td></tr>
      <tr><td>Volume</td><td class="num">\${fmtVol(d.volume)}</td></tr>
      <tr><td>Category</td><td>\${d.category || '—'}</td></tr>
      <tr><td>As of</td><td>\${d.date ? String(d.date).slice(0,10) : '—'}</td></tr>
    </table>\`;

  } else if (name === 'options') {
    const opts = await fetch(\`/api/db/options/\${currentTicker}?limit=40\`).then(r=>r.json()).catch(()=>[]);
    if (!opts.length) { el.innerHTML = '<div class="empty">No options data in DB for this instrument</div>'; return; }

    // Split calls/puts
    const calls = opts.filter(o=>o.contract_type==='call').slice(0,15);
    const puts  = opts.filter(o=>o.contract_type==='put').slice(0,15);

    const rowHtml = (o,type) => \`<tr>
      <td class="\${type}">\${String(o.expiry).slice(0,10)}</td>
      <td class="num">\${o.strike ? '$'+parseFloat(o.strike).toFixed(0) : '—'}</td>
      <td class="num">\${o.delta ? parseFloat(o.delta).toFixed(3) : '—'}</td>
      <td class="num">\${o.iv ? (parseFloat(o.iv)*100).toFixed(1)+'%' : '—'}</td>
      <td class="num">\${o.open_interest ? parseInt(o.open_interest).toLocaleString() : '—'}</td>
      <td class="num">\${o.last_price ? '$'+parseFloat(o.last_price).toFixed(2) : '—'}</td>
    </tr>\`;

    el.innerHTML = \`<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <div style="font-size:10px;color:var(--blue);margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">Calls</div>
        <table class="db-table"><tr><th>Expiry</th><th>Strike</th><th>Δ</th><th>IV</th><th>OI</th><th>Last</th></tr>
        \${calls.map(o=>rowHtml(o,'call')).join('')}</table>
      </div>
      <div>
        <div style="font-size:10px;color:#f0883e;margin-bottom:6px;text-transform:uppercase;letter-spacing:.06em">Puts</div>
        <table class="db-table"><tr><th>Expiry</th><th>Strike</th><th>Δ</th><th>IV</th><th>OI</th><th>Last</th></tr>
        \${puts.map(o=>rowHtml(o,'put')).join('')}</table>
      </div>
    </div>\`;

  } else if (name === 'fundamentals') {
    const fund = await fetch(\`/api/db/fundamentals/\${currentTicker}\`).then(r=>r.json()).catch(()=>[]);
    if (!fund.length) { el.innerHTML = '<div class="empty">No fundamentals in DB for this instrument</div>'; return; }
    const pct = v => v != null ? (parseFloat(v)*100).toFixed(1)+'%' : '—';
    el.innerHTML = \`<table class="db-table">
      <tr><th>Period</th>\${fund.map(f=>\`<th class="num">\${String(f.period_end||f.period).slice(0,7)}</th>\`).join('')}</tr>
      <tr><td>Revenue</td>\${fund.map(f=>\`<td class="num">\${fmtNum(f.revenue)}</td>\`).join('')}</tr>
      <tr><td>Gross Profit</td>\${fund.map(f=>\`<td class="num">\${fmtNum(f.gross_profit)}</td>\`).join('')}</tr>
      <tr><td>EBITDA</td>\${fund.map(f=>\`<td class="num">\${fmtNum(f.ebitda)}</td>\`).join('')}</tr>
      <tr><td>Net Income</td>\${fund.map(f=>\`<td class="num">\${fmtNum(f.net_income)}</td>\`).join('')}</tr>
      <tr><td>EPS</td>\${fund.map(f=>\`<td class="num">\${f.eps ? '$'+parseFloat(f.eps).toFixed(2) : '—'}</td>\`).join('')}</tr>
      <tr><td>Gross Margin</td>\${fund.map(f=>\`<td class="num">\${pct(f.gross_margin)}</td>\`).join('')}</tr>
      <tr><td>Net Margin</td>\${fund.map(f=>\`<td class="num">\${pct(f.net_margin)}</td>\`).join('')}</tr>
      <tr><td>Rev Growth YoY</td>\${fund.map(f=>\`<td class="num \${f.revenue_growth_yoy>0?'positive':'negative'}">\${pct(f.revenue_growth_yoy)}</td>\`).join('')}</tr>
    </table>\`;

  } else if (name === 'cycles') {
    const cycles = await fetch('/api/db/cycles?n=10').then(r=>r.json()).catch(()=>[]);
    if (!cycles.length) { el.innerHTML = '<div class="empty">No collection cycles recorded yet</div>'; return; }
    const dur = ms => !ms ? '—' : ms < 60000 ? Math.round(ms/1000)+'s' : Math.round(ms/60000)+'m '+Math.round((ms%60000)/1000)+'s';
    el.innerHTML = \`<table class="db-table">
      <tr><th>#</th><th>Started (ET)</th><th>Dur</th><th>Prices</th><th>Options</th><th>Fund</th><th>API P/F/Y</th><th>Errors</th><th>Status</th></tr>
      \${cycles.map(c => \`<tr class="cycle-row">
        <td>\${c.id}</td>
        <td>\${new Date(c.started_at).toLocaleString('en-US',{timeZone:'America/New_York',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false})}</td>
        <td>\${dur(c.duration_ms)}</td>
        <td class="num">\${parseInt(c.price_rows||0).toLocaleString()}</td>
        <td class="num">\${parseInt(c.options_contracts||0).toLocaleString()}</td>
        <td class="num">\${c.fundamental_records||0}</td>
        <td class="num">\${c.polygon_calls||0}/\${c.fmp_calls||0}/\${c.yfinance_calls||0}</td>
        <td class="num \${(c.errors||0)>0?'negative':''}">\${c.errors||0}</td>
        <td class="status-\${(c.status||'').replace('-','')}">\${c.status}</td>
      </tr>\`).join('')}
    </table>\`;

  } else if (name === 'news') {
    el.innerHTML = '<div class="empty">Loading news...</div>';
    await loadNewsSection('tab-content', currentTicker);
  }
}

// ── News rendering ────────────────────────────────────────────────────────────
function timeAgo(dateStr) {
  if (!dateStr) return '';
  const diff = Date.now() - new Date(dateStr).getTime();
  const m = Math.floor(diff / 60000);
  if (m < 1)  return 'just now';
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  const d = Math.floor(h / 24);
  return d + 'd ago';
}

function renderNewsCards(articles, maxRelated = 3) {
  if (!articles.length) return '<div class="empty">No news articles in DB yet — pipeline collects at 6 AM ET</div>';
  return '<div class="news-feed">' + articles.map((a, idx) => {
    const related = (a.related_tickers || []).slice(0, maxRelated)
      .map(t => \`<span class="news-ticker-badge" onclick="selectTicker('\${t}')">\${t}</span>\`).join('');
    const url = a.url || '';
    const readMore = url
      ? \`<a class="news-read-more" href="\${url}" target="_blank" rel="noopener">Read full article →</a>\`
      : '';
    const extras = a.related_articles || [];
    const extraHtml = extras.length ? \`
      <div class="news-extras" id="extras-\${idx}" style="display:none;margin-top:8px;padding-top:8px;border-top:1px solid var(--border2)">
        \${extras.map(e => {
          const eu = e.url || '';
          return \`<div style="margin-bottom:7px">
            <div style="font-size:10px;color:var(--muted);margin-bottom:2px">\${e.publisher || ''} · \${timeAgo(e.published_at)}</div>
            \${eu
              ? \`<a href="\${eu}" target="_blank" rel="noopener" style="font-size:11px;color:var(--text);text-decoration:none;line-height:1.4;display:block">\${e.title}</a>\`
              : \`<div style="font-size:11px;color:var(--text);line-height:1.4">\${e.title}</div>\`
            }
            \${e.summary ? \`<div style="font-size:10px;color:var(--dim);margin-top:2px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden">\${e.summary}</div>\` : ''}
          </div>\`;
        }).join('')}
      </div>
      <span class="news-read-more" style="cursor:pointer" onclick="(function(el,btn){el.style.display=el.style.display==='none'?'block':'none';btn.textContent=el.style.display==='none'?'\${extras.length} more articles ▾':'\${extras.length} more articles ▴'})(document.getElementById('extras-\${idx}'),this)">\${extras.length} more articles ▾</span>\`
    : '';
    return \`<div class="news-card">
      <div class="news-meta">
        <span class="news-source">\${a.publisher || 'News'}</span>
        <span class="news-time">\${timeAgo(a.published_at)}</span>
        \${a.primary_ticker ? \`<span class="news-ticker-badge" onclick="selectTicker('\${a.primary_ticker}')">\${a.primary_ticker}</span>\` : ''}
        \${related}
      </div>
      \${url
        ? \`<a class="news-title" href="\${url}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;display:block">\${a.title}</a>\`
        : \`<div class="news-title">\${a.title}</div>\`
      }
      \${a.summary ? \`<div class="news-summary">\${a.summary}</div>\` : ''}
      \${readMore}
      \${extraHtml}
    </div>\`;
  }).join('') + '</div>';
}

async function loadNewsSection(containerId, ticker) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = '<div class="empty">Loading news...</div>';
  const qs = ticker ? \`?ticker=\${ticker}&limit=25\` : '?limit=30';
  const articles = await fetch(\`/api/db/news\${qs}\`).then(r=>r.json()).catch(()=>[]);
  el.innerHTML = renderNewsCards(articles);
}

// ── Portfolio ─────────────────────────────────────────────────────────────────
let pnlChart     = null;
let pnlMode      = 'pnl';   // 'pnl' | 'value'
let pnlCurveData = null;    // cached {rows, base_value}
let valueCurveData = null;  // cached from /api/portfolio/value-curve

function _setNavActive(which) {
  for (const k of ['market','portfolio','strategies']) {
    const el = document.getElementById('nav-'+k);
    if (!el) continue;
    if (k === which) el.classList.add('active'); else el.classList.remove('active');
  }
}
function _hideAllPages() {
  document.getElementById('body').style.display = 'none';
  document.getElementById('portfolio-page').style.display = 'none';
  const st = document.getElementById('strategies-page');
  if (st) st.style.display = 'none';
}

function showMarket() {
  _hideAllPages();
  document.getElementById('body').style.display = 'flex';
  _setNavActive('market');
  showOverview();
}

async function showPortfolio() {
  _hideAllPages();
  document.getElementById('portfolio-page').style.display = 'block';
  _setNavActive('portfolio');
  await loadPortfolio();
}

async function showStrategies() {
  _hideAllPages();
  document.getElementById('strategies-page').style.display = 'block';
  _setNavActive('strategies');
  await loadStrategies();
}

async function loadPortfolio() {
  const [summary, positions, history, curve, account, valCurve] = await Promise.all([
    fetch('/api/portfolio/summary').then(r=>r.json()).catch(()=>({})),
    fetch('/api/portfolio/positions').then(r=>r.json()).catch(()=>[]),
    fetch('/api/portfolio/history').then(r=>r.json()).catch(()=>[]),
    fetch('/api/portfolio/pnl-curve?days=90').then(r=>r.json()).catch(()=>[]),
    fetch('/api/portfolio/account').then(r=>r.json()).catch(()=>({})),
    fetch('/api/portfolio/value-curve?period=1M').then(r=>r.json()).catch(()=>({})),
  ]);
  renderAccountRow(account);
  renderPortfolioSummary(summary);
  renderPositions(positions);
  renderHistory(history);
  pnlCurveData   = curve;
  valueCurveData = valCurve;
  renderChartForMode();
}

function setPnlMode(mode) {
  pnlMode = mode;
  document.getElementById('btn-pnl-mode').classList.toggle('active', mode === 'pnl');
  document.getElementById('btn-value-mode').classList.toggle('active', mode === 'value');
  renderChartForMode();
}

function renderChartForMode() {
  if (pnlMode === 'value') {
    const rows = valueCurveData?.rows || [];
    const title = 'Portfolio Value — 1 Month';
    document.getElementById('pf-chart-title').textContent = title;
    renderValueChart(rows);
  } else {
    document.getElementById('pf-chart-title').textContent = 'Portfolio P&L Curve (90d)';
    renderPnlChart(pnlCurveData || []);
  }
}

function renderAccountRow(a) {
  const fmt = (v) => v != null ? '$' + parseFloat(v).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '—';
  const fmtPct = (v) => v != null ? (v >= 0 ? '+' : '') + parseFloat(v).toFixed(2) + '%' : '';

  const equity   = a.equity;
  const cash     = a.cash;
  const dayPnl   = a.day_pnl;
  const dayPct   = a.day_pnl_pct;
  const invested = a.long_market_value;

  const equityEl = document.getElementById('pf-equity');
  equityEl.textContent = fmt(equity);
  equityEl.className   = 'pf-stat-value';
  document.getElementById('pf-equity-sub').textContent = a.last_equity ? 'prev close ' + fmt(a.last_equity) : '';

  document.getElementById('pf-cash').textContent     = fmt(cash);
  document.getElementById('pf-cash-sub').textContent = a.buying_power ? 'buying power ' + fmt(a.buying_power) : '';

  const dayEl  = document.getElementById('pf-daypnl');
  const daySub = document.getElementById('pf-daypnl-sub');
  dayEl.textContent  = fmt(dayPnl);
  dayEl.className    = 'pf-stat-value ' + pnlCls(dayPnl);
  daySub.textContent = fmtPct(dayPct);

  document.getElementById('pf-invested').textContent     = fmt(invested);
  document.getElementById('pf-invested-sub').textContent = a.short_market_value
    ? 'short ' + fmt(a.short_market_value) : '';
}

function renderPortfolioSummary(s) {
  const pnl = s.avg_realized != null ? parseFloat(s.avg_realized) * 100 : null;
  const wr  = s.win_rate != null ? s.win_rate + '%' : '—';
  document.getElementById('pf-open').textContent    = s.open_count ?? '—';
  document.getElementById('pf-closed').textContent  = s.closed_count ?? '—';
  document.getElementById('pf-winrate').textContent = wr;
  document.getElementById('pf-winrate-sub').textContent = s.closed_count
    ? s.win_rate + '% of ' + s.closed_count + ' trades' : 'No closed trades';
  if (pnl != null) {
    const el = document.getElementById('pf-avgpnl');
    el.textContent = (pnl > 0 ? '+' : '') + pnl.toFixed(2) + '%';
    el.className = 'pf-stat-value ' + pnlCls(pnl, 'positive', 'negative', 'neutral');
    const best = s.best_trade != null ? parseFloat(s.best_trade) * 100 : null;
    const worst = s.worst_trade != null ? parseFloat(s.worst_trade) * 100 : null;
    document.getElementById('pf-pnl-sub').textContent =
      'Best: ' + (best != null ? (best > 0 ? '+' : '') + best.toFixed(2) + '%' : '—') +
      '  Worst: ' + (worst != null ? worst.toFixed(2) + '%' : '—');
  } else {
    document.getElementById('pf-avgpnl').textContent = '—';
    document.getElementById('pf-pnl-sub').textContent = 'No closed trades';
  }
}

function renderPositions(rows) {
  const el = document.getElementById('pf-positions');
  document.getElementById('pf-pos-count').textContent = rows.length ? rows.length + ' open' : '';
  if (!rows.length) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  el.innerHTML = \`<table class="db-table" style="min-width:700px">
    <tr><th>Strategy</th><th>Ticker</th><th>Dir</th><th>Entry</th><th>Current</th><th class="num">P&amp;L %</th><th class="num">Size %</th><th class="num">Days</th><th>Stop</th><th>Status</th></tr>
    \${rows.map(r => {
      const pnl = r.unrealized_pnl_pct != null ? parseFloat(r.unrealized_pnl_pct) * 100 : null;
      const pnlTxt = pnl != null ? (pnl > 0 ? '+' : '') + pnl.toFixed(2) + '%' : '—';
      const pnlClsName = pnlCls(pnl);
      const dir = (r.direction || '').toUpperCase();
      return \`<tr>
        <td>\${r.strategy_id || '—'}</td>
        <td style="font-weight:600;cursor:pointer;color:var(--blue)" onclick="showMarket();selectTicker('\${r.ticker}')">\${r.ticker}</td>
        <td class="\${dir === 'LONG' ? 'dir-long' : 'dir-short'}">\${dir}</td>
        <td class="num">\${r.entry_price != null ? '$' + parseFloat(r.entry_price).toFixed(2) : '—'}</td>
        <td class="num">\${r.current_price != null ? '$' + parseFloat(r.current_price).toFixed(2) : '—'}</td>
        <td class="num \${pnlClsName}">\${pnlTxt}</td>
        <td class="num">\${r.position_size_pct != null ? (parseFloat(r.position_size_pct) * 100).toFixed(1) + '%' : '—'}</td>
        <td class="num">\${r.days_held ?? '—'}</td>
        <td class="num">\${r.stop_loss != null ? '$' + parseFloat(r.stop_loss).toFixed(2) : '—'}</td>
        <td>\${r.status || '—'}</td>
      </tr>\`;
    }).join('')}
  </table>\`;
}

function renderHistory(rows) {
  const el = document.getElementById('pf-history');
  document.getElementById('pf-hist-count').textContent = rows.length ? rows.length + ' trades' : '';
  if (!rows.length) { el.innerHTML = '<div class="empty">No closed trades yet</div>'; return; }
  el.innerHTML = \`<table class="db-table" style="min-width:680px">
    <tr><th>Strategy</th><th>Ticker</th><th>Dir</th><th>Entry</th><th>Close</th><th class="num">P&amp;L %</th><th class="num">Days</th><th>Reason</th><th>Closed</th></tr>
    \${rows.map(r => {
      const pnl = r.realized_pnl_pct != null ? parseFloat(r.realized_pnl_pct) * 100 : null;
      const pnlTxt = pnl != null ? (pnl > 0 ? '+' : '') + pnl.toFixed(2) + '%' : '—';
      const pnlClsName = pnlCls(pnl);
      const dir = (r.direction || '').toUpperCase();
      const closedAt = r.closed_at ? new Date(r.closed_at).toLocaleDateString('en-US',{month:'numeric',day:'numeric',year:'2-digit'}) : '—';
      return \`<tr>
        <td>\${r.strategy_id || '—'}</td>
        <td style="font-weight:600;cursor:pointer;color:var(--blue)" onclick="showMarket();selectTicker('\${r.ticker}')">\${r.ticker}</td>
        <td class="\${dir === 'LONG' ? 'dir-long' : 'dir-short'}">\${dir}</td>
        <td class="num">\${r.entry_price != null ? '$' + parseFloat(r.entry_price).toFixed(2) : '—'}</td>
        <td class="num">\${r.closed_price != null ? '$' + parseFloat(r.closed_price).toFixed(2) : '—'}</td>
        <td class="num \${pnlClsName}">\${pnlTxt}</td>
        <td class="num">\${r.days_held ?? '—'}</td>
        <td style="color:var(--muted)">\${r.close_reason || '—'}</td>
        <td style="color:var(--dim)">\${closedAt}</td>
      </tr>\`;
    }).join('')}
  </table>\`;
}

// ── Strategies page ─────────────────────────────────────────────────────────
let strategiesData  = [];
let strategyState   = 'all';
let strategyRegime  = 'all';
let strategySortKey = 'avg_realized_pct';
let strategySortDir = 'desc';

async function loadStrategies() {
  try {
    const rows = await fetch('/api/strategies').then(r => r.json()).catch(() => []);
    strategiesData = Array.isArray(rows) ? rows : [];
  } catch {
    strategiesData = [];
  }
  renderStrategyTiles();
  renderStrategyTable();
}

function renderStrategyTiles() {
  const rows = strategiesData;
  const total = rows.length;
  const open  = rows.reduce((s, r) => s + (r.open_count || 0), 0);
  const closed = rows.reduce((s, r) => s + (r.closed_count || 0), 0);
  const totalWins = rows.reduce((s, r) => s + (r.wins || 0), 0);
  const totalClosed = rows.reduce((s, r) => s + ((r.wins || 0) + (r.losses || 0)), 0);
  const stackWR = totalClosed > 0 ? Math.round(totalWins / totalClosed * 100) : null;
  document.getElementById('st-total').textContent   = total;
  document.getElementById('st-open').textContent    = open;
  document.getElementById('st-closed').textContent  = closed;
  document.getElementById('st-winrate').textContent = stackWR != null ? stackWR + '%' : '—';
}

function setStateFilter(s) {
  strategyState = s;
  for (const el of document.querySelectorAll('.st-pill[data-state]')) {
    el.classList.toggle('active', el.dataset.state === s);
  }
  renderStrategyTable();
}
function setRegimeFilter(r) {
  strategyRegime = r;
  for (const el of document.querySelectorAll('.st-pill[data-regime]')) {
    el.classList.toggle('active', el.dataset.regime === r);
  }
  renderStrategyTable();
}
function setStrategySort(key) {
  if (strategySortKey === key) {
    strategySortDir = strategySortDir === 'desc' ? 'asc' : 'desc';
  } else {
    strategySortKey = key;
    strategySortDir = 'desc';
  }
  renderStrategyTable();
}

// Display: we paper-trade the fund as a whole, so every strategy in manifest
// state "live" or "paper" is operationally running. Collapse both to "active"
// for the user-facing badge and filter.
function _displayState(s) {
  return (s === 'live' || s === 'paper') ? 'active' : (s || 'orphan');
}

function renderStrategyTable() {
  let rows = strategiesData.slice();
  if (strategyState !== 'all')   rows = rows.filter(r => _displayState(r.state) === strategyState);
  if (strategyRegime !== 'all')  rows = rows.filter(r => r.dominant_regime === strategyRegime);

  const dir = strategySortDir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const av = a[strategySortKey], bv = b[strategySortKey];
    const an = av == null, bn = bv == null;
    if (an && bn) return 0;
    if (an) return 1;
    if (bn) return -1;
    if (typeof av === 'string') return av.localeCompare(bv) * dir;
    return (parseFloat(av) - parseFloat(bv)) * dir;
  });

  document.getElementById('st-shown-count').textContent =
    rows.length + ' of ' + strategiesData.length;

  const el = document.getElementById('st-table-wrap');
  if (!rows.length) { el.innerHTML = '<div class="empty">No strategies match filters</div>'; return; }

  const sortedCls = (key) => strategySortKey === key
    ? (strategySortDir === 'asc' ? 'st-sortable st-sorted-asc' : 'st-sortable st-sorted')
    : 'st-sortable';
  const fmtPct  = (v) => v == null ? '—' : (parseFloat(v)*100 > 0 ? '+' : '') + (parseFloat(v)*100).toFixed(2) + '%';
  const fmtRate = (v) => v == null ? '—' : Math.round(parseFloat(v)*100) + '%';
  const fmtDate = (v) => v ? new Date(v).toLocaleDateString('en-US',{month:'numeric',day:'numeric',year:'2-digit'}) : '—';

  el.innerHTML = \`<table class="db-table" style="min-width:1100px">
    <tr>
      <th class="\${sortedCls('strategy_id')}" onclick="setStrategySort('strategy_id')">Strategy</th>
      <th class="\${sortedCls('state')}" onclick="setStrategySort('state')">State</th>
      <th class="\${sortedCls('dominant_regime')}" onclick="setStrategySort('dominant_regime')">Regime</th>
      <th class="num \${sortedCls('open_count')}" onclick="setStrategySort('open_count')">Open</th>
      <th class="num \${sortedCls('closed_count')}" onclick="setStrategySort('closed_count')">Closed</th>
      <th class="num \${sortedCls('win_rate')}" onclick="setStrategySort('win_rate')">Win %</th>
      <th class="num \${sortedCls('avg_realized_pct')}" onclick="setStrategySort('avg_realized_pct')">Avg Return</th>
      <th class="num \${sortedCls('avg_unrealized_pct')}" onclick="setStrategySort('avg_unrealized_pct')">Unreal. Avg</th>
      <th class="num \${sortedCls('best_trade_pct')}" onclick="setStrategySort('best_trade_pct')">Best</th>
      <th class="num \${sortedCls('worst_trade_pct')}" onclick="setStrategySort('worst_trade_pct')">Worst</th>
      <th class="num \${sortedCls('avg_days_held')}" onclick="setStrategySort('avg_days_held')">Avg Days</th>
      <th class="\${sortedCls('last_signal_date')}" onclick="setStrategySort('last_signal_date')">Last Signal</th>
    </tr>
    \${rows.map(r => {
      const avgR     = r.avg_realized_pct   != null ? parseFloat(r.avg_realized_pct)*100   : null;
      const avgU     = r.avg_unrealized_pct != null ? parseFloat(r.avg_unrealized_pct)*100 : null;
      const best     = r.best_trade_pct     != null ? parseFloat(r.best_trade_pct)*100     : null;
      const worst    = r.worst_trade_pct    != null ? parseFloat(r.worst_trade_pct)*100    : null;
      const dispState = _displayState(r.state);
      const stateCls  = 'state-' + dispState;
      const stateLbl  = dispState === 'active' ? 'LIVE' : dispState.toUpperCase();
      const regimeLbl = r.dominant_regime || '—';
      const regimeCls = r.dominant_regime ? ('regime-state-' + r.dominant_regime) : '';
      const titleAttr = (r.description || '').replace(/"/g, '&quot;');
      return \`<tr>
        <td style="font-weight:600" title="\${titleAttr}">\${r.strategy_id}</td>
        <td><span class="\${stateCls}">\${stateLbl}</span></td>
        <td>\${regimeCls ? \`<span class="\${regimeCls}" style="padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.04em">\${regimeLbl}</span>\` : '<span style="color:var(--dim)">—</span>'}</td>
        <td class="num">\${r.open_count || 0}</td>
        <td class="num">\${r.closed_count || 0}</td>
        <td class="num">\${fmtRate(r.win_rate)}</td>
        <td class="num \${pnlCls(avgR)}">\${fmtPct(r.avg_realized_pct)}</td>
        <td class="num \${pnlCls(avgU)}">\${fmtPct(r.avg_unrealized_pct)}</td>
        <td class="num \${pnlCls(best)}">\${fmtPct(r.best_trade_pct)}</td>
        <td class="num \${pnlCls(worst)}">\${fmtPct(r.worst_trade_pct)}</td>
        <td class="num" style="color:var(--muted)">\${r.avg_days_held != null ? parseFloat(r.avg_days_held).toFixed(1) : '—'}</td>
        <td style="color:var(--dim)">\${fmtDate(r.last_signal_date)}</td>
      </tr>\`;
    }).join('')}
  </table>\`;
}

function _buildChart(labels, values, yFmt, tooltipFmt, color) {
  const wrap = document.getElementById('pnlChart');
  if (!wrap) return;
  if (pnlChart) { pnlChart.destroy(); pnlChart = null; }
  const fill = color === '#3fb950' ? 'rgba(63,185,80,0.08)' : color === '#f85149' ? 'rgba(248,81,73,0.08)' : 'rgba(88,166,255,0.08)';
  pnlChart = new Chart(wrap.getContext('2d'), {
    type: 'line',
    data: { labels, datasets: [{
      data: values, borderColor: color, backgroundColor: fill,
      borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.1,
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { display: false },
        tooltip: { backgroundColor:'#161b22', borderColor:'#30363d', borderWidth:1,
          titleColor:'#8b949e', bodyColor:'#e6edf3',
          callbacks: { label: ctx => tooltipFmt(ctx.parsed.y) }
        }
      },
      scales: {
        x: { ticks: { color:'#484f58', maxTicksLimit:6, font:{size:10} }, grid: { color:'#21262d' } },
        y: { position:'right', ticks: { color:'#484f58', font:{size:10}, callback: yFmt }, grid: { color:'#21262d' } }
      }
    }
  });
}

function renderPnlChart(rows) {
  if (!rows || !rows.length) {
    const wrap = document.getElementById('pnlChart');
    if (pnlChart) { pnlChart.destroy(); pnlChart = null; }
    if (wrap) wrap.getContext('2d').clearRect(0, 0, wrap.width, wrap.height);
    return;
  }
  const labels = rows.map(r => String(r.pnl_date).slice(0,10));
  const values = rows.map(r => parseFloat(r.avg_unrealized) || 0);
  const last   = values[values.length - 1];
  _buildChart(labels, values,
    v => (v >= 0 ? '+' : '') + v.toFixed(1) + '%',
    v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%',
    last >= 0 ? '#3fb950' : '#f85149');
}

function renderValueChart(rows) {
  if (!rows || !rows.length) {
    const wrap = document.getElementById('pnlChart');
    if (pnlChart) { pnlChart.destroy(); pnlChart = null; }
    if (wrap) wrap.getContext('2d').clearRect(0, 0, wrap.width, wrap.height);
    return;
  }
  const labels = rows.map(r => String(r.date).slice(0,10));
  const values = rows.map(r => parseFloat(r.equity) || 0);
  const fmt$   = v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0});
  _buildChart(labels, values, fmt$, fmt$, '#58a6ff');
}

// ── Pipeline badge ────────────────────────────────────────────────────────────
async function refreshPipeline() {
  const data = await fetch('/api/pipeline/status').then(r=>r.json()).catch(()=>null);
  if (!data?.coverage) return;
  const cov = data.coverage;
  const total = Object.values(universeData).flat().length || 456;
  document.getElementById('pipeline-badge').textContent =
    \`📡 prices:\${cov.price_coverage} options:\${cov.options_coverage} tech:\${cov.tech_coverage} fund:\${cov.fund_coverage}\`;
}
</script>
</body>
</html>`;
}

// Start server
app.listen(PORT, '0.0.0.0', () => {
  console.log(`[dashboard] OpenClaw dashboard → http://0.0.0.0:${PORT}`);
});

module.exports.app = app;
