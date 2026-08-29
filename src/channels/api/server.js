'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../../.env') });

const express = require('express');
const { getAllSubagentStatuses, getBucketStatus } = require('../../database/redis');
const { verdictCache, query: dbQuery } = require('../../database/postgres');
const { readParquet } = require('../../data/parquet_store');
const fs = require('fs');
const { runAlpaca } = require('./alpaca_cli');
const { groupByStrategy, computeDayPnlUsd } = require('./positions_grouped');
const { buildStrategyRow } = require('./strategy_row');
const { blendScope } = require('./blend_scope');
const { parseActivationDryRun } = require('./activation_preview');
const { isRegimeEligibleNow, regimeForStrategy } = require('./regime_active');
const { regimeFreshness } = require('./regime_freshness');
const { realizedLeverage } = require('./leverage');
const REGIME_FILE        = require('path').join(__dirname, '../../../.agents/market-state/regime_latest.json');
const CRYPTO_REGIME_FILE = require('path').join(__dirname, '../../../.agents/crypto-market-state/crypto_regime_latest.json');

const app  = express();
app.use(express.json());

// ── Dashboard build token (2026-08-22) ──────────────────────────────────────
// The dashboard is a single inline SPA: a tab loaded before a johnbot restart
// keeps running the OLD client JS against the NEW API (08-22: a pre-restart
// tab kept PUTting the retired `min_corr_cum_sharpe` key and rendered the
// server's "retired" 400 under the old Sharpe-floor sliders). The token is
// this file's mtime at process start — it changes exactly when the served
// page changes. Every /api/* response carries it; the page polls
// /api/dashboard-build and compares against the token it was served with,
// and reloads itself on mismatch (banner + 8s countdown).
const DASHBOARD_BUILD = (() => {
  try { return String(Math.round(require('fs').statSync(__filename).mtimeMs)); }
  catch (_) { return String(Date.now()); }
})();
app.use((req, res, next) => {
  if (req.path && req.path.startsWith('/api/')) res.setHeader('X-Dashboard-Build', DASHBOARD_BUILD);
  next();
});
app.get('/api/dashboard-build', (req, res) => res.json({ build: DASHBOARD_BUILD }));
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

// Async approval-job orchestration (staging/candidate/paper Approve button).
const approvals = require('../../agent/approvals');
approvals.init({ broadcast });

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
// Market overview — parquet-primary. Latest+prev close per ticker joined with
// universe_config (DB, still authoritative for name/category metadata).
app.get('/api/db/market-overview', async (req, res) => {
  try {
    const [priceRows, uniRes] = await Promise.all([
      readParquet('market_overview', {}),
      dbQuery(`SELECT ticker, name, category FROM universe_config WHERE active = true`),
    ]);
    const uni = new Map(uniRes.rows.map(r => [r.ticker, r]));
    const out = [];
    for (const p of (priceRows || [])) {
      const u = uni.get(p.ticker);
      if (!u) continue;   // DB-view behavior: INNER JOIN on universe_config
      out.push({
        ticker:     p.ticker,
        date:       p.date,
        close:      p.close,
        open:       p.open,
        high:       p.high,
        low:        p.low,
        volume:     p.volume,
        prev_close: p.prev_close,
        change_pct: p.change_pct != null ? Math.round(p.change_pct * 100) / 100 : null,
        name:       u.name,
        category:   u.category,
      });
    }
    out.sort((a, b) => (a.category || '').localeCompare(b.category || '') || a.ticker.localeCompare(b.ticker));
    res.json(out);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Price history — parquet-primary.
app.get('/api/db/prices/:ticker', async (req, res) => {
  const ticker = req.params.ticker.toUpperCase();
  try {
    if (req.query.limit) {
      const n = Math.min(parseInt(req.query.limit) || 5, 3650);
      const rows = await readParquet('prices', { ticker, limit: n });
      // Returned DESC — reverse to chronological to match legacy SQL shape.
      const out = (rows || []).reverse().map(r => ({
        date: r.date, open: r.open, high: r.high, low: r.low, close: r.close, volume: r.volume,
      }));
      return res.json(out);
    }
    const days = Math.min(parseInt(req.query.days) || 365, 3650);
    const rows = await readParquet('prices', { ticker, days });
    // Sort chronological (reader returns DESC by default).
    const out = (rows || [])
      .map(r => ({ date: r.date, open: r.open, high: r.high, low: r.low, close: r.close, volume: r.volume }))
      .sort((a, b) => String(a.date).localeCompare(String(b.date)));
    res.json(out);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Options contracts — parquet-primary.
app.get('/api/db/options/:ticker', async (req, res) => {
  const ticker = req.params.ticker.toUpperCase();
  const limit  = Math.min(parseInt(req.query.limit) || 30, 100);
  const type   = req.query.type; // 'call' or 'put'
  try {
    const rows = await readParquet('options', {
      ticker, limit,
      type: type === 'call' ? 'call' : (type === 'put' ? 'put' : null),
    });
    // Map parquet columns to legacy SQL shape: option_type→contract_type, market_price→last_price,
    // implied_volatility→iv.
    const out = (rows || []).map(r => ({
      expiry:        r.expiry,
      strike:        r.strike,
      contract_type: r.option_type,
      delta:         r.delta,
      gamma:         r.gamma,
      theta:         r.theta,
      vega:          r.vega,
      iv:            r.implied_volatility,
      open_interest: r.open_interest,
      volume:        r.volume,
      last_price:    r.market_price,
      bid:           r.bid,
      ask:           r.ask,
    }));
    res.json(out);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Fundamentals — parquet-primary. Returns last 4 quarterly rows.
app.get('/api/db/fundamentals/:ticker', async (req, res) => {
  const ticker = req.params.ticker.toUpperCase();
  try {
    const rows = await readParquet('fundamentals', { ticker, limit: 4 });
    // Map parquet shape back to legacy field names.
    const out = (rows || []).map(r => ({
      period:             r.period,
      period_end:         r.date,
      revenue:            r.revenue,
      gross_profit:       r.gross_profit,
      ebitda:             r.ebitda,
      net_income:         r.net_income,
      eps:                r.eps,
      gross_margin:       r.gross_margin,
      operating_margin:   r.operating_margin,
      net_margin:         r.net_margin,
      revenue_growth_yoy: r.revenue_growth,
      source:             'fmp',
    }));
    res.json(out);
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

// Prune market_news older than 30d on demand (real ingestion runs in the sentiment step)
app.post('/api/trigger/news', async (req, res) => {
  try {
    const collector = require('../../pipeline/collector');
    const store     = require('../../pipeline/store');
    res.json({ ok: true, message: 'Old news pruned (>30d)' });
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
    // No LIMIT: the prior cap of 500 (by signal_date DESC) was squeezing
    // out 4000+ older positions with real marked PnL in favor of today's
    // freshly-opened zero-PnL signals, which left the heatmap uniformly
    // grey + days_held=0 everywhere. Now the client receives every open
    // signal in the recent window (~5500 rows / ~1MB) and the ticker
    // rollups carry meaningful color + days_held.
    //
    // Two LATERAL JOINs: `sp` = most recent mark, `sp_prev` = the mark
    // immediately before that. We deliberately use rank-by-pnl_date rather
    // than `pnl_date < CURRENT_DATE` so that on stale-data days (pipeline
    // hasn't written today's row yet) `sp` falls back to the last available
    // mark and `sp_prev` is still the one before — which is exactly the
    // "two most recent marks" delta an operator wants on those days.
    const result = await dbQuery(`
      SELECT es.id, es.strategy_id, es.ticker, es.direction,
             es.entry_price, es.stop_loss, es.target_1, es.position_size_pct,
             es.signal_date, es.status,
             sp.close_price AS current_price,
             sp.unrealized_pnl_pct, sp.days_held,
             sp_prev.unrealized_pnl_pct AS prev_pnl_pct,
             sp_prev.close_price        AS prev_close
      FROM execution_signals es
      LEFT JOIN LATERAL (
        SELECT close_price, unrealized_pnl_pct, days_held, pnl_date
        FROM signal_pnl WHERE signal_id = es.id ORDER BY pnl_date DESC LIMIT 1
      ) sp ON true
      LEFT JOIN LATERAL (
        SELECT close_price, unrealized_pnl_pct
        FROM signal_pnl
        WHERE signal_id = es.id AND pnl_date < sp.pnl_date
        ORDER BY pnl_date DESC LIMIT 1
      ) sp_prev ON true
      WHERE es.status = 'open'
        AND es.signal_date >= CURRENT_DATE - INTERVAL '90 days'
      ORDER BY es.signal_date DESC
    `);

    // NAV via Alpaca CLI account.equity. One round-trip per request; this
    // endpoint is render-driven, not hot-loop. If Alpaca is unreachable the
    // rows still ship with day_pnl_usd=null and the dashboard's em-dash
    // fallback fires — preferable to wedging the entire positions list on
    // a degraded broker leg.
    let nav = null;
    try {
      const a = await runAlpaca(['account', 'get'], { timeout: 10_000 });
      if (a.ok && a.payload && a.payload.equity != null) {
        const eq = parseFloat(a.payload.equity);
        if (Number.isFinite(eq) && eq > 0) nav = eq;
      }
    } catch (_) { /* nav stays null → day_pnl_usd renders as em-dash */ }

    // Broker truth (Phase 2I, 2026-05-19): the strategy-intent rows below
    // describe what each strategy wants; the broker holds ONE consolidated
    // position per ticker. Fetch alpaca position list once and attach the
    // broker's view to each row keyed by ticker so the dashboard's
    // _groupByTicker can show the parent (ticker) row from broker truth
    // and the expanded sub-rows from strategy intents. Pre-fix the rollup
    // summed intents and produced > 100% NAV on multi-strategy tickers
    // (WBD hit 409% across 17 strategies).
    const brokerPositions = {};
    try {
      const positionsResp = await runAlpaca(['position', 'list'], { timeout: 10_000 });
      const list = positionsResp && positionsResp.ok && Array.isArray(positionsResp.payload)
        ? positionsResp.payload : [];
      for (const p of list) {
        if (!p || !p.symbol) continue;
        const qty  = parseFloat(p.qty);
        const mv   = parseFloat(p.market_value);
        const avg  = parseFloat(p.avg_entry_price);
        const cur  = parseFloat(p.current_price);
        const plpc = parseFloat(p.unrealized_plpc);
        // Capture the broker's actual position-lifetime $ P&L so the
        // dashboard tile can display it directly (matching the % shown
        // alongside). Without this field the client falls back to
        // nav × contrib_pct, which is DB-derived from signal_pnl marks
        // and disagrees with the live broker number whenever today's
        // price has moved since the last reconcile.
        const upl  = parseFloat(p.unrealized_pl);
        brokerPositions[p.symbol] = {
          qty:               Number.isFinite(qty)  ? qty  : null,
          market_value:      Number.isFinite(mv)   ? mv   : null,
          avg_entry_price:   Number.isFinite(avg)  ? avg  : null,
          current_price:     Number.isFinite(cur)  ? cur  : null,
          unrealized_plpc:   Number.isFinite(plpc) ? plpc : null,
          unrealized_pl:     Number.isFinite(upl)  ? upl  : null,
          // size_pct = market_value / NAV, normalised to a fraction (0.05 = 5%).
          // Absolute value so shorts contribute positive size to the heatmap
          // tile area; direction (side) is the qty sign carrier.
          size_pct: (Number.isFinite(mv) && nav && nav > 0)
            ? Math.abs(mv) / nav : null,
          side: Number.isFinite(qty) ? (qty >= 0 ? 'long' : 'short') : null,
        };
      }
    } catch (_) { /* broker leg degraded — rows ship without broker_position */ }

    // Resting exit legs at the broker (2026-07-15), keyed by symbol. This is
    // exit TRUTH — the stop/take-profit orders that will actually fire — as
    // opposed to execution_signals.stop_loss/target_1, which records each
    // strategy's *intent* and says nothing about whether a bracket is live.
    // A position with no resting stop is unprotected however good the intent
    // was, so the panel must be able to report that rather than draw a level
    // that exists only in the DB.
    //
    // pending_cancel legs are excluded: openclaw-afterhours-tp-rth-reconcile
    // cancels the extended-hours TPs at 13:30 UTC and those rows linger in the
    // list while the cancel settles — counting them would show a take-profit
    // that is on its way out.
    // brokerBracketsOk separates "asked the broker, this position has no stop"
    // from "could not ask the broker". Both would otherwise arrive at the panel
    // as an absent block and render as "none" — turning a degraded API leg into
    // a false report that the ENTIRE book is unprotected, which is the exact
    // alarm an operator would act on. Fetch failure must read as unknown.
    // --nested is REQUIRED, not a nicety: an OCO exit is ONE order whose
    // take-profit shows at the top level while its protective stop hides in
    // `legs[]` (status 'held' until the sibling cancels). Reading only
    // top-level stop_price finds 3 stops across this book; reading the legs
    // finds 9 — i.e. the flat read invents unprotected positions that are in
    // fact bracketed, which is a worse lie than showing nothing.
    //
    // --limit 500 is the API max (default 50). At 14 positions the default
    // would do, but a silently truncated page would drop real brackets and
    // report them as absent — the same false alarm, arriving later and only
    // once the book grows.
    const brokerBrackets = {};
    let brokerBracketsOk = false;
    const absorbLeg = (o) => {
      if (!o || !o.symbol || o.status === 'pending_cancel') return;
      const leg = brokerBrackets[o.symbol]
        || (brokerBrackets[o.symbol] = { stop: null, target: null, exiting: null });
      const sp = parseFloat(o.stop_price);
      const lp = parseFloat(o.limit_price);
      // emex_ (past-stop emergency exit) / ahsx_ (ext-hours monitor exit) are
      // LIQUIDATIONS at ~current price, not take-profits — folding their limit
      // into `target` would paint a breached position as happily bracketed.
      const coid = o.client_order_id || '';
      if (coid.startsWith('emex_') || coid.startsWith('ahsx_')) {
        if (Number.isFinite(lp)) leg.exiting = lp;
        return;
      }
      if (Number.isFinite(sp)) leg.stop = sp;
      if (Number.isFinite(lp)) leg.target = lp;
      for (const child of (o.legs || [])) absorbLeg(child);
    };
    try {
      const ordersResp = await runAlpaca(
        ['order', 'list', '--nested', '--limit', '500'], { timeout: 10_000 });
      if (ordersResp && ordersResp.ok && Array.isArray(ordersResp.payload)) {
        brokerBracketsOk = true;
        for (const o of ordersResp.payload) absorbLeg(o);
      }
    } catch (_) { /* brokerBracketsOk stays false — panel renders brackets as unknown */ }

    // Exits QUEUED but not yet resting: the ext-hours monitor's pending-exit
    // journal — the reattach pass writes past-stop positions here when no
    // session is open, and the monitor's next tick (04:00–19:50 ET, 10-min
    // cadence) acts on it. Same file the monitor reads, same 24h TTL, so
    // "queued" here is exactly "the monitor will act at its next tick".
    const pendingExitSyms = new Set();
    try {
      const intentsPath = process.env.OPENCLAW_AH_INTENTS_PATH
        || require('path').join(__dirname, '../../../logs/afterhours_pending_exits.json');
      const raw = JSON.parse(fs.readFileSync(intentsPath, 'utf8'));
      const nowS = Date.now() / 1000;
      for (const [sym, it] of Object.entries(raw)) {
        if (it && typeof it === 'object' && nowS - (it.ts || 0) < 24 * 3600) {
          pendingExitSyms.add(sym);
        }
      }
    } catch (_) { /* no journal file = nothing queued */ }

    // Per-ticker corr-adjusted cumulative Sharpe (S_adj) — the conviction the
    // position was last sized at (stamped by the sizer each cycle into
    // cycle_contributing_strategies, 2026-07-14). Latest non-null row per
    // ticker; best-effort (tiles render an em-dash without it).
    const sadjByTicker = {};
    try {
      const sadjRes = await dbQuery(`
        SELECT DISTINCT ON (ticker) ticker,
               corr_cum_sharpe::float AS corr_cum_sharpe,
               run_date               AS corr_cum_sharpe_asof
          FROM cycle_contributing_strategies
         WHERE corr_cum_sharpe IS NOT NULL
         ORDER BY ticker, run_date DESC
      `);
      for (const r of sadjRes.rows) sadjByTicker[r.ticker] = r;
    } catch (_) { /* column may predate migration 143 on a fresh clone */ }

    // Per-ticker SIZER contributions — the strategies that actually SIZED the
    // position (signed sizing weight), same cycle_contributing_strategies source
    // as /api/portfolio/ticker-alpha. Latest row per ticker that CARRIES
    // contributions (looser than the S_adj filter → full open-book coverage:
    // 48/48 today). The expanded per-position view renders THESE instead of the
    // raw open-intent list, which also carries gated-out + stale-unclosed signals.
    const contribByTicker = {};
    try {
      const contribRes = await dbQuery(`
        SELECT DISTINCT ON (ticker) ticker, contributions
          FROM cycle_contributing_strategies
         WHERE contributions IS NOT NULL AND jsonb_array_length(contributions) > 0
         ORDER BY ticker, run_date DESC
      `);
      for (const r of contribRes.rows) contribByTicker[r.ticker] = r.contributions;
    } catch (_) { /* column may predate migration 143 on a fresh clone */ }

    // Active positions = broker truth. execution_signals carries every
    // strategy's *intent*, including stale rows the reconcile sweep hasn't
    // closed yet (phantoms). The dashboard's "Active Positions" panel must
    // reflect what we actually hold, so we drop any row whose ticker the
    // broker isn't carrying. Degraded-mode fallback: if the broker fetch
    // failed (brokerPositions empty AND we caught earlier), ship every row
    // so the operator still sees something rather than a blank panel.
    const brokerHasAny = Object.keys(brokerPositions).length > 0;
    const enriched = result.rows
      .filter(r => !brokerHasAny || brokerPositions[r.ticker])
      .map(r => ({
        ...r,
        day_pnl_usd: computeDayPnlUsd({
          today_pnl_pct:     r.unrealized_pnl_pct,
          prev_pnl_pct:      r.prev_pnl_pct,
          position_size_pct: r.position_size_pct,
          nav,
        }),
        broker_position: brokerPositions[r.ticker] || null,
        // Present-and-empty = genuinely no resting leg; null = broker unreachable.
        // exiting = a liquidation order (emex_/ahsx_) resting at the broker;
        // queued = the monitor journal owes this position an exit at its next tick.
        broker_bracket: brokerBracketsOk
          ? { ...(brokerBrackets[r.ticker] || { stop: null, target: null, exiting: null }),
              queued: pendingExitSyms.has(r.ticker) }
          : null,
        corr_cum_sharpe: sadjByTicker[r.ticker]
          ? sadjByTicker[r.ticker].corr_cum_sharpe : null,
        corr_cum_sharpe_asof: sadjByTicker[r.ticker]
          ? sadjByTicker[r.ticker].corr_cum_sharpe_asof : null,
        sizer_contributions: contribByTicker[r.ticker] || null,
      }));

    if (req.query.group_by === 'strategy') {
      return res.json({ groups: groupByStrategy(enriched) });
    }
    res.json(enriched);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.get('/api/portfolio/history', async (req, res) => {
  try {
    // Tie-breaker on signal_id is critical: closed_at is a date column, so
    // dozens of rows share a timestamp. Without a deterministic secondary
    // sort, LIMIT cuts non-deterministically and per-strategy entries can
    // disappear between page loads. Bumped 100→500 so older closes stay
    // visible — with ~230 closed rows total today this comfortably covers
    // the live history.
    const sid = (req.query.strategy_id || '').toString().trim();
    const params = [];
    let where = `WHERE sp.status = 'closed'`;
    if (sid) { params.push(sid); where += ` AND es.strategy_id = $${params.length}`; }
    const result = await dbQuery(`
      SELECT es.strategy_id, es.ticker, es.direction, es.entry_price,
             es.position_size_pct,
             sp.closed_price, sp.realized_pnl_pct, sp.days_held,
             sp.close_reason, sp.closed_at, sp.signal_id::text
      FROM signal_pnl sp
      JOIN execution_signals es ON es.id = sp.signal_id
      ${where}
      ORDER BY sp.closed_at DESC, sp.signal_id DESC
    `, params);
    res.json(result.rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Per-ticker SIGNED alpha contributions for the position modal. Reads the
// latest cycle's corr-gate representative contributions (daily_weight ×
// size_scalar × direction) from cycle_contributing_strategies via
// /api/portfolio/ticker-alpha; rendered as signed bars by _buildAlphaBarsHtml.
// Session-cached (TTL + LRU cap below).
const _tickerAlphaCache = new Map();   // ticker → { ts, payload }
const _TICKER_ALPHA_TTL_MS = 5 * 60 * 1000;
const _TICKER_ALPHA_CAP    = 256;

app.get('/api/portfolio/ticker-alpha/:ticker', async (req, res) => {
  const ticker = String(req.params.ticker || '').trim().toUpperCase();
  if (!ticker) return res.status(400).json({ error: 'ticker required' });
  try {
    const now = Date.now();
    const hit = _tickerAlphaCache.get(ticker);
    if (hit && now - hit.ts < _TICKER_ALPHA_TTL_MS) return res.json(hit.payload);

    // Read the latest cycle's SIGNED per-representative contributions for this
    // ticker (migration 134). Open positions → latest run_date; closed → the
    // last run_date that sized the ticker (same row, most-recent). Each entry:
    // {strategy_id, contribution = daily_weight × size_scalar × direction,
    //  direction}. Graceful fallback to the legacy strategies[] list (no values).
    const cr = await dbQuery(
      `SELECT strategies, contributions, run_date
         FROM cycle_contributing_strategies
        WHERE ticker = $1 ORDER BY run_date DESC LIMIT 1`, [ticker]);
    const row = cr.rows[0] || null;
    let contributions = [];
    if (row && Array.isArray(row.contributions)) {
      contributions = row.contributions
        .map(c => ({
          strategy_id: c.strategy_id,
          contribution: Number(c.contribution),
          direction: Number(c.direction),
        }))
        .filter(c => isFinite(c.contribution))
        .sort((a, b) => b.contribution - a.contribution);   // longs (green) top, shorts (red) bottom
    } else if (row && Array.isArray(row.strategies)) {
      // Back-compat: pre-migration-134 row with names only, no signed values.
      contributions = row.strategies.map(s => ({ strategy_id: s, contribution: null, direction: null }));
    }
    const net = contributions.reduce((s, c) => s + (c.contribution || 0), 0);
    const payload = {
      ticker,
      contributions,
      net,
      run_date: row ? row.run_date : null,
      has_values: contributions.some(c => c.contribution != null),
      computed_at: new Date().toISOString(),
    };
    if (_tickerAlphaCache.size >= _TICKER_ALPHA_CAP) {
      const oldestKey = _tickerAlphaCache.keys().next().value;
      _tickerAlphaCache.delete(oldestKey);
    }
    _tickerAlphaCache.set(ticker, { ts: now, payload });
    return res.json(payload);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Lambda — overnight (Reg T) leverage multiplier for the daily process.
// Operator-tunable from the Portfolio page slider. Sizer reads pipeline_config
// on every cycle so a slider change takes effect on the next pipeline tick.
// Range [0.10, 2.00] (default 2.0) — capped at Reg T overnight max (2×
// equity, 50% initial margin). 2026-05-19: cap reduced from 3.50 to 2.00
// after the operator noted the prior range exceeded Reg T overnight
// limits; the existing value (3.0 set 2026-05-16) was clamped to 2.0.
const LAMBDA_OVERNIGHT_MIN = 0.10;
const LAMBDA_OVERNIGHT_MAX = 2.00;
// Asset-correlation de-gross threshold slider band (replaced the unused
// intraday-leverage placeholder 2026-06-26). Pearson cutoff for clustering
// correlated same-direction positions before the per-cluster gross cap.
const ASSET_CORR_THR_MIN = 0.50;
const ASSET_CORR_THR_MAX = 0.90;
const ASSET_CORR_THR_DEFAULT = 0.70;

app.get('/api/config/lambda', async (req, res) => {
  try {
    const r = await dbQuery("SELECT value, updated_at FROM pipeline_config WHERE key = 'position_sizing_lambda'");
    const row = r.rows[0];
    res.json({
      value:      row ? Math.min(parseFloat(row.value), LAMBDA_OVERNIGHT_MAX) : 2.0,
      min:        LAMBDA_OVERNIGHT_MIN, max: LAMBDA_OVERNIGHT_MAX,
      updated_at: row ? row.updated_at : null,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.put('/api/config/lambda', async (req, res) => {
  const v = parseFloat(req.body && req.body.value);
  if (!isFinite(v) || v < LAMBDA_OVERNIGHT_MIN || v > LAMBDA_OVERNIGHT_MAX) {
    return res.status(400).json({ error: `value must be a number in [${LAMBDA_OVERNIGHT_MIN}, ${LAMBDA_OVERNIGHT_MAX}]` });
  }
  try {
    await dbQuery(`
      INSERT INTO pipeline_config (key, value, description, updated_at)
      VALUES ('position_sizing_lambda', $1, 'Overnight (Reg T) leverage. Daily notional deployed = lambda × NAV. Range [0.10, 2.00]. Adjustable via dashboard.', NOW())
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    `, [String(v)]);
    res.json({ value: v });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Asset-correlation de-gross threshold — the Pearson cutoff for clustering
// correlated same-direction positions before the per-cluster gross cap
// (asset_correlation_filter.py). The sizer reads pipeline_config.asset_corr_cap_thr
// every cycle, so a slider change takes effect on the next pipeline tick with no
// johnbot restart. Lower de-grosses more aggressively (0.60 catches the semi
// sector; 0.70 mostly catches ETF/market-beta). The `enabled` flag reflects the
// asset_corr_cap_enabled gate/kill-switch (also pipeline_config). Replaced the
// unused intraday-leverage placeholder 2026-06-26.
app.get('/api/config/asset-corr-thr', async (req, res) => {
  try {
    const [thrRes, enRes] = await Promise.all([
      dbQuery("SELECT value, updated_at FROM pipeline_config WHERE key = 'asset_corr_cap_thr'"),
      dbQuery("SELECT value FROM pipeline_config WHERE key = 'asset_corr_cap_enabled'"),
    ]);
    const row = thrRes.rows[0];
    res.json({
      value:      row ? Math.max(ASSET_CORR_THR_MIN, Math.min(parseFloat(row.value), ASSET_CORR_THR_MAX)) : ASSET_CORR_THR_DEFAULT,
      min:        ASSET_CORR_THR_MIN, max: ASSET_CORR_THR_MAX,
      enabled:    enRes.rows[0] ? ['1', 'true', 'on'].includes(String(enRes.rows[0].value).trim().toLowerCase()) : false,
      updated_at: row ? row.updated_at : null,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.put('/api/config/asset-corr-thr', async (req, res) => {
  const v = parseFloat(req.body && req.body.value);
  if (!isFinite(v) || v < ASSET_CORR_THR_MIN || v > ASSET_CORR_THR_MAX) {
    return res.status(400).json({ error: `value must be a number in [${ASSET_CORR_THR_MIN}, ${ASSET_CORR_THR_MAX}]` });
  }
  try {
    await dbQuery(`
      INSERT INTO pipeline_config (key, value, description, updated_at)
      VALUES ('asset_corr_cap_thr', $1, 'Asset-correlation de-gross threshold (Pearson cutoff for same-direction clustering). Range [0.50, 0.90]. Sizer reads every cycle. Adjustable via dashboard.', NOW())
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    `, [String(v)]);
    res.json({ value: v });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Risk switches + values (operator 2026-07-27: Risk & Sizing panel keeps ONE
// slider — leverage; the asset-corr de-gross and the net-exposure cap become
// switch + value pairs). Partial body; each present field validated + upserted
// into pipeline_config, which the sizer re-reads every cycle (no restart).
const _RISK_TOGGLE_FIELDS = {
  asset_corr_cap_enabled:  { kind: 'bool',
    desc: 'Asset-correlation de-gross ON/OFF. Sizer reads every cycle. Dashboard switch.' },
  asset_corr_cap_pct:      { kind: 'num', min: 0.05, max: 0.50,
    desc: 'Asset-correlation de-gross cluster cap (share of NAV per correlated same-direction cluster). Range [0.05, 0.50]. Sizer reads every cycle. Dashboard value.' },
  net_exposure_cap_enabled: { kind: 'bool',
    desc: 'Book net-exposure cap ON/OFF (|net| <= max_net_frac x gross; heavy side de-levered). Sizer reads every cycle. Dashboard switch.' },
  max_net_frac:            { kind: 'num', min: 0.10, max: 1.00,
    desc: 'Book net-exposure cap fraction: |net| <= this x intended gross. Range [0.10, 1.00]. Sizer reads every cycle. Dashboard value.' },
};
app.put('/api/config/risk-toggles', async (req, res) => {
  const body = req.body || {};
  const updates = {};
  for (const [key, spec] of Object.entries(_RISK_TOGGLE_FIELDS)) {
    if (body[key] === undefined) continue;
    if (spec.kind === 'bool') {
      updates[key] = body[key] ? '1' : '0';
    } else {
      const v = parseFloat(body[key]);
      if (!isFinite(v) || v < spec.min || v > spec.max) {
        return res.status(400).json({ error: `${key} must be a number in [${spec.min}, ${spec.max}]` });
      }
      updates[key] = String(v);
    }
  }
  if (!Object.keys(updates).length) {
    return res.status(400).json({ error: 'no recognized fields in body' });
  }
  try {
    for (const [key, value] of Object.entries(updates)) {
      await dbQuery(`
        INSERT INTO pipeline_config (key, value, description, updated_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
      `, [key, value, _RISK_TOGGLE_FIELDS[key].desc]);
    }
    res.json({ updated: updates });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── Strategy Activation threshold + dry-run preview ─────────────────────────
// Slider band for pipeline_config.strategy_activation_min_sharpe — the extra
// sharpe floor the activation assigner (src/backtest/activation_assigner.py)
// applies ON TOP of the shared per-regime qualification gate (>0 sharpe,
// class-DD sleeve ceiling, ≥100 trades — regime_qualification.py /
// promotion_service.js, policy 2026-07-13 v2). Slider at 0 → a strategy is
// active in exactly its qualifying regimes. The assigner reads this key fresh
// on every run and fail-safes to 0.5 when the row is absent/malformed — the
// GET mirrors that exact fallback so slider and assigner can never disagree.
// LIVE since 2026-07-13 (OPENCLAW_ACTIVATION_ASSIGNER=1): the Mon 00:00 ET
// weekly_live_sharpe.js run applies this threshold to
// strategy_regime_params.eligible before the weights rebuild. Since
// 2026-08-22 the daily compute chain's FIRST step (`activation` →
// src/execution/activation_apply.py) does the same whenever a slider row is
// newer than the `strategy_activation_last_applied` marker the assigner
// stamps — so a slider moved today is applied (eligibility + weights) at
// the next daily compute (15:00 ET same-day lane), not next Monday. This
// dashboard endpoint itself still only (a) sets the threshold and (b)
// previews via --dry-run — the server MUST NEVER invoke the assigner without
// --dry-run; see the hard-coded argv + guard in POST /api/activation/dry-run
// below. The GETs report `pending` / `last_applied` from the marker so the
// card can say whether the saved value is live yet.
const ACTIVATION_MIN_SHARPE_MIN     = 0.0;
const ACTIVATION_MIN_SHARPE_MAX     = 3.0;
const ACTIVATION_MIN_SHARPE_DEFAULT = 0.5;   // mirrors the assigner's fail-safe
const ACTIVATION_DRY_RUN_TIMEOUT_MS = 120_000;

// Marker written by activation_assigner.stamp_last_applied after every clean
// non-dry-run --all (weekly, Sunday finale, daily activation step). pending =
// the slider row is newer than the marker (server-clock updated_at on both
// sides — the same comparison activation_apply.py makes), or no marker yet.
async function _activationApplyState(sliderUpdatedAt) {
  const out = { last_applied: null, applied_at: null, pending: null };
  try {
    const r = await dbQuery("SELECT value, updated_at FROM pipeline_config WHERE key = 'strategy_activation_last_applied'");
    const row = r.rows[0];
    if (!row) { out.pending = true; return out; }
    try { out.last_applied = JSON.parse(row.value); } catch (_) { out.last_applied = { raw: row.value }; }
    out.applied_at = row.updated_at;
    out.pending = sliderUpdatedAt ? (new Date(sliderUpdatedAt) > new Date(row.updated_at)) : false;
  } catch (_) { /* advisory only — the sliders must still load */ }
  return out;
}

app.get('/api/config/activation-min-sharpe', async (req, res) => {
  try {
    const r = await dbQuery("SELECT value, updated_at FROM pipeline_config WHERE key = 'strategy_activation_min_sharpe'");
    const row = r.rows[0];
    const raw = row ? parseFloat(row.value) : NaN;
    const applyState = await _activationApplyState(row ? row.updated_at : null);
    res.json({
      ...applyState,
      value:      isFinite(raw)
                    ? Math.max(ACTIVATION_MIN_SHARPE_MIN, Math.min(raw, ACTIVATION_MIN_SHARPE_MAX))
                    : ACTIVATION_MIN_SHARPE_DEFAULT,
      min:        ACTIVATION_MIN_SHARPE_MIN, max: ACTIVATION_MIN_SHARPE_MAX,
      default:    ACTIVATION_MIN_SHARPE_DEFAULT,
      row_exists: !!row,
      updated_at: row ? row.updated_at : null,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.put('/api/config/activation-min-sharpe', async (req, res) => {
  const v = parseFloat(req.body && req.body.value);
  if (!isFinite(v) || v < ACTIVATION_MIN_SHARPE_MIN || v > ACTIVATION_MIN_SHARPE_MAX) {
    return res.status(400).json({ error: `value must be a number in [${ACTIVATION_MIN_SHARPE_MIN}, ${ACTIVATION_MIN_SHARPE_MAX}]` });
  }
  try {
    // Upsert — the row may not exist yet (the assigner fail-safes to 0.5
    // when absent, so first write creates it with a description).
    await dbQuery(`
      INSERT INTO pipeline_config (key, value, description, updated_at)
      VALUES ('strategy_activation_min_sharpe', $1, 'Activation min-Sharpe slider: extra per-regime sharpe floor on top of the qualification gate (>0 sharpe, class-DD sleeve, >=100 trades). 0 = trade in exactly the qualifying regimes. Range [0.0, 3.0]; assigner fail-safe default 0.5. Applied by activation_assigner at the next daily compute (daily-cycle activation step, 15:00 ET same-day lane) and by the weekly Mon 00:00 ET refresh (OPENCLAW_ACTIVATION_ASSIGNER=1). Adjustable via dashboard.', NOW())
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, description = EXCLUDED.description, updated_at = NOW()
    `, [String(v)]);
    res.json({ value: v });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── Activation min-TRADES slider ────────────────────────────────────────────
// Sibling of the min-Sharpe slider above, for the per-regime SAMPLE-SIZE floor:
// a regime only activates when its backtest produced >= this many trades in that
// regime. Previously reachable only from regime_qualification.class_thresholds
// (a hardcoded 100) or the assigner's --min-trades flag, so tuning it needed a
// code change; operator directive 2026-07-16 made it dashboard-adjustable.
//
// Fail-safe direction is the OPPOSITE of a normal slider and deliberately so:
// when the row is ABSENT the assigner defers to the per-class gate (100) rather
// than loosening to 0 — an unset key must never activate a regime on a 1-trade
// sample. So `row_exists:false` reports the class floor, not a stored value.
// 0 IS a legal explicit choice ("no sample floor") and is honoured once written.
const ACTIVATION_MIN_TRADES_MIN     = 0;
const ACTIVATION_MIN_TRADES_MAX     = 1000;
const ACTIVATION_MIN_TRADES_DEFAULT = 100;   // == regime_qualification class gate

app.get('/api/config/activation-min-trades', async (req, res) => {
  try {
    const r = await dbQuery("SELECT value, updated_at FROM pipeline_config WHERE key = 'strategy_activation_min_trades'");
    const row = r.rows[0];
    const raw = row ? parseInt(row.value, 10) : NaN;
    // Mirror the assigner's resolution exactly so slider and assigner can never
    // disagree: unset/malformed/negative -> the class gate.
    const usable = Number.isInteger(raw) && raw >= ACTIVATION_MIN_TRADES_MIN;
    const applyState = await _activationApplyState(row ? row.updated_at : null);
    res.json({
      ...applyState,
      value:      usable ? Math.min(raw, ACTIVATION_MIN_TRADES_MAX) : ACTIVATION_MIN_TRADES_DEFAULT,
      min:        ACTIVATION_MIN_TRADES_MIN, max: ACTIVATION_MIN_TRADES_MAX,
      default:    ACTIVATION_MIN_TRADES_DEFAULT,
      row_exists: !!row,
      source:     usable ? 'pipeline_config' : 'class_gate',
      updated_at: row ? row.updated_at : null,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.put('/api/config/activation-min-trades', async (req, res) => {
  const v = parseInt(req.body && req.body.value, 10);
  if (!Number.isInteger(v) || v < ACTIVATION_MIN_TRADES_MIN || v > ACTIVATION_MIN_TRADES_MAX) {
    return res.status(400).json({ error: `value must be an integer in [${ACTIVATION_MIN_TRADES_MIN}, ${ACTIVATION_MIN_TRADES_MAX}]` });
  }
  try {
    await dbQuery(`
      INSERT INTO pipeline_config (key, value, description, updated_at)
      VALUES ('strategy_activation_min_trades', $1, 'Activation min-TRADES slider: per-regime sample-size floor — a regime activates only when its backtest produced >= this many trades in that regime. Resolution order: --min-trades flag > this key > regime_qualification class gate (100). Range [0, 1000]; 0 = no sample floor. When ABSENT the assigner defers to the class gate (100), never to 0. Adjustable via dashboard.', NOW())
      ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
    `, [String(v)]);
    res.json({ value: v });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// POST /api/activation/dry-run — on-demand hypothetical preview of what the
// activation assigner WOULD do at the currently-saved threshold. Shells out
// to `nice -n 19 python3 -m backtest.activation_assigner --all --dry-run`
// (VPS is 2-core — always nice heavy python) with the server's own env
// (dotenv already loaded POSTGRES_URI at boot) + PYTHONPATH=src, then parses
// the line-oriented diff via activation_preview.parseActivationDryRun.
// --dry-run performs ZERO database writes (the assigner's write helper is
// never called) — this endpoint is safe to hit at any time. Synchronous
// (~seconds for ~150 strategies) with a hard 120s kill; one at a time via a
// simple in-memory busy flag (second caller gets 409).
let _activationDryRunBusy = false;
app.post('/api/activation/dry-run', (req, res) => {
  if (_activationDryRunBusy) {
    return res.status(409).json({ error: 'an activation dry-run is already running — try again in a moment' });
  }
  _activationDryRunBusy = true;

  const { spawn } = require('child_process');
  const path = require('path');
  const rootDir = path.resolve(__dirname, '../../..');
  // HARD INVARIANT (Phase 1e gate): --dry-run is non-negotiable. The argv is
  // a hard-coded literal (no request-derived args) and the belt-and-braces
  // guard below refuses to spawn if a future edit ever drops the flag.
  const args = ['-n', '19', 'python3', '-m', 'backtest.activation_assigner', '--all', '--dry-run'];
  if (!args.includes('--dry-run')) {
    _activationDryRunBusy = false;
    return res.status(500).json({ error: 'refusing to run activation assigner without --dry-run' });
  }

  const started = Date.now();
  let child;
  try {
    child = spawn('nice', args, {
      cwd:   rootDir,
      env:   { ...process.env, PYTHONPATH: 'src' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
  } catch (e) {
    _activationDryRunBusy = false;
    return res.status(500).json({ error: `assigner spawn failed: ${e.message}` });
  }

  let out = '', errOut = '', responded = false;
  const finish = (status, body) => {
    if (responded) return;
    responded = true;
    clearTimeout(killTimer);
    _activationDryRunBusy = false;
    res.status(status).json(body);
  };
  const killTimer = setTimeout(() => {
    try { child.kill('SIGKILL'); } catch (_) { /* already gone */ }
    finish(504, { error: `activation dry-run timed out after ${ACTIVATION_DRY_RUN_TIMEOUT_MS / 1000}s` });
  }, ACTIVATION_DRY_RUN_TIMEOUT_MS);

  child.stdout.on('data', d => { out += d; });
  child.stderr.on('data', d => { errOut += d; });
  child.on('error', e => finish(500, { error: `assigner spawn error: ${e.message}` }));
  child.on('close', (code) => {
    if (responded) return;
    const parsed = parseActivationDryRun(out);
    // Exit 1 with a parsable summary = per-strategy errors (assigner still
    // summarises; `errors` is surfaced in the payload). No summary at all =
    // hard failure (e.g. POSTGRES_URI missing, import error) → 500 with tails.
    if (!parsed.summary_found) {
      return finish(500, {
        error:       'activation assigner produced no parsable dry-run summary',
        exit_code:   code,
        stdout_tail: out.slice(-2000),
        stderr_tail: errOut.slice(-2000),
      });
    }
    finish(200, {
      ok:          true,
      dry_run:     true,   // always — this endpoint never performs the live write
      exit_code:   code,
      duration_ms: Date.now() - started,
      ...parsed,
    });
  });
});

// Risk & Sizing — bundled config for the Portfolio page control panel.
// Returns the global λ alongside per-regime liquidity / min-notional /
// circuit-breaker so the operator can see effective leverage per regime
// (= λ × liquidity_param) on one screen.
app.get('/api/config/regime-sizing', async (req, res) => {
  try {
    const [lamRes, acThrRes, acEnRes, riskRes, regimeRes] = await Promise.all([
      dbQuery("SELECT value, updated_at FROM pipeline_config WHERE key = 'position_sizing_lambda'"),
      dbQuery("SELECT value, updated_at FROM pipeline_config WHERE key = 'asset_corr_cap_thr'"),
      dbQuery("SELECT value FROM pipeline_config WHERE key = 'asset_corr_cap_enabled'"),
      dbQuery("SELECT key, value FROM pipeline_config WHERE key IN ('asset_corr_cap_pct', 'net_exposure_cap_enabled', 'max_net_frac')"),
      dbQuery(`SELECT regime_state, liquidity_param, position_circuit_breaker_pct,
                      min_acting_strategies, updated_at
               FROM regime_sizer_params
               ORDER BY CASE regime_state
                          WHEN 'LOW_VOL' THEN 1
                          WHEN 'TRANSITIONING' THEN 2
                          WHEN 'HIGH_VOL' THEN 3
                          WHEN 'CRISIS' THEN 4
                          ELSE 5 END`),
    ]);
    // 2026-05-19: prior code used `path.join(...)` but `path` is not imported
    // at module scope (only inline as `require('path')` for REGIME_FILE). The
    // ReferenceError was caught silently and the endpoint always returned
    // the TRANSITIONING fallback, even though the file said LOW_VOL. Use the
    // already-resolved REGIME_FILE constant — same target, no inline join.
    let currentRegime = 'TRANSITIONING';
    try {
      const latestJson = JSON.parse(fs.readFileSync(REGIME_FILE, 'utf8'));
      currentRegime = latestJson.state || 'TRANSITIONING';
    } catch (e) {
      console.warn(`[regime-sizing] regime file read failed: ${e.message}`);
    }
    const lam = lamRes.rows[0];
    const acThr = acThrRes.rows[0];
    const acEn  = acEnRes.rows[0];
    const riskMap = Object.fromEntries((riskRes.rows || []).map(r => [r.key, r.value]));
    res.json({
      current_regime: currentRegime,
      global: {
        lambda:     lam ? Math.min(parseFloat(lam.value), LAMBDA_OVERNIGHT_MAX) : 2.0,
        min:        LAMBDA_OVERNIGHT_MIN,
        max:        LAMBDA_OVERNIGHT_MAX,
        updated_at: lam ? lam.updated_at : null,
        // Asset-correlation de-gross threshold — separate slider in dashboard.
        // LIVE: consumed by regime_blended_sizer._apply_asset_corr_cap (2026-06-26).
        asset_corr_thr:         acThr ? Math.max(ASSET_CORR_THR_MIN, Math.min(parseFloat(acThr.value), ASSET_CORR_THR_MAX)) : ASSET_CORR_THR_DEFAULT,
        asset_corr_thr_min:     ASSET_CORR_THR_MIN,
        asset_corr_thr_max:     ASSET_CORR_THR_MAX,
        asset_corr_thr_updated_at: acThr ? acThr.updated_at : null,
        asset_corr_enabled:     acEn ? ['1', 'true', 'on'].includes(String(acEn.value).trim().toLowerCase()) : false,
        // Switch+value pairs for the reworked Risk & Sizing panel
        // (operator 2026-07-27: only the leverage slider remains a slider).
        asset_corr_cap_pct:     riskMap.asset_corr_cap_pct != null ? parseFloat(riskMap.asset_corr_cap_pct) : 0.20,
        net_cap_enabled:        ['1', 'true', 'on'].includes(String(riskMap.net_exposure_cap_enabled ?? '0').trim().toLowerCase()),
        max_net_frac:           riskMap.max_net_frac != null ? parseFloat(riskMap.max_net_frac) : 0.6,
      },
      regimes: regimeRes.rows.map(r => ({
        state:                          r.regime_state,
        liquidity_param:                parseFloat(r.liquidity_param),
        position_circuit_breaker_pct:   parseFloat(r.position_circuit_breaker_pct),
        // Per-regime conviction gate (2026-08-22, migration 147): the minimum
        // number of DISTINCT strategies acting on a ticker in its net direction
        // for the sizer to take the position. Integer [1, 10] by table CHECK;
        // 1 = open (every contributor-backed ticker passes). Replaced the
        // S_adj floor (min_corr_cum_sharpe — retired, unread). Read fresh each
        // sizer cycle (no restart needed).
        min_acting_strategies:          r.min_acting_strategies != null ? parseInt(r.min_acting_strategies, 10) : 1,
        updated_at:                     r.updated_at,
      })),
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.put('/api/config/regime-sizing/:regime', async (req, res) => {
  const regime = String(req.params.regime || '').toUpperCase();
  const validRegimes = new Set(['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']);
  if (!validRegimes.has(regime)) {
    return res.status(400).json({ error: `regime must be one of ${[...validRegimes].join(', ')}` });
  }
  const body = req.body || {};
  // Validate each optional field individually so partial updates work.
  const updates = {};
  if (body.liquidity_param !== undefined) {
    const v = parseFloat(body.liquidity_param);
    if (!isFinite(v) || v < 0.0 || v > 1.0) {
      return res.status(400).json({ error: 'liquidity_param must be a number in [0.0, 1.0] (per-regime dampener; pairs with global lambda ≤ 2.0 to honor Reg T)' });
    }
    updates.liquidity_param = v;
  }
  // min_signal_notional_{usd,pct} removed 2026-06-08 — the per-regime
  // min-notional parameter was retired (it concentrated the book and capped
  // diversification); the sizer now uses only a fixed ~$1 dust floor.
  if (body.position_circuit_breaker_pct !== undefined) {
    const v = parseFloat(body.position_circuit_breaker_pct);
    if (!isFinite(v) || v < 0 || v > 0.5) {
      return res.status(400).json({ error: 'position_circuit_breaker_pct must be a number in [0.0, 0.5]' });
    }
    updates.position_circuit_breaker_pct = v;
  }
  if (body.min_acting_strategies !== undefined) {
    const v = Number(body.min_acting_strategies);
    // Integer range matches regime_sizer_params_min_acting_strategies_check (migration 147).
    if (!Number.isInteger(v) || v < 1 || v > 10) {
      return res.status(400).json({ error: 'min_acting_strategies must be an integer in [1, 10] (minimum distinct strategies acting in the net direction; 1 = open)' });
    }
    updates.min_acting_strategies = v;
  }
  if (body.min_corr_cum_sharpe !== undefined) {
    // Retired 2026-08-22: the S_adj floor was replaced by min_acting_strategies.
    return res.status(400).json({ error: 'min_corr_cum_sharpe is retired — set min_acting_strategies (integer 1..10) instead' });
  }
  if (Object.keys(updates).length === 0) {
    return res.status(400).json({ error: 'no valid fields to update' });
  }
  try {
    const setClauses = Object.keys(updates).map((k, i) => `${k} = $${i + 2}`).join(', ');
    const params = [regime, ...Object.values(updates)];
    const r = await dbQuery(
      `UPDATE regime_sizer_params SET ${setClauses}, updated_at = NOW() WHERE regime_state = $1 RETURNING *`,
      params
    );
    if (r.rowCount === 0) {
      return res.status(404).json({ error: `regime ${regime} not found in regime_sizer_params` });
    }
    res.json({ regime, ...updates });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// SP-7 √ln(N) threshold-proposal endpoints removed 2026-07-01 — they proposed
// the retired legacy min_cumulative_sharpe floor. The universe_threshold_proposals
// table is left intact (append-only); any proposal-creation job now writes a
// deprecated column with no consumer.

// 30-trading-day threshold derived from Alpaca's NYSE calendar.
// Cached for 1h — the calendar only changes daily and an extra CLI
// shell-out per /api/portfolio/summary request would double its latency.
let _last30TdCache = null;   // { fetchedAt: number, date: 'YYYY-MM-DD' }
const _LAST_30_TD_TTL_MS = 60 * 60 * 1000;

async function _last30TradingDayThreshold() {
  if (_last30TdCache && (Date.now() - _last30TdCache.fetchedAt) < _LAST_30_TD_TTL_MS) {
    return _last30TdCache.date;
  }
  // 50 calendar days back covers ~34 trading days (5/7 weekdays × 50 = 35.7,
  // minus typical holiday). Asking for slightly more than 30 means we
  // always have a valid 30th-most-recent date even across holiday-heavy
  // months like November/December.
  const today = new Date();
  const start = new Date(today);
  start.setDate(today.getDate() - 50);
  const fmt = d => d.toISOString().slice(0, 10);
  const resp = await runAlpaca(
    ['calendar', '--start', fmt(start), '--end', fmt(today)],
    { timeout: 10_000 },
  ).catch(() => null);
  if (!resp || !resp.ok || !Array.isArray(resp.payload) || resp.payload.length === 0) {
    return null;
  }
  const dates = resp.payload
    .map(e => e && e.date)
    .filter(Boolean)
    .sort()      // ascending YYYY-MM-DD
    .reverse();  // most recent first
  const date = dates.length >= 30 ? dates[29] : dates[dates.length - 1];
  _last30TdCache = { fetchedAt: Date.now(), date };
  return date;
}

app.get('/api/portfolio/summary', async (req, res) => {
  try {
    // Per-position win rate at close: each closed signal_pnl row counts as
    // one "position taken" (a strategy opening a ticker). Win = realized
    // pnl > 0 at close. Two windows: lifetime (all closed positions) and
    // 30 trading days (closed_at >= the 30th-most-recent NYSE session).
    // Alpaca doesn't expose a per-position win rate — verified via
    // /v2/account/* surface — so we compute from signal_pnl which IS our
    // position-close-of-record.
    //
    // 30-trading-day threshold comes from `alpaca calendar` (cached 1h);
    // if Alpaca is unreachable we fall back to a 42-calendar-day
    // approximation (~30 trading days when weekends + 1 holiday are
    // averaged out) and mark the source field so the operator sees it.
    const threshold30Td = await _last30TradingDayThreshold();
    const fallback30TdInterval = "CURRENT_DATE - INTERVAL '42 days'";
    // rolled_continuation rows are roll segments of an ongoing position
    // (SP-6 D1), not trades; excluded from stats (NULL-safe).
    const win30dSql = threshold30Td
      ? `SELECT COUNT(*) AS closed_count,
                COUNT(*) FILTER (WHERE realized_pnl_pct > 0) AS wins
           FROM signal_pnl
          WHERE status='closed' AND realized_pnl_pct IS NOT NULL
            AND close_reason IS DISTINCT FROM 'rolled_continuation'
            AND closed_at >= $1::date`
      : `SELECT COUNT(*) AS closed_count,
                COUNT(*) FILTER (WHERE realized_pnl_pct > 0) AS wins
           FROM signal_pnl
          WHERE status='closed' AND realized_pnl_pct IS NOT NULL
            AND close_reason IS DISTINCT FROM 'rolled_continuation'
            AND closed_at >= ${fallback30TdInterval}`;
    const win30dArgs = threshold30Td ? [threshold30Td] : [];
    // Broker-truth open position count via Alpaca. The previous DB-only
    // count (SELECT COUNT(*) FROM execution_signals WHERE status='open')
    // included phantom intent rows the reconcile sweep hadn't cleaned —
    // a 56-position book typically had ~1000 phantom rows, displaying
    // "1032 open" on the dashboard. Now: query Alpaca for the actual
    // position list and count rows. Fail-soft → fall back to DB count
    // if Alpaca is unreachable (operator sees something rather than '—').
    let openCountAlpaca = null;
    try {
      const apos = await runAlpaca(['position', 'list'], { timeout: 8_000 });
      if (apos && apos.ok && Array.isArray(apos.payload)) {
        openCountAlpaca = apos.payload.length;
      }
    } catch (_) { /* fall back to DB below */ }
    const [openRes, statsRes, winLifeRes, win30dRes] = await Promise.all([
      openCountAlpaca != null
        ? Promise.resolve({ rows: [{ open_count: openCountAlpaca }] })
        : dbQuery(`SELECT COUNT(DISTINCT ticker) AS open_count FROM execution_signals WHERE status = 'open' AND signal_date >= CURRENT_DATE - INTERVAL '90 days'`),
      dbQuery(`
        -- rolled_continuation rows are roll segments of an ongoing position
        -- (SP-6 D1), not trades; excluded from stats (NULL-safe).
        SELECT ROUND(AVG(realized_pnl_pct)::numeric, 4) AS avg_pnl,
               ROUND(MAX(realized_pnl_pct)::numeric, 4) AS best,
               ROUND(MIN(realized_pnl_pct)::numeric, 4) AS worst,
               ROUND(AVG(NULLIF(days_held, 0))::numeric, 2) AS avg_days_held
          FROM signal_pnl WHERE status = 'closed'
            AND close_reason IS DISTINCT FROM 'rolled_continuation'
      `),
      dbQuery(`
        -- rolled_continuation rows are roll segments of an ongoing position
        -- (SP-6 D1), not trades; excluded from stats (NULL-safe).
        SELECT COUNT(*) AS closed_count,
               COUNT(*) FILTER (WHERE realized_pnl_pct > 0) AS wins
          FROM signal_pnl
         WHERE status = 'closed' AND realized_pnl_pct IS NOT NULL
           AND close_reason IS DISTINCT FROM 'rolled_continuation'
      `),
      dbQuery(win30dSql, win30dArgs),
    ]);
    const open       = openRes.rows[0];
    const stats      = statsRes.rows[0];
    const life       = winLifeRes.rows[0];
    const w30        = win30dRes.rows[0];
    const closedLife = parseInt(life.closed_count) || 0;
    const winsLife   = parseInt(life.wins) || 0;
    const closed30   = parseInt(w30.closed_count) || 0;
    const wins30     = parseInt(w30.wins) || 0;

    res.json({
      open_count:           parseInt(open.open_count) || 0,
      // Closed-position counts: lifetime + 30-trading-day window.
      closed_count:         closedLife,             // legacy alias = lifetime
      closed_count_30d:     closed30,
      // Wins and rates split by window. win_rate retained as alias for
      // the lifetime value so any older client that only reads `win_rate`
      // still shows a sensible number.
      wins_lifetime:        winsLife,
      wins_30d:             wins30,
      win_rate_lifetime:    closedLife > 0 ? Math.round(winsLife / closedLife * 100) : null,
      win_rate_30d:         closed30   > 0 ? Math.round(wins30   / closed30   * 100) : null,
      win_rate:             closedLife > 0 ? Math.round(winsLife / closedLife * 100) : null,
      // Surface the trading-day threshold + source so the operator can
      // sanity-check the window. 'alpaca_calendar' = strict NYSE 30
      // trading days; 'calendar_42d_fallback' = Alpaca CLI unreachable,
      // approximated 30 trading days as 42 calendar days.
      win_30d_threshold:    threshold30Td || null,
      win_30d_source:       threshold30Td ? 'alpaca_calendar' : 'calendar_42d_fallback',
      avg_realized:         stats.avg_pnl,
      best_trade:           stats.best,
      worst_trade:          stats.worst,
      avg_days_held:        stats.avg_days_held,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.get('/api/portfolio/pnl-curve', async (req, res) => {
  const days = Math.min(parseInt(req.query.days) || 90, 365);
  try {
    // unrealized_pnl_pct is stored as a fraction (0.05 = 5%). Round to 4
    // decimals so the client's ×100 multiplication lands at 2 decimal-
    // places of percent without pre-rounding artifacts.
    const result = await dbQuery(`
      SELECT pnl_date,
             ROUND(AVG(unrealized_pnl_pct)::numeric, 4) AS avg_unrealized,
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

// Per-parameter data-usage list.
// Granularity: one row per individual financial parameter (e.g. close,
// implied_volatility, ebitda) instead of the parent data category.
// Sourced from data/master/schema_registry.json (the canonical column
// dictionary per parquet) plus computed columns from the data_columns
// table that don't appear in schema_registry (returns, log_returns,
// realized_vol).
//
// Identifier columns (ticker, date, …) are filtered out so the list
// only carries columns an operator would call a "financial parameter".
//
// Strategies still declare data dependencies at the parent-category
// level (e.g. parameters.required_columns = ['prices', 'options_eod']).
// To stay honest about what we know, every parameter inherits the
// category-level user count: if 14 strategies declared `prices`, all
// of close / open / high / low / volume / vwap / transactions report
// 14 users. Once strategy authors start declaring at column-level the
// counts will diverge naturally.
const DATA_CATEGORY_ORDER = [
  'prices', 'returns', 'log_returns', 'realized_vol',
  'options_eod', 'financials', 'earnings', 'insider', 'macro',
];
const DATA_PARAM_IGNORE = new Set([
  'ticker', 'date', 'datetime', 'period', 'source',
  'transaction_date', 'filing_date', 'last_updated',
  'id', 'symbol', 'raw', 'cusip',
]);
const DATA_PARAM_DESC = {
  // prices
  open: 'Session open price', high: 'Intraday high', low: 'Intraday low',
  close: 'Session close price', volume: 'Daily trading volume',
  vwap: 'Volume-weighted average price', transactions: 'Trade count',
  // options_eod
  expiry: 'Option expiration date', strike: 'Option strike price',
  option_type: 'Call vs put',
  market_price: 'Last option price',
  implied_volatility: 'Black–Scholes implied volatility',
  delta: 'Option delta (∂price/∂spot)',
  gamma: 'Option gamma (∂²price/∂spot²)',
  theta: 'Option theta (decay)',
  vega:  'Option vega (∂price/∂σ)',
  rho:   'Option rho (∂price/∂rate)',
  open_interest: 'Open contracts outstanding',
  bid: 'Best bid', ask: 'Best ask',
  // financials
  revenue: 'Quarterly revenue', gross_profit: 'Gross profit',
  ebitda: 'EBITDA', net_income: 'Net income', eps: 'Earnings per share',
  gross_margin: 'Gross margin', operating_margin: 'Operating margin',
  net_margin: 'Net margin', revenue_growth: 'YoY revenue growth',
  ev_revenue: 'Enterprise value / revenue',
  ev_ebitda:  'Enterprise value / EBITDA',
  pe_ratio: 'Price / earnings', market_cap: 'Market capitalization',
  roe: 'Return on equity', roic: 'Return on invested capital',
  debt_equity_ratio: 'Debt / equity',
  p_fcf_ratio: 'Price / free cash flow',
  // earnings
  eps_actual: 'Reported EPS',
  eps_estimated: 'Consensus EPS estimate',
  revenue_actual: 'Reported revenue',
  revenue_estimated: 'Consensus revenue estimate',
  // insider
  insider_name: 'Filer name', role: 'Filer role at issuer',
  transaction_type: 'Buy / sell / award / option exercise',
  shares: 'Shares transacted',
  price_per_share: 'Filing price per share',
  net_value: 'Notional value of filing',
  shares_owned_after: 'Position after filing',
  // macro
  series: 'FRED / economic series id',
  value:  'Series observation',
  // computed
  returns: 'Daily simple returns', log_returns: 'Daily log returns',
  realized_vol: 'Realized volatility (rolling)',
};

app.get('/api/data/usage', async (req, res) => {
  try {
    const fs2   = require('fs');
    const path2 = require('path');

    // Category-level metadata from data_columns.
    const colsRes = await dbQuery(`
      SELECT column_name AS category, provider, refresh_cadence,
             min_date, max_date, row_count, ticker_count
      FROM data_columns
    `);
    const catMeta = {};
    for (const r of colsRes.rows) {
      const minDate = r.min_date ? r.min_date.toISOString().slice(0, 10) : null;
      const maxDate = r.max_date ? r.max_date.toISOString().slice(0, 10) : null;
      const days = (minDate && maxDate)
        ? Math.round((Date.parse(maxDate) - Date.parse(minDate)) / 86400000) + 1
        : 0;
      catMeta[r.category] = {
        provider:        r.provider || null,
        refresh_cadence: r.refresh_cadence || null,
        min_date:        minDate,
        max_date:        maxDate,
        history_days:    days,
        row_count:       parseInt(r.row_count)    || 0,
        ticker_count:    parseInt(r.ticker_count) || 0,
      };
    }

    // Schema registry: per-category list of fine-grained columns.
    let registry = {};
    try {
      registry = JSON.parse(fs2.readFileSync(
        path2.join(__dirname, '../../../data/master/schema_registry.json'), 'utf8'));
    } catch (_) {}

    // Per-strategy declared categories — canonical source is each strategy's
    // .requirements.json on disk (the same source the staging approver uses).
    // strategy_registry.parameters.required_columns / .data_requirements_planned
    // are stale on most rows (only 27/106 had data as of 2026-05-19), so
    // they'd undercount users. We read manifest.json for state filtering
    // and walk the on-disk requirements files for true required+optional.
    //
    // "Active stack" = live / candidate / staging / monitoring. Deprecated
    // and archived strategies don't count as users — they no longer drive
    // demand for the data column, so the operator should see the live
    // dependency picture, not the historical one.
    const _USAGE_ACTIVE_STATES = new Set(['live', 'candidate', 'staging', 'monitoring']);
    const { _internals: _stgInternalsForUsage } = require('../../agent/approvals/staging_approver');
    const _readReqs = _stgInternalsForUsage.readRequirements;
    let _manifestForUsage;
    try {
      _manifestForUsage = JSON.parse(fs2.readFileSync(
        path2.join(__dirname, '../../strategies/manifest.json'), 'utf8'));
    } catch (_) { _manifestForUsage = { strategies: {} }; }
    const usersByCategory = {};
    let strategiesTotal = 0;
    for (const [sid, rec] of Object.entries(_manifestForUsage.strategies || {})) {
      if (!_USAGE_ACTIVE_STATES.has(rec.state)) continue;
      strategiesTotal++;
      let reqs;
      try { reqs = _readReqs(sid); } catch (_) { reqs = { required: [], optional: [] }; }
      const cats = [...new Set([
        ...(Array.isArray(reqs.required) ? reqs.required : []),
        ...(Array.isArray(reqs.optional) ? reqs.optional : []),
      ])].filter(c => typeof c === 'string');
      for (const c of cats) {
        if (!usersByCategory[c]) usersByCategory[c] = [];
        usersByCategory[c].push(sid);
      }
    }
    for (const k of Object.keys(usersByCategory)) usersByCategory[k].sort();

    // Build the flat parameter list:
    //   1. Walk schema_registry → emit one parameter per non-identifier column
    //   2. Append computed-only categories from data_columns (returns,
    //      log_returns, realized_vol) which don't appear in the registry
    const params = [];
    const seenCats = new Set();
    for (const cat of Object.keys(registry)) {
      seenCats.add(cat);
      const cols = (registry[cat] && Array.isArray(registry[cat].columns)) ? registry[cat].columns : [];
      for (const col of cols) {
        if (DATA_PARAM_IGNORE.has(col)) continue;
        const meta = catMeta[cat] || {};
        params.push({
          name:          col,
          category:      cat,
          description:   DATA_PARAM_DESC[col] || null,
          provider:      meta.provider     || null,
          refresh_cadence: meta.refresh_cadence || null,
          history_days:  meta.history_days || 0,
          min_date:      meta.min_date     || null,
          max_date:      meta.max_date     || null,
          row_count:     meta.row_count    || 0,
          ticker_count:  meta.ticker_count || 0,
          user_count:    (usersByCategory[cat] || []).length,
          users:         (usersByCategory[cat] || []).slice(0, 50),
        });
      }
    }
    // Computed/derived columns tracked in data_columns but not in the
    // schema registry (they have no parquet schema — they live as
    // post-collection feature columns).
    for (const r of colsRes.rows) {
      if (seenCats.has(r.category)) continue;
      const meta = catMeta[r.category] || {};
      params.push({
        name:          r.category,
        category:      'computed',
        description:   DATA_PARAM_DESC[r.category] || null,
        provider:      meta.provider     || 'computed',
        refresh_cadence: meta.refresh_cadence || null,
        history_days:  meta.history_days || 0,
        min_date:      meta.min_date     || null,
        max_date:      meta.max_date     || null,
        row_count:     meta.row_count    || 0,
        ticker_count:  meta.ticker_count || 0,
        user_count:    (usersByCategory[r.category] || []).length,
        users:         (usersByCategory[r.category] || []).slice(0, 50),
      });
    }

    // Default sort: rank desc by user_count, ties broken by the canonical
    // category order (so prices columns float to the top of equal-count
    // ties), then by parameter name. Client may re-sort.
    const catRank = {};
    DATA_CATEGORY_ORDER.forEach((c, i) => { catRank[c] = i; });
    params.sort((a, b) => {
      const d = (b.user_count || 0) - (a.user_count || 0);
      if (d !== 0) return d;
      const ra = catRank[a.category] != null ? catRank[a.category] : 999;
      const rb = catRank[b.category] != null ? catRank[b.category] : 999;
      if (ra !== rb) return ra - rb;
      return a.name.localeCompare(b.name);
    });

    res.json({
      parameters:        params,
      strategies_total:  strategiesTotal,
      categories_total:  Object.keys(catMeta).length,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
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
    // strategy_stats is retained SOLELY to discover orphan strategy_ids
    // (signals but no manifest entry). Its metric columns are no longer read
    // — every per-strategy metric is now backtest-sourced (Task 7 rewrite).
    const statsRows = (await dbQuery(`SELECT strategy_id FROM strategy_stats`)).rows;

    // Load regime_conditions + backtest/live metrics from strategy_registry DB.
    // Saturday-brain additions (058): data_requirements_planned (the missing
    // data spec the dashboard renders for STAGING strategies) and
    // staging_approved_at (operator's data-fetch approval timestamp).
    const srRows = (await dbQuery(`
      SELECT id, status, regime_conditions,
             backtest_sharpe, backtest_return_pct, backtest_max_dd_pct, backtest_trade_count,
             backtest_regime_breakdown,
             data_requirements_planned, staging_approved_at
      FROM strategy_registry
    `)).rows;
    const srById = Object.fromEntries(srRows.map(r => [r.id, r]));

    // Phase 2A canonical eligibility = strategy_regime_params DB rows. The
    // live regime-blended sizer reads these to filter strategies per regime;
    // the dashboard must display the same truth so the operator sees what
    // the live stack actually trades. Map: sid → { regime: eligible_bool }.
    const regimeParamsById = {};
    try {
      const rpRows = (await dbQuery(`
        SELECT strategy_id, regime_state, eligible
          FROM strategy_regime_params
      `)).rows;
      for (const r of rpRows) {
        regimeParamsById[r.strategy_id] = regimeParamsById[r.strategy_id] || {};
        regimeParamsById[r.strategy_id][r.regime_state] = r.eligible;
      }
    } catch (_) { /* table missing in tests / pre-Phase-2A — fall back to manifest */ }

    // Unified-backtest (2026-05-14) is the canonical source-of-truth for
    // backtest metrics. We read the latest primary_window=true run per
    // strategy and the per-regime breakdown, then merge into a {sharpe,
    // max_dd_pct, return_pct, trade_count, regime_breakdown} map keyed by
    // strategy_id. Strategies that haven't been backfilled yet fall back
    // to strategy_registry's legacy columns (which still get populated by
    // auto_backtest until the research-orch migration in phase 2.3).
    const ubtRunRows = (await dbQuery(`
      SELECT DISTINCT ON (strategy_id)
             strategy_id, total_sharpe, total_max_dd_pct, total_return_pct,
             total_trades, total_hit_rate, avg_holding_days, run_at,
             total_sortino, total_calmar, total_avg_pnl_pct
      FROM strategy_backtest_runs
      WHERE primary_window = TRUE
      ORDER BY strategy_id, run_at DESC
    `).catch(() => ({ rows: [] }))).rows;
    const ubtRunById = Object.fromEntries(ubtRunRows.map(r => [r.strategy_id, r]));
    const ubtRegimeRows = (await dbQuery(`
      SELECT r.strategy_id, br.regime_state, br.trade_count, br.sharpe,
             br.max_dd_pct, br.return_pct, br.hit_rate,
             br.avg_pnl_pct, br.avg_holding_days, br.oos_days_in_regime,
             br.sortino, br.calmar
      FROM strategy_backtest_regimes br
      JOIN (
        SELECT DISTINCT ON (strategy_id) strategy_id, run_id
        FROM strategy_backtest_runs WHERE primary_window = TRUE
        ORDER BY strategy_id, run_at DESC
      ) r ON r.run_id = br.run_id
    `).catch(() => ({ rows: [] }))).rows;
    const unifiedBacktest = {};
    for (const r of ubtRunRows) {
      unifiedBacktest[r.strategy_id] = {
        sharpe:      r.total_sharpe,
        sortino:     r.total_sortino,
        calmar:      r.total_calmar,
        avg_pnl_pct: r.total_avg_pnl_pct,
        return_pct:  r.total_return_pct,
        max_dd_pct:  r.total_max_dd_pct,
        trade_count: r.total_trades,
        run_at:      r.run_at,
        regime_breakdown: {},
      };
    }
    for (const r of ubtRegimeRows) {
      const entry = unifiedBacktest[r.strategy_id];
      if (!entry) continue;
      entry.regime_breakdown[r.regime_state] = {
        sharpe:      r.sharpe,
        max_dd:      r.max_dd_pct != null ? r.max_dd_pct / 100 : null,  // breakdown JSON uses fraction
        total_return_pct: r.return_pct,
        trade_count: r.trade_count,
        hit_rate:    r.hit_rate,
        // Raw per-regime fields consumed by blendScope (percent units kept as-is):
        max_dd_pct:        r.max_dd_pct,
        return_pct:        r.return_pct,
        avg_pnl_pct:       r.avg_pnl_pct,
        avg_holding_days:  r.avg_holding_days,
        oos_days_in_regime: r.oos_days_in_regime,
        // Per-regime risk metrics for the expanded "Backtest by Regime" grid
        // (operator 2026-07-27: Calmar/maxDD surfaced alongside the DD-gate hatch).
        sortino:           r.sortino,
        calmar:            r.calmar,
      };
    }

    // ── Chosen-universe overlay (universe ladder campaign W4, 2026-07-21) ──
    // When the shrink pass has selected+marked a ladder tier for a strategy's
    // primary run (universe_shrink_metrics.chosen), the dashboard must show
    // THAT universe's metrics, not the full-universe run numbers. Overlay
    // both the run-level map (ubtRunById feeds _allScope) and the per-regime
    // breakdown (feeds blendScope / regime chips). Full-universe values remain
    // in strategy_backtest_runs; `universe_tier` marks overlaid entries.
    // avg_pnl / best-worst stay trade-level from the full run (not persisted
    // per tier); oos_days_in_regime is universe-independent so it is kept.
    const shrinkChosenRows = (await dbQuery(`
      SELECT m.strategy_id, m.tier, m.regime_state, m.sharpe, m.max_dd_pct,
             m.trade_count, m.win_rate, m.sortino, m.calmar,
             m.mean_holding_days, m.return_pct
        FROM universe_shrink_metrics m
        JOIN (SELECT DISTINCT ON (strategy_id) strategy_id, run_id
                FROM strategy_backtest_runs WHERE primary_window = TRUE
               ORDER BY strategy_id, run_at DESC) r ON r.run_id = m.run_id
       WHERE m.chosen
    `).catch(() => ({ rows: [] }))).rows;
    for (const m of shrinkChosenRows) {
      const entry = unifiedBacktest[m.strategy_id];
      const run   = ubtRunById[m.strategy_id];
      if (!entry || !run) continue;
      if (m.regime_state === 'TOTAL') {
        run.total_sharpe     = m.sharpe;
        run.total_max_dd_pct = m.max_dd_pct;
        run.total_return_pct = m.return_pct;
        run.total_trades     = m.trade_count;
        run.total_hit_rate   = m.win_rate;
        run.total_sortino    = m.sortino;
        run.total_calmar     = m.calmar;
        run.avg_holding_days = m.mean_holding_days;
        Object.assign(entry, {
          sharpe: m.sharpe, sortino: m.sortino, calmar: m.calmar,
          return_pct: m.return_pct, max_dd_pct: m.max_dd_pct,
          trade_count: m.trade_count, universe_tier: m.tier,
        });
      } else {
        const prior = entry.regime_breakdown[m.regime_state] || {};
        entry.regime_breakdown[m.regime_state] = {
          sharpe:      m.sharpe,
          max_dd:      m.max_dd_pct != null ? m.max_dd_pct / 100 : null,
          total_return_pct: m.return_pct,
          trade_count: m.trade_count,
          hit_rate:    m.win_rate,
          max_dd_pct:        m.max_dd_pct,
          return_pct:        m.return_pct,
          avg_pnl_pct:       null,   // not persisted per tier
          avg_holding_days:  m.mean_holding_days,
          oos_days_in_regime: prior.oos_days_in_regime ?? null,
          universe_tier:     m.tier,
          sortino:           m.sortino,
          calmar:            m.calmar,
        };
      }
    }

    // Decorate every breakdown entry with `declared` so the dashboard can
    // render BT Sharpe for ALL traded regimes (including non-declared) and
    // still show eligibility independently. eligibleSet = manifest's
    // literature-driven eligible_regimes; falls back to active_in_regimes
    // when manifest hasn't set it. NULL eligible_regimes is treated as
    // eligible-everywhere (matches regime_gate.is_eligible() semantics).
    const _decorateDeclared = (breakdown, eligibleSet) => {
      if (!breakdown) return breakdown;
      const out = {};
      for (const [rg, b] of Object.entries(breakdown)) {
        if (b == null) { out[rg] = b; continue; }
        out[rg] = Object.assign({}, b, {
          declared: b.declared != null ? b.declared : (eligibleSet ? eligibleSet.has(rg) : true),
        });
      }
      return out;
    };

    // Resolve which `data_requirements_planned` columns are NOT known to any
    // provider/collector. These rows would be rejected by the staging worker
    // with `error: 'unsupported_source'`. We surface them on staging rows as
    // a ⚠ data badge so the operator sees the gap before clicking Approve.
    const { _internals: _stgInternals } = require('../../agent/approvals/staging_approver');
    const _schemaReg = _stgInternals.readSchemaRegistry();
    const unsupportedByStrategyId = {};
    for (const sr of srRows) {
      let planned = sr.data_requirements_planned;
      if (!planned) continue;
      if (typeof planned === 'string') {
        try { planned = JSON.parse(planned); } catch { continue; }
      }
      if (!Array.isArray(planned) || planned.length === 0) continue;
      const columns = [...new Set(planned.map(p => p?.column || p?.data_type).filter(Boolean))];
      const bad = [];
      for (const c of columns) {
        if (!await _stgInternals.sourceIsKnownToProvider(c, _schemaReg, dbQuery)) bad.push(c);
      }
      if (bad.length) unsupportedByStrategyId[sr.id] = bad;
    }

    // Current regime state from latest.json (authoritative)
    // 2026-05-19: prior code used `path.join(...)` but `path` is not imported
    // at module scope (only inline as `require('path')` for REGIME_FILE). The
    // ReferenceError was caught silently and the endpoint always returned
    // the TRANSITIONING fallback, even though the file said LOW_VOL. Use the
    // already-resolved REGIME_FILE constant — same target, no inline join.
    let currentRegime = 'TRANSITIONING';
    try {
      const latestJson = JSON.parse(fs.readFileSync(REGIME_FILE, 'utf8'));
      currentRegime = latestJson.state || 'TRANSITIONING';
    } catch (e) {
      console.warn(`[regime-sizing] regime file read failed: ${e.message}`);
    }

    // Crypto regime — loaded once before the badge loop; null on any error
    // (best-effort, mirrors how currentRegime is loaded above). When null,
    // regimeForStrategy falls back to the equity regime for crypto strategies.
    let cryptoRegime = null;
    try {
      const cryptoJson = JSON.parse(fs.readFileSync(CRYPTO_REGIME_FILE, 'utf8'));
      cryptoRegime = cryptoJson.state || null;
    } catch (e) {
      // best-effort: no crypto regime file → cryptoRegime stays null → fallback to equity
    }

    // ── Backtest dashboard panel (precomputed; strategy_backtest_panel) ──
    // Effective Sharpe + GBM-σ OUE counts (overall + per regime). This
    // REPLACES the prior live OUE (execution_signals.oue_kind), the live
    // per-regime aggregates (signal_pnl × execution_signals), and the live
    // strategy_weights_by_regime OUE multiplier — every per-strategy metric
    // is now backtest-sourced (only last_signal_date + status stay live).
    const panelRows = (await dbQuery(`
      SELECT strategy_id, effective_sharpe, oue_over, oue_under, oue_expected, oue_by_regime
        FROM strategy_backtest_panel`).catch(() => ({ rows: [] }))).rows;
    const panelById = Object.fromEntries(panelRows.map(r => [r.strategy_id, r]));

    // Best/worst realized trade % from the latest primary_window backtest run.
    const bwRows = (await dbQuery(`
      SELECT t.strategy_id, MAX(t.pnl_pct) AS best, MIN(t.pnl_pct) AS worst,
             AVG(t.pnl_pct) AS avg_pnl
        FROM strategy_backtest_trades t
        JOIN (SELECT DISTINCT ON (strategy_id) strategy_id, run_id
                FROM strategy_backtest_runs WHERE primary_window=TRUE
                ORDER BY strategy_id, run_at DESC) r ON r.run_id=t.run_id
       GROUP BY t.strategy_id`).catch(() => ({ rows: [] }))).rows;
    const bwById = Object.fromEntries(bwRows.map(r => [r.strategy_id, r]));

    // Last signal date — the ONLY live per-strategy fact retained (drives
    // is_stale + the Last Signal column).
    const lsRows = (await dbQuery(`
      SELECT strategy_id, MAX(signal_date) AS last_signal_date
        FROM execution_signals WHERE strategy_id IS NOT NULL GROUP BY strategy_id`
      ).catch(() => ({ rows: [] }))).rows;
    const lastSignalById = Object.fromEntries(lsRows.map(r => [r.strategy_id, r.last_signal_date]));

    // is_stale: manifest-active, regime-active, but hasn't produced a signal in N days.
    const STALE_DAYS = 7;
    const staleCutoff = Date.now() - STALE_DAYS * 24 * 3600 * 1000;

    const rows = [];
    const seen = new Set();
    for (const [sid, rec] of Object.entries(manifest.strategies || {})) {
      seen.add(sid);
      const sr  = srById[sid]    || {};
      const rc  = sr.regime_conditions || {};
      const activeRegimes = rc.active_in_regimes || ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL'];
      // regime_active mirrors the LIVE engine gate (regime_param_resolver.is_eligible
      // over strategy_regime_params), NOT code-level active_in_regimes — so the status
      // badge + staleness match what actually trades. Previously a strategy toggled to
      // e.g. CRISIS-only still showed LIVE in a LOW_VOL market while the engine skipped
      // it. regimeParamsById[sid] is the same source the live sizer reads. See
      // regime_active.js. Crypto strategies are gated on the crypto regime (D4).
      const _regime      = regimeForStrategy(rec.instrument_class, currentRegime, cryptoRegime);
      const regimeActive = isRegimeEligibleNow(regimeParamsById[sid], _regime);
      // last_signal_date is now sourced from execution_signals (lastSignalById)
      // so is_stale and the Last Signal column share one live source.
      const _lastSig = lastSignalById[sid] || null;
      const lastTs   = _lastSig ? new Date(_lastSig).getTime() : 0;
      const isStale  = regimeActive && (!lastTs || lastTs < staleCutoff);
      // Eligibility precedence: strategy_regime_params (Phase 2A DB
      // source-of-truth, what the live sizer enforces) → manifest
      // eligible_regimes (legacy, may still be set on unmigrated rows) →
      // active_in_regimes (literature default). _CANON_AXIS preserves
      // canonical order for stable diffs across renders.
      const _CANON_AXIS = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
      const _rpFor = regimeParamsById[sid] || null;
      const _rpDerived = _rpFor && Object.keys(_rpFor).length
        ? _CANON_AXIS.filter(rg => _rpFor[rg] === true)
        : null;
      const _eligRaw   = _rpDerived !== null
        ? _rpDerived
        : (Array.isArray(rec.eligible_regimes) ? rec.eligible_regimes : null);
      const _eligibleSet = new Set(_eligRaw || activeRegimes);
      const _rawBreakdown = unifiedBacktest[sid]?.regime_breakdown ?? sr.backtest_regime_breakdown ?? null;
      const _decoratedBreakdown = _decorateDeclared(_rawBreakdown, _eligibleSet);
      // ── Regime-scoped metric variants (Active Stack filter) ──────────────
      // ALL mirrors the legacy top-level fields exactly (built from the same
      // run/panel/bestWorst sources strategy_row.js uses). ELIGIBLE +
      // single-regime come from blendScope over the raw per-regime breakdown.
      const _runForScope   = ubtRunById[sid] || {};
      const _panelForScope = panelById[sid] || null;
      const _bwForScope    = bwById[sid] || {};
      const _actAll = _runForScope.avg_holding_days != null ? Number(_runForScope.avg_holding_days) : null;
      const _arrAll = _bwForScope.avg_pnl != null ? Number(_bwForScope.avg_pnl) * 100 : null;
      const _allScope = {
        sharpe:           _runForScope.total_sharpe ?? null,
        effective_sharpe: _panelForScope?.effective_sharpe ?? null,
        return_pct:       _runForScope.total_return_pct ?? null,
        max_dd_pct:       _runForScope.total_max_dd_pct ?? null,
        closed_count:     _runForScope.total_trades ?? 0,
        win_rate:         _runForScope.total_hit_rate ?? null,
        arr_pct:          _arrAll,
        adr_pct:          (_arrAll != null && _actAll) ? (_arrAll / Math.max(1, _actAll)) : null,
        act_days:         _actAll,
      };
      const _metricsByScope = { ALL: _allScope };
      for (const _rg of _CANON_AXIS) {
        const _s = blendScope(_rawBreakdown, [_rg]);
        if (_s) _metricsByScope[_rg] = _s;
      }
      // ELIGIBLE: blend over the eligible set; null eligRaw (eligible-everywhere) ⇒ ALL.
      const _eligScope = _eligRaw ? blendScope(_rawBreakdown, _eligRaw) : null;
      _metricsByScope.ELIGIBLE = _eligScope || _allScope;
      const _defaultScope = _eligRaw ? 'ELIGIBLE' : 'ALL';
      rows.push({
        ...buildStrategyRow({
          sid, rec, isStale, regimeActive, activeRegimes, eligRaw: _eligRaw, currentRegime: _regime,
          run: ubtRunById[sid] || {},
          regimeBreakdown: _decoratedBreakdown,
          panel: panelById[sid] || null,
          bestWorst: bwById[sid] || {},
          lastSignalDate: _lastSig,
          metricsByScope: _metricsByScope,
          defaultScope: _defaultScope,
          registryStatus: (srById[sid] || {}).status ?? null,
        }),
        // Carry-overs the active-stack rewrite (Tasks 9/10) does NOT cover but
        // the CANDIDATE-table renderer (_renderCandidates) still reads. The
        // mapper already emits backtest_return_pct / backtest_max_dd_pct /
        // backtest_regime_breakdown; it does NOT emit these two, so restore
        // them (same unified→legacy fallback) to keep the candidate table's
        // BT Sharpe + Backtest Trades columns + sort keys working.
        backtest_sharpe:           unifiedBacktest[sid]?.sharpe      ?? sr.backtest_sharpe      ?? null,
        backtest_sortino:          unifiedBacktest[sid]?.sortino     ?? null,
        backtest_calmar:           unifiedBacktest[sid]?.calmar      ?? null,
        backtest_avg_pnl_pct:      unifiedBacktest[sid]?.avg_pnl_pct ?? null,
        backtest_trade_count:      unifiedBacktest[sid]?.trade_count ?? sr.backtest_trade_count ?? null,
        backtest_return_pct:       unifiedBacktest[sid]?.return_pct  ?? sr.backtest_return_pct  ?? null,
        backtest_max_dd_pct:       unifiedBacktest[sid]?.max_dd_pct  ?? sr.backtest_max_dd_pct  ?? null,
        // W4: non-null iff the backtest_* fields above are CHOSEN-universe
        // (shrink) numbers rather than the full-universe run's.
        universe_tier:             unifiedBacktest[sid]?.universe_tier ?? null,
        // Non-metric carry-overs preserved for the staging UI (NOT backtest
        // metrics): lifecycle timestamp + Saturday-brain staging fields that
        // drive the candidate table's Approve flow + ⚠ data badge.
        state_since:               rec.state_since || null,
        data_requirements_planned: sr.data_requirements_planned || null,
        staging_approved_at:       sr.staging_approved_at || null,
        unsupported_sources:       unsupportedByStrategyId[sid] || null,
      });
    }
    // Orphans: strategy_ids with signals but no manifest entry. statsRows
    // (Q1 strategy_stats) is retained SOLELY to discover these — its metric
    // columns are no longer read (every metric is now backtest-sourced).
    // Orphans route through the same backtest mapper with a synthetic rec.
    for (const s of statsRows) {
      if (seen.has(s.strategy_id)) continue;
      const sid = s.strategy_id;
      const sr  = srById[sid] || {};
      const _orphanBreakdown = _decorateDeclared(
        unifiedBacktest[sid]?.regime_breakdown ?? sr.backtest_regime_breakdown ?? null,
        null,  // orphans have no manifest → no declared eligibility
      );
      rows.push({
        ...buildStrategyRow({
          sid, rec: { state: 'orphan', metadata: {} },
          isStale: false, regimeActive: true, activeRegimes: [], eligRaw: null,
          currentRegime,
          run: ubtRunById[sid] || {},
          regimeBreakdown: _orphanBreakdown,
          panel: panelById[sid] || null,
          bestWorst: bwById[sid] || {},
          lastSignalDate: lastSignalById[sid] || null,
        }),
        backtest_sharpe:      unifiedBacktest[sid]?.sharpe      ?? sr.backtest_sharpe      ?? null,
        backtest_trade_count: unifiedBacktest[sid]?.trade_count ?? sr.backtest_trade_count ?? null,
        backtest_return_pct:  unifiedBacktest[sid]?.return_pct  ?? sr.backtest_return_pct  ?? null,
        backtest_max_dd_pct:  unifiedBacktest[sid]?.max_dd_pct  ?? sr.backtest_max_dd_pct  ?? null,
      });
    }
    // Server-side default order: highest backtest Sharpe first, nulls last.
    // (The candidate table is client-sortable via _bindSortable; when no
    // column sort is active _renderCandidates re-sorts by strategy_id, so
    // this sets the JSON/source order, not the table's visible default.)
    rows.sort((a, b) =>
      (b.backtest_sharpe ?? -Infinity) - (a.backtest_sharpe ?? -Infinity));
    res.json(rows);
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Current-regime strategy similarity matrix (operator 2026-07-27): powers the
// expanded panel's "Similar Active Strategies" ordering + pairwise-ρ column.
// Same store the sizer's corr/tangency S_adj reads (strategy_similarity_matrix,
// is_current row for the regime of record). {regime, computed_at, matrix} —
// matrix = {sid: {sid: rho}}. Fail-soft: empty matrix on any error.
app.get('/api/strategy-similarity', async (_req, res) => {
  let currentRegime = 'TRANSITIONING';
  try {
    const latestJson = JSON.parse(fs.readFileSync(REGIME_FILE, 'utf8'));
    currentRegime = latestJson.state || 'TRANSITIONING';
  } catch { /* fallback regime */ }
  try {
    const r = await dbQuery(`
      SELECT regime_state, matrix, computed_at
        FROM strategy_similarity_matrix
       WHERE regime_state = $1 AND is_current
       ORDER BY computed_at DESC LIMIT 1`, [currentRegime]);
    const row = r.rows[0] || null;
    res.json({
      regime:      currentRegime,
      computed_at: row ? row.computed_at : null,
      matrix:      row ? (row.matrix || {}) : {},
    });
  } catch (err) {
    res.json({ regime: currentRegime, computed_at: null, matrix: {}, error: err.message });
  }
});

// Strategy lifecycle transition (manual dashboard actions).
// Body: { to_state, force?, reason?, actor? }
//
// Lifecycle (post fused-staging-approval rewrite, 2026-04-27):
//   staging → candidate    fused worker only (no manual)
//   candidate → live       operator click; sharpe/dd gated
//   PAPER is frozen-legacy. Existing PAPER manifest rows were migrated to
//   CANDIDATE by scripts/migrate_paper_to_candidate.py. The transitions
//   below keep `paper:archived` as an escape hatch for any orphaned rows.
const STRATEGY_VALID_TRANSITIONS = new Map([
  ['staging:candidate',     'fused approval: backfill + strategycoder + backtest complete'],
  ['staging:archived',      'archive staging without going live'],
  ['candidate:live',        'promote to live after passing backtest guards'],
  ['candidate:staging',     'regress candidate — needs additional data sources'],
  ['candidate:archived',    'abandon without going live'],
  ['paper:archived',        'archive legacy paper row'],
  ['live:candidate',        'pull back from live to candidate to re-observe backtest metrics'],
  ['live:monitoring',       'escalate to monitoring'],
  ['live:deprecated',       'demote from live'],
  ['monitoring:live',       'restore confidence, back to live'],
  ['monitoring:deprecated', 'demote from monitoring'],
  ['deprecated:archived',   'archive after review period'],
]);
// candidate→live Sharpe/DD thresholds moved into the shared promotion service
// (src/lib/promotion_service.js PROMOTION_THRESHOLDS.equity = 0.5 / 20). The old
// CANDIDATE_TO_LIVE_MIN_SHARPE / _MAX_DD_PCT module constants were deleted in
// W4-T3-C3 when /transition was refactored to call that class-aware gate.

app.post('/api/strategies/:id/transition', async (req, res) => {
  const fs   = require('fs');
  const path = require('path');
  const sid     = req.params.id;
  const toState = req.body && req.body.to_state;
  const force   = req.body && req.body.force === true;
  const reason  = (req.body && req.body.reason) || '';
  const actor   = (req.body && req.body.actor)  || 'manual:dashboard';
  // Batch callers (Sunday auto-approval / backlog flush) promote many
  // strategies in a loop and trigger ONE weights rebuild at the end —
  // per-transition fire-and-forget rebuilds racing each other corrupted the
  // is_current snapshot generation (2026-07-13 lesson: 247 duplicated pairs).
  const skipWeightsRebuild = req.body && req.body.skip_weights_rebuild === true;
  // sid feeds a shell one-liner below (activation chain) — hard-validate the
  // charset even though unknown sids 404 against the manifest first.
  if (!/^[A-Za-z0-9_.\-]+$/.test(String(sid || ''))) {
    return res.status(400).json({ error: 'invalid strategy id' });
  }
  // Optional: caller (typically the approval picker on the dashboard) can
  // overwrite the strategy's manifest.eligible_regimes atomically with the
  // state transition. NULL/undefined → leave existing eligible_regimes
  // untouched. Validated against the canonical 4-regime axis below.
  const _CANON_REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
  let eligibleRegimes = null;
  if (req.body && Array.isArray(req.body.eligible_regimes)) {
    const cleaned = [...new Set(req.body.eligible_regimes)].filter(r => _CANON_REGIMES.includes(r));
    if (cleaned.length === 0) {
      return res.status(400).json({ error: 'eligible_regimes cannot be empty' });
    }
    // Preserve canonical order for stable diffing.
    eligibleRegimes = _CANON_REGIMES.filter(r => cleaned.includes(r));
  }

  if (!sid || !toState) {
    return res.status(400).json({ error: 'sid and to_state are required' });
  }

  const manifestPath = path.join(__dirname, '../../strategies/manifest.json');
  let manifest;
  try {
    manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  } catch (e) {
    return res.status(500).json({ error: 'Cannot read manifest: ' + e.message });
  }

  const rec = (manifest.strategies || {})[sid];
  if (!rec) return res.status(404).json({ error: `Strategy ${sid} not found in manifest` });

  const fromState = rec.state;
  const tKey = `${fromState}:${toState}`;

  // Dashboard callers can't directly promote staging — they must use
  // POST /api/strategies/:id/approve, which runs the fused approval pipeline
  // (backfill + strategycoder + backtest) and then calls this endpoint as a
  // system actor (actor starts with 'system:') to finalize. The candidate→live
  // gate stays manual (sharpe/dd guarded below).
  const isSystemActor = typeof actor === 'string' && actor.startsWith('system:');
  if (!isSystemActor) {
    if (fromState === 'staging') {
      const forwardTargets = new Set(['candidate', 'live', 'monitoring']);
      if (forwardTargets.has(toState)) {
        return res.status(409).json({
          error: `Use POST /api/strategies/${sid}/approve — staging strategies can't be promoted directly.`,
        });
      }
    }
  }

  if (!STRATEGY_VALID_TRANSITIONS.has(tKey)) {
    const validDests = [...STRATEGY_VALID_TRANSITIONS.keys()]
      .filter(k => k.startsWith(fromState + ':'))
      .map(k => k.split(':')[1]);
    return res.status(422).json({
      error: `No valid path from '${fromState}' to '${toState}'`,
      valid_destinations: validDests,
    });
  }

  // ── Class-aware promotion gate + C7 registry-first transition core ────────
  // Both this dashboard route AND the Discord /approve-strategy path now run
  // through the shared promotion service (src/lib/promotion_service.js), so the
  // engine's trade-gate (strategy_registry.status='approved') is reachable ONLY
  // through the same class-aware Sharpe/DD gate. Behavior-preserving for equity
  // (thresholds 0.5/20 == the old CANDIDATE_TO_LIVE_* constants, same backtest-
  // runs→registry fallback read, same registry-first-then-manifest order); the
  // only intended change is that instrument_class now selects per-class
  // thresholds (option 0.80/30, crypto 0.50/70). The gate fires inside
  // transitionStrategy when gateApplies (candidate→live) and !force, returning
  // failedGates with NOTHING written — same as the old 422-before-write path.
  const { transitionStrategy } = require('../../lib/promotion_service');
  const instrumentClass = rec.instrument_class || 'equity';

  const result = await transitionStrategy({
    dbQuery, manifestPath, sid, toState, fromState, force,
    actor,
    // Preserve the route's lifecycle_events.reason default: when the operator
    // gives no reason, the human-readable transition description is used (e.g.
    // 'promote to live after passing backtest guards'), not the arrow form.
    reason: reason || STRATEGY_VALID_TRANSITIONS.get(tKey),
    instrumentClass,
    gateApplies: (tKey === 'candidate:live'),
    // Per-activated-regime Sharpe gate (2026-07-13): the picker's chosen set
    // is the activation the gate judges; absent → transitionStrategy derives
    // it from the assigner's manifest metadata.
    eligibleRegimes,
    // Dashboard-only manifest mutation, run inside the same lock: drop the
    // now-DB-owned eligible_regimes copy (else the doctor's
    // manifest_eligibility_drift check FAILs) and fold before/after into the
    // lifecycle event metadata so /api/lifecycle/audit shows the decision.
    manifestMutator: eligibleRegimes ? (r, event) => {
      const prior = Array.isArray(r.eligible_regimes) ? r.eligible_regimes.slice() : null;
      delete r.eligible_regimes;
      event.metadata = Object.assign({}, event.metadata || {}, {
        eligible_regimes_before: prior,
        eligible_regimes_after:  eligibleRegimes,
      });
    } : undefined,
  });

  // Gate failure → 422 (registry-first: nothing written). Identical JSON shape
  // to the pre-refactor route.
  if (!result.ok && result.failedGates) {
    return res.status(422).json({
      error: `candidate→live blocked: ${result.failedGates.join(', ')} gate(s) failed`,
      failed_gates: result.failedGates,
      allow_override: true,
    });
  }
  // Registry-sync or manifest-write failure → 500. The service refuses with
  // NOTHING written on a registry-sync failure (manifest stays put); a manifest
  // write that fails AFTER the registry synced surfaces as a drift badge. Log
  // loudly — these logs are how the operator detects registry↔manifest drift.
  if (!result.ok) {
    console.error(`[transition] ${sid} ${fromState}→${toState} refused: ${result.error}`);
    return res.status(500).json({ error: result.error });
  }

  // ── Sync strategy_regime_params (source-of-truth, Phase 2A) ──────────────
  // The live regime-blended sizer reads strategy_regime_params.eligible per
  // (strategy, regime) to decide whether to size positions for that regime.
  // The picker writes the operator's chosen set here so the live stack
  // matches the operator's intent. We upsert all 4 canonical regimes per
  // call so deselecting LOW_VOL (for example) explicitly flips
  // eligible=False, rather than leaving a stale True from a prior approval.
  // Auto mode (2026-07-13 v2): when the caller named no set, the gate's
  // qualifying-regime set (returned by transitionStrategy) is the activation
  // — sync it identically so the sizer funds exactly the regimes that earned
  // promotion.
  const syncRegimes = eligibleRegimes ||
    (Array.isArray(result.qualifyingRegimes) && result.qualifyingRegimes.length > 0 && tKey === 'candidate:live'
      ? result.qualifyingRegimes : null);
  if (syncRegimes) {
    const chosen = new Set(syncRegimes);
    try {
      for (const rg of _CANON_REGIMES) {
        const isElig = chosen.has(rg);
        // Read current row (FOR UPDATE not strictly needed here — the picker
        // is operator-driven and serialised by manifest_lock above; concurrent
        // writers to the same (sid, regime) row are unexpected).
        const { rows: beforeRows } = await dbQuery(
          `SELECT strategy_id, regime_state, eligible,
                  size_scalar, stop_pct, target_pct, max_hold_days
             FROM strategy_regime_params
            WHERE strategy_id=$1 AND regime_state=$2`,
          [sid, rg],
        );
        const before = beforeRows[0] || null;
        // Skip the write when the row already matches what we'd set — avoids
        // an audit row spam on re-approvals that don't actually change the
        // eligibility set.
        if (before && before.eligible === isElig) continue;
        const beforeJson = before ? JSON.stringify({
          strategy_id: sid, regime_state: rg, eligible: before.eligible,
          size_scalar: before.size_scalar != null ? Number(before.size_scalar) : null,
          stop_pct:    before.stop_pct    != null ? Number(before.stop_pct)    : null,
          target_pct:  before.target_pct  != null ? Number(before.target_pct)  : null,
          max_hold_days: before.max_hold_days,
        }) : null;
        const afterJson = JSON.stringify({
          strategy_id: sid, regime_state: rg, eligible: isElig,
          // Preserve sizing params verbatim — picker only owns the
          // eligible flag.
          size_scalar:   before ? (before.size_scalar != null ? Number(before.size_scalar) : null) : null,
          stop_pct:      before ? (before.stop_pct    != null ? Number(before.stop_pct)    : null) : null,
          target_pct:    before ? (before.target_pct  != null ? Number(before.target_pct)  : null) : null,
          max_hold_days: before ? before.max_hold_days : null,
        });
        await dbQuery(
          `INSERT INTO strategy_regime_param_changes
              (actor, strategy_id, regime_state,
               before_row, after_row, reason, source)
           VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6, 'dashboard_picker')`,
          [`dashboard:${actor || 'unknown'}`, sid, rg, beforeJson, afterJson,
           `picker set eligible=${isElig} (transition ${fromState}→${toState})`],
        );
        await dbQuery(
          `INSERT INTO strategy_regime_params
              (strategy_id, regime_state, eligible, set_by)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (strategy_id, regime_state) DO UPDATE
              SET eligible = EXCLUDED.eligible,
                  set_at   = NOW(),
                  set_by   = EXCLUDED.set_by`,
          [sid, rg, isElig, `dashboard:${actor || 'unknown'}`],
        );
      }
    } catch (e) {
      // Don't fail the whole transition on DB write — the manifest write
      // already succeeded and the operator can re-approve. Log loudly.
      console.error(`[transition] strategy_regime_params upsert failed for ${sid}: ${e.message}`);
    }
  }

  // NOTE: the lifecycle_events audit insert now happens INSIDE
  // transitionStrategy (after the manifest write, using the same `event` the
  // manifestMutator folded the eligible_regimes before/after into), so the
  // route no longer inserts it here.

  // Broadcast SSE so the dashboard refreshes even without polling.
  try { broadcast({ type: 'strategy_transition', strategy_id: sid, from_state: fromState, to_state: toState, at: result.event.timestamp }); } catch (_) {}

  // Strategy-weights rebuild: any transition that adds or removes the
  // strategy from the active stack (live/monitoring) invalidates the
  // current strategy_weights_by_regime snapshot. Fire-and-forget — the
  // response doesn't block on the python invocation; the next pipeline
  // cycle reads the refreshed table. The service computes whether the
  // active stack changed (same ACTIVE={live,monitoring} XOR logic).
  // skip_weights_rebuild lets batch callers suppress the per-transition
  // spawn and run ONE rebuild at the end (racing rebuilds corrupt the
  // is_current snapshot generation — 2026-07-13 lesson).
  // candidate→live promotions first apply the activation min-Sharpe slider
  // to the just-synced strategy_regime_params rows (activation_assigner
  // --strategy-id: qualification gate AND slider) so a sub-slider sleeve
  // never carries weight even transiently before the Monday refresh.
  if (result.weights_rebuild_triggered && !skipWeightsRebuild) {
    const { spawn } = require('child_process');
    const path = require('path');
    const rootDir = path.join(__dirname, '../../..');
    const rebuildCmd = 'nice -n 19 python3 -m execution.strategy_weights --rebuild --trigger=lifecycle_change';
    const cmd = (tKey === 'candidate:live')
      ? `nice -n 19 python3 -m backtest.activation_assigner --strategy-id '${sid}' ; ${rebuildCmd}`
      : rebuildCmd;
    const child = spawn('/bin/bash', ['-c', cmd],
      { cwd: rootDir, env: { ...process.env, PYTHONPATH: 'src' }, detached: true, stdio: 'ignore' });
    child.unref();
    console.log('[lifecycle] strategy_weights rebuild triggered (' + sid + ': ' + fromState + '→' + toState +
                (tKey === 'candidate:live' ? ', activation slider applied first' : '') + ')');
  } else if (result.weights_rebuild_triggered) {
    console.log('[lifecycle] weights rebuild SKIPPED by caller (' + sid + ': ' + fromState + '→' + toState + ') — batch finale owns it');
  }

  res.json({ ok: true, strategy_id: sid, from_state: fromState, to_state: toState, at: result.event.timestamp,
             weights_rebuild_triggered: result.weights_rebuild_triggered,
             qualifying_regimes: result.qualifyingRegimes || null });
});

// ── Manual backtest rerun ────────────────────────────────────────────────────
// POST /api/strategies/:id/rerun_backtest
// Fires unified_backtest + eligibility_assigner for one strategy. Async —
// returns 202 immediately with a run_token the client can poll the runs
// table with (`SELECT run_at FROM strategy_backtest_runs WHERE strategy_id`).
// Auth note: same trust model as the rest of the dashboard endpoints
// (operator-internal, port 3000 behind nginx).
app.post('/api/strategies/:id/rerun_backtest', async (req, res) => {
  const { spawn } = require('child_process');
  const sid = req.params.id;
  const actor = (req.body && req.body.actor) || 'manual:dashboard';

  // Best-effort: confirm the strategy_id exists in the manifest before spawning.
  const fs = require('fs');
  const path = require('path');
  const manifestPath = path.join(__dirname, '../../strategies/manifest.json');
  try {
    const m = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    if (!(m?.strategies || {})[sid]) {
      return res.status(404).json({ error: `strategy_id ${sid} not in manifest` });
    }
  } catch (e) {
    return res.status(500).json({ error: `manifest read failed: ${e.message}` });
  }

  // Spawn the backtest detached so the HTTP response can return immediately.
  // Logs land in /tmp/unified_backtest_${sid}.log for operator inspection.
  const logPath = `/tmp/unified_backtest_${sid}_${Date.now()}.log`;
  const logFd   = fs.openSync(logPath, 'a');
  const cwd     = path.resolve(__dirname, '../../..');
  const env     = { ...process.env, PYTHONPATH: 'src' };

  const child = spawn(
    process.execPath.endsWith('/node') ? 'python3' : 'python3',  // explicit
    ['-m', 'backtest.unified_backtest', '--strategy-id', sid],
    { cwd, env, detached: true, stdio: ['ignore', logFd, logFd] }
  );
  child.unref();

  // Schedule eligibility_assigner to run after the backtest completes via a
  // chained spawn — exec a small shell wrapper that runs the assigner once
  // the backtest process exits. Simpler than node-side child-event chaining
  // when we've already detached.
  try {
    const wrapper = spawn(
      'sh', ['-c',
        `wait ${child.pid} 2>/dev/null; ` +
        `python3 -m backtest.eligibility_assigner --strategy-id ${JSON.stringify(sid)} >> ${JSON.stringify(logPath)} 2>&1`
      ],
      { cwd, env, detached: true, stdio: 'ignore' }
    );
    wrapper.unref();
  } catch (_) { /* non-fatal */ }

  res.status(202).json({
    ok:           true,
    strategy_id:  sid,
    actor,
    log_path:     logPath,
    note:         'unified_backtest spawned detached; poll strategy_backtest_runs for new run_at',
  });
});

// ── Approval dispatcher ──────────────────────────────────────────────────────
// POST /api/strategies/:id/approve — state-aware entry point for the
// Approve button.
//
// Under the fused-approval lifecycle (2026-04-27):
//   staging   → kicks off the async fused worker (backfill + strategycoder
//               + validate + backtest → manifest staging→candidate)
//   candidate → use POST /transition with to_state=live (sharpe/dd guarded)
//   paper     → legacy; redirect to /transition with to_state=archived (the
//               only remaining outbound transition)
app.post('/api/strategies/:id/approve', async (req, res) => {
  const path = require('path');
  const fs   = require('fs');
  const sid   = req.params.id;
  const actor = (req.body && req.body.actor) || 'manual:dashboard';

  try {
    const manifestPath = path.join(__dirname, '../../strategies/manifest.json');
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
    const rec = manifest.strategies[sid];
    if (!rec) return res.status(404).json({ error: `Strategy ${sid} not found in manifest` });

    const existing = await approvals.hasActiveJob(sid);
    if (existing) return res.status(409).json({ error: 'job already running', job_id: existing.job_id });

    if (rec.state === 'staging') {
      // Defensive: ensure a strategy_registry row exists before the worker
      // creates strategy_approval_jobs (FK constraint). Older manifest
      // entries (S_HV10_triple_gate_fear, hand-curated SXX series) sometimes
      // exist in manifest without a registry row. Upsert via the canonical
      // helper using whatever metadata the manifest carries.
      const { upsertStrategyRegistry } = require('../../lib/strategy_registry_upsert');
      const md = rec.metadata || {};
      const canonical = md.canonical_file || `${sid.toLowerCase()}.py`;
      try {
        await upsertStrategyRegistry({
          id: sid,
          name: md.class || sid,
          implementationPath: `src/strategies/implementations/${canonical}`,
          status: 'pending_approval',
          parameters: { description: md.description || '' },
          dbQuery,
        });
      } catch (e) {
        return res.status(500).json({ error: `registry upsert failed: ${e.message}` });
      }
      const { status, body } = await approvals.approveStaging(sid, rec, actor);
      return res.status(status).json(body);
    }
    if (rec.state === 'candidate') {
      // Operator's promote-to-live click. Sharpe/dd gate runs in /transition.
      return res.status(409).json({
        error: 'candidate strategies use POST /api/strategies/:id/transition with to_state=live',
        redirect: 'transition',
      });
    }
    if (rec.state === 'paper') {
      // Legacy/frozen state. Existing rows should have been migrated to
      // candidate; if any orphan slipped through, archive it via /transition.
      return res.status(409).json({
        error: 'paper is a legacy state — use POST /api/strategies/:id/transition with to_state=archived',
        redirect: 'transition',
      });
    }
    return res.status(422).json({ error: `approve not applicable from state='${rec.state}'` });
  } catch (e) {
    console.error('[/approve]', e);
    return res.status(500).json({ error: e.message });
  }
});

app.get('/api/approvals/active', async (req, res) => {
  try { res.json(await approvals.listActive()); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

// Persisted failure banners — latest failed/cancelled job per strategy that
// the user hasn't dismissed. Dashboard hydrates from here on load so red
// banners survive page refresh and johnbot restart.
app.get('/api/approvals/recent-failures', async (req, res) => {
  try { res.json(await approvals.listRecentFailures(30)); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/approvals/:jobId/dismiss', async (req, res) => {
  try {
    const row = await approvals.dismissFailure(req.params.jobId);
    if (!row) return res.status(404).json({ error: 'job not found' });
    res.json({ ok: true, job_id: row.job_id, strategy_id: row.strategy_id });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.post('/api/strategies/:id/approve/cancel', async (req, res) => {
  try {
    const { status, body } = await approvals.cancelJob(req.params.id);
    res.status(status).json(body);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

app.get('/api/portfolio/account', async (req, res) => {
  try {
    const r = await runAlpaca(['account', 'get']);
    if (!r.ok) {
      const status = r.error?.status || 500;
      return res.status(status).json({ error: r.error?.error || r.stderr || 'alpaca cli error' });
    }
    const a = r.payload || {};
    const _lev = realizedLeverage(a);
    res.json({
      equity:             parseFloat(a.equity)             || 0,
      cash:               parseFloat(a.cash)               || 0,
      buying_power:       parseFloat(a.buying_power)       || 0,
      last_equity:        parseFloat(a.last_equity)        || 0,
      long_market_value:  parseFloat(a.long_market_value)  || 0,
      short_market_value: parseFloat(a.short_market_value) || 0,
      gross_leverage:     _lev.gross,
      net_leverage:       _lev.net,
      day_pnl:           (parseFloat(a.equity) - parseFloat(a.last_equity)) || 0,
      day_pnl_pct:        parseFloat(a.last_equity) > 0
                            ? ((parseFloat(a.equity) - parseFloat(a.last_equity)) / parseFloat(a.last_equity) * 100)
                            : 0,
    });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

async function _buildValueCurve(period) {
  const r = await runAlpaca(['account', 'portfolio',
                             '--period', period, '--timeframe', '1D'],
                            { timeout: 30_000 });
  if (!r.ok) {
    const e = new Error(r.error?.error || r.stderr || 'alpaca cli error');
    e.status = r.error?.status || 500;
    throw e;
  }
  const h = r.payload || {};
  // Zip timestamps + equity into [{date, equity, profit_loss, profit_loss_pct}]
  const rows = (h.timestamp || []).map((ts, i) => ({
    date:             new Date(ts * 1000).toISOString().slice(0, 10),
    equity:           h.equity?.[i]          ?? null,
    profit_loss:      h.profit_loss?.[i]     ?? null,
    profit_loss_pct:  h.profit_loss_pct?.[i] ?? null,
  })).filter(r => r.equity !== null && r.equity > 0);

  // Alpaca paper's portfolio history stamps each day's snapshot only
  // after the NEXT session opens, so the curve lags by ~1 trading day.
  // Synthesize today's running point from `alpaca account get` equity
  // unless OPENCLAW_TRUST_CLI_PORTFOLIO=1 says the CLI already did it.
  //
  // Verified 2026-04-28: the alpha-preview CLI's `account portfolio`
  // DOES emit today's date in its timestamp array, but the equity value
  // for today's slot is `last_equity` (prior session close), not the
  // live mark-to-market. So the synthesis fallback is still load-
  // bearing — flipping the trust flag would replace live equity with
  // the stale prior close in the dashboard.
  const trustCli = process.env.OPENCLAW_TRUST_CLI_PORTFOLIO === '1';
  if (!trustCli) {
    try {
      const todayIso = new Date().toISOString().slice(0, 10);
      const lastRowDate = rows.length ? rows[rows.length - 1].date : null;
      if (lastRowDate !== todayIso) {
        const acctR = await runAlpaca(['account', 'get'], { timeout: 10_000 });
        if (acctR.ok && acctR.payload) {
          const a = acctR.payload;
          const todayEquity = parseFloat(a.equity);
          if (todayEquity > 0) {
            const prevEquity = rows.length ? parseFloat(rows[rows.length - 1].equity)
                                            : parseFloat(a.last_equity || 0);
            const pl    = prevEquity > 0 ? todayEquity - prevEquity : null;
            const plPct = prevEquity > 0 ? (todayEquity - prevEquity) / prevEquity : null;
            rows.push({
              date:            todayIso,
              equity:          todayEquity,
              profit_loss:     pl,
              profit_loss_pct: plPct,
              _live:           true,
            });
          }
        }
      }
    } catch (_) { /* fall through with whatever the history endpoint returned */ }
  }

  return { rows, base_value: h.base_value ?? null };
}

app.get('/api/portfolio/value-curve', async (req, res) => {
  // Alpaca's history endpoint accepts "all" only as a lowercase keyword;
  // the dashboard surfaces it as "all" so just pass through.
  const period = req.query.period || '1M';
  try {
    const data = await _cached(`vc:${period}`, VALUE_CURVE_TTL,
                                () => _buildValueCurve(period));
    res.json(data);
  } catch (err) {
    res.status(err.status || 500).json({ error: err.message });
  }
});

// Portfolio P&L candlestick — daily OHLC of account equity for a 90d window.
// Alpaca caps intraday timeframe at 30 days. We fetch:
//   - last 30D at 1H        → real OHLC per day from intraday samples
//   - last 90D at 1D        → daily marks for older days (degenerate candle:
//                             open=prev_close, close=today, high=max,
//                             low=min). Bands beyond day 30 will look like
//                             thin "marks" but still convey direction.
// Alpaca's intraday endpoint can spike to 15-30s under load (verified
// 2026-04-29), so we cache responses and stream a background warmer to
// keep dashboard loads instant. The data only gains a new daily candle
// once per session, and the live close is approximated by the latest
// 1H sample — caching at minute granularity is plenty fresh for a
// daily-resolution chart.
const _portfolioCache = new Map();  // key → { ts, ttl, value, inflight }
const PNL_CANDLES_TTL  = 90_000;
const VALUE_CURVE_TTL  = 60_000;

// ── Live daily P&L candle store (operator spec 2026-07-22) ───────────────────
// Candle day = midnight-to-midnight ET so consecutive candles always TOUCH:
// each day opens at the prior day's close (equity is flat through the full
// market close for an all-equity book; crypto sleeve / fees / dividend marks
// that move overnight land inside the day's wicks instead of as inter-candle
// gaps). Built live from a 60s equity sampler: close tracks the latest mark,
// high/low are running extremes, the row solidifies when the ET date rolls.
// Persisted as ONE compact row of 4 floats per day (no tick-level portfolio
// history is stored, per operator constraint) — this store is authoritative
// going forward; days before it began fall back to Alpaca-derived
// approximations in _buildCandles.
const PNL_OHLC_PATH = require('path').join(__dirname, '../../../logs/pnl_daily_ohlc.json');
const PNL_OHLC_KEEP_DAYS = 400;
let _ohlcStore = null;   // { days: { 'YYYY-MM-DD': {open, high, low, close} } }

function _loadOhlcStore() {
  try {
    const raw = JSON.parse(fs.readFileSync(PNL_OHLC_PATH, 'utf8'));
    if (raw && typeof raw.days === 'object') return raw;
  } catch (_) { /* first run / unreadable → start fresh */ }
  return { days: {} };
}

function _saveOhlcStore() {
  try {
    const dates = Object.keys(_ohlcStore.days).sort();
    for (const d of dates.slice(0, Math.max(0, dates.length - PNL_OHLC_KEEP_DAYS))) {
      delete _ohlcStore.days[d];
    }
    fs.writeFileSync(PNL_OHLC_PATH + '.tmp', JSON.stringify(_ohlcStore));
    fs.renameSync(PNL_OHLC_PATH + '.tmp', PNL_OHLC_PATH);
  } catch (e) {
    console.warn('[pnl-ohlc] persist failed:', e.message);
  }
}

const _etDateNow = () => new Date()
  .toLocaleDateString('en-CA', { timeZone: 'America/New_York' });

// Is today an equity trading day? Resolved from the broker clock (is_open,
// or next_open still dated today — true at any hour of a trading day, false
// on weekends/holidays where next_open has rolled to the next session).
// Weekday fallback if the clock is unreachable. Cached 30 min.
let _tradingTodayCache = null;
async function _isTradingToday() {
  const now = Date.now();
  if (_tradingTodayCache && now - _tradingTodayCache.ts < 30 * 60_000) {
    return _tradingTodayCache.val;
  }
  let val;
  try {
    const r = await runAlpaca(['clock'], { timeout: 10_000 });
    const etDate = (iso) => new Date(iso)
      .toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    val = Boolean(r.ok && r.payload &&
      (r.payload.is_open || etDate(r.payload.next_open) === _etDateNow()));
    if (!r.ok || !r.payload) throw new Error('clock unavailable');
  } catch (_) {
    const dow = new Date().toLocaleDateString('en-US',
      { timeZone: 'America/New_York', weekday: 'short' });
    val = dow !== 'Sat' && dow !== 'Sun';
  }
  _tradingTodayCache = { ts: now, val };
  return val;
}

async function samplePnlCandle() {
  const r = await runAlpaca(['account', 'get'], { timeout: 10_000 });
  if (!r.ok) return;
  const v = parseFloat(r.payload?.equity);
  if (!Number.isFinite(v) || v <= 0) return;
  if (!_ohlcStore) _ohlcStore = _loadOhlcStore();
  const days = _ohlcStore.days;
  const d = _etDateNow();
  let c = days[d];
  // Glitch gate: Alpaca occasionally emits an equity sample 1-2 orders of
  // magnitude off (see _buildCandles outlier note). A bad sample accepted
  // here would poison a persisted wick permanently, so reject anything >2x
  // away from the candle's running close (or the prior day's close on
  // rollover).
  const ref = c ? c.close
    : (Object.keys(days).sort().slice(-1).map(k => days[k].close)[0] ?? null);
  if (ref != null && (v < ref * 0.5 || v > ref * 2.0)) return;
  if (!c) {
    const open = ref ?? v;   // midnight rollover: open at prior close → candles touch
    c = days[d] = { open, high: Math.max(open, v), low: Math.min(open, v), close: v };
  } else {
    c.high = Math.max(c.high, v);
    c.low = Math.min(c.low, v);
    c.close = v;
  }
  _saveOhlcStore();
}

async function _cached(key, ttlMs, factory) {
  const now = Date.now();
  const hit = _portfolioCache.get(key);
  if (hit && (now - hit.ts) < hit.ttl && hit.value !== undefined) {
    return hit.value;
  }
  if (hit && hit.inflight) return hit.inflight;
  const inflight = (async () => {
    try {
      const value = await factory();
      _portfolioCache.set(key, { ts: Date.now(), ttl: ttlMs, value });
      return value;
    } catch (err) {
      // On failure, drop the inflight marker so the next caller retries.
      // Keep any prior cached value (stale-on-error) for 30s so the
      // dashboard renders something while Alpaca recovers.
      const prior = _portfolioCache.get(key);
      if (prior && prior.value !== undefined) {
        _portfolioCache.set(key, { ts: prior.ts, ttl: 30_000, value: prior.value });
        return prior.value;
      }
      _portfolioCache.delete(key);
      throw err;
    }
  })();
  _portfolioCache.set(key, { ts: now, ttl: ttlMs, value: hit?.value, inflight });
  return inflight;
}

async function _buildCandles(days) {
  // Alpaca's intraday endpoint rejects period >30 days when paired with
  // --intraday-reporting continuous (the API errors "invalid timeframe
  // provided: 1H. Valid timeframe for days > 30 is 1D" verified
  // 2026-04-29) — even though the message says "> 30" the actual cap is
  // 29 days. Floor at 29 so candles always come back populated.
  //
  // We pull two parallel intraday streams so the candles can show
  // Candle semantics (operator spec 2026-07-22, supersedes the 07-21
  // extended-session spec): each candle is the full CALENDAR day midnight-
  // to-midnight ET, and every candle opens at the previous candle's close
  // so the series always touches — overnight/pre-market repricing shows up
  // INSIDE a day's body/wicks, never as an inter-candle gap. Days covered
  // by the live sampler store (logs/pnl_daily_ohlc.json, authoritative
  // going forward) use its running OHLC; older days approximate from the
  // continuous stream bucketed by ET calendar day, then the 1D fallback.
  // market_hours (9:30-16:00 ET) samples are kept ONLY for the tooltip's
  // regular-session open/close fields.
  const intradayPeriod = `${Math.min(days, 29)}D`;
  const [contR, mktR, dailyR] = await Promise.all([
    runAlpaca(['account', 'portfolio',
               '--period', intradayPeriod, '--timeframe', '1H',
               '--intraday-reporting', 'continuous'],
              { timeout: 45_000 }),
    runAlpaca(['account', 'portfolio',
               '--period', intradayPeriod, '--timeframe', '1H',
               '--intraday-reporting', 'market_hours'],
              { timeout: 45_000 }),
    runAlpaca(['account', 'portfolio',
               '--period', `${days}D`, '--timeframe', '1D'],
              { timeout: 30_000 }),
  ]);
  // We need at least one of the three to succeed; if all three fail,
  // surface the first error.
  if (!contR.ok && !mktR.ok && !dailyR.ok) {
    const err = contR.error || mktR.error || dailyR.error;
    const e = new Error(err?.error || 'alpaca cli error');
    e.status = err?.status || 500;
    throw e;
  }

  // Outlier gate: Alpaca's portfolio-history endpoint occasionally emits a
  // single intraday equity sample that's 1-2 orders of magnitude off (saw
  // a $1,285 sample stamped on 2026-05-04 while the same day's
  // market-hours stream and account endpoint both reported ~$100k). One
  // bad sample destroys the y-axis range on the 90d chart. Reject any
  // sample that diverges by >50% from a running baseline; the threshold
  // is generous (legitimate intraday moves on a 3x-leverage paper account
  // are well under that), and the baseline self-heals because we only
  // update it on accepted samples.
  const RATIO_HI = 2.0;
  const RATIO_LO = 0.5;

  // Per-day extended-session OHLC (04:00-20:00 ET) from the continuous
  // stream: first sample = pre-market open, last = after-hours close,
  // high/low over the window.
  const _etDayTime = (unixSec) => {
    const s = new Date(unixSec * 1000).toLocaleString('en-CA',
      { timeZone: 'America/New_York', hourCycle: 'h23',
        year: 'numeric', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit' });
    return s.split(', ');                  // ['YYYY-MM-DD', 'HH:MM']
  };
  const contByDay = new Map();
  if (contR.ok && contR.payload) {
    const h = contR.payload;
    const ts = h.timestamp || [];
    const eq = h.equity    || [];
    let baseline = null;
    for (let i = 0; i < ts.length; i++) {
      const v = parseFloat(eq[i]);
      if (!Number.isFinite(v) || v <= 0) continue;
      if (baseline != null && (v < baseline * RATIO_LO || v > baseline * RATIO_HI)) continue;
      baseline = v;
      const [d] = _etDayTime(ts[i]);   // full calendar day — overnight samples included
      const slot = contByDay.get(d) || { open: v, high: v, low: v, close: v };
      slot.high  = Math.max(slot.high, v);
      slot.low   = Math.min(slot.low, v);
      slot.close = v;
      contByDay.set(d, slot);
    }
  }

  // Per-day market-hours open/close (regular session 9:30-16:00 ET only).
  const mktByDay = new Map();
  if (mktR.ok && mktR.payload) {
    const h = mktR.payload;
    const ts = h.timestamp || [];
    const eq = h.equity    || [];
    let baseline = null;
    for (let i = 0; i < ts.length; i++) {
      const v = parseFloat(eq[i]);
      if (!Number.isFinite(v) || v <= 0) continue;
      if (baseline != null && (v < baseline * RATIO_LO || v > baseline * RATIO_HI)) continue;
      baseline = v;
      const d = new Date(ts[i] * 1000)
        .toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
      const slot = mktByDay.get(d) || { open: v, high: v, low: v, close: v };
      slot.high  = Math.max(slot.high, v);
      slot.low   = Math.min(slot.low, v);
      slot.close = v;
      mktByDay.set(d, slot);
    }
  }

  // Daily fallback for days beyond the 30-day intraday window.
  const dailyByDay = new Map();
  if (dailyR.ok && dailyR.payload) {
    const h  = dailyR.payload;
    const ts = h.timestamp || [];
    const eq = h.equity    || [];
    for (let i = 0; i < ts.length; i++) {
      const v = parseFloat(eq[i]);
      if (!Number.isFinite(v) || v <= 0) continue;
      const d = new Date(ts[i] * 1000)
        .toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
      dailyByDay.set(d, v);
    }
  }

  // Merge strategy (operator spec 2026-07-21 — EXTENDED-session geometry):
  //   - Chart open/high/low/close come from the 04:00-20:00 ET extended
  //     window: open at the pre-market start, close at the after-hours
  //     end, wicks the true high/low across it.
  //   - market_open / market_close (regular 9:30-16:00 session bounds)
  //     stay surfaced for the tooltip.
  //   - Daily fallback mark is used only when the intraday window
  //     doesn't cover the day (older than ~29 days back).
  // Live-sampler store rows are authoritative for the days they cover.
  const liveDays = (_ohlcStore || (_ohlcStore = _loadOhlcStore())).days;

  // Equity trading days only (operator spec 2026-07-22): weekends/holidays
  // emit no candle. The market_hours and 1D streams only carry samples on
  // session days, so their key union IS the session calendar for the
  // window — no separate calendar dependency. Today may be absent from
  // both streams before its first sample, so it's resolved via the broker
  // clock. Movement ON a closed day (crypto sleeve marking 24/7) is not
  // dropped: its high/low fold into the NEXT session's candle below.
  const tradingDays = new Set([...mktByDay.keys(), ...dailyByDay.keys()]);
  if (!tradingDays.size) {
    // Both session-calendar sources failed — degrade to a weekday filter
    // rather than rendering an empty chart.
    for (const d of contByDay.keys()) {
      const dow = new Date(d + 'T12:00:00Z').getUTCDay();
      if (dow !== 0 && dow !== 6) tradingDays.add(d);
    }
  }
  const todayEt = _etDateNow();
  if (!tradingDays.has(todayEt) && await _isTradingToday()) {
    tradingDays.add(todayEt);
  }

  const allDays = new Set([...contByDay.keys(), ...mktByDay.keys(),
                           ...dailyByDay.keys(), ...Object.keys(liveDays)]);
  const sorted  = [...allDays].sort();
  const candles = [];
  let prevClose = null;
  let pendHigh = null;   // closed-day (weekend/holiday) excursion awaiting fold
  let pendLow  = null;
  for (const d of sorted) {
    const live = liveDays[d];
    const cont = contByDay.get(d);
    const mkt  = mktByDay.get(d);
    if (!tradingDays.has(d)) {
      // Closed day: no candle of its own. An all-equity book is flat here
      // (fold is a no-op); crypto/fee movement carries into the next
      // session's wicks so nothing real is lost.
      const src = live || cont;
      if (src) {
        pendHigh = pendHigh == null ? src.high : Math.max(pendHigh, src.high);
        pendLow  = pendLow  == null ? src.low  : Math.min(pendLow,  src.low);
      }
      continue;
    }
    let row;
    if (live || cont || mkt) {
      const geo = live || cont || mkt;
      row = {
        date: d,
        open: geo.open,
        high: geo.high,
        low:  geo.low,
        close: geo.close,
        market_open:  mkt ? mkt.open  : null,
        market_close: mkt ? mkt.close : null,
        intraday: true,
        live: Boolean(live),
      };
    } else {
      const v = dailyByDay.get(d);
      const op = prevClose ?? v;
      row = {
        date: d,
        open: op, high: Math.max(op, v), low: Math.min(op, v), close: v,
        market_open:  null,
        market_close: null,
        intraday: false,
        live: false,
      };
    }
    // Touching invariant: every candle opens exactly at the prior close.
    // A no-op in steady state (live-store days already chain open=prev
    // close; equity is flat through the overnight halt) — it only absorbs
    // the residual seams between the three sources so overnight repricing
    // renders inside the candle, never as a gap.
    if (prevClose != null) {
      row.open = prevClose;
      row.high = Math.max(row.high, row.open);
      row.low  = Math.min(row.low,  row.open);
    }
    // Fold any weekend/holiday excursion since the last session into this
    // candle's wicks (pct_change below already spans the closed stretch
    // because prevClose is the last SESSION close).
    if (pendHigh != null) {
      row.high = Math.max(row.high, pendHigh);
      row.low  = Math.min(row.low,  pendLow);
      pendHigh = pendLow = null;
    }
    row.pct_change = prevClose != null && prevClose > 0
      ? (row.close - prevClose) / prevClose
      : null;
    candles.push(row);
    prevClose = row.close;
  }
  return candles;
}

app.get('/api/portfolio/pnl-candles', async (req, res) => {
  const days = Math.min(parseInt(req.query.days) || 90, 365);
  try {
    const candles = await _cached(`candles:${days}`, PNL_CANDLES_TTL,
                                   () => _buildCandles(days));
    res.json(candles);
  } catch (err) {
    res.status(err.status || 500).json({ error: err.message });
  }
});

// Volatility-regime history for chart background bands.
// Sources, in priority order per date:
//   1. .agents/market-state/regime_YYYY-MM-DD.json (recent days, written
//      by run_market_state.py — covers up to ~today)
//   2. data/master/historical_regimes.parquet (long history, refreshed by
//      strategies/historical_regimes.py)
// Parquet read is delegated to a short python3 helper; result cached for
// 1 hour since the parquet only changes on the daily refit cadence.
let _regimeParquetCache = null;
async function _loadRegimeParquet() {
  const now = Date.now();
  if (_regimeParquetCache && (now - _regimeParquetCache.ts) < 60 * 60 * 1000) {
    return _regimeParquetCache.rows;
  }
  const path = require('path');
  const parquetPath = path.join(__dirname, '../../../data/master/historical_regimes.parquet');
  if (!fs.existsSync(parquetPath)) { _regimeParquetCache = { ts: now, rows: [] }; return []; }
  const { spawn } = require('child_process');
  const py = `
import sys, json
import pandas as pd
df = pd.read_parquet(sys.argv[1])
df = df[['date', 'regime']].copy()
df['date'] = df['date'].astype(str).str.slice(0, 10)
sys.stdout.write(df.to_json(orient='records'))
`;
  const rows = await new Promise((resolve) => {
    let out = '', err = '';
    const proc = spawn('python3', ['-c', py, parquetPath], { stdio: ['ignore', 'pipe', 'pipe'] });
    const t = setTimeout(() => { try { proc.kill('SIGKILL'); } catch (_) {} resolve([]); }, 10_000);
    proc.stdout.on('data', c => { out += c; });
    proc.stderr.on('data', c => { err += c; });
    proc.on('close', code => {
      clearTimeout(t);
      if (code !== 0) { console.error('[regime-history] parquet read failed:', err); return resolve([]); }
      try { resolve(JSON.parse(out)); } catch (e) { console.error('[regime-history] parse:', e.message); resolve([]); }
    });
  });
  _regimeParquetCache = { ts: now, rows };
  return rows;
}

app.get('/api/portfolio/regime-history', async (req, res) => {
  const days = Math.min(parseInt(req.query.days) || 90, 4_000);
  try {
    const cutoff = new Date(Date.now() - days * 86400 * 1000)
      .toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
    const byDate = new Map();
    // Bulk historical base: parquet.
    const REAL_REGIMES = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
    const parq = await _loadRegimeParquet();
    for (const r of parq) {
      if (!r.date || r.date < cutoff) continue;
      // "Don't know" is not a state: skip so the forward-fill inherits the
      // prior real regime instead of painting an UNKNOWN/NO_DATA hole.
      if (!REAL_REGIMES.includes(r.regime)) continue;
      byDate.set(r.date, r.regime);
    }
    // Authoritative per-day override: the regime-of-record (market_regime,
    // latest row per day). 2026-06-08: the daily detector is diagnostic-only
    // and its regime_YYYY-MM-DD.json archives are no longer authoritative — the
    // intraday HMM owns market_regime. DISTINCT ON gives the day's FINAL state,
    // so a same-day correction (e.g. the LOW_VOL resync row that supersedes a
    // false-CRISIS daily row) wins, and the chart matches what the breaker/
    // sizer actually traded.
    try {
      // Bucket by ET trading day — the candles key their days in
      // America/New_York, so the backdrop must too (UTC bucketing pushed
      // evening rows onto the next calendar day and painted the band one
      // day ahead of the candle it belongs to).
      const mr = await dbQuery(
        `SELECT DISTINCT ON ((updated_at AT TIME ZONE 'America/New_York')::date)
                to_char((updated_at AT TIME ZONE 'America/New_York')::date, 'YYYY-MM-DD') AS d, state
           FROM market_regime
          WHERE (updated_at AT TIME ZONE 'America/New_York')::date >= $1::date
            AND state = ANY($2)
          ORDER BY (updated_at AT TIME ZONE 'America/New_York')::date, updated_at DESC`,
        [cutoff, REAL_REGIMES]);
      for (const r of mr.rows) {
        if (r.state) byDate.set(r.d, r.state);
      }
    } catch (e) {
      console.warn('[regime-history] market_regime override failed:', e.message);
    }
    // Regime-of-record on top: intraday_regime_states — the state the
    // breaker/sizer actually traded (5-min HMM, sole author since 06-08).
    // market_regime went stale 2026-06-15; without this tier the backdrop
    // forward-fills a month-old state over every recent day.
    try {
      // state = ANY(REAL_REGIMES) matters doubly here: the intraday HMM's
      // 05-08→05-18 warm-up wrote all-UNKNOWN days, and being the top
      // override tier they blanked a stretch market_regime knew perfectly
      // well (the 2026-07-21 "regime absent 5/8-5/18" report).
      const ir = await dbQuery(
        `SELECT DISTINCT ON ((ts_utc AT TIME ZONE 'America/New_York')::date)
                to_char((ts_utc AT TIME ZONE 'America/New_York')::date, 'YYYY-MM-DD') AS d, state
           FROM intraday_regime_states
          WHERE (ts_utc AT TIME ZONE 'America/New_York')::date >= $1::date
            AND state = ANY($2)
          ORDER BY (ts_utc AT TIME ZONE 'America/New_York')::date, ts_utc DESC`,
        [cutoff, REAL_REGIMES]);
      for (const r of ir.rows) {
        if (r.state) byDate.set(r.d, r.state);
      }
    } catch (e) {
      console.warn('[regime-history] intraday_regime_states override failed:', e.message);
    }
    // Forward-fill across every calendar day in the window so weekends,
    // holidays, and any single-day gaps inherit the prior trading day's
    // regime. The regime is a *state* — Friday's reading carries through
    // Saturday and Sunday until Monday's update — so chart backdrops
    // should paint continuously rather than leaving uncoloured strips
    // between trading sessions.
    const start = new Date(cutoff + 'T00:00:00Z');
    // "Today" in ET — iterating to UTC-today would extend the newest band a
    // day past the newest candle for the 4 hours after 20:00 ET.
    const today = new Date(new Date().toLocaleDateString('en-CA',
      { timeZone: 'America/New_York' }) + 'T00:00:00Z');
    const filled = [];
    let last = null;
    for (let cur = new Date(start); cur <= today; cur.setUTCDate(cur.getUTCDate() + 1)) {
      const d = cur.toISOString().slice(0, 10);
      if (byDate.has(d)) last = byDate.get(d);
      // Skip leading days where we have no regime reading yet (last
      // remains null until the first observation in the window).
      if (last) filled.push({ date: d, regime: last });
    }
    res.json(filled);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── Precomputed backtest equity curve (vs SP500, regime-tagged) ──────────────
// Returns strategy_backtest_panel.equity_curve JSONB for the expansion-panel
// chart. Each element: { date, strat_equity, spx_equity, regime }.
// Returns { rows: [] } for strategies without a panel row.
app.get('/api/strategies/:id/backtest-curve', async (req, res) => {
  const sid = String(req.params.id || '');
  if (!sid) return res.status(400).json({ error: 'strategy id required' });
  try {
    const r = (await dbQuery(
      `SELECT equity_curve FROM strategy_backtest_panel WHERE strategy_id=$1`, [sid])).rows[0];
    res.json({ rows: (r && r.equity_curve) ? r.equity_curve : [] });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ── Broker-side watchlist (Phase 2.5 of alpaca-cli integration) ──────────────
// Replaces the manual operator-edited list pattern with a broker watchlist
// named OPENCLAW_WATCHLIST_NAME (default 'fundjohn-core'). The dashboard
// surfaces add/remove buttons that call these endpoints; no JSON file edits.
const WATCHLIST_NAME = process.env.OPENCLAW_WATCHLIST_NAME || 'fundjohn-core';

app.get('/api/watchlist', async (req, res) => {
  try {
    const r = await runAlpaca(['watchlist', 'get-by-name', '--name', WATCHLIST_NAME]);
    if (!r.ok) {
      // 404 → watchlist doesn't exist yet (caller may want to call /create)
      const status = r.error?.status || 500;
      return res.status(status).json({
        error: r.error?.error || r.stderr || 'alpaca cli error',
        watchlist: WATCHLIST_NAME,
      });
    }
    res.json(r.payload || {});
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.post('/api/watchlist/create', async (req, res) => {
  const symbols = (req.body && req.body.symbols) || [];
  try {
    const args = ['watchlist', 'create', '--name', WATCHLIST_NAME];
    if (Array.isArray(symbols) && symbols.length) {
      args.push('--symbols', symbols.join(','));
    }
    const r = await runAlpaca(args);
    if (!r.ok) {
      const status = r.error?.status || 500;
      return res.status(status).json({ error: r.error?.error || r.stderr });
    }
    res.json(r.payload || {});
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.post('/api/watchlist/add', async (req, res) => {
  const symbol = (req.body && req.body.symbol) || '';
  if (!symbol) return res.status(400).json({ error: 'symbol required' });
  try {
    const r = await runAlpaca(['watchlist', 'add-by-name',
                                '--name',   WATCHLIST_NAME,
                                '--symbol', symbol]);
    if (!r.ok) {
      const status = r.error?.status || 500;
      return res.status(status).json({ error: r.error?.error || r.stderr });
    }
    res.json(r.payload || {});
  } catch (err) { res.status(500).json({ error: err.message }); }
});

app.post('/api/watchlist/remove', async (req, res) => {
  const symbol = (req.body && req.body.symbol) || '';
  if (!symbol) return res.status(400).json({ error: 'symbol required' });
  try {
    const r = await runAlpaca(['watchlist', 'remove-by-name',
                                '--name',   WATCHLIST_NAME,
                                '--symbol', symbol]);
    if (!r.ok) {
      const status = r.error?.status || 500;
      return res.status(status).json({ error: r.error?.error || r.stderr });
    }
    res.json(r.payload || {});
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// Called by cron after pipeline completes — pushes market_update to all SSE clients
app.post('/api/events/data-updated', (req, res) => {
  broadcast({ type: 'market_update' });
  res.json({ ok: true });
});

// Volatility regime — reads regime_latest.json. The file is written by
// run_market_state.py (daily 9 AM ET) AND by run_intraday_market_state.py
// on confirmed intraday transitions (2026-05-21 fix), so the returned
// `state` reflects whichever cadence wrote most recently. /api/regime/intraday
// below has the per-5-min audit trail when consumers need it.
app.get('/api/regime', async (req, res) => {
  try {
    const raw = await fs.promises.readFile(REGIME_FILE, 'utf8');
    const parsed = JSON.parse(raw);
    res.json({ available: true, ...parsed, ...regimeFreshness(parsed, Date.now()) });
  } catch (err) {
    if (err.code === 'ENOENT' || err instanceof SyntaxError)
      return res.json({ available: false, state: 'NO_DATA' });
    res.status(500).json({ error: err.message });
  }
});

// Intraday regime — primary regime indicator. Reads intraday_regime_states
// (written every 5 min during 9-19 ET by scripts/run_intraday_market_state.py
// using the 6-feature HMM at .agents/market-state/hmm_intraday_latest.pkl).
//
// Returns the latest tick + last-24h history + derived structure fields:
//   • derived.position_scale_pct — looked up from regime_sizer_params for
//     the current intraday state (system-wide sizing scale).
//   • derived.duration_minutes — minutes since the most recent state change.
//     Falls back to the total history span if the state has never changed.
// Model file mtime surfaces as `model.trained_at` so the operator can see
// when the active HMM was last retrained.
app.get('/api/regime/intraday', async (req, res) => {
  try {
    const latestRes = await dbQuery(
      `SELECT ts_utc, state, prior_state, confidence, hysteresis_streak,
              fired_liquidation, transition_tag, features_json
         FROM intraday_regime_states
         ORDER BY ts_utc DESC
         LIMIT 1`
    );
    if (!latestRes.rows.length) {
      return res.json({ available: false, reason: 'no_ticks_yet' });
    }
    const latest = latestRes.rows[0];

    const [histRes, sizerRes, prevChangeRes] = await Promise.all([
      dbQuery(
        `SELECT ts_utc, state, confidence
           FROM intraday_regime_states
           WHERE ts_utc >= NOW() - INTERVAL '24 hours'
           ORDER BY ts_utc ASC`
      ),
      // Per-regime scale set by operator (LOW=1.0, TRANS=0.75, HIGH=0.5, CRISIS=0.25)
      dbQuery(
        `SELECT liquidity_param FROM regime_sizer_params
          WHERE regime_state = $1`,
        [latest.state]
      ),
      // Most recent tick whose state differs from current — duration starts after it.
      dbQuery(
        `SELECT ts_utc FROM intraday_regime_states
          WHERE state IS DISTINCT FROM $1
          ORDER BY ts_utc DESC
          LIMIT 1`,
        [latest.state]
      ),
    ]);

    const scale = sizerRes.rows.length ? Number(sizerRes.rows[0].liquidity_param) : null;
    // Duration: time since the FIRST tick of the current run. If no prior
    // different-state row exists, fall back to the very first tick on record.
    let durationStartTs;
    if (prevChangeRes.rows.length) {
      durationStartTs = prevChangeRes.rows[0].ts_utc;
    } else {
      const firstEverRes = await dbQuery(
        `SELECT MIN(ts_utc) AS ts FROM intraday_regime_states`
      );
      durationStartTs = firstEverRes.rows[0]?.ts || null;
    }
    const durationMinutes = durationStartTs
      ? Math.max(0, Math.round((Date.now() - new Date(durationStartTs).getTime()) / 60000))
      : null;

    const modelPath = require('path').join(
      __dirname, '../../../.agents/market-state/hmm_intraday_latest.pkl'
    );
    let model = null;
    try {
      const st = await fs.promises.stat(modelPath);
      model = { trained_at: st.mtime.toISOString(), bytes: st.size };
    } catch (_) {}

    res.json({
      available: true,
      latest,
      history:   histRes.rows,
      model,
      derived: {
        position_scale_pct: scale,
        duration_minutes:   durationMinutes,
      },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/regime/crypto', async (req, res) => {
  try {
    const latestRes = await dbQuery(
      `SELECT ts_utc, state, prior_state, confidence, hysteresis_streak,
              fired_redeploy, transition_tag, features_json
         FROM crypto_regime_states
         ORDER BY ts_utc DESC
         LIMIT 1`
    );
    if (!latestRes.rows.length) {
      return res.json({ available: false, reason: 'no_ticks_yet' });
    }
    const histRes = await dbQuery(
      `SELECT ts_utc, state, confidence
         FROM crypto_regime_states
         WHERE ts_utc >= NOW() - INTERVAL '24 hours'
         ORDER BY ts_utc ASC`
    );
    res.json({ available: true, latest: latestRes.rows[0], history: histRes.rows });
  } catch (err) {
    // Table may not exist if migration 118 isn't applied — degrade gracefully.
    res.json({ available: false, reason: 'error', error: err.message });
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

// ── Research page ──────────────────────────────────────────────────────────────
app.use('/api/research', require('./routes_research'));

// ── Pipeline Diagnostics — LangGraph run inspector ────────────────────────────
app.use('/api/pipelines', require('./routes_pipelines'));

// ── Regime eligibility (operator trim/expand) ─────────────────────────────────
app.use('/api/regime-eligibility', require('./routes_regime_eligibility'));

// Phase 2A — per-(strategy, regime) parameter table
app.use('/api/regime-params', require('./routes_regime_params'));

// Phase 2B — Mastermind proposal queue
app.use('/api/regime-proposals', require('./routes_regime_proposals'));

// Phase 2C — drift detection + priors
app.use('/api', require('./routes_regime_drift'));

// Phase 2D — bootstrap MC + calibration + overlap
// Phase 2E intraday-path MC also lives in routes_regime_2d.js
app.use('/api', require('./routes_regime_2d'));

// 2026-05-19: Phase 2F calibration-addenda routes removed. The operator
// no longer touches Mastermind's weekly review prompt — only the research
// page (papers / sources / hand-developed strategies) is the entry point.

// ── Dashboard ──────────────────────────────────────────────────────────────────
app.get('/', (req, res) => res.send(getDashboardHtml()));

function getDashboardHtml() {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenClaw</title>
<script>window.__DASHBOARD_BUILD__ = ${JSON.stringify(DASHBOARD_BUILD)};</script>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🦞</text></svg>">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.js" integrity="sha384-FcQlsUOd0TJjROrBxhJdUhXTUgNJQxTMcxZe6nHbaEfFL1zjQ+bq/uRoBQxb0KMo" crossorigin="anonymous"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d1117;--panel:#161b22;--border:#30363d;--border2:#21262d;
  --text:#e6edf3;--muted:#8b949e;--dim:#484f58;
  --blue:#58a6ff;--green:#3fb950;--red:#f85149;--yellow:#d29922;--purple:#bc8cff;--orange:#f0883e;
  /* Aliases used by Phase 2F Add-Addenda form (and any future inline forms).
     Without these, the form's var(--bg2,#f8f8f8) fallback fires light-gray
     on dark theme → unreadable. */
  --bg2:#161b22;--fg:#e6edf3;--accent:#3fb950;
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
.db-table th[data-sort-key]{user-select:none;transition:color .12s}
.db-table th[data-sort-key]:hover{color:var(--text)}
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
#research-page{display:none;position:absolute;inset:0;overflow:hidden;background:var(--bg)}

/* Pipeline Diagnostics — LangGraph run inspector */
#pipeline-page{display:none;position:absolute;inset:0;overflow-y:auto;overflow-x:hidden;background:var(--bg)}
#pipeline-inner{max-width:1600px;margin:0 auto;padding:14px 16px 28px;display:flex;flex-direction:column;gap:14px}
.pl-page-intro{display:flex;justify-content:space-between;align-items:flex-end;gap:16px;padding:2px 2px 0}
.pl-page-intro h2{font-size:15px;font-weight:600;color:var(--text);margin:0 0 2px;letter-spacing:.01em}
.pl-page-intro .pl-sub{font-size:11px;color:var(--muted);max-width:720px;line-height:1.45}
.pl-page-intro .pl-sub code{background:var(--border2);padding:1px 5px;border-radius:3px;font-size:10px}
.pl-actions{display:flex;gap:6px;align-items:center;font-size:10px;color:var(--dim)}

.pl-tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.pl-tile{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:2px;position:relative;overflow:hidden;transition:border-color .15s ease}
.pl-tile::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--accent,var(--border2));opacity:.7}
.pl-tile[data-state="warn"]{border-color:#71421b}
.pl-tile[data-state="warn"]::before{background:#f59e0b;opacity:1}
.pl-tile[data-state="err"]{border-color:#5a2222}
.pl-tile[data-state="err"]::before{background:#ef4444;opacity:1}
.pl-tile[data-state="ok"]::before{background:#22c55e;opacity:.85}
.pl-tile[data-state="live"]::before{background:#3b82f6;opacity:1}
.pl-tile-row{display:flex;align-items:center;gap:8px}
.pl-tile-icon{font-size:14px;line-height:1;opacity:.85}
.pl-tile-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:500}
.pl-tile-value{font-size:24px;font-weight:600;color:var(--text);font-variant-numeric:tabular-nums;line-height:1.1;margin-top:2px}
.pl-tile-sub{font-size:10px;color:var(--dim);margin-top:1px}

.pl-card{background:var(--panel);border:1px solid var(--border);border-radius:8px;display:flex;flex-direction:column;overflow:hidden}
.pl-card>header{background:#0d1117;border-bottom:1px solid var(--border2);padding:8px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);display:flex;justify-content:space-between;align-items:center;gap:8px}
.pl-card>header h3{font-size:11px;font-weight:700;color:var(--text);letter-spacing:.04em;margin:0;display:flex;align-items:center;gap:6px}
.pl-card .pl-body{padding:0;max-height:540px;overflow:auto}

.pl-runs-table{width:100%;font-size:11px;border-collapse:collapse}
.pl-runs-table th{text-align:left;padding:7px 12px;color:var(--muted);font-weight:500;text-transform:uppercase;font-size:10px;letter-spacing:.04em;border-bottom:1px solid var(--border2);position:sticky;top:0;background:var(--panel);z-index:1}
.pl-runs-table td{padding:7px 12px;border-bottom:1px solid #14181d;font-variant-numeric:tabular-nums;vertical-align:middle}
.pl-runs-table tbody tr{cursor:pointer;transition:background .12s ease}
.pl-runs-table tbody tr:hover{background:#11161c}
.pl-runs-table tbody tr.selected{background:#0e2540}
.pl-runs-table tbody tr.selected td:first-child{box-shadow:inset 3px 0 0 var(--blue)}
.pl-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.pl-thread-mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;color:var(--text)}

/* Status pills */
.pl-status{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:10px;font-size:9.5px;font-weight:600;text-transform:uppercase;letter-spacing:.04em;line-height:1.4}
.pl-status::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;flex-shrink:0}
.pl-status.running{background:rgba(59,130,246,.18);color:#7dd3fc}
.pl-status.running::before{animation:pl-pulse 1.4s ease-in-out infinite}
.pl-status.success,.pl-status.done,.pl-status.completed,.pl-status.ok{background:rgba(34,197,94,.15);color:#86efac}
.pl-status.error,.pl-status.failed,.pl-status.aborted{background:rgba(239,68,68,.16);color:#fca5a5}
.pl-status.unknown,.pl-status.persisted{background:rgba(148,163,184,.15);color:#94a3b8}
.pl-status.warn{background:rgba(245,158,11,.16);color:#fcd34d}
@keyframes pl-pulse{0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(125,211,252,.6)}50%{opacity:.6;box-shadow:0 0 0 4px rgba(125,211,252,0)}}

/* Graph pills — color per registered graph name */
.pl-graph-pill{display:inline-block;padding:2px 9px;border-radius:10px;font-size:10px;font-weight:500;background:var(--border2);color:var(--text);letter-spacing:.02em}
.pl-graph-pill[data-graph="daily-cycle"]{background:rgba(59,130,246,.18);color:#7dd3fc}
.pl-graph-pill[data-graph="paperhunter"]{background:rgba(168,85,247,.18);color:#d8b4fe}
.pl-graph-pill[data-graph="cycle"]{background:rgba(20,184,166,.18);color:#5eead4}
.pl-graph-pill[data-graph="unknown"]{background:rgba(148,163,184,.12);color:var(--muted)}

/* Detail panel */
.pl-detail{padding:14px 16px;min-height:60px;font-size:11px;color:var(--muted)}
.pl-detail-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--border2)}
.pl-detail-head .pl-thread-mono{font-size:11px;color:var(--text);user-select:all}
.pl-detail-meta{display:flex;gap:14px;font-size:10px;color:var(--muted);align-items:center;flex-wrap:wrap}
.pl-detail-meta b{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums}
.pl-section-title{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600;margin:14px 0 6px}
.pl-section-title:first-child{margin-top:0}

/* Gantt-style timeline */
.pl-timeline{display:flex;flex-direction:column;gap:3px;background:#0a0d12;border:1px solid var(--border2);border-radius:5px;padding:8px}
.pl-node-row{display:grid;grid-template-columns:130px 1fr 60px;align-items:center;gap:10px;padding:3px 6px;border-radius:3px;font-size:11px}
.pl-node-row:hover{background:#10151b}
.pl-node-row .pl-node-name{color:var(--text);font-weight:500;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px}
.pl-node-row .pl-node-bar-cell{position:relative;height:14px;background:#0d1117;border-radius:2px;overflow:hidden}
.pl-node-row .pl-node-bar{position:absolute;top:0;bottom:0;left:0;border-radius:2px;min-width:2px}
.pl-node-row.ok .pl-node-bar{background:linear-gradient(90deg,#16a34a 0%,#22c55e 100%)}
.pl-node-row.err .pl-node-bar{background:linear-gradient(90deg,#b91c1c 0%,#ef4444 100%)}
.pl-node-row.run .pl-node-bar{background:linear-gradient(90deg,#1d4ed8 0%,#3b82f6 100%);animation:pl-shimmer 1.6s linear infinite}
.pl-node-row.skip .pl-node-bar{background:#334155;opacity:.5}
.pl-node-row .pl-node-time{font-size:10px;color:var(--dim);text-align:right;font-variant-numeric:tabular-nums}
@keyframes pl-shimmer{0%{background-position:0% 0%}100%{background-position:100% 0%}}

/* Collapsible JSON viewer */
.pl-collapsible{border:1px solid var(--border2);border-radius:4px;background:#0a0d12;overflow:hidden}
.pl-collapsible-head{padding:6px 10px;font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);cursor:pointer;display:flex;justify-content:space-between;align-items:center;user-select:none}
.pl-collapsible-head:hover{color:var(--text)}
.pl-collapsible-head::after{content:"▾";font-size:9px;transition:transform .15s ease}
.pl-collapsible.collapsed .pl-collapsible-head::after{transform:rotate(-90deg)}
.pl-collapsible-body{padding:0 10px 8px;display:block}
.pl-collapsible.collapsed .pl-collapsible-body{display:none}
.pl-state-pre{font-size:10px;color:var(--text);overflow:auto;max-height:320px;white-space:pre;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;line-height:1.45;margin:0;padding:4px 0}

/* Live stream */
.pl-stream{background:#0a0d12;border:1px solid var(--border2);border-radius:5px;padding:8px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;color:var(--text);max-height:300px;overflow:auto;line-height:1.55}
.pl-stream-line{padding:1px 0;white-space:pre-wrap;word-break:break-word;display:flex;gap:8px}
.pl-stream-line .pl-stream-ts{color:var(--dim);flex-shrink:0;font-size:10px}
.pl-stream-line.evt{color:#7dd3fc}
.pl-stream-line.run{color:#86efac}
.pl-stream-line.err{color:#fca5a5}
.pl-stream-line.sys{color:var(--dim);font-style:italic}

.pl-dim{color:var(--dim)}
.pl-empty{padding:30px 20px;text-align:center;color:var(--muted);font-size:11px;line-height:1.6}
.pl-empty .pl-empty-icon{font-size:24px;opacity:.4;margin-bottom:8px}
.pl-empty code{background:var(--border2);padding:1px 6px;border-radius:3px;font-size:10px;color:var(--text)}
.pl-controls{display:flex;gap:8px;align-items:center;font-size:11px;color:var(--muted)}
.pl-controls label{display:flex;align-items:center;gap:4px;font-size:10px;text-transform:none;letter-spacing:0}
.pl-controls select,.pl-controls input{background:#0d1117;color:var(--text);border:1px solid var(--border);border-radius:4px;padding:3px 8px;font-size:11px;font-family:inherit}
.pl-controls select:focus,.pl-controls input:focus{outline:none;border-color:var(--blue)}
.pl-btn{background:none;border:1px solid var(--border);color:var(--muted);font-size:10px;cursor:pointer;font-family:inherit;padding:3px 9px;border-radius:4px;transition:all .12s ease;display:inline-flex;align-items:center;gap:4px}
.pl-btn:hover:not(:disabled){color:var(--text);border-color:var(--blue)}
.pl-btn:disabled{opacity:.5;cursor:not-allowed}
.pl-btn.primary{background:rgba(59,130,246,.12);border-color:rgba(59,130,246,.4);color:#7dd3fc}
.pl-btn.primary:hover{background:rgba(59,130,246,.2)}
.pl-btn.danger:hover{border-color:#ef4444;color:#fca5a5}
.pl-spinner{display:inline-block;width:10px;height:10px;border:1.5px solid var(--border);border-top-color:var(--blue);border-radius:50%;animation:pl-spin .8s linear infinite;vertical-align:middle}
@keyframes pl-spin{to{transform:rotate(360deg)}}

@media (max-width:900px){
  .pl-tiles{grid-template-columns:repeat(2,1fr)}
  .pl-page-intro{flex-direction:column;align-items:flex-start}
  .pl-runs-table th:nth-child(4),.pl-runs-table td:nth-child(4){display:none}
}
@media (max-width:560px){
  .pl-tiles{grid-template-columns:1fr}
  .pl-runs-table th:nth-child(5),.pl-runs-table td:nth-child(5){display:none}
  .pl-node-row{grid-template-columns:90px 1fr 50px}
}
#research-inner{height:100%;max-width:1600px;margin:0 auto;padding:12px;display:flex;flex-direction:column;gap:10px;overflow-y:auto;overflow-x:hidden}
.rs-card{background:var(--panel);border:1px solid var(--border);border-radius:8px;display:flex;flex-direction:column;overflow:hidden;min-height:0}
.rs-card header{background:#0d1117;border-bottom:1px solid var(--border2);padding:7px 12px;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);display:flex;justify-content:space-between;align-items:center;gap:8px}
.rs-card header h3{font-size:11px;font-weight:700;color:var(--text);letter-spacing:.04em}
.rs-card .rs-body{flex:1;overflow:auto;padding:8px 12px;min-height:0}
/* Chat is now a fullscreen overlay reached via the floating speech-bubble FAB. */
.rs-chat{position:fixed;inset:0;z-index:90;background:rgba(13,17,23,0.96);backdrop-filter:blur(8px);display:none;flex-direction:column;border-radius:0;border:0}
.rs-chat.open{display:flex}
body.rs-chat-locked{overflow:hidden}
/* Right-col now spans full research-inner width with a 2-col grid. Papers + Staging + Runs go full-width. */
.rs-right-col{display:grid;grid-template-columns:1fr 1fr;gap:10px;flex:1;min-height:0}
.rs-campaigns{grid-column:1;max-height:230px}
.rs-queue{grid-column:2;min-height:230px;max-height:none}
.rs-papers{grid-column:1 / -1;min-height:340px}
.rs-right-col > .rs-card:nth-child(4){grid-column:1 / -1;max-height:240px}
.rs-runs{max-height:110px}
/* Floating speech-bubble + brain FAB to launch the chat overlay. */
.chat-fab{position:fixed;right:22px;bottom:22px;z-index:80;width:58px;height:58px;border-radius:50%;border:0;background:#0d0420;box-shadow:0 8px 24px rgba(168,85,247,0.45),0 2px 6px rgba(0,0,0,0.5);cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;transition:transform .15s ease,box-shadow .2s ease}
.chat-fab:hover{transform:translateY(-2px) scale(1.04);box-shadow:0 12px 32px rgba(168,85,247,0.6),0 3px 8px rgba(0,0,0,0.5)}
.chat-fab:active{transform:translateY(0) scale(0.98)}
.chat-fab svg{display:block}
.chat-fab.hidden{display:none}
.rs-chat .chat-close-bar{position:absolute;top:14px;right:14px;z-index:5}
.rs-chat .chat-close-bar button{background:rgba(168,85,247,0.12);border:1px solid rgba(168,85,247,0.45);color:#c084fc;padding:5px 12px;border-radius:6px;font-size:11px;cursor:pointer;font-family:inherit;letter-spacing:.04em}
.rs-chat .chat-close-bar button:hover{background:rgba(168,85,247,0.22);color:#fff}
.rs-chat > header{background:rgba(13,17,23,0.85);border-bottom:1px solid rgba(168,85,247,0.25);padding:14px 24px}
.rs-chat .chat-scroll{max-width:920px;width:100%;margin:0 auto}
.rs-chat .chat-input-row{max-width:920px;width:100%;margin:0 auto;border-top:1px solid rgba(168,85,247,0.2);background:transparent}
/* Chat */
.chat-sessions-bar{display:flex;flex-wrap:wrap;gap:4px;align-items:center}
.session-pill{display:inline-block;padding:2px 8px;border-radius:10px;background:var(--border2);border:1px solid var(--border);color:var(--muted);font-size:10px;cursor:pointer;font-family:inherit}
.session-pill.active{color:var(--blue);border-color:var(--blue)}
.chat-scroll{flex:1;overflow-y:auto;padding:10px 14px;display:flex;flex-direction:column;gap:8px;min-height:0;background:var(--bg)}
.chat-msg{max-width:80%;padding:7px 11px;border-radius:8px;font-size:12px;white-space:pre-wrap;word-wrap:break-word;line-height:1.5;font-family:'SF Pro Text','Inter',system-ui,sans-serif}
.chat-msg.user{align-self:flex-end;background:rgba(88,166,255,0.12);border:1px solid rgba(88,166,255,0.3);color:var(--text)}
.chat-msg.assistant{align-self:flex-start;background:var(--panel);border:1px solid var(--border);color:var(--text)}
.chat-msg.tool{align-self:flex-start;background:rgba(188,140,255,0.08);border:1px dashed var(--border);color:var(--muted);font-family:'SF Mono',monospace;font-size:10px;max-width:92%}
.chat-msg.tool .tname{color:var(--purple)}
.chat-msg.err{align-self:flex-start;background:rgba(248,81,73,0.12);border:1px solid rgba(248,81,73,0.35);color:var(--red);font-family:'SF Mono',monospace;font-size:11px}
.chat-meta{font-size:9px;color:var(--dim);font-family:'SF Mono',monospace;text-align:right;padding:0 14px}
.chat-input-row{display:flex;gap:6px;padding:8px 10px;border-top:1px solid var(--border2);background:var(--panel)}
.chat-input-row textarea{flex:1;resize:none;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font:inherit;font-size:12px;min-height:36px;max-height:120px;outline:none}
.chat-input-row textarea:focus{border-color:var(--blue)}
.chat-input-row button{background:var(--blue);color:#fff;border:0;border-radius:6px;font-weight:600;font-size:12px;padding:0 14px;cursor:pointer;font-family:inherit}
.chat-input-row button:disabled{opacity:.4;cursor:not-allowed}
/* Queue + papers */
.rs-row{padding:5px 0;border-bottom:1px solid var(--border2);font-size:11px}
.rs-row:last-child{border-bottom:none}
.rs-row .rs-title{font-weight:500;color:var(--text);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:100%}
.rs-row .rs-meta{color:var(--muted);font-size:9px;font-family:'SF Mono',monospace;margin-top:2px}
.rs-pill{display:inline-block;padding:0 6px;border-radius:8px;font-size:9px;font-family:'SF Mono',monospace;margin-right:4px}
.rs-pill.ok{background:rgba(63,185,80,0.15);color:var(--green)}
.rs-pill.warn{background:rgba(210,153,34,0.15);color:var(--yellow)}
.rs-pill.err{background:rgba(248,81,73,0.15);color:var(--red)}
.rs-pill.muted{background:var(--border2);color:var(--muted)}
.rs-filter{display:flex;gap:6px;align-items:center}
.rs-filter select,.rs-filter input{background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:2px 6px;font:inherit;font-size:10px;font-family:'SF Mono',monospace;outline:none}
.rs-filter select:focus,.rs-filter input:focus{border-color:var(--blue)}
/* Staging approve/reject */
.stage-row{display:grid;grid-template-columns:1fr auto;gap:10px;padding:6px 0;border-bottom:1px solid var(--border2)}
.stage-actions{display:flex;gap:4px;flex-shrink:0}
.stage-btn{background:var(--border2);border:1px solid var(--border);color:var(--muted);padding:2px 8px;border-radius:4px;font-size:10px;cursor:pointer;font-family:inherit}
.stage-btn.ok{color:var(--green);border-color:rgba(63,185,80,0.35)}
.stage-btn.err{color:var(--red);border-color:rgba(248,81,73,0.35)}
.stage-btn:disabled{opacity:.4;cursor:not-allowed}
/* Iteration-2 Research polish */
.rs-campaigns{flex:0 0 auto;max-height:200px}
.chat-header-strip{display:flex;align-items:center;gap:6px;flex:1;min-width:0}
.chat-session-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px;color:var(--text);font-weight:600;letter-spacing:.02em;text-transform:none}
.chat-session-cost{color:var(--muted);font-size:10px;font-family:'SF Mono',monospace;flex-shrink:0;white-space:nowrap}
.chat-session-btns{display:flex;gap:4px;flex-shrink:0}
.chat-btn-slim{background:var(--border2);border:1px solid var(--border);color:var(--muted);padding:2px 8px;border-radius:4px;font-size:10px;cursor:pointer;font-family:inherit;letter-spacing:0;text-transform:none}
.chat-btn-slim:hover{color:var(--text);border-color:var(--blue)}
.chat-msg.tool{cursor:pointer;transition:background .15s ease}
.chat-msg.tool:hover{background:rgba(188,140,255,0.12)}
.chat-msg.tool .chat-tool-summary{display:flex;align-items:center;gap:6px;font-size:10px}
.chat-msg.tool .chat-tool-body{display:none;margin-top:6px;padding-top:6px;border-top:1px dashed var(--border2);white-space:pre-wrap;font-size:10px;max-height:280px;overflow-y:auto;color:var(--text)}
.chat-msg.tool.expanded .chat-tool-body{display:block}
.chat-msg.tool .chat-tool-badge{color:var(--purple);font-weight:600}
.chat-msg.tool.result{background:rgba(63,185,80,0.05);border-left:2px solid var(--purple);border-color:var(--border2) var(--border2) var(--border2) var(--purple)}
/* Single-line streaming progress — replaces per-tool bubble spam during a turn.
 * The text in .chat-progress-text updates in place as new tool_use events
 * arrive; on turn completion the whole row removes itself. Three pulsing
 * purple dots (Claude shimmer style) sit to the left of the text. */
.chat-progress{align-self:flex-start;display:flex;align-items:center;gap:9px;font-size:11px;color:#c084fc;font-family:'SF Pro Text','Inter',system-ui,sans-serif;padding:6px 12px;background:rgba(168,85,247,0.06);border:1px solid rgba(168,85,247,0.18);border-radius:8px;max-width:80%;letter-spacing:.01em}
.chat-progress .chat-progress-text{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat-progress-dots{display:inline-flex;gap:4px;align-items:center;flex-shrink:0}
.chat-progress-dots span{width:6px;height:6px;border-radius:50%;background:#a855f7;display:block;animation:chat-pulse 1.3s ease-in-out infinite}
.chat-progress-dots span:nth-child(2){animation-delay:.18s}
.chat-progress-dots span:nth-child(3){animation-delay:.36s}
@keyframes chat-pulse{0%,70%,100%{opacity:.25;transform:scale(.78)}35%{opacity:1;transform:scale(1.12)}}
/* Used-tools footer shown after the assistant message — clickable to expand
 * a compact log of every tool call from this turn. Lives inside the assistant
 * bubble so it visually attaches to the answer it produced. */
.chat-tool-log{margin-top:8px;padding-top:6px;border-top:1px dashed var(--border2)}
.chat-tool-log-header{display:inline-flex;align-items:center;gap:5px;font-size:10px;color:var(--purple);cursor:pointer;font-family:'SF Mono',monospace;background:rgba(168,85,247,0.07);padding:2px 8px;border-radius:9px;border:1px solid rgba(168,85,247,0.18)}
.chat-tool-log-header:hover{background:rgba(168,85,247,0.15)}
.chat-tool-log-list{display:none;margin-top:6px;flex-direction:column;gap:4px}
.chat-tool-log.expanded .chat-tool-log-list{display:flex}
.chat-tool-log-row{font-family:'SF Mono',monospace;font-size:10px;color:var(--muted);padding:3px 6px;background:var(--bg);border:1px solid var(--border2);border-radius:4px;cursor:pointer}
.chat-tool-log-row:hover{border-color:var(--purple);color:var(--text)}
.chat-tool-log-row.expanded{white-space:pre-wrap;word-break:break-word}
.chat-md{line-height:1.55}
.chat-md p{margin:0 0 6px 0}
.chat-md p:last-child{margin-bottom:0}
.chat-md strong{color:var(--text);font-weight:700}
.chat-md em{color:var(--text);font-style:italic}
.chat-md code{background:var(--bg);padding:1px 5px;border-radius:3px;font-family:'SF Mono',monospace;font-size:11px;color:var(--yellow)}
.chat-md pre{background:var(--bg);border:1px solid var(--border2);border-radius:4px;padding:8px 10px;margin:6px 0;overflow-x:auto;font-family:'SF Mono',monospace;font-size:11px;line-height:1.4}
.chat-md pre code{background:none;padding:0;color:var(--text)}
.chat-md ul,.chat-md ol{margin:4px 0 6px 18px;padding:0}
.chat-md li{margin:2px 0}
.chat-md a{color:var(--blue);text-decoration:none}
.chat-md a:hover{text-decoration:underline}
.chat-md h1,.chat-md h2,.chat-md h3{font-size:12px;font-weight:700;color:var(--text);margin:8px 0 4px 0;letter-spacing:0;text-transform:none}
.chat-md blockquote{border-left:3px solid var(--border);padding-left:8px;margin:6px 0;color:var(--muted);font-style:italic}
/* Campaigns card */
.camp-row{padding:6px 0;border-bottom:1px solid var(--border2);cursor:pointer;transition:background .12s ease}
.camp-row:last-child{border-bottom:none}
.camp-row:hover{background:rgba(88,166,255,0.05)}
.camp-head{display:flex;justify-content:space-between;align-items:center;gap:6px;font-size:11px}
.camp-name{font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.camp-meta{color:var(--muted);font-size:9px;font-family:'SF Mono',monospace;margin-top:2px;display:flex;gap:10px;flex-wrap:wrap}
.camp-progress-bar{height:3px;background:var(--border2);border-radius:2px;margin-top:4px;overflow:hidden}
.camp-progress-fill{height:100%;background:var(--blue);transition:width .3s ease}
.camp-pill.planning{background:var(--border2);color:var(--muted)}
.camp-pill.awaiting_ack{background:rgba(210,153,34,0.18);color:var(--yellow)}
.camp-pill.running{background:rgba(88,166,255,0.15);color:var(--blue)}
.camp-pill.completed{background:rgba(63,185,80,0.15);color:var(--green)}
.camp-pill.cancelled,.camp-pill.failed{background:rgba(248,81,73,0.12);color:var(--red)}
.camp-detail{padding:6px 0 0 0;font-size:10px;color:var(--muted);font-family:'SF Mono',monospace}
.camp-detail .detail-item{padding:2px 0}
.camp-cancel-btn{margin-top:6px;background:transparent;border:1px solid var(--border);color:var(--muted);padding:3px 8px;border-radius:3px;font-size:10px;cursor:pointer;font-family:inherit}
.camp-cancel-btn:hover{color:var(--red);border-color:var(--red)}
.camp-dag{margin-top:8px;border:1px solid var(--border2);border-radius:4px;overflow:hidden;background:var(--bg)}
.camp-dag table{width:100%;border-collapse:collapse;font-family:'SF Mono',monospace;font-size:10px}
.camp-dag thead th{background:#0d1117;color:var(--muted);font-weight:600;padding:5px 8px;text-align:left;border-bottom:1px solid var(--border2);letter-spacing:.04em}
.camp-dag tbody td{padding:4px 8px;border-bottom:1px solid var(--border2);color:var(--text);white-space:nowrap}
.camp-dag tbody tr:last-child td{border-bottom:none}
.camp-dag tbody tr:hover{background:rgba(88,166,255,0.04)}
.dag-slug{font-weight:600;color:var(--text)}
.dag-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}
.dag-dot.on{background:var(--green)}
.dag-dot.off{background:var(--border);border:1px solid var(--border2)}
.dag-dot.partial{background:var(--yellow)}
.dag-dot.neg{background:var(--red)}
.dag-dot.blue{background:var(--blue)}
.dag-cell{display:flex;align-items:center;gap:3px}
.dag-cell-label{color:var(--muted);font-size:9px}
/* Quick-backtest badge on staging rows */
.qbt-badge{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;border-radius:3px;font-size:9px;font-family:'SF Mono',monospace;background:var(--border2);border:1px solid var(--border);color:var(--muted);margin-right:4px}
.qbt-badge.ok{background:rgba(63,185,80,0.10);border-color:rgba(63,185,80,0.35);color:var(--green)}
.qbt-badge.neg{background:rgba(248,81,73,0.10);border-color:rgba(248,81,73,0.35);color:var(--red)}
.qbt-badge.pending{background:rgba(210,153,34,0.08);border-color:rgba(210,153,34,0.3);color:var(--yellow)}
.qbt-badge.deferred{background:rgba(188,140,255,0.08);border-color:rgba(188,140,255,0.3);color:var(--purple)}
.qbt-metrics{display:flex;gap:3px;flex-wrap:wrap}
/* Staging expanded */
.stage-row{cursor:pointer}
.stage-row.expanded{background:rgba(88,166,255,0.04)}
.stage-expanded-body{grid-column:1 / -1;padding:8px 12px 4px 0;border-top:1px dashed var(--border2);margin-top:5px;font-size:10px;color:var(--muted);font-family:'SF Mono',monospace;line-height:1.5}
.stage-expanded-body pre{background:var(--bg);border:1px solid var(--border2);border-radius:3px;padding:6px 8px;overflow-x:auto;color:var(--text);font-size:10px;margin:4px 0}
.stage-expanded-body .stage-ask-btn{background:transparent;border:1px solid var(--blue);color:var(--blue);padding:3px 9px;border-radius:3px;font-size:10px;cursor:pointer;font-family:inherit;margin-top:6px}
.stage-expanded-body .stage-ask-btn:hover{background:rgba(88,166,255,0.12)}
/* Sessions drawer */
.sessions-drawer{position:fixed;right:-400px;top:0;bottom:0;width:360px;background:var(--panel);border-left:1px solid var(--border);box-shadow:-4px 0 16px rgba(0,0,0,0.5);z-index:9998;transition:right .22s ease;display:flex;flex-direction:column}
.sessions-drawer.open{right:0}
.sessions-drawer header{padding:10px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
.sessions-drawer header h3{font-size:12px;font-weight:700;color:var(--text);letter-spacing:.04em;text-transform:uppercase}
.sessions-drawer-body{flex:1;overflow-y:auto;padding:4px 0}
.session-item{padding:8px 14px;border-bottom:1px solid var(--border2);cursor:pointer;transition:background .12s ease}
.session-item:hover{background:rgba(88,166,255,0.05)}
.session-item.active{background:rgba(88,166,255,0.10);border-left:2px solid var(--blue)}
.session-item .si-title{font-size:11px;color:var(--text);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.session-item .si-meta{font-size:9px;color:var(--muted);font-family:'SF Mono',monospace;margin-top:2px;display:flex;gap:8px}
.sessions-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.45);z-index:9997;opacity:0;pointer-events:none;transition:opacity .22s ease}
.sessions-backdrop.open{opacity:1;pointer-events:auto}
/* Paper modal */
.paper-modal{position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:none;align-items:center;justify-content:center;padding:40px}
.paper-modal.open{display:flex}
.paper-modal .pm-card{background:var(--panel);border:1px solid var(--border);border-radius:8px;max-width:900px;width:100%;max-height:90vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.6)}
.paper-modal .pm-card.du-card{max-width:980px}

/* ── Data Usage (ranked horizontal bar list) ─────────────────────────── */
.du-wrap{padding:14px 18px 18px}
.du-summary{font-size:10.5px;color:var(--muted);margin-bottom:10px}
.du-summary b{color:var(--text);font-weight:700}
.du-summary-top{display:flex;flex-wrap:wrap;gap:14px;align-items:baseline;justify-content:space-between}
.du-summary-explain{font-size:10px;color:var(--dim);margin-top:4px}
.du-list{max-height:70vh;overflow-y:auto;display:flex;flex-direction:column;gap:2px;padding-right:4px}
/* Single-row, table-like layout: name | category | history | description |
   bar | count. Each row collapses to ~24px so 50+ parameters fit without
   scrolling for normal usage. */
.du-row{display:grid;grid-template-columns:135px 92px 86px minmax(160px,1fr) minmax(140px,1.4fr) 56px;gap:10px;align-items:center;padding:4px 10px;border-radius:4px;background:var(--border2);border:1px solid transparent;transition:border-color .12s;min-height:24px}
.du-row:hover{border-color:var(--border)}
.du-row .du-name{min-width:0;font-size:11px;font-weight:700;color:var(--text);letter-spacing:.02em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.du-row .du-cat{font-size:8.5px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:2px 7px;border-radius:3px;color:#fff;text-align:center;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.du-cat-prices       {background:rgba(88,166,255,0.85)}
.du-cat-options_eod  {background:rgba(188,140,255,0.85)}
.du-cat-financials   {background:rgba(63,185,80,0.85)}
.du-cat-earnings     {background:rgba(46,160,67,0.85);color:#fff}
.du-cat-insider      {background:rgba(240,136,62,0.85)}
.du-cat-macro        {background:rgba(210,153,34,0.95);color:#000}
.du-cat-computed     {background:var(--border);color:var(--muted)}
.du-cat-other        {background:var(--border);color:var(--muted)}
.du-row .du-history{font-size:9.5px;color:var(--dim);letter-spacing:.04em;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.du-row .du-desc{min-width:0;font-size:9.5px;color:var(--muted);font-style:italic;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.du-bar-track{position:relative;height:14px;border-radius:3px;background:rgba(48,54,61,0.5);overflow:hidden}
.du-bar-fill{position:absolute;top:0;left:0;bottom:0;background:linear-gradient(90deg,rgba(88,166,255,0.85),rgba(88,166,255,0.55));border-radius:3px;transition:width .3s ease}
.du-bar-empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:8.5px;color:var(--dim);font-style:italic;letter-spacing:.04em}
.du-row.du-zero .du-bar-fill{display:none}
.du-count{text-align:right;font-size:11px;font-weight:700;color:var(--text);font-variant-numeric:tabular-nums;white-space:nowrap}
.du-count small{font-size:8.5px;font-weight:400;color:var(--dim);margin-left:3px}
/* Header strip mirroring the row grid so columns line up. */
.du-list-head{display:grid;grid-template-columns:135px 92px 86px minmax(160px,1fr) minmax(140px,1.4fr) 56px;gap:10px;align-items:center;padding:0 10px 4px;font-size:9px;font-weight:700;letter-spacing:.06em;color:var(--dim);text-transform:uppercase;border-bottom:1px solid var(--border2);margin-bottom:4px}
.du-list-head > span:nth-child(6){text-align:right}
@media (max-width: 760px){
  .du-row, .du-list-head{grid-template-columns:1fr 80px 70px 56px;}
  .du-row .du-cat, .du-list-head > span:nth-child(2){grid-column:2 / 3}
  .du-row .du-history, .du-list-head > span:nth-child(3){grid-column:3 / 4}
  .du-row .du-desc, .du-list-head > span:nth-child(4){display:none}
  .du-row .du-bar-track, .du-list-head > span:nth-child(5){grid-column:1 / -1}
  .du-row .du-count, .du-list-head > span:nth-child(6){grid-column:4 / 5}
}
.pm-head{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.pm-head h2{font-size:14px;font-weight:700;color:var(--text);line-height:1.4;letter-spacing:0;text-transform:none}
.pm-close{background:transparent;border:1px solid var(--border);color:var(--muted);font-size:12px;cursor:pointer;padding:3px 10px;border-radius:3px;font-family:inherit;flex-shrink:0}
.pm-close:hover{color:var(--red);border-color:var(--red)}
.pm-body{flex:1;overflow-y:auto;padding:16px 20px;font-size:12px;line-height:1.55;color:var(--text)}
.pm-section{margin-bottom:14px}
.pm-section h4{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700;margin-bottom:4px}
.pm-section .pm-abstract{color:var(--text);line-height:1.55;font-size:11px}
.pm-meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;font-family:'SF Mono',monospace;font-size:10px;color:var(--muted)}
.pm-meta span strong{color:var(--text);font-weight:600}
.pm-gates{font-family:'SF Mono',monospace;font-size:10px}
.pm-gate-row{padding:4px 0;border-bottom:1px dashed var(--border2);display:grid;grid-template-columns:120px 70px 1fr;gap:8px}
.pm-gate-row:last-child{border-bottom:none}
.pm-gate-outcome.pass{color:var(--green)}
.pm-gate-outcome.reject{color:var(--red)}
.pm-gate-outcome.buildable,.pm-gate-outcome.error{color:var(--yellow)}
.pm-json{background:var(--bg);border:1px solid var(--border2);border-radius:4px;padding:8px 10px;font-family:'SF Mono',monospace;font-size:10px;max-height:260px;overflow:auto;color:var(--text);white-space:pre-wrap}
#strategies-inner{max-width:1400px;margin:0 auto;padding:20px;display:flex;flex-direction:column;gap:12px}
/* Per-regime conviction-gate sliders on the Strategies page — minimum
   number of distinct strategies acting on a ticker in its net direction
   (2026-08-22; integer 1..10). Mirrors the Portfolio-page leverage slider's
   visual language but stacked into a 4-card grid so each regime gets its
   own slider. Gradient anchors: yellow at 1 (open — any single strategy
   acts) → green at 3 → blue at ~6.5 (tightening) → purple at 10 (only
   ten-strategy consensus trades). The hue progression reads as
   "broader → focused" rather than "safe → dangerous". */
.st-sharpe-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;padding:10px 4px 4px}
.st-sharpe-card{background:rgba(15,20,28,0.5);border:1px solid var(--border2);border-radius:8px;padding:14px 16px;display:flex;flex-direction:column;gap:8px}
.st-sharpe-card.current-regime{border-color:var(--blue);box-shadow:0 0 0 1px rgba(88,166,255,0.35),0 4px 14px rgba(88,166,255,0.08)}
.st-sharpe-card-head{display:flex;justify-content:space-between;align-items:baseline;gap:6px}
.st-sharpe-card-regime{font-size:11px;font-weight:700;letter-spacing:.08em;color:var(--text);text-transform:uppercase}
.st-sharpe-card-tag{font-size:9px;color:var(--blue);letter-spacing:.06em;text-transform:uppercase;font-weight:600;opacity:0;transition:opacity .12s}
.st-sharpe-card.current-regime .st-sharpe-card-tag{opacity:1}
.st-sharpe-card-value{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--text);text-align:center;line-height:1;padding:4px 0 2px}
.st-sharpe-card-slider{width:100%;height:8px;-webkit-appearance:none;appearance:none;background:linear-gradient(90deg,#facc15 0%,#2ea043 22.2%,#58a6ff 61%,#a371f7 100%);border-radius:4px;outline:none;cursor:pointer;border:1px solid var(--border)}
.st-sharpe-card-slider::-webkit-slider-thumb{-webkit-appearance:none;width:20px;height:20px;border-radius:50%;background:var(--bg);border:2px solid var(--blue);cursor:grab;box-shadow:0 1px 4px rgba(0,0,0,0.5)}
.st-sharpe-card-slider::-webkit-slider-thumb:active{cursor:grabbing;background:var(--blue);box-shadow:0 0 0 4px rgba(88,166,255,0.18)}
.st-sharpe-card-slider::-moz-range-thumb{width:20px;height:20px;border-radius:50%;background:var(--bg);border:2px solid var(--blue);cursor:grab;box-shadow:0 1px 4px rgba(0,0,0,0.5)}
.st-sharpe-card-range{display:flex;justify-content:space-between;font-size:9px;color:var(--dim);padding:0 2px;font-variant-numeric:tabular-nums;letter-spacing:.04em}
.st-sharpe-card-status{font-size:9px;color:var(--muted);text-align:center;min-height:11px;line-height:1;letter-spacing:.04em}
.st-sharpe-card-empty{grid-column:1/-1;text-align:center;color:var(--muted);font-size:11px;padding:18px}
@media(max-width:920px){.st-sharpe-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.st-sharpe-grid{grid-template-columns:1fr}}
/* Strategy Activation card — threshold slider + assigner dry-run preview.
   Reuses the conviction-gate slider visual language (st-sharpe-card). The
   note states the LIVE weekly enforcement (armed 2026-07-13),
   so everything this card does is config + hypothetical preview. */
.st-act-grid{display:grid;grid-template-columns:minmax(230px,300px) 1fr;gap:14px;padding:10px 4px 4px;align-items:stretch}
.st-act-preview{background:rgba(15,20,28,0.5);border:1px solid var(--border2);border-radius:8px;padding:12px 14px;display:flex;flex-direction:column;gap:10px}
.st-act-preview-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.st-act-preview-bar .st-action-btn{font-size:11px;padding:5px 12px;color:var(--text);border-color:var(--blue)}
.st-act-preview-bar .st-action-btn:hover{color:var(--blue)}
.st-act-preview-bar .st-action-btn:disabled{opacity:.5;cursor:wait}
.st-act-preview-empty{color:var(--muted);font-size:11px;padding:6px 2px}
.st-act-summary{font-size:11px;color:var(--text);line-height:1.7}
.st-act-summary .st-act-hypo{color:var(--yellow);font-weight:700;letter-spacing:.06em;font-size:9px;text-transform:uppercase;border:1px solid rgba(250,204,21,0.5);border-radius:3px;padding:1px 6px;margin-right:8px;white-space:nowrap}
.st-act-table{border-collapse:collapse;width:100%;font-size:11px;font-variant-numeric:tabular-nums}
.st-act-table th{padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right;color:var(--muted);font-size:10px;font-weight:600}
.st-act-table th:first-child,.st-act-table td:first-child{text-align:left}
.st-act-table td{padding:5px 8px;border-bottom:1px dashed var(--border2);text-align:right;color:var(--text)}
.st-act-table td.st-act-up{color:var(--green)}
.st-act-table td.st-act-down{color:var(--red)}
.st-act-dormant{max-height:140px;overflow-y:auto;border:1px solid var(--border2);border-radius:6px;padding:8px 10px;background:var(--bg);font-family:'SF Mono',monospace;font-size:10px;line-height:1.9}
.st-act-dormant span{display:inline-block;margin-right:10px;color:var(--red)}
@media(max-width:920px){.st-act-grid{grid-template-columns:1fr}}

.st-tiles{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:4px}
.st-tile{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.st-tile-label{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:6px}
.st-tile-value{font-size:22px;font-weight:700}
.st-tile-sub{font-size:10px;color:var(--muted);margin-top:3px}
.st-tile-link{display:inline-block;font-size:10px;color:var(--blue);margin-top:6px;cursor:pointer;letter-spacing:.04em;border-bottom:1px dashed transparent;transition:border-color .12s}
.st-tile-link:hover{border-bottom-color:var(--blue)}
.st-sub-label{color:var(--muted);font-weight:400;font-size:10px}
/* Sub-status badges (Active Stack) */
.sg-status{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.04em;white-space:nowrap}
.sg-status-live   {color:#fff;background:var(--green)}
.sg-status-stale  {color:#000;background:var(--yellow)}
.sg-status-waiting{color:var(--muted);background:transparent;border:1px dashed var(--border)}
/* Lifecycle badges (Inactive Stack + Candidates) */
.st-badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.st-badge-paper      {color:var(--muted);background:var(--border2);border:1px solid var(--border)}
.st-badge-candidate  {color:var(--muted);background:var(--border2);border:1px solid var(--border)}
.st-badge-deprecated {color:var(--dim);background:transparent;border:1px solid var(--border)}
.st-badge-archived   {color:var(--dim);background:transparent;border:1px solid var(--border)}
.st-badge-orphan     {color:var(--dim);background:transparent;border:1px dashed var(--border)}
/* Action buttons */
.st-action-btn{background:none;border:1px solid var(--border);color:var(--muted);font-size:10px;cursor:pointer;font-family:inherit;padding:2px 8px;border-radius:4px;margin-right:4px}
.st-action-btn:hover{color:var(--text);border-color:var(--blue)}
.st-unstack-btn:hover{border-color:var(--red);color:var(--red)}
.st-approve-btn:hover{border-color:var(--green);color:var(--green)}
/* Approve button variants — consistent sizing, clear state communication */
.st-approve-async{border-style:solid;color:var(--blue);border-color:rgba(88,166,255,0.45)}
.st-approve-async:hover{border-color:var(--blue);background:rgba(88,166,255,0.08)}
/* Approval-picker modal: regime-set chooser fired by stApprove(). */
.st-approve-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.55);display:flex;align-items:center;justify-content:center;z-index:9999;animation:stModalIn .12s ease-out}
.st-approve-modal{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:18px 20px;min-width:480px;max-width:560px;color:var(--text);box-shadow:0 12px 36px rgba(0,0,0,0.55);font-family:inherit}
.st-approve-bt-table{width:100%;border-collapse:collapse;margin:6px 0 12px;font-size:11px}
.st-approve-bt-table th{text-align:left;font-weight:600;color:var(--muted);font-size:9px;text-transform:uppercase;letter-spacing:.05em;padding:4px 6px;border-bottom:1px solid var(--border2)}
.st-approve-bt-table td{padding:5px 6px;border-bottom:1px solid var(--border2)}
.st-approve-bt-table td.num{text-align:right;font-variant-numeric:tabular-nums}
.st-approve-bt-table td.num.pos{color:var(--green)}
.st-approve-bt-table td.num.neg{color:var(--red)}
.st-approve-bt-table td.num.low{color:var(--yellow)}
.st-approve-modes{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 12px;padding:8px;background:var(--bg);border-radius:5px;border:1px solid var(--border2)}
.st-approve-mode-btn{background:var(--panel);border:1px solid var(--border);color:var(--text);font-size:11px;padding:4px 10px;border-radius:4px;cursor:pointer;font-family:inherit;display:inline-flex;align-items:center;gap:6px}
.st-approve-mode-btn:hover{border-color:var(--blue);color:var(--blue)}
.st-approve-mode-hint{font-size:10px;color:var(--muted);margin-left:auto}
.st-approve-footer{display:flex;gap:8px;align-items:center}
.st-approve-footer button{background:var(--panel);border:1px solid var(--border);color:var(--text);font-size:11px;padding:5px 14px;border-radius:4px;cursor:pointer;font-family:inherit}
.st-approve-footer button.st-approve-confirm{background:var(--green);color:#000;border-color:var(--green);font-weight:600}
.st-approve-footer button.st-approve-confirm:hover{filter:brightness(1.1)}
.st-approve-footer button.st-approve-confirm:disabled{opacity:0.45;cursor:not-allowed}
.st-approve-footer button.st-approve-cancel:hover{border-color:var(--red);color:var(--red)}
@keyframes stModalIn{from{opacity:0;transform:scale(.97)}to{opacity:1;transform:scale(1)}}
.st-approve-retry{border-color:var(--red);color:var(--red)}
.st-approve-retry:hover{background:rgba(248,81,73,0.10);border-color:var(--red)}
.st-action-btn{transition:all .12s ease;white-space:nowrap}
.st-action-btn:active{transform:translateY(1px)}
.st-action-btn:disabled{opacity:0.5;cursor:not-allowed}

/* ── Regime filter strip (Active Stack) ────────────────────────────────── */
.st-regime-filter{display:flex;flex-wrap:wrap;align-items:center;gap:6px;padding:8px 14px;border-bottom:1px solid var(--border2);background:var(--panel)}
.st-regime-filter .srf-label{font-size:9.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-right:6px}
.st-regime-filter .srf-btn{font-family:inherit;font-size:10px;font-weight:600;padding:3px 10px;border-radius:12px;border:1px solid var(--border);background:var(--border2);color:var(--muted);cursor:pointer;letter-spacing:.04em;transition:all .12s ease}
.st-regime-filter .srf-btn:hover{color:var(--text);border-color:var(--blue)}
.st-regime-filter .srf-btn.active{color:#fff;background:var(--blue);border-color:var(--blue)}
.st-regime-filter .srf-btn.srf-LOW_VOL.active      {background:#2ea043;border-color:#2ea043}
.st-regime-filter .srf-btn.srf-TRANSITIONING.active{background:#d29922;border-color:#d29922;color:#000}
.st-regime-filter .srf-btn.srf-HIGH_VOL.active     {background:#f0883e;border-color:#f0883e}
.st-regime-filter .srf-btn.srf-CRISIS.active       {background:#f85149;border-color:#f85149}
.st-regime-filter .srf-hint{margin-left:auto;font-size:9.5px;color:var(--dim);letter-spacing:.04em}
/* Inline tag in column headers when a regime filter is active */
.srf-col-tag{display:inline-block;font-size:8.5px;font-weight:700;letter-spacing:.06em;padding:1px 4px;border-radius:3px;margin-left:4px;color:#fff;vertical-align:middle}
.srf-col-tag-LOW_VOL      {background:rgba(63,185,80,0.5)}
.srf-col-tag-TRANSITIONING{background:rgba(210,153,34,0.55);color:#000}
.srf-col-tag-HIGH_VOL     {background:rgba(240,136,62,0.6)}
.srf-col-tag-CRISIS       {background:rgba(248,81,73,0.7)}

/* ── By-Regime breakdown cell (Active Stack) ───────────────────────────── */
/* Only the strategy's active regimes are rendered. Each cell is a
   self-contained rounded pill — same artistic language as the regime
   pills used in the Research Candidates "Regimes" column — with a
   small gap separating adjacent pills. The full regime name is shown
   (LOW_VOL / TRANSITIONING / HIGH_VOL / CRISIS) so the format matches
   the candidates page; cells auto-size to fit the longest visible name. */
.st-regime-grid{display:inline-flex;flex-wrap:nowrap;gap:3px;min-width:fit-content;letter-spacing:.04em}
.st-regime-cell{position:relative;display:inline-flex;flex-direction:column;align-items:center;justify-content:center;padding:2px 9px 3px;border-radius:5px;font-size:9px;font-weight:600;line-height:1.15;min-width:fit-content}
.st-regime-cell .st-rg-tag{font-size:8.5px;font-weight:700;text-transform:none;letter-spacing:.04em;opacity:0.95;white-space:nowrap}
.st-regime-cell .st-rg-val{font-size:10px;font-weight:700;margin-top:1px}
.st-regime-cell.st-rg-na .st-rg-val{opacity:0.55;font-weight:500}
.st-regime-cell.st-rg-current::after{content:'';position:absolute;top:1px;right:2px;width:5px;height:5px;border-radius:50%;background:#fff;box-shadow:0 0 0 1.5px var(--blue)}
.st-regime-cell.st-rg-ineligible{opacity:0.32;filter:grayscale(1);text-decoration:line-through;text-decoration-thickness:1px}
/* Non-declared regimes on the candidates table: keep the BT Sharpe value
   fully readable but mark the cell with a dashed border to flag that the
   strategy's literature manifest does NOT list this regime as eligible. */
.st-regime-cell.st-rg-not-declared{box-shadow:inset 0 0 0 1px rgba(255,255,255,0.18); border:1px dashed rgba(255,255,255,0.35); padding:1px 8px 2px}
.st-regime-cell.st-rg-not-declared .st-rg-tag::after{content:' (not declared)'; font-weight:500; opacity:0.7; font-size:7.5px}
.st-regime-cell.st-rg-editable{cursor:pointer;transition:transform .08s ease, box-shadow .08s ease}
.st-regime-cell.st-rg-editable:hover{transform:translateY(-1px);box-shadow:0 0 0 1.5px var(--blue);opacity:1;filter:none}
.st-rg-LOW_VOL      {color:#fff;background:var(--green)}
.st-rg-TRANSITIONING{color:#000;background:var(--yellow)}
.st-rg-HIGH_VOL     {color:#fff;background:var(--orange)}
.st-rg-CRISIS       {color:#fff;background:var(--red)}

/* ── Strategy-row expand/collapse (Active Stack) ───────────────────────── */
.st-row-expandable td.st-name-cell{cursor:pointer;user-select:none}
.st-row-expandable td.st-name-cell:hover .st-chevron{color:var(--blue)}
.st-chevron{display:inline-block;width:12px;color:var(--dim);transition:transform .18s ease;margin-right:4px;font-size:11px}
.st-row-open .st-chevron{transform:rotate(90deg);color:var(--blue)}
.st-row-open td.st-name-cell{color:var(--blue)}
.st-expand-row > td{padding:0 !important;background:var(--bg);border-top:1px solid var(--border2);border-bottom:1px solid var(--border2)}
.st-expand-shell{overflow:hidden;max-height:0;opacity:0;transition:max-height .35s ease, opacity .25s ease}
.st-expand-shell.st-open{max-height:780px;opacity:1}
.st-expand-pad{padding:14px 18px 16px}
.st-expand-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;gap:12px}
.st-expand-title{font-size:12px;font-weight:600;color:var(--text);letter-spacing:.02em}
.st-expand-title small{display:block;font-size:10px;color:var(--muted);font-weight:400;margin-top:2px}
.st-expand-close{background:none;border:1px solid var(--border);color:var(--muted);font-size:11px;padding:3px 8px;border-radius:4px;cursor:pointer;font-family:inherit}
.st-expand-close:hover{color:var(--text);border-color:var(--red)}
.st-expand-grid{display:grid;grid-template-columns:1.05fr 1.4fr;gap:14px;align-items:start}
@media (max-width: 900px){.st-expand-grid{grid-template-columns:1fr}}
.st-expand-section{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:10px 12px}
.st-expand-section-title{font-size:9.5px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:8px;display:flex;align-items:center;justify-content:space-between}
.st-regime-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.st-regime-stats .st-rs-cell{padding:7px 8px;border-radius:5px;border:1px solid var(--border);background:var(--border2);min-height:78px}
.st-regime-stats .st-rs-head{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px;display:flex;align-items:center;justify-content:space-between}
.st-regime-stats .st-rs-row{display:flex;justify-content:space-between;font-size:10px;line-height:1.4;color:var(--muted)}
.st-regime-stats .st-rs-row b{color:var(--text);font-weight:600}
.st-regime-stats .st-rs-na{color:var(--dim);font-style:italic}
.st-similar{display:flex;flex-direction:column;gap:4px;max-height:180px;overflow-y:auto;padding-right:2px}
.st-similar-row{display:grid;grid-template-columns:1fr repeat(5,40px);gap:4px;align-items:center;font-size:10px;padding:3px 6px;border-radius:4px;background:var(--border2)}
.st-similar-row .st-sm-name{color:var(--text);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.st-similar-row .st-sm-cell{padding:2px 4px;border-radius:3px;text-align:center;font-size:9.5px;font-weight:600;background:var(--bg);color:var(--muted)}
.st-similar-row .st-sm-cell.pos{color:var(--green)}
.st-similar-row .st-sm-cell.neg{color:var(--red)}
.st-similar-row .st-sm-cell.empty{color:var(--dim)}
.st-arr-wrap{position:relative}
.st-arr-meta{font-size:9.5px;color:var(--muted);margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.st-arr-empty{padding:30px 0;text-align:center;color:var(--dim);font-size:10.5px}
/* Stable, fixed-height wrapper so Chart.js responsive sizing has a real
   reference frame. Without this the canvas measured during the expand
   max-height transition, which shrinks the chart on every re-render. */
.st-arr-canvas-wrap{position:relative;height:200px;width:100%}
/* Position-flow bar chart sits directly under the ARR curve, sharing
   its x-axis dates so the operator can read trade flow alongside
   per-trade economics. */
.st-flow-row{margin-top:10px}
.st-flow-meta{display:flex;justify-content:space-between;align-items:center;font-size:9.5px;color:var(--muted);letter-spacing:.04em;margin-bottom:4px}
.st-flow-canvas-wrap{position:relative;height:80px;width:100%}

/* In-flight job chip: progress-bar fill + label overlay */
.st-job-wrap{display:inline-flex;align-items:center;gap:6px}
.st-job-chip{position:relative;display:inline-block;padding:3px 10px;min-width:230px;border:1px solid var(--blue);border-radius:4px;background:rgba(88,166,255,0.05);font-size:10px;font-weight:600;letter-spacing:.04em;overflow:hidden;color:var(--blue)}
.st-job-fill{position:absolute;left:0;top:0;bottom:0;background:rgba(88,166,255,0.22);transition:width .6s ease;z-index:0}
.st-job-text{position:relative;z-index:1}
.st-cancel-btn{border-color:var(--border2)}
.st-cancel-btn:hover{border-color:var(--red);color:var(--red);background:rgba(248,81,73,0.08)}

/* Inline failure banner on a candidate row — wraps vertically, never widens the column */
.st-fail-banner{display:flex;align-items:flex-start;gap:6px;padding:3px 7px;margin:0 0 4px 0;border:1px solid var(--red);border-radius:4px;background:rgba(248,81,73,0.08);color:var(--red);font-size:10px;font-weight:500;width:200px;max-width:200px;line-height:1.35}
.st-fail-ico{flex:0 0 auto;line-height:1.35}
.st-fail-msg{flex:1 1 auto;white-space:normal;word-break:break-word;overflow-wrap:anywhere}
.st-fail-dismiss{flex:0 0 auto;padding:0 6px;border-color:transparent;color:var(--red);align-self:flex-start}
.st-fail-dismiss:hover{background:rgba(248,81,73,0.15)}

/* Toast notifications */
#toast-host{position:fixed;right:20px;bottom:20px;display:flex;flex-direction:column;gap:8px;z-index:9999;pointer-events:none}
.toast{pointer-events:auto;padding:10px 14px;border-radius:6px;border:1px solid var(--border);background:var(--panel);color:var(--text);font-size:12px;line-height:1.4;max-width:420px;box-shadow:0 4px 12px rgba(0,0,0,0.35);cursor:pointer;opacity:0;transform:translateX(20px);transition:opacity .3s ease,transform .3s ease}
.toast-in{opacity:1;transform:translateX(0)}
.toast-out{opacity:0;transform:translateX(20px)}
.toast-ok{border-color:var(--green)}
.toast-error{border-color:var(--red)}
.toast-warn{border-color:var(--yellow)}
.toast-info{border-color:var(--blue)}
.st-reject-btn:hover {border-color:var(--yellow);color:var(--yellow)}
.st-gate-fail{color:var(--red)}
/* Staging unsupported-source warning — shown on staging rows whose
   data_requirements_planned references a column no provider knows about. */
.st-data-warn{display:inline-block;font-size:10px;font-weight:700;padding:0 5px;margin-left:6px;border-radius:3px;color:var(--yellow);border:1px solid var(--yellow);background:transparent;cursor:help;vertical-align:middle}
/* Manifest↔registry drift badge — additive, never replaces the primary status. */
.st-drift-badge{display:inline-block;font-size:10px;font-weight:700;padding:0 5px;margin-left:6px;border-radius:3px;color:var(--yellow);border:1px solid var(--yellow);background:transparent;cursor:help;vertical-align:middle}
.st-fresh-badge{display:inline-block;font-size:10px;font-weight:700;padding:0 5px;margin-left:6px;border-radius:3px;color:var(--green);border:1px solid var(--green);background:transparent;cursor:help;vertical-align:middle}
.st-uni-badge{display:inline-block;font-size:9px;font-weight:700;padding:0 4px;margin-left:6px;border-radius:3px;color:var(--blue);border:1px solid var(--blue);background:transparent;cursor:help;vertical-align:middle;letter-spacing:.03em}
.st-drift-header{font-size:11px;color:var(--yellow);padding:4px 6px 8px;letter-spacing:.03em}
#pf-inner{display:flex;flex-direction:column;gap:16px;padding:20px 24px}
.pf-summary-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.pf-stat-card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px}
.pf-stat-label{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:6px}
.pf-stat-value{font-size:22px;font-weight:700;color:var(--text)}
.pf-stat-sub{font-size:10px;color:var(--dim);margin-top:3px}
.pf-chart-wrap{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px;position:relative;height:218px}
.pf-chart-label{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:8px}
.pf-regime-legend{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:6px;font-size:9.5px;color:var(--muted);letter-spacing:0.04em}
.pf-regime-legend .rl-label{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em}
.pf-regime-legend .rl-item{display:inline-flex;align-items:center;gap:5px;opacity:.45;transition:opacity .15s}
.pf-regime-legend .rl-item.rl-on{opacity:1}
.pf-regime-legend .rl-dot{display:inline-block;width:10px;height:10px;border-radius:2px;border:1px solid rgba(255,255,255,0.06)}
.pf-regime-legend .rl-dot-low{background:rgba(63,185,80,0.55)}
.pf-regime-legend .rl-dot-trans{background:rgba(210,153,34,0.6)}
.pf-regime-legend .rl-dot-high{background:rgba(240,136,62,0.7)}
.pf-regime-legend .rl-dot-crisis{background:rgba(248,81,73,0.8)}
.pf-section{background:var(--panel);border:1px solid var(--border);border-radius:8px;overflow:hidden}
.pf-section-header{padding:11px 16px;border-bottom:1px solid var(--border2);font-size:11px;font-weight:600;color:var(--text);display:flex;align-items:center;justify-content:space-between}
.pf-section-body{overflow-x:auto}
.pf-pnl-pos{color:var(--green)}.pf-pnl-neg{color:var(--red)}.pf-pnl-flat{color:var(--muted)}
.dir-long{color:var(--green);font-weight:600}.dir-short{color:var(--red);font-weight:600}
.dir-mixed{color:var(--yellow);font-weight:600;font-size:9.5px;letter-spacing:.04em}
/* ── Ticker-grouped tables (positions + history) ──────────────────────────
 * Parent row carries chevron + ticker name (drillable). Sub-rows are
 * indented strategy detail, dimmer background. Click anywhere on the
 * parent to toggle — the ticker text itself stops propagation so the
 * Market drill-in still works. */
.db-table.grouped tbody.tk-group{}
.db-table.grouped tr.ticker-row{background:var(--panel)}
.db-table.grouped tr.ticker-row:hover{background:rgba(88,166,255,0.06)}
.db-table.grouped tr.ticker-row.expanded{background:rgba(88,166,255,0.05);border-left:2px solid var(--blue)}
.db-table.grouped tr.ticker-row .chev{display:inline-block;width:11px;color:var(--muted);font-size:10px;margin-right:4px;transition:transform .15s}
.db-table.grouped tr.ticker-row.expanded .chev{color:var(--blue)}
.db-table.grouped tr.ticker-row .tk-name{font-weight:700;color:var(--blue);cursor:pointer;letter-spacing:.02em}
.db-table.grouped tr.ticker-row .tk-name:hover{text-decoration:underline}
.db-table.grouped tr.strat-row{background:rgba(13,17,23,0.4);font-size:10px}
.db-table.grouped tr.strat-row td{padding-top:3px;padding-bottom:3px;border-bottom:1px dotted rgba(48,54,61,0.5)}
.db-table.grouped tr.strat-row .strat-name{color:var(--muted);font-family:inherit;font-size:9.5px;letter-spacing:.01em;display:inline-block;max-width:118px;overflow:hidden;text-overflow:ellipsis;vertical-align:middle}
.db-table.grouped tr.strat-row:last-child td{border-bottom:1px solid var(--border2)}
.wl-badge{display:inline-block;font-size:8.5px;font-weight:600;color:var(--dim);margin-left:4px;letter-spacing:.03em}
.pf-strategy-group-header{padding:8px 12px;background:rgba(88,166,255,0.06);border-top:1px solid var(--border2);border-bottom:1px solid var(--border2);font-size:11px;font-weight:600;color:var(--text);display:flex;justify-content:space-between;align-items:center;letter-spacing:.02em}
.pf-strategy-group-header .pf-sg-name{color:var(--blue)}
.pf-strategy-group-header .pf-sg-meta{color:var(--muted);font-weight:400;font-size:10px}

/* ── Positions heatmap (slice-and-dice) ────────────────────────────────────
 * Default layout: top-12 tickers in 3-4 rows, container ~280px tall. The
 * "Show all" button on the section header flips _heatmapExpanded which
 * renders all 260 tickers in adaptive rows of 12 inside a taller, scrollable
 * container. Tile area encodes total_size_pct; tile background encodes
 * net_pnl (red↔green at ±5%). Click any tile → inline alpha-bars panel
 * appears below the heatmap; click the selected tile again to close.
 */
.pf-heatmap-controls{display:flex;align-items:center;justify-content:space-between;padding:8px 12px;border-bottom:1px solid var(--border2);background:rgba(13,17,23,0.35);font-size:10px;color:var(--muted);letter-spacing:.02em}
.pf-heatmap-controls .pf-hm-info{flex:1}
.pf-heatmap-controls .pf-hm-btn{background:transparent;border:1px solid var(--border);color:var(--blue);padding:3px 12px;border-radius:4px;font-size:10px;cursor:pointer;letter-spacing:.04em;transition:all .12s}
.pf-heatmap-controls .pf-hm-btn:hover{border-color:var(--blue);background:rgba(88,166,255,0.08)}
/* ── Risk & Sizing panel ───────────────────────────────────────────────────
 * Full-width risk-preference slider (λ_global) with regime parameter cards
 * below. Lives in #pf-risk-panel on the Portfolio page. The slider is the
 * single most-consequential operator-tunable knob — sized to reflect that.
 */
.pf-risk-section .pf-section-body{padding:0}
.pf-risk-panel{padding:18px 22px 22px 22px;background:linear-gradient(180deg,rgba(13,17,23,0.5) 0%,rgba(13,17,23,0.15) 100%)}
.pf-risk-headline{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;gap:18px;flex-wrap:wrap}
.pf-risk-title{font-size:12px;color:var(--text);font-weight:600;letter-spacing:.03em;text-transform:uppercase}
.pf-risk-help{font-size:11px;color:var(--muted);line-height:1.5;flex:1;max-width:65%;text-align:right;font-weight:400}
.pf-risk-help code{background:rgba(88,166,255,0.08);color:var(--blue);padding:1px 5px;border-radius:3px;font-size:10.5px;font-family:'SF Mono',monospace}
.pf-lambda-mega{position:relative;margin:12px 0 4px}
.pf-lambda-mega input[type=range]{width:100%;height:10px;-webkit-appearance:none;background:linear-gradient(90deg,#1f6f43 0%,#2ea043 22%,#d29922 55%,#f85149 90%,#7c2128 100%);border-radius:5px;outline:none;cursor:pointer;border:1px solid var(--border)}
.pf-lambda-mega input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:28px;height:28px;border-radius:50%;background:var(--bg);cursor:grab;border:3px solid var(--blue);box-shadow:0 2px 8px rgba(0,0,0,0.5),0 0 0 1px rgba(88,166,255,0.4)}
.pf-lambda-mega input[type=range]::-webkit-slider-thumb:active{cursor:grabbing;background:var(--blue);box-shadow:0 2px 12px rgba(88,166,255,0.6),0 0 0 4px rgba(88,166,255,0.2)}
.pf-lambda-mega input[type=range]::-moz-range-thumb{width:28px;height:28px;border-radius:50%;background:var(--bg);cursor:grab;border:3px solid var(--blue);box-shadow:0 2px 8px rgba(0,0,0,0.5)}
/* Inset matches half the thumb width (28px / 2 = 14px) so tick centers
 * align with where the thumb center can actually travel — without this,
 * ticks drift up to ~2% of width off the true slider value. */
.pf-lambda-ticks{position:relative;height:34px;margin:10px 14px 0 14px;font-size:10px;color:var(--dim);font-variant-numeric:tabular-nums;letter-spacing:.04em}
.pf-lambda-ticks .pf-tick{position:absolute;top:0;transform:translateX(-50%);display:flex;flex-direction:column;align-items:center;gap:2px;cursor:pointer;transition:color .12s;white-space:nowrap}
.pf-lambda-ticks .pf-tick::before{content:'';position:absolute;left:50%;top:-6px;width:1px;height:5px;background:var(--border);transform:translateX(-50%)}
.pf-lambda-ticks .pf-tick:hover{color:var(--blue)}
.pf-lambda-ticks .pf-tick:hover::before{background:var(--blue)}
.pf-lambda-ticks .pf-tick-val{font-weight:700;color:var(--muted);font-size:10.5px}
.pf-lambda-ticks .pf-tick-label{font-size:9px;text-transform:uppercase;letter-spacing:.06em}
.pf-lambda-readout{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:14px;padding:10px 16px;background:rgba(88,166,255,0.05);border:1px solid rgba(88,166,255,0.18);border-radius:6px}
.pf-lambda-readout .pf-lr-label{color:var(--muted);font-size:11px;letter-spacing:.04em;text-transform:uppercase}
.pf-lambda-readout .pf-lr-value{color:var(--blue);font-size:24px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1}
.pf-lambda-readout .pf-lr-sub{color:var(--muted);font-size:10.5px;font-family:'SF Mono',monospace}
.pf-regime-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:18px}
.pf-regime-card{background:var(--bg);border:1px solid var(--border2);border-radius:6px;padding:11px 12px;transition:border-color .15s}
.pf-regime-card:hover{border-color:var(--border)}
.pf-regime-card.regime-active{border-color:var(--blue);box-shadow:0 0 0 1px rgba(88,166,255,0.3)}
.pf-regime-card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.pf-regime-name{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
.pf-regime-name.low_vol{color:#2ea043}
.pf-regime-name.transitioning{color:#d29922}
.pf-regime-name.high_vol{color:#f85149}
.pf-regime-name.crisis{color:#a371f7}
.pf-regime-active-badge{font-size:9px;color:var(--blue);background:rgba(88,166,255,0.15);padding:1px 6px;border-radius:8px;letter-spacing:.05em}
.pf-regime-eff{font-size:18px;font-weight:700;color:var(--text);font-variant-numeric:tabular-nums;margin-bottom:2px;line-height:1.1}
.pf-regime-eff-label{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px}
.pf-regime-eff-formula{font-size:9.5px;color:var(--muted);font-family:'SF Mono',monospace;margin-bottom:10px}
.pf-regime-param-row{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;padding:7px 0;border-top:1px solid var(--border2);font-size:10.5px}
.pf-regime-param-row:first-of-type{border-top:0}
.pf-regime-param-label-block{display:flex;flex-direction:column;gap:2px;min-width:0}
.pf-regime-param-label{color:var(--text);font-size:10.5px;letter-spacing:.02em;font-weight:600}
.pf-regime-param-hint{color:var(--dim);font-size:9.5px;line-height:1.3;letter-spacing:0;font-weight:400}
.pf-regime-param-input{background:rgba(13,17,23,0.6);border:1px solid var(--border2);color:var(--text);padding:3px 6px;border-radius:3px;font-family:'SF Mono',monospace;font-size:10.5px;width:64px;text-align:right;font-variant-numeric:tabular-nums}
.pf-regime-param-input:focus{outline:0;border-color:var(--blue);background:var(--bg)}
.pf-regime-param-input.dirty{border-color:#d29922;background:rgba(210,153,34,0.05)}
.pf-regime-param-input.saving{border-color:var(--blue);background:rgba(88,166,255,0.05)}
.pf-regime-param-suffix{color:var(--dim);font-size:9.5px;margin-left:-4px}
/* ── Risk switch cards (2026-07-27): side-by-side switch+value controls —
   corr de-gross + net-exposure cap. Pill toggle is CSS-only over a hidden
   checkbox; card dims when OFF. */
.pf-risk-switch-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}
.pf-risk-switch-card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:11px 13px;display:flex;flex-direction:column;gap:8px;transition:opacity .18s,border-color .18s}
.pf-risk-switch-card.pf-off{opacity:.62}
.pf-risk-switch-card:not(.pf-off){border-color:rgba(63,185,80,0.28)}
.pf-rsw-head{display:flex;align-items:center;justify-content:space-between;gap:10px}
.pf-rsw-title{font-size:11px;font-weight:600;color:var(--text);letter-spacing:.02em;display:flex;align-items:center;gap:7px}
.pf-rsw-state{font-size:8.5px;font-weight:700;letter-spacing:.08em;padding:1.5px 6px;border-radius:8px}
.pf-rsw-state.on{color:var(--green);background:rgba(63,185,80,0.12);border:1px solid rgba(63,185,80,0.3)}
.pf-rsw-state.off{color:var(--dim);background:rgba(139,148,158,0.08);border:1px solid var(--border2)}
.pf-rsw-hint{font-size:9.5px;color:var(--dim);line-height:1.45;flex:1}
.pf-rsw-vals{display:flex;align-items:center;gap:14px;padding-top:7px;border-top:1px dashed var(--border2)}
.pf-rsw-val{display:flex;flex-direction:column;gap:3px}
.pf-rsw-val-label{font-size:8.5px;text-transform:uppercase;letter-spacing:.07em;color:var(--dim)}
.pf-toggle{position:relative;width:36px;height:20px;flex:0 0 auto;display:inline-block;cursor:pointer}
.pf-toggle input{position:absolute;inset:0;opacity:0;margin:0;cursor:pointer;z-index:1}
.pf-toggle-track{position:absolute;inset:0;border-radius:10px;background:var(--border);border:1px solid var(--border2);transition:background .18s,border-color .18s}
.pf-toggle-knob{position:absolute;top:2px;left:2px;width:14px;height:14px;border-radius:50%;background:var(--muted);transition:left .18s,background .18s;box-shadow:0 1px 2px rgba(0,0,0,0.4)}
.pf-toggle input:checked ~ .pf-toggle-track{background:rgba(63,185,80,0.30);border-color:rgba(63,185,80,0.5)}
.pf-toggle input:checked ~ .pf-toggle-track .pf-toggle-knob{left:18px;background:var(--green)}
.pf-toggle input:focus-visible ~ .pf-toggle-track{outline:1px solid var(--blue);outline-offset:1px}
@media(max-width:920px){.pf-regime-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:560px){.pf-regime-grid{grid-template-columns:1fr}.pf-risk-help{display:none}.pf-risk-switch-grid{grid-template-columns:1fr}}
/* Squares-only layout (2026-05-21): each tile is a 1:1 square, area
   encodes portfolio size_pct. Tiles are absolutely positioned by JS
   (_packHeatmapShelves) using First-Fit Decreasing Height shelf
   packing — small tiles slot into shelves opened by tall tiles so
   the total vertical extent is minimized. Container height is set
   to the last shelf's bottom edge so trailing whitespace stays tight. */
.pf-heatmap{position:relative;padding:8px;background:var(--bg);overflow:hidden}
.pf-heatmap.expanded{max-height:none;overflow-y:auto}
/* Heatmap tile — option C: centered hero + dark footer strip. Hero is
 * ticker (big) + pct return / $ P&L (second line, centered as a pair).
 * Strip pins to bottom with size % left, days right. Size % is normalized
 * share-of-book (sums to 100% across the recent universe). */
.pf-tile{position:relative;border:1px solid rgba(255,255,255,0.06);border-radius:6px;cursor:pointer;overflow:hidden;display:flex;flex-direction:column;min-width:0;color:#fff;transition:transform .12s,border-color .12s,box-shadow .12s;box-shadow:0 1px 3px rgba(0,0,0,0.35)}
.pf-tile:hover{border-color:rgba(88,166,255,0.6);transform:translateY(-1px);z-index:3;box-shadow:0 6px 14px rgba(0,0,0,0.6)}
.pf-tile.selected{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue),0 6px 14px rgba(88,166,255,0.25);z-index:4}
/* Tile interior — base styles (= medium tier defaults). Per-tier overrides
   below tune font size, padding, and visibility so content fits cleanly
   at every tile footprint from 56px (tiny) up to 168px (large). The
   tier class (pf-tile-large / pf-tile-medium / pf-tile-small /
   pf-tile-tiny) is set in _buildHeatmapHtml from the computed side. */
.pf-tile-hero{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:4px;text-align:center;gap:2px;min-height:0}
.pf-tile-hero .tk-symbol{font-weight:800;font-size:13px;letter-spacing:.04em;line-height:1.1;text-shadow:0 1px 2px rgba(0,0,0,0.65);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}
.pf-tile-hero .tk-row{display:flex;align-items:baseline;justify-content:center;gap:6px;line-height:1.1;max-width:100%}
.pf-tile-hero .tk-pnl{font-size:11px;font-weight:700;font-variant-numeric:tabular-nums;text-shadow:0 1px 2px rgba(0,0,0,0.55)}
.pf-tile-hero .tk-dollar{font-size:10px;font-weight:600;font-variant-numeric:tabular-nums;opacity:0.92;text-shadow:0 1px 2px rgba(0,0,0,0.55)}
.pf-tile-strip{display:flex;justify-content:space-between;align-items:center;padding:2px 6px;font-size:9px;font-weight:500;letter-spacing:.04em;background:rgba(0,0,0,0.32);border-top:1px solid rgba(255,255,255,0.07);color:rgba(255,255,255,0.85);flex-shrink:0}
.pf-tile-strip span{font-variant-numeric:tabular-nums}

/* Large tier (side >= 140px) — generous spacing, prominent type. */
.pf-tile.pf-tile-large .pf-tile-hero{padding:8px;gap:4px}
.pf-tile.pf-tile-large .tk-symbol{font-size:18px;letter-spacing:.08em}
.pf-tile.pf-tile-large .tk-row{gap:8px}
.pf-tile.pf-tile-large .tk-pnl{font-size:15px}
.pf-tile.pf-tile-large .tk-dollar{font-size:12px}
.pf-tile.pf-tile-large .pf-tile-strip{font-size:10.5px;padding:3px 10px}

/* Medium tier (100..139px) — uses the base sizes above. */

/* Small tier (70..99px) — drop the dollar (% + color carry direction)
   and drop days held from the strip, but keep portfolio share% visible
   (always-on per operator request: it's the primary "how big is this
   bet" signal). Strip centers its single remaining value. */
.pf-tile.pf-tile-small .pf-tile-hero{padding:3px;gap:1px}
.pf-tile.pf-tile-small .tk-symbol{font-size:11px;letter-spacing:.02em}
.pf-tile.pf-tile-small .tk-pnl{font-size:10px}
.pf-tile.pf-tile-small .tk-dollar{display:none}
.pf-tile.pf-tile-small .pf-tile-strip{font-size:8px;padding:1px 4px;justify-content:center;letter-spacing:.02em}
.pf-tile.pf-tile-small .pf-tile-strip .tk-days{display:none}

/* Tiny tier (<70px) — ticker + pnl% in the hero, strip collapses to
   just portfolio share% (always-on). Days held + dollar drop. Color
   still encodes direction, so the remaining text reads cleanly even
   at 56px square. */
.pf-tile.pf-tile-tiny .pf-tile-hero{padding:2px 2px 0;gap:0}
.pf-tile.pf-tile-tiny .tk-symbol{font-size:9.5px;letter-spacing:.01em;font-weight:700}
.pf-tile.pf-tile-tiny .tk-pnl{font-size:8.5px;font-weight:700}
.pf-tile.pf-tile-tiny .tk-dollar{display:none}
.pf-tile.pf-tile-tiny .pf-tile-strip{font-size:7.5px;padding:0 3px;justify-content:center;letter-spacing:0;line-height:1.1}
.pf-tile.pf-tile-tiny .pf-tile-strip .tk-days{display:none}

/* Open Positions: two-column LONG/SHORT layout with size-encoded tiles.
   Tile AREA = position share of NAV (3 tiers via grid-row/column span);
   color = unrealized P&L. CSS Grid auto-flow:dense packs smaller tiles
   into the gaps left by larger ones — no JS packer needed, works at
   any column width. Closed-trade history still uses the legacy
   variable-area heatmap (renderHistory). */
.pf-pos-twocol{display:grid;grid-template-columns:1fr 1fr;gap:14px;padding:10px}
.pf-poscol{min-width:0;background:var(--bg);border-radius:6px;padding:8px}
.pf-poscol-header{display:flex;justify-content:space-between;align-items:baseline;padding:2px 4px 8px;font-size:10px;text-transform:uppercase;letter-spacing:.1em;font-weight:600;border-bottom:1px solid var(--border2);margin-bottom:8px}
.pf-poscol-long .pf-poscol-header{color:var(--green)}
.pf-poscol-short .pf-poscol-header{color:var(--red)}
.pf-poscol-meta{color:var(--dim);font-weight:500;font-size:9px;letter-spacing:.04em}
/* Treemap container — squarified algorithm fills it with rectangles
   whose area is exactly proportional to position size. JS computes the
   layout after the container width is known; ResizeObserver re-runs on
   width change. Fixed height keeps total area constant so the long and
   short columns are visually comparable. */
.pf-poscol-tm{position:relative;width:100%;height:540px;background:rgba(13,17,23,0.4);border-radius:4px;overflow:hidden}
.pf-postile{position:absolute;border-radius:3px;display:flex;flex-direction:column;justify-content:space-between;color:#fff;cursor:pointer;border:1px solid rgba(0,0,0,0.35);overflow:hidden;box-sizing:border-box;transition:filter .12s,box-shadow .12s,border-color .12s}
.pf-postile:hover{filter:brightness(1.18);border-color:rgba(88,166,255,0.7);z-index:5;box-shadow:0 4px 12px rgba(0,0,0,0.55)}
.pf-postile.selected{border-color:var(--blue);box-shadow:0 0 0 2px var(--blue),0 4px 12px rgba(88,166,255,0.3);z-index:6}
.pf-postile-sym{font-weight:700;letter-spacing:.04em;text-shadow:0 1px 1px rgba(0,0,0,0.55);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.15}
.pf-postile-bottom{display:flex;justify-content:space-between;align-items:baseline;gap:4px;font-variant-numeric:tabular-nums;line-height:1.1}
.pf-postile-pnl{font-weight:600;text-shadow:0 1px 1px rgba(0,0,0,0.55)}
.pf-postile-share{opacity:0.8}
/* Per-size typography — JS sets data-size based on min(width,height) so
   text scales with tile area. Tiny tiles drop text entirely (color +
   tooltip carry the meaning). */
.pf-postile[data-size="huge"]{padding:10px}
.pf-postile[data-size="huge"] .pf-postile-sym{font-size:22px;letter-spacing:.06em}
.pf-postile[data-size="huge"] .pf-postile-pnl{font-size:17px}
.pf-postile[data-size="huge"] .pf-postile-share{font-size:12px}
.pf-postile[data-size="large"]{padding:7px}
.pf-postile[data-size="large"] .pf-postile-sym{font-size:15px;letter-spacing:.05em}
.pf-postile[data-size="large"] .pf-postile-pnl{font-size:12px}
.pf-postile[data-size="large"] .pf-postile-share{font-size:10px}
.pf-postile[data-size="medium"]{padding:4px}
.pf-postile[data-size="medium"] .pf-postile-sym{font-size:11px}
.pf-postile[data-size="medium"] .pf-postile-pnl{font-size:10px}
.pf-postile[data-size="medium"] .pf-postile-share{display:none}
.pf-postile[data-size="small"]{padding:2px 3px;justify-content:center;align-items:center}
.pf-postile[data-size="small"] .pf-postile-sym{font-size:9px;letter-spacing:0}
.pf-postile[data-size="small"] .pf-postile-bottom{display:none}
.pf-postile[data-size="tiny"]{padding:0}
.pf-postile[data-size="tiny"] .pf-postile-sym{display:none}
.pf-postile[data-size="tiny"] .pf-postile-bottom{display:none}
@media(max-width:760px){.pf-pos-twocol{grid-template-columns:1fr}.pf-poscol-tm{height:420px}}

/* ── Alpha contribution bars ─────────────────────────────────────────────── */
.alpha-bars{padding:12px 14px;background:rgba(13,17,23,0.55);border-top:1px solid var(--border)}
.alpha-bars.empty{color:var(--dim);font-size:11px;padding:14px;text-align:center}
.alpha-bars.empty b{color:var(--text);font-weight:600}
.ab-title{display:flex;align-items:baseline;gap:10px;margin-bottom:10px;padding-bottom:6px;border-bottom:1px dotted var(--border2)}
.ab-title .ab-ticker{font-size:14px;font-weight:700;color:var(--blue);letter-spacing:.05em}
.ab-title .ab-meta{font-size:10px;color:var(--muted);letter-spacing:.02em}
/* Price/bracket header. These classes were emitted since the panel shipped but
   never had rules — the row rendered as unstyled inline spans. */
.ab-prices{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:8px;font-size:10.5px;color:var(--muted);font-variant-numeric:tabular-nums}
.ab-px{letter-spacing:.02em}
.ab-px b{color:var(--text);font-weight:600;margin-left:2px}
.ab-px-none b{color:var(--red);font-weight:600}
.ab-bars{display:flex;flex-direction:column;gap:3px}
.ab-row{display:flex;align-items:center;gap:8px;padding:1px 0;font-size:10.5px;min-height:18px}
.ab-label{width:240px;flex-shrink:0;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500;letter-spacing:.01em}
.ab-dir{width:42px;flex-shrink:0;font-size:9px;letter-spacing:.04em;text-align:center}
.ab-track{flex:1;position:relative;height:14px;background:rgba(48,54,61,0.35);border-radius:2px;min-width:120px}
.ab-zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:rgba(255,255,255,0.22)}
.ab-fill{position:absolute;top:1px;bottom:1px;border-radius:2px;transition:width .3s ease}
.ab-fill.pos{left:50%;background:linear-gradient(90deg,rgba(63,185,80,0.55),rgba(63,185,80,0.95))}
.ab-fill.neg{right:50%;background:linear-gradient(270deg,rgba(248,81,73,0.55),rgba(248,81,73,0.95))}
.ab-value{width:64px;flex-shrink:0;text-align:right;font-weight:600;font-variant-numeric:tabular-nums}
.bars-row td.bars-cell{padding:0!important;background:rgba(13,17,23,0.4)}
.ab-badge{display:inline-block;margin-left:6px;padding:1px 7px;border-radius:10px;font-size:9px;font-weight:600;letter-spacing:.05em;background:rgba(63,185,80,0.16);color:var(--green);border:1px solid rgba(63,185,80,0.35)}
.ab-badge-warn{background:rgba(248,81,73,0.16);color:var(--red);border-color:rgba(248,81,73,0.35)}
.ab-row.ab-unranked{opacity:0.55}
.ab-row.ab-unranked .ab-label{font-style:italic}
.ab-track.muted{background:rgba(48,54,61,0.15)}
.ab-value.muted{color:var(--dim);font-weight:400;font-style:italic}
.ab-track.over-one{box-shadow:inset 0 0 0 1px rgba(88,166,255,0.35)}
/* ── Regime Panel ── */
.regime-panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:20px}
.regime-panel-header{font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:12px;display:flex;align-items:center;gap:6px}
.regime-panel-header::after{content:'';flex:1;height:1px;background:var(--border2)}
.regime-section-header{font-size:9px;text-transform:uppercase;letter-spacing:.09em;color:var(--muted);font-weight:600;margin:0 0 8px;display:flex;align-items:center;gap:6px}
.regime-section-header::after{content:'';flex:1;height:1px;background:var(--border2)}
.regime-section{margin-bottom:16px}
.regime-section:last-child{margin-bottom:0}
.regime-hero{display:flex;align-items:center;gap:18px;flex-wrap:wrap;padding:4px 0 14px;border-bottom:1px solid var(--border2);margin-bottom:14px}
.regime-hero .regime-state-badge{font-size:16px;padding:7px 16px}
.regime-body-grid{display:grid;grid-template-columns:1.45fr 1fr;gap:24px;align-items:start}
@media(max-width:760px){.regime-body-grid{grid-template-columns:1fr}}
.regime-spark-tick{display:inline-block;width:4px;height:14px;border-radius:1px}
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
.regime-bar-group{width:100%}
.regime-bar-label{font-size:9px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin-bottom:3px;display:flex;justify-content:space-between}
.regime-bar-label span:last-child{color:var(--muted)}
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
.regime-alert-badge{font-size:10px;padding:2px 8px;border-radius:4px;background:rgba(248,81,73,.15);color:var(--red);border:1px solid rgba(248,81,73,.3)}

/* ── Mobile-only overrides (≤768px = iPhone-class width) ────────────────────
 * Everything here is additive — desktop layout above is untouched. The
 * goals: show essential information first, hide deep-dive columns behind
 * horizontal scroll, give every tap target ≥32px height, kill the 300ms
 * tap delay, and turn the 230px sidebar into an off-canvas drawer behind
 * a hamburger. Sections also default-collapse to top-5 (vs top-10 on
 * desktop) via the JS-side _collapseLimit() helper. */

#mobile-menu, #mobile-backdrop { display: none; }

@media (max-width: 768px) {
  /* Snappier touch — disable iOS double-tap zoom + remove tap highlight. */
  *,*::before,*::after { -webkit-tap-highlight-color: transparent; touch-action: manipulation; }

  /* Allow page-level scroll on mobile (desktop pins overflow:hidden). */
  html, body { overflow-y: auto; }
  body { font-size: 12px; -webkit-text-size-adjust: 100%; }

  /* ── Header ──────────────────────────────────────────────────────────── */
  #header {
    height: 38px; padding: 0 8px; gap: 4px; flex-wrap: nowrap;
    overflow-x: auto; -webkit-overflow-scrolling: touch;
    backdrop-filter: blur(12px);
    background: rgba(22, 27, 34, 0.88);
    position: sticky; top: 0; z-index: 50;
  }
  #header::-webkit-scrollbar { display: none; }
  #header h1 { font-size: 12px; flex-shrink: 0; }
  #header h1 + .nav-btn { margin-left: 2px; }
  .nav-btn {
    padding: 5px 9px; font-size: 11px; flex-shrink: 0;
    border-radius: 5px; transition: all .15s ease-out;
  }
  .nav-btn.active { box-shadow: 0 0 0 1px var(--blue) inset; }
  #pipeline-badge, #clock { display: none; }
  .refresh-btn { padding: 4px 7px !important; font-size: 10px !important; flex-shrink: 0; }

  /* Hamburger toggle — visible only on mobile, sits at top-left. */
  #mobile-menu {
    display: inline-flex; align-items: center; justify-content: center;
    width: 28px; height: 28px; background: var(--border2);
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 14px; cursor: pointer; padding: 0;
    flex-shrink: 0; transition: transform .2s ease-out, background .15s;
  }
  #mobile-menu:active { background: var(--border); transform: scale(0.94); }
  body:has(#sidebar.mobile-open) #mobile-menu { transform: rotate(90deg); }

  /* ── Market strip ───────────────────────────────────────────────────── */
  #strip { height: 24px; }
  .strip-item { padding: 0 10px; font-size: 9.5px; }

  /* ── Off-canvas drawer + dim backdrop ──────────────────────────────── */
  #sidebar {
    position: fixed; top: 0; bottom: 0; left: 0;
    width: 86vw; max-width: 320px;
    transform: translateX(-100%);
    transition: transform .26s cubic-bezier(0.32, 0.72, 0, 1);
    z-index: 1000;
    background: var(--bg);
    box-shadow: 8px 0 32px rgba(0,0,0,0.55);
    border-right: 1px solid var(--border);
  }
  #sidebar.mobile-open { transform: translateX(0); }

  #mobile-backdrop {
    display: block;
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.45);
    backdrop-filter: blur(2px);
    opacity: 0;
    pointer-events: none;
    z-index: 999;
    transition: opacity .22s ease-out;
  }
  body:has(#sidebar.mobile-open) #mobile-backdrop { opacity: 1; pointer-events: auto; }

  /* ── Main view ─────────────────────────────────────────────────────── */
  #body { display: block; }
  #view-wrap { padding-bottom: 8px; overflow: visible; }

  /* Subtle fade-in when switching tabs — feels less jarring. */
  #portfolio-page, #strategies-page, #research-page, #tab-content { animation: mobile-fade-in .18s ease-out; }
  @keyframes mobile-fade-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

  /* Tight tab-content padding — every pixel of vertical real-estate counts. */
  #tab-content { padding: 6px 6px 14px; }

  /* Compress section-to-section gaps. */
  #portfolio-page > div > .pf-section,
  #strategies-page > div > section { margin-top: 6px !important; margin-bottom: 6px !important; }

  /* ── Strategies sub-tabs ─────────────────────────────────────────────── */
  .st-tabs { flex-wrap: wrap; gap: 3px !important; }
  .st-tab {
    padding: 5px 9px !important; font-size: 10.5px !important;
    min-height: 28px; border-radius: 5px; transition: all .15s;
  }
  .st-tab:active { transform: scale(0.96); }

  /* ── Stat cards — extreme density, 4-up, uniform height ─────────────── *
   * Verbose labels ("Annualized Equity Realization %") were wrapping to
   * 3-4 lines while neighbours like "Cash" wrapped to 1 — created the
   * "blank space" complaint. Fix: force all labels onto a single line
   * with hard ellipsis, and hide the sub-text on mobile (it duplicates
   * info already in the value or chart). Full text remains accessible
   * via tap-and-hold on the card (title attr). */
  .pf-summary-row { grid-template-columns: repeat(4, 1fr); gap: 3px; align-items: stretch; }
  .pf-stat-card {
    padding: 5px 6px; border-radius: 5px;
    display: flex; flex-direction: column; justify-content: center;
    min-height: 44px;
    transition: transform .15s, border-color .15s;
  }
  .pf-stat-card:active { transform: scale(0.985); border-color: var(--blue); }
  .pf-stat-label {
    font-size: 7.5px; margin-bottom: 1px; letter-spacing: .03em;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    color: var(--dim);
  }
  .pf-stat-value {
    font-size: 13px; line-height: 1.1;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  /* Sub-text adds clutter on phones — full context lives in title attrs. */
  .pf-stat-sub { display: none !important; }

  /* ── Charts ─────────────────────────────────────────────────────────── */
  .pf-chart-wrap { height: 175px !important; padding: 8px 10px; border-radius: 8px; }
  .pf-chart-label { font-size: 9.5px !important; }
  .pf-regime-legend { gap: 8px; font-size: 9px; margin-bottom: 4px; }
  .pf-regime-legend .rl-dot { width: 8px; height: 8px; }

  /* ── Range buttons (1W/1M/3M/...) ───────────────────────────────────── */
  .range-btn {
    padding: 4px 8px; font-size: 10px; min-height: 26px;
    border-radius: 5px; transition: all .15s;
  }
  .range-btn:active { transform: scale(0.94); }

  /* ── Section panels & headers ───────────────────────────────────────── */
  .pf-section { border-radius: 7px; }
  .pf-section-header {
    padding: 6px 9px; font-size: 10px;
    background: linear-gradient(180deg, rgba(255,255,255,0.02), transparent);
  }

  /* ── Horizontal scroll on tables ─────────────────────────────────────── *
   * Only ONE scroll container — the outer .pf-section-body. The inner
   * #pf-positions / #st-active-wrap divs get overflow:visible so they
   * don't create a competing nested scroll region. Tables keep their
   * desktop inline min-widths (700–1100px) which exceed any phone
   * viewport, so the parent's overflow-x:auto naturally engages. The
   * table itself is sized to content via min-width: max-content so all
   * visible columns get their natural widths instead of being squeezed.
   */
  .pf-section-body {
    overflow-x: auto;
    overflow-y: hidden;
    -webkit-overflow-scrolling: touch;
    /* Right-edge fade indicator — anchored to the visible viewport via
     * background-attachment: local. Vanishes when the user reaches the
     * rightmost edge. */
    background-image: linear-gradient(90deg, transparent 88%, rgba(22,27,34,0.9));
    background-repeat: no-repeat;
    background-attachment: local;
    background-size: 100% 100%;
  }
  /* Inner wrappers must NOT have their own scroll context — collapses the
   * nested-scroll problem that was eating the swipe gesture. */
  #pf-positions, #pf-history,
  #st-active-wrap, #st-inactive-wrap {
    overflow: visible !important;
    width: max-content;
    min-width: 100%;
  }
  /* Thin custom horizontal scrollbar — visible enough to hint at the gesture. */
  .pf-section-body::-webkit-scrollbar { height: 5px; }
  .pf-section-body::-webkit-scrollbar-track { background: transparent; }
  .pf-section-body::-webkit-scrollbar-thumb {
    background: var(--border); border-radius: 3px;
    transition: background .2s;
  }
  .pf-section-body::-webkit-scrollbar-thumb:active { background: var(--blue); }

  /* ── Tables — extreme density, swipeable ──────────────────────────── *
   * Cell padding cut to 4×5; row height 26px. First column (strategy /
   * ticker name) hard-capped at 78px with ellipsis — long IDs like
   * "S_robust_minimum_variance_hedge" become "S_robust_…", with the
   * full name still on tap (the renderers already set title=). */
  .db-table { font-size: 10.5px; }
  .db-table th, .db-table td { white-space: nowrap; }
  .db-table th {
    padding: 4px 5px; font-size: 8.5px;
    background: var(--panel); position: sticky; top: 0; z-index: 3;
    letter-spacing: .02em;
  }
  .db-table td { padding: 5px 5px; }
  .db-table tr { min-height: 26px; }
  .db-table tr:active td { background: rgba(88,166,255,0.06); }

  /* Sticky first column — pinned, tight, hard-ellipsis. */
  .db-table th:first-child,
  .db-table td:first-child {
    position: sticky; left: 0; z-index: 4;
    background: var(--panel);
    box-shadow: 3px 0 5px -4px rgba(0,0,0,0.45);
    width: 78px !important;
    max-width: 78px !important;
    min-width: 78px !important;
    overflow: hidden;
    text-overflow: ellipsis;
    font-size: 10px;
  }
  .db-table th:first-child { z-index: 5; }
  /* Pf-positions / pf-history: first column is the ticker rollup row,
   * which carries the chevron + ticker name (parent) or "↳ strategy"
   * (sub-row). Give it more room than the default 78px since strategy
   * IDs can be long (S_robust_minimum_variance_hedge). */
  #pf-positions .db-table th:first-child,
  #pf-positions .db-table td:first-child,
  #pf-history   .db-table th:first-child,
  #pf-history   .db-table td:first-child {
    width: 130px !important; max-width: 130px !important; min-width: 130px !important;
  }

  /* ── Table buttons — compact ───────────────────────────────────────── */
  .st-action-btn {
    padding: 3px 7px !important; font-size: 9.5px !important;
    min-height: 24px; border-radius: 4px; transition: transform .12s;
  }
  .st-action-btn:active { transform: scale(0.94); }

  /* ── Per-table column hides ──────────────────────────────────────── *
   * Active Stack: Strategy | Status | Regimes | Open | Closed | Win% | ARR% | ADR% | ACT | #O/U/E | Last Signal | Actions
   * Hide: 3 (Regimes), 11 (Last Signal). */
  #st-active-wrap .db-table th:nth-child(3),
  #st-active-wrap .db-table td:nth-child(3),
  #st-active-wrap .db-table th:nth-child(11),
  #st-active-wrap .db-table td:nth-child(11) { display: none; }

  /* Active positions: Ticker | # | Dir | Entry | Current | P&L% | Contrib% | Size% | Days
   * On phones: hide Entry (4) and Days (9). Keep the contribution column —
   * it's the most informative cell in the rollup. */
  #pf-positions .db-table th:nth-child(4),
  #pf-positions .db-table td:nth-child(4),
  #pf-positions .db-table th:nth-child(9),
  #pf-positions .db-table td:nth-child(9) { display: none; }

  /* Closed history: Ticker | # | Dir | Entry | Close | P&L% | Contrib% | Days | Closed
   * On phones: hide Entry (4), Days (8), Closed (9) — same density logic. */
  #pf-history .db-table th:nth-child(4),
  #pf-history .db-table td:nth-child(4),
  #pf-history .db-table th:nth-child(8),
  #pf-history .db-table td:nth-child(8),
  #pf-history .db-table th:nth-child(9),
  #pf-history .db-table td:nth-child(9) { display: none; }

  /* ── Research page ─────────────────────────────────────────────────── *
   * Desktop uses CSS Grid with 1.4fr/1fr columns and the chat spanning
   * both rows on the left. On phones we collapse the entire layout to a
   * single linear column: chat first (fixed-height for usability), then
   * the right-column cards stacked, then the runs row at the bottom.
   * Also unlock #research-page from overflow:hidden so the page itself
   * scrolls instead of clipping its grid children. */
  #research-page { overflow-y: auto !important; overflow-x: hidden; }
  #research-inner {
    display: flex !important;
    flex-direction: column !important;
    grid-template-columns: none !important;
    grid-template-rows: none !important;
    height: auto !important;
    overflow: visible !important;
    padding: 6px !important;
    gap: 6px !important;
    max-width: 100% !important;
  }
  /* Reset desktop grid placements — chat is now a fullscreen overlay, not a column. */
  .rs-right-col, .rs-runs, .rs-campaigns, .rs-queue, .rs-papers {
    grid-row: auto !important;
    grid-column: auto !important;
  }
  .rs-card { min-height: auto !important; max-height: none !important; border-radius: 7px; }
  .rs-card header { padding: 5px 9px !important; font-size: 9.5px !important; }
  .rs-card header h3 { font-size: 9.5px !important; }
  .rs-card .rs-body { padding: 6px 9px !important; }
  /* Chat overlay still works as fullscreen on mobile when opened. */
  .rs-chat.open { height: 100vh !important; }
  .rs-right-col { display: flex !important; flex-direction: column !important; gap: 6px !important; min-height: auto !important; }
  /* Smaller FAB on mobile so it doesn't dominate. */
  .chat-fab { width: 50px !important; height: 50px !important; right: 14px !important; bottom: 14px !important; }
  .chat-fab svg { transform: scale(0.85); }
  /* Cap each side-card body so the long lists don't dominate the page —
   * each gets its own internal scroll, every section stays browsable. */
  .rs-campaigns .rs-body, .rs-queue .rs-body, .rs-papers .rs-body {
    max-height: 180px !important; overflow-y: auto !important;
  }
  .rs-runs { max-height: none !important; }
  .rs-runs .rs-body { max-height: 160px !important; overflow-y: auto !important; }
  .chat-scroll { padding: 6px 10px; gap: 6px; }
  .chat-input-row { padding: 6px !important; }
  .chat-input-row textarea { font-size: 16px; border-radius: 6px; min-height: 30px !important; }
  /* Compact pills + meta lines inside research cards. */
  .rs-row { padding: 5px 0 !important; }
  .rs-title { font-size: 11px !important; line-height: 1.25 !important; }
  .rs-meta { font-size: 9.5px !important; }
  .rs-pill { font-size: 9px !important; padding: 1px 5px !important; }
  .rs-filter { gap: 4px !important; flex-wrap: wrap; }
  .rs-filter select, .rs-filter input { font-size: 11px; padding: 2px 5px; }

  /* iOS auto-zooms text inputs <16px — bump to 16px so tap doesn't yank viewport. */
  #search, .rs-filter input { font-size: 16px; border-radius: 8px; }

  /* Collapse/expand footer — bigger tap area, gentler bg on press. */
  [id$="-collapse"] {
    padding: 12px 8px !important; font-size: 11px !important;
    transition: background .15s;
  }
  [id$="-collapse"]:active { background: var(--border2) !important; }

  /* Disable hover-only effects on touch devices. */
  .nav-btn:hover { background: transparent; color: var(--muted); }
  .nav-btn.active:hover, .nav-btn.active { background: var(--border2); color: var(--blue); }
}
</style>
</head>
<body>

<div id="header">
  <button id="mobile-menu" onclick="toggleSidebar()" aria-label="Toggle sidebar">☰</button>
  <span class="dot" id="dot"></span>
  <h1>🦞 OpenClaw</h1>
  <button class="nav-btn active" id="nav-market" onclick="showMarket()">Market</button>
  <button class="nav-btn" id="nav-portfolio" onclick="showPortfolio()">Portfolio</button>
  <button class="nav-btn" id="nav-strategies" onclick="showStrategies()">Strategies</button>
  <button class="nav-btn" id="nav-research" onclick="showResearch()">Research</button>
  <button class="nav-btn" id="nav-pipelines" onclick="showPipelines()">Pipeline Diagnostics</button>
  <span id="pipeline-badge">Loading pipeline...</span>
  <button class="refresh-btn" onclick="loadMarket();refreshPipeline()" title="Refresh data">↺ Refresh</button>
  <span id="clock"></span>
</div>

<!-- Mobile drawer backdrop — only visible when #sidebar.mobile-open is set.
     Tap-to-dismiss is handled by the document-level outside-click listener
     in JS; no inline onclick (would double-fire with the capture listener). -->
<div id="mobile-backdrop" aria-hidden="true"></div>

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
    <div class="pf-stat-card" title="Per-position win rate at close = positions with realized P&L > 0 / total positions closed. 30d on the left, lifetime on the right."><div class="pf-stat-label">Win Rate<br><span style="font-size:9px;color:var(--dim);font-weight:400">30d&nbsp;|&nbsp;Lifetime</span></div><div class="pf-stat-value" id="pf-winrate">—&nbsp;|&nbsp;—</div><div class="pf-stat-sub" id="pf-winrate-sub"></div></div>
    <div class="pf-stat-card"><div class="pf-stat-label" title="Annualized equity-curve return: (1 + period_return)^(252 / trading_days) - 1. Predicted = last 30 trading days only. Lifetime = since account inception. Identical until 30 trading days of equity history have accumulated.">Annualized Equity Realization %<br><span style="font-size:9px;color:var(--dim);font-weight:400">Predicted&nbsp;|&nbsp;Lifetime</span></div><div class="pf-stat-value" id="pf-avgpnl">—</div><div class="pf-stat-sub" id="pf-pnl-sub"></div></div>
  </div>
  <div class="pf-chart-wrap">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
      <div class="pf-chart-label" style="margin-bottom:0" id="pf-chart-title">Portfolio P&amp;L Curve (90d)</div>
      <div style="display:flex;gap:4px">
        <button class="range-btn active" id="btn-range-90d"  onclick="setPnlRange('90d')"  style="font-size:10px;padding:2px 8px">90d</button>
        <button class="range-btn"        id="btn-range-1y"   onclick="setPnlRange('1y')"   style="font-size:10px;padding:2px 8px">1y</button>
        <button class="range-btn"        id="btn-range-life" onclick="setPnlRange('life')" style="font-size:10px;padding:2px 8px">Lifetime</button>
      </div>
    </div>
    <div id="pf-regime-legend" class="pf-regime-legend" style="display:none">
      <span class="rl-label">Vol Regime</span>
      <span class="rl-item" data-state="LOW_VOL"><span class="rl-dot rl-dot-low"></span>Low Vol</span>
      <span class="rl-item" data-state="TRANSITIONING"><span class="rl-dot rl-dot-trans"></span>Transitioning</span>
      <span class="rl-item" data-state="HIGH_VOL"><span class="rl-dot rl-dot-high"></span>High Vol</span>
      <span class="rl-item" data-state="CRISIS"><span class="rl-dot rl-dot-crisis"></span>Crisis</span>
    </div>
    <canvas id="pnlChart" style="width:100%;height:150px"></canvas>
  </div>
  <div class="pf-section pf-risk-section">
    <div class="pf-section-header"><span>Risk &amp; Sizing</span><span id="pf-risk-saved-stamp" style="color:var(--muted);font-weight:400;font-size:10px"></span></div>
    <div class="pf-section-body">
      <div id="pf-risk-panel"><div class="empty">Loading risk controls…</div></div>
    </div>
  </div>
  <div class="pf-section">
    <div class="pf-section-header"><span>Active Positions</span><span style="display:flex;align-items:center;gap:10px"><select id="pf-pos-group-toggle" style="font-size:10px;background:var(--bg);color:var(--text);border:1px solid var(--border2);border-radius:3px;padding:2px 6px"><option value="">Flat</option><option value="strategy">By strategy</option></select><span id="pf-pos-count" style="color:var(--muted);font-weight:400;font-size:10px"></span></span></div>
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
      <div class="st-tile"><div class="st-tile-label">Total</div><div class="st-tile-value" id="st-total">—</div></div>
      <div class="st-tile"><div class="st-tile-label">Active Stack</div><div class="st-tile-value" id="st-active-tile">—</div><div class="st-tile-sub" id="st-active-sub">— live / — stale / — waiting</div></div>
      <div class="st-tile"><div class="st-tile-label">Inactive Stack</div><div class="st-tile-value" id="st-inactive-tile">—</div><div class="st-tile-sub">decommissioned</div></div>
      <div class="st-tile"><div class="st-tile-label">Research Candidates</div><div class="st-tile-value" id="st-candidate-tile">—</div><div class="st-tile-sub">in pipeline (auto-gated weekly)</div></div>
      <div class="st-tile"><div class="st-tile-label">Data</div><div class="st-tile-value" id="st-data-tile">—</div><div class="st-tile-sub" id="st-data-sub">financial parameters ingested</div><a class="st-tile-link" id="st-data-usage-link" onclick="_stOpenDataUsage()">View Data Usage →</a></div>
    </div>

    <!-- Phase 2B — Mastermind proposals panel (collapsed if none pending) -->
    <div class="pf-section" id="rp-section" style="display:none">
      <div class="pf-section-header">
        <span>⚙️ Strategy Adjustments</span>
        <span class="st-sub-label">fully-automatic Saturdays: brackets apply on a backtest Sharpe gain; size/eligibility auto-applies at confidence &gt; 0.8; the rest is noted below</span>
      </div>
      <div class="st-sub-label" style="margin:4px 0">Recent changes (7d) <span id="sa-applied-count">—</span></div>
      <table id="sa-applied-table" style="border-collapse:collapse;width:100%;font-size:12px;margin-bottom:14px">
        <thead><tr style="text-align:left;color:var(--muted);font-size:11px">
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Strategy</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Regime</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Via</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Change</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2);text-align:right">ΔSharpe</th>
          <th style="padding:6px 8px;border-bottom:1px solid var(--border2);text-align:right">Trades</th>
        </tr></thead>
        <tbody></tbody>
      </table>
      <div class="st-sub-label" style="margin:4px 0">Noted — low-confidence, re-evaluated next Saturday <span class="st-sub-label" id="rp-count">—</span></div>
      <table id="rp-table" style="border-collapse:collapse;width:100%;font-size:12px">
        <thead>
          <tr style="text-align:left;color:var(--muted);font-size:11px">
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Strategy</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Regime</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Proposed</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Conf</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Reasoning</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2);text-align:right">Decide / MC</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <!-- Per-regime conviction-gate sliders. Each card binds to
         regime_sizer_params.min_acting_strategies (migration 147) via debounced
         PUT to /api/config/regime-sizing/:regime. Integer range 1..10 enforced
         both by slider attr and DB CHECK constraint. 1 = today's open gate. -->
    <div class="pf-section">
      <div class="pf-section-header">
        <span>🎯 Conviction Gates <span class="st-sub-label">minimum strategies acting on a position in the same direction, per regime (1 = any single strategy trades; higher = only multi-strategy consensus trades)</span></span>
      </div>
      <div class="st-sharpe-grid" id="st-sharpe-gates">
        <div class="st-sharpe-card-empty">Loading conviction gates…</div>
      </div>
    </div>

    <!-- Strategy Activation threshold slider + assigner dry-run preview.
         Slider binds pipeline_config.strategy_activation_min_sharpe via
         debounced PUT /api/config/activation-min-sharpe (same UX as the
         conviction gates above). Preview POSTs /api/activation/dry-run —
         the server only ever runs the assigner in dry-run mode; the LIVE
         apply happens in the daily compute chain's 'activation' step (first
         step, 15:00 ET same-day lane — only when a slider moved since the
         last apply; 2026-08-22) and in the weekly Mon 00:00 ET
         weekly_live_sharpe.js run (OPENCLAW_ACTIVATION_ASSIGNER=1). -->
    <div class="pf-section">
      <div class="pf-section-header">
        <span>🎚️ Strategy Activation <span class="st-sub-label">per-regime floors applied on top of the qualification gate (&gt;0 Sharpe · class max-DD)</span></span>
      </div>
      <div class="st-act-grid">
        <div class="st-sharpe-card">
          <div class="st-sharpe-card-head">
            <span class="st-sharpe-card-regime">Activation min Sharpe</span>
            <span class="st-sub-label">pipeline_config</span>
          </div>
          <div class="st-sharpe-card-value" id="st-act-val">—</div>
          <input type="range" class="st-sharpe-card-slider" id="st-act-slider"
                 min="0" max="3" step="0.05" value="0.5" disabled />
          <div class="st-sharpe-card-range"><span>0.00</span><span>3.00</span></div>
          <div class="st-sharpe-card-status" id="st-act-status"></div>
        </div>
        <!-- Sample-size floor. Was hardcoded at 100 in
             regime_qualification.class_thresholds (tunable only via the
             assigner's --min-trades flag); made dashboard-adjustable
             2026-07-16. Unset => the assigner defers to the class gate (100),
             NOT to 0 — an absent key must never activate a regime on a
             1-trade sample, so the status line names which source is live. -->
        <div class="st-sharpe-card">
          <div class="st-sharpe-card-head">
            <span class="st-sharpe-card-regime">Activation min trades</span>
            <span class="st-sub-label">per regime</span>
          </div>
          <div class="st-sharpe-card-value" id="st-actn-val">—</div>
          <input type="range" class="st-sharpe-card-slider" id="st-actn-slider"
                 min="0" max="1000" step="10" value="100" disabled />
          <div class="st-sharpe-card-range"><span>0</span><span>1000</span></div>
          <div class="st-sharpe-card-status" id="st-actn-status"></div>
        </div>
        <div class="st-act-preview">
          <div class="st-act-preview-bar">
            <button class="st-action-btn" id="st-act-preview-btn" onclick="_actPreviewDryRun()">Preview (dry-run)</button>
            <span class="st-sub-label">hypothetical — zero DB writes; compares current eligible flags (DB reality) vs what the assigner would set at the saved threshold</span>
          </div>
          <div id="st-act-preview-out"><div class="st-act-preview-empty">No preview yet — set the threshold, then run a dry-run preview.</div></div>
        </div>
      </div>
    </div>

    <div id="st-drift-summary"></div>

    <!-- Section 1: Active Stack (live + stale + waiting) -->
    <div class="pf-section">
      <div class="pf-section-header">
        <span>Active Stack <span class="st-sub-label">(live / stale / waiting on regime)</span></span>
        <span id="st-active-count" class="st-sub-label"></span>
      </div>
      <div class="st-regime-filter" id="st-regime-filter">
        <span class="srf-label">View by Regime</span>
        <button class="srf-btn active" data-regime="ELIGIBLE"      onclick="_stSetRegimeFilter('ELIGIBLE')">Eligible</button>
        <button class="srf-btn" data-regime="ALL"                  onclick="_stSetRegimeFilter('ALL')">All</button>
        <button class="srf-btn srf-LOW_VOL"       data-regime="LOW_VOL"       onclick="_stSetRegimeFilter('LOW_VOL')">Low Vol</button>
        <button class="srf-btn srf-TRANSITIONING" data-regime="TRANSITIONING" onclick="_stSetRegimeFilter('TRANSITIONING')">Transitioning</button>
        <button class="srf-btn srf-HIGH_VOL"      data-regime="HIGH_VOL"      onclick="_stSetRegimeFilter('HIGH_VOL')">High Vol</button>
        <button class="srf-btn srf-CRISIS"        data-regime="CRISIS"        onclick="_stSetRegimeFilter('CRISIS')">Crisis</button>
        <span class="srf-hint" id="srf-hint">showing eligible-regimes blend (regimes the strategy is approved to trade)</span>
      </div>
      <div class="pf-section-body"><div id="st-active-wrap"><div class="empty">Loading...</div></div></div>
    </div>

    <!-- Section 2: Inactive Stack (deprecated / archived / orphan) -->
    <div class="pf-section">
      <div class="pf-section-header">
        <span>Inactive Stack <span class="st-sub-label">(decommissioned — historical metrics from prior live period)</span></span>
        <span id="st-inactive-count" class="st-sub-label"></span>
      </div>
      <div class="pf-section-body"><div id="st-inactive-wrap"><div class="empty">Loading...</div></div></div>
    </div>

    <!-- Research Candidates section REMOVED 2026-07-13 (operator directive):
         approvals are fully automatic now — candidates flow through the
         per-regime qualification gate every Sunday (auto_approval.js) and on
         every fused staging approval; newly promoted strategies surface via
         the ✨ fresh badge in the Active Stack. The tile above keeps the
         pipeline count visible; the Research tab's queue card still shows
         the research_candidates paper pipeline. -->
  </div>
</div><!-- #strategies-page -->

<div id="research-page">
  <!-- Chat lives as a fullscreen overlay; toggled open/closed by the FAB below. -->
  <section class="rs-card rs-chat" id="rs-chat-overlay">
    <div class="chat-close-bar">
      <button id="chat-close" title="Close chat (Esc)">✕ close</button>
    </div>
    <header>
      <div class="chat-header-strip">
        <span class="chat-session-name" id="chat-session-name">MasterMindJohn — Chat</span>
        <span class="chat-session-cost" id="chat-session-cost"></span>
      </div>
      <div class="chat-session-btns">
        <button class="chat-btn-slim" id="btn-sessions-drawer" title="Session history">history</button>
        <button class="chat-btn-slim" id="btn-new-session" title="New session">+ new</button>
      </div>
    </header>
    <div class="chat-scroll" id="chat-scroll">
      <div style="color:var(--muted);padding:14px;font-size:11px">Start a new session or pick one from history. MasterMindJohn has the dashboard snapshot loaded.</div>
    </div>
    <div class="chat-input-row">
      <textarea id="chat-input" placeholder="Ask MasterMindJohn… (Enter sends · Shift+Enter newline)"></textarea>
      <button id="chat-send">Send</button>
    </div>
  </section>

  <!-- Speech-bubble + brain FAB. Clicking opens the chat overlay above. -->
  <button class="chat-fab" id="chat-fab" aria-label="Open MasterMindJohn chat" title="Open chat">
    <svg viewBox="0 0 40 40" width="30" height="30" aria-hidden="true">
      <path d="M6 9 Q6 5 10 5 H30 Q34 5 34 9 V22 Q34 26 30 26 H22 L15 32 V26 H10 Q6 26 6 22 Z"
            fill="#0d0420" stroke="#a855f7" stroke-width="1.8" stroke-linejoin="round"/>
      <g transform="translate(20 16)">
        <path d="M -1.5 -5 Q -6 -5 -6 -1.5 Q -8 -.5 -7 1.5 Q -8 3 -6 4.5 Q -5 6.5 -2.5 5.5 Q -1.5 6.5 0 5.8 V -5.5 Q -1 -5 -1.5 -5Z" fill="#c084fc"/>
        <path d="M 1.5 -5 Q 6 -5 6 -1.5 Q 8 -.5 7 1.5 Q 8 3 6 4.5 Q 5 6.5 2.5 5.5 Q 1.5 6.5 0 5.8 V -5.5 Q 1 -5 1.5 -5Z" fill="#c084fc"/>
        <line x1="0" y1="-5.4" x2="0" y2="5.7" stroke="#0d0420" stroke-width="0.8"/>
        <path d="M -3.5 -2.5 Q -5 -1 -3.5 .5" stroke="#0d0420" stroke-width="0.6" fill="none"/>
        <path d="M 3.5 -2.5 Q 5 -1 3.5 .5" stroke="#0d0420" stroke-width="0.6" fill="none"/>
        <path d="M -4 2 Q -3 3 -2 2.5" stroke="#0d0420" stroke-width="0.5" fill="none"/>
        <path d="M 4 2 Q 3 3 2 2.5" stroke="#0d0420" stroke-width="0.5" fill="none"/>
      </g>
    </svg>
  </button>

  <div id="research-inner">
    <div class="rs-right-col">
      <section class="rs-card rs-campaigns">
        <header><h3>Active Campaigns</h3><span id="camp-count" style="color:var(--muted);font-size:10px"></span></header>
        <div class="rs-body" id="camp-body"><div style="color:var(--muted);font-size:11px">Loading…</div></div>
      </section>

      <section class="rs-card rs-queue">
        <header><h3>Research Candidates</h3><span id="queue-count" style="color:var(--muted);font-size:10px"></span></header>
        <div class="rs-body" id="queue-body"><div style="color:var(--muted);font-size:11px">Loading…</div></div>
      </section>

      <section class="rs-card rs-papers">
        <header>
          <h3>Papers + Findings</h3>
          <div class="rs-filter">
            <select id="papers-status">
              <option value="">all</option>
              <option value="done">done</option>
              <option value="pending">pending</option>
              <option value="blocked_rejected">rejected</option>
              <option value="blocked_buildable">buildable</option>
              <option value="blocked_unclassified">unclassified</option>
            </select>
            <input id="papers-q" placeholder="search…"/>
            <span id="papers-count" style="color:var(--muted);font-size:10px"></span>
          </div>
        </header>
        <div class="rs-body" id="papers-body"><div style="color:var(--muted);font-size:11px">Loading…</div></div>
      </section>

      <section class="rs-card" style="flex:0 0 auto;max-height:220px">
        <header><h3>Strategy Staging</h3><span id="staging-count" style="color:var(--muted);font-size:10px"></span></header>
        <div class="rs-body" id="staging-body"><div style="color:var(--muted);font-size:11px">Loading…</div></div>
      </section>
    </div>

    <section class="rs-card rs-runs">
      <header><h3>Weekly MasterMind Runs</h3><span id="runs-hist-count" style="color:var(--muted);font-size:10px"></span></header>
      <div class="rs-body" id="runs-hist-body" style="padding:6px 12px"><div style="color:var(--muted);font-size:10px">Loading…</div></div>
    </section>
  </div>

  <div class="sessions-backdrop" id="sessions-backdrop"></div>
  <aside class="sessions-drawer" id="sessions-drawer">
    <header>
      <h3>Sessions</h3>
      <button class="chat-btn-slim" id="btn-close-drawer">close</button>
    </header>
    <div class="sessions-drawer-body" id="sessions-drawer-body"><div style="color:var(--muted);padding:14px;font-size:11px">Loading…</div></div>
  </aside>

  <div class="paper-modal" id="paper-modal">
    <div class="pm-card">
      <div class="pm-head">
        <h2 id="pm-title">—</h2>
        <button class="pm-close" id="pm-close">close (esc)</button>
      </div>
      <div class="pm-body" id="pm-body"></div>
    </div>
  </div>
</div><!-- #research-page -->

<!-- Pipeline Diagnostics — LangGraph run inspector -->
<div id="pipeline-page">
  <div id="pipeline-inner">
    <div class="pl-page-intro">
      <div>
        <h2>Pipeline Diagnostics</h2>
        <div class="pl-sub">Live + historical view of every LangGraph run. The daily-cycle graph fires at <code>10:00 ET Mon-Fri</code>; paperhunter runs ad-hoc. Click a row to see node-by-node timing, checkpoint state, and live traces.</div>
      </div>
      <div class="pl-actions" id="pl-last-updated">—</div>
    </div>

    <div class="pl-tiles">
      <div class="pl-tile" id="pl-tile-card-active">
        <div class="pl-tile-row"><span class="pl-tile-icon">⚡</span><span class="pl-tile-label">Active runs</span></div>
        <div class="pl-tile-value" id="pl-tile-active">—</div>
        <div class="pl-tile-sub">currently in-flight</div>
      </div>
      <div class="pl-tile" id="pl-tile-card-today">
        <div class="pl-tile-row"><span class="pl-tile-icon">📅</span><span class="pl-tile-label">Started today</span></div>
        <div class="pl-tile-value" id="pl-tile-today">—</div>
        <div class="pl-tile-sub">since 00:00 local</div>
      </div>
      <div class="pl-tile" id="pl-tile-card-failures">
        <div class="pl-tile-row"><span class="pl-tile-icon">⚠</span><span class="pl-tile-label">Failures · 24h</span></div>
        <div class="pl-tile-value" id="pl-tile-failures">—</div>
        <div class="pl-tile-sub">error / aborted status</div>
      </div>
      <div class="pl-tile" id="pl-tile-card-durable">
        <div class="pl-tile-row"><span class="pl-tile-icon">💾</span><span class="pl-tile-label">Durable threads</span></div>
        <div class="pl-tile-value" id="pl-tile-durable">—</div>
        <div class="pl-tile-sub">in langgraph.checkpoints</div>
      </div>
    </div>
    <div id="pl-count-window" style="font-size:10px;color:var(--muted);margin-top:6px;display:none"></div>

    <div class="pl-card">
      <header>
        <h3>Recent runs <span class="pl-dim" id="pl-runs-count" style="text-transform:none;letter-spacing:0;font-weight:400">—</span></h3>
        <div class="pl-controls">
          <label>Graph <select id="pl-filter-graph"><option value="">All</option></select></label>
          <label>Show <select id="pl-filter-limit">
              <option>25</option><option selected>50</option><option>100</option><option>200</option>
            </select></label>
          <button class="pl-btn" id="pl-refresh" title="Reload runs + summary"><span id="pl-refresh-icon">↺</span> Reload</button>
        </div>
      </header>
      <div class="pl-body">
        <table class="pl-runs-table">
          <thead>
            <tr>
              <th>Graph</th><th>Thread</th><th>Status</th><th>Last node</th>
              <th>Started</th><th>Duration</th><th>Activity</th>
            </tr>
          </thead>
          <tbody id="pl-runs-tbody">
            <tr><td colspan="7" class="pl-empty">
              <div class="pl-empty-icon">⏳</div>Loading runs…
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="pl-card">
      <header>
        <h3>Run detail</h3>
        <div class="pl-controls">
          <button class="pl-btn" id="pl-detail-copy" title="Copy thread id" disabled>📋 Copy ID</button>
        </div>
      </header>
      <div class="pl-detail" id="pl-detail-body">
        <div class="pl-empty">
          <div class="pl-empty-icon">👆</div>
          Click a row above to load the node-by-node timeline, checkpoint metadata, and any live trace events for that run.
        </div>
      </div>
    </div>

    <div class="pl-card">
      <header>
        <h3>Live stream <span class="pl-dim" id="pl-stream-count" style="text-transform:none;letter-spacing:0;font-weight:400"></span></h3>
        <div class="pl-controls">
          <span class="pl-status persisted" id="pl-stream-status">disconnected</span>
          <button class="pl-btn primary" id="pl-stream-toggle">▶ Connect</button>
          <button class="pl-btn" id="pl-stream-pause" disabled title="Pause incoming events">⏸ Pause</button>
          <button class="pl-btn danger" id="pl-stream-clear">Clear</button>
        </div>
      </header>
      <div class="pl-stream" id="pl-stream-pane">
        <div class="pl-stream-line sys"><span class="pl-stream-ts">—</span>Stream idle. Click <b>Connect</b> to subscribe to traceBus events.</div>
      </div>
    </div>

    <div class="pl-card">
      <header>
        <h3>Universe Inflation <span class="pl-dim" id="pl-uni-inf-count" style="text-transform:none;letter-spacing:0;font-weight:400"></span></h3>
        <div class="pl-controls">
          <span class="pl-dim" id="pl-uni-inf-ts"></span>
        </div>
      </header>
      <div class="pl-body" id="pl-uni-inf-body">
        <div class="pl-empty"><div class="pl-spinner"></div>Loading…</div>
      </div>
    </div>

    <div class="pl-card">
      <header>
        <h3>Backfill History <span class="pl-dim" id="pl-bfh-count" style="text-transform:none;letter-spacing:0;font-weight:400"></span></h3>
        <div class="pl-controls">
          <span class="pl-dim" id="pl-bfh-ts"></span>
        </div>
      </header>
      <div class="pl-body" id="pl-bfh-body">
        <div class="pl-empty"><div class="pl-spinner"></div>Loading…</div>
      </div>
    </div>
  </div>
</div><!-- #pipeline-page -->

<!-- Data usage panel (opened from the strategies page Data tile) -->
<div class="paper-modal data-usage-modal" id="data-usage-modal">
  <div class="pm-card du-card">
    <div class="pm-head">
      <h2>Data Usage</h2>
      <button class="pm-close" onclick="_stCloseDataUsage()">close (esc)</button>
    </div>
    <div class="pm-body" id="data-usage-body"><div style="color:var(--muted);padding:20px;font-size:11px">Loading…</div></div>
  </div>
</div>

</div><!-- #view-wrap -->

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let universeData = {};     // {category: [{ticker, name, ...}]}
let marketData   = {};     // {ticker: {close, change_pct, name, category, date, ...}}
let currentTicker = null;
let currentRange  = 365;
let currentCat    = 'all';
let priceChart    = null;

const CAT_LABELS = {equity:'US Equities',index:'Indices',etf:'ETFs',crypto:'Crypto',commodity:'Commodities',forex:'Forex'};
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
  setInterval(_checkDashboardBuild, 60000);
  setInterval(loadMarket, 300000);
  // Intraday regime card refreshes every 60s. The detector cron is 5min,
  // so most polls see the same tick — but the "Last Tick X min ago" age
  // and STALE badge update in real time, which is what the operator wants
  // to glance at to confirm the live regime engine is still healthy.
  setInterval(() => {
    if (document.getElementById('regime-panel')) loadRegime();
  }, 60_000);
  // Portfolio refresh: pulls live broker truth (unrealized_pl + plpc per
  // ticker) on every poll. 30s during RTH so the heatmap tracks intraday
  // moves; 5min outside RTH since prices are static. Alpaca paper rate
  // limit is 200/min — two CLI calls per request × 2/min during RTH is
  // 4/min, well under the cap. SSE market_update still retriggers ad-hoc.
  setInterval(() => {
    const pp = document.getElementById('portfolio-page');
    if (!pp || pp.style.display !== 'block') return;
    // Approximate RTH check on the client: weekday + 09:30-16:00 ET.
    // Cheap; server-side _alpaca_session_kind would be more correct but
    // requires an extra round-trip just to gate a poll.
    const nowEt = new Date(new Date().toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const day   = nowEt.getDay();           // 0 = Sun, 6 = Sat
    const mins  = nowEt.getHours() * 60 + nowEt.getMinutes();
    const inRth = day >= 1 && day <= 5 && mins >= 570 && mins < 960;  // 9:30-16:00
    const interval = inRth ? 30_000 : 300_000;
    if (Date.now() - (window._lastPortfolioPoll || 0) >= interval) {
      window._lastPortfolioPoll = Date.now();
      loadPortfolio();
    }
  }, 15_000);

  // SSE with auto-reconnect. EventSource has a built-in retry but it won't
  // resubscribe cleanly after the process behind /events restarts; we
  // re-open the connection after a short backoff and re-hydrate state.
  let es = null;
  function _openSSE() {
    es = new EventSource('/events');
    es.onmessage = _handleSSE;
    es.onerror = () => {
      document.getElementById('dot').style.background = 'var(--red)';
      try { es.close(); } catch (_) {}
      setTimeout(_openSSE, 3_000);
    };
    es.onopen = () => {
      document.getElementById('dot').style.background = 'var(--green)';
      // re-hydrate chips after a reconnect
      const st = document.getElementById('strategies-page');
      if (st && st.style.display === 'block') loadStrategies();
    };
  }
  function _handleSSE(e) {
    const d = JSON.parse(e.data);
    if (d.type === 'pipeline') refreshPipeline();
    if (d.type === 'market_update') {
      loadMarket();
      refreshPipeline();
      const st = document.getElementById('strategies-page');
      if (st && st.style.display === 'block') loadStrategies();
      if (document.getElementById('portfolio-page').style.display === 'block') loadPortfolio();
    }
    if (d.type === 'strategy_transition') {
      const st = document.getElementById('strategies-page');
      if (st && st.style.display === 'block') loadStrategies();
    }
    if (d.type === 'approval_job') {
      const sid = d.strategy_id;
      if (!sid) return;
      if (d.status === 'running') {
        _stActiveJobs[sid] = {
          job_id: d.job_id, phase: d.phase, progress: d.progress || 0,
          strategy_id: sid, payload: d.payload || {},
        };
      } else {
        // succeeded / failed / cancelled → drop chip + refresh row
        delete _stActiveJobs[sid];
        if (d.status === 'failed') {
          const reason = _stFailReason(d.result);
          _stLastFailures[sid] = { job_id: d.job_id, reason };
          toast('❌ ' + sid + ' — ' + reason, 'error', 8_000);
        } else if (d.status === 'succeeded') {
          delete _stLastFailures[sid];
          toast('✅ ' + sid + ' approved successfully', 'ok', 4_000);
        } else if (d.status === 'cancelled') {
          toast('⚠️ ' + sid + ' — approval cancelled', 'warn', 3_000);
        }
      }
      const st = document.getElementById('strategies-page');
      if (st && st.style.display === 'block') _stRenderJobChips();
      if (d.status !== 'running') loadStrategies();
    }
  }
  _openSSE();

  // Polling safety net: while any approval chip is showing, poll
  // /api/approvals/active every 3s so stale chips clear even if SSE misses
  // a 'finished' event (network blip / reconnect race).
  setInterval(async () => {
    if (Object.keys(_stActiveJobs).length === 0) return;
    try {
      const active = await fetch('/api/approvals/active').then(r => r.json());
      const alive = new Set((active || []).map(j => j.strategy_id));
      let changed = false;
      for (const sid of Object.keys(_stActiveJobs)) {
        if (!alive.has(sid)) { delete _stActiveJobs[sid]; changed = true; }
      }
      for (const j of active) {
        const cur = _stActiveJobs[j.strategy_id];
        if (!cur || cur.progress !== j.progress || cur.phase !== j.phase) {
          _stActiveJobs[j.strategy_id] = j; changed = true;
        }
      }
      if (changed) {
        const st = document.getElementById('strategies-page');
        if (st && st.style.display === 'block') _stRenderJobChips();
      }
    } catch (_) {}
  }, 3_000);
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

// Compound-annualize a per-trade return over its average holding window.
// r = fractional return (0.05 = 5%), days = avg trading-days held.
// Returns the annualized return as a percent (e.g. 42.5), or null when
// inputs are missing / non-positive. Capped at ±500% so short-hold
// outliers don't blow out the display.
function _annualizePct(r, days) {
  if (r == null || isNaN(r)) return null;
  if (days == null || isNaN(days) || days <= 0) return null;
  const exp = 252 / days;
  const aar = (r >= -1) ? (Math.pow(1 + r, exp) - 1) : -1;
  const pct = aar * 100;
  if (!isFinite(pct)) return null;
  return Math.max(-500, Math.min(500, pct));
}

// ── Sortable tables (shared) ───────────────────────────────────────────────
// Click any <th data-sort-key> to toggle asc/desc. Each render function that
// opts in: caches its rows in _tableDataCache[id], emits ths with data-sort-key
// + data-sort-type, and calls _bindSortable(id, renderFn) at the end.
const _sortState      = {};   // { tableId: { key, dir } }
const _tableDataCache = {};   // { tableId: rawRows }

function _sortRows(rows, key, dir, type) {
  const mul = dir === 'desc' ? -1 : 1;
  const coerce = type === 'num'
    ? v => (v == null || v === '' ? NaN : parseFloat(v))
    : type === 'date'
      ? v => (v == null || v === '' ? NaN : new Date(v).getTime())
      : v => (v == null ? '' : String(v).toLowerCase());
  const out = rows.slice();
  out.sort((a, b) => {
    const va = coerce(a[key]);
    const vb = coerce(b[key]);
    const aN = typeof va === 'number' && isNaN(va);
    const bN = typeof vb === 'number' && isNaN(vb);
    if (aN && bN) return 0;
    if (aN) return 1;   // nulls always sort last
    if (bN) return -1;
    if (va < vb) return -1 * mul;
    if (va > vb) return  1 * mul;
    return 0;
  });
  return out;
}

function _bindSortable(tableId, renderFn) {
  const host = document.getElementById(tableId);
  if (!host) return;
  host.querySelectorAll('th[data-sort-key]').forEach(th => {
    th.style.cursor = 'pointer';
    th.addEventListener('click', () => {
      const key  = th.dataset.sortKey;
      const type = th.dataset.sortType || 'str';
      const s    = _sortState[tableId] || {};
      if (s.key === key) s.dir = s.dir === 'asc' ? 'desc' : 'asc';
      else { s.key = key; s.dir = 'asc'; }
      s.type = type;
      _sortState[tableId] = s;
      const rows = _tableDataCache[tableId] || [];
      renderFn(rows);
    });
    // Arrow indicator on the active sort column
    const s = _sortState[tableId];
    if (s && s.key === th.dataset.sortKey) {
      const arrow = s.dir === 'asc' ? ' ▲' : ' ▼';
      th.innerHTML = th.innerHTML.replace(/\s*[▲▼]$/, '') + arrow;
    }
  });
}

function _applySort(tableId, rows, defaultKey, defaultType) {
  _tableDataCache[tableId] = rows;
  const s = _sortState[tableId];
  if (!s || !s.key) return rows;
  return _sortRows(rows, s.key, s.dir, s.type || 'str');
}

// ── Collapse-to-top-N (shared) ─────────────────────────────────────────────
// Each table can toggle between a top-10 preview and the full list. State
// persists across re-renders in _collapseState. Default = collapsed.
const _collapseState = {}; // { tableId: false means expanded; otherwise collapsed }
// Collapse limit is mobile-aware: 5 on phones, 10 on desktop. Phones
// have a fraction of the vertical real-estate, so the top-N preview
// needs to be tighter or every section pushes the next off-screen.
function _collapseLimit() {
  // Show more rows by default on mobile — operator wants info density
  // over whitespace. 8 rows × ~32px = ~256px, still leaves room for
  // headers + the next section on screen.
  return (window.matchMedia && window.matchMedia('(max-width: 768px)').matches) ? 8 : 10;
}

function _collapseRows(tableId, rows) {
  const collapsed = _collapseState[tableId] !== false; // default true
  const COLLAPSE_N = _collapseLimit();
  if (rows.length <= COLLAPSE_N) return { shown: rows, footer: '' };
  const shown = collapsed ? rows.slice(0, COLLAPSE_N) : rows;
  const label = collapsed
    ? \`+\${rows.length - COLLAPSE_N} more · <span style="color:var(--blue);cursor:pointer">Show all</span>\`
    : \`Showing \${rows.length} · <span style="color:var(--blue);cursor:pointer">Collapse</span>\`;
  const footer = \`<div id="\${tableId}-collapse" style="padding:6px 8px;font-size:10px;color:var(--dim);text-align:center;cursor:pointer;border-top:1px solid var(--border2)">\${label}</div>\`;
  return { shown, footer };
}

function _bindCollapse(tableId, renderFn) {
  const btn = document.getElementById(\`\${tableId}-collapse\`);
  if (!btn) return;
  btn.addEventListener('click', () => {
    _collapseState[tableId] = _collapseState[tableId] === false; // toggle
    renderFn(_tableDataCache[tableId] || []);
  });
}

// Rank helpers for the "Status" column sort on Strategies tables.
// Active Stack:   Waiting → Stale → Live   (Live = most-active = highest)
// Candidates:     Staging → Candidate → Paper  (Paper = most-advanced)
const _ACTIVE_RANK    = { waiting: 0, stale: 1, live: 2 };
const _CANDIDATE_RANK = { staging: 0, candidate: 1, paper: 2 };
function _activeRankFor(row) {
  return _ACTIVE_RANK[_activeSub(row)] ?? -1;
}
function _candidateRankFor(row) {
  return _CANDIDATE_RANK[String(row.state || '').toLowerCase()] ?? -1;
}

// ── Bar chart builder (daily P&L) ──────────────────────────────────────────
// Positive bars green, negative red, flat (|v| < 0.005%) grey. Style mirrors
// the line chart: same accent hues, translucent fills, 1px top-edge border
// for definition. Designed to stay readable at 90 bars:
//   - barPercentage + categoryPercentage = 1.0  → bars touch (no gaps)
//   - 0.55 alpha fill, 0.85 alpha border         → lifts a faint "filled-area"
//     feel like the line chart's underlay
//   - borderRadius: 0, borderWidth: 1 top-only  → clean rectangles at ~3px
//   - hover = full-opacity same-hue bar          → pops on inspection
//   - hover interaction mode 'nearest'           → precise single-bar lookup
function _buildBarChart(labels, values, yFmt, tooltipFmt) {
  const wrap = document.getElementById('pnlChart');
  if (!wrap) return;
  if (pnlChart) { pnlChart.destroy(); pnlChart = null; }
  const hues = {
    pos:  { r:63,  g:185, b:80  },
    neg:  { r:248, g:81,  b:73  },
    flat: { r:110, g:118, b:129 },
  };
  const rgba = (h, a) => \`rgba(\${h.r},\${h.g},\${h.b},\${a})\`;
  const classify = v => Math.abs(v) < 0.005 ? 'flat' : (v >= 0 ? 'pos' : 'neg');
  const fillCol   = values.map(v => rgba(hues[classify(v)], 0.55));
  const borderCol = values.map(v => rgba(hues[classify(v)], 0.85));
  const hoverCol  = values.map(v => rgba(hues[classify(v)], 0.95));
  pnlChart = new Chart(wrap.getContext('2d'), {
    type: 'bar',
    data: { labels, datasets: [{
      data:                 values,
      backgroundColor:      fillCol,
      borderColor:          borderCol,
      hoverBackgroundColor: hoverCol,
      hoverBorderColor:     borderCol,
      borderWidth:          { top: 1, right: 0, bottom: 0, left: 0 },
      borderRadius:         0,
      borderSkipped:        false,
      barPercentage:        1.0,
      categoryPercentage:   1.0,
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 250 },
      interaction: { mode: 'nearest', intersect: false, axis: 'x' },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor:'#161b22', borderColor:'#30363d', borderWidth:1,
          titleColor:'#8b949e', bodyColor:'#e6edf3',
          displayColors: false, padding: 8,
          callbacks: { label: ctx => tooltipFmt(ctx.parsed.y) },
        },
      },
      scales: {
        x: {
          ticks: { color:'#484f58', maxTicksLimit:8, font:{size:10}, maxRotation:0, autoSkip:true },
          grid:  { display:false },
          border:{ color:'#30363d' },
        },
        y: {
          position:'right',
          ticks: { color:'#484f58', font:{size:10}, callback: yFmt },
          grid:  { color:'#21262d', drawTicks:false },
          border:{ display:false },
          beginAtZero: true,
        },
      },
    },
  });
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

// ── Mobile sidebar drawer ─────────────────────────────────────────────────
// Below 768px the sidebar slides in from the left as an overlay drawer.
// Tap the hamburger to show, tap a ticker / outside to dismiss. No-op on
// desktop because the hamburger is display:none there.
function toggleSidebar() {
  const sb = document.getElementById('sidebar');
  if (sb) sb.classList.toggle('mobile-open');
}
// Auto-close the drawer when the user picks a ticker (single-tap nav feels
// natural). Also closes on tab switch via showMarket/Portfolio/etc.
document.addEventListener('click', (e) => {
  if (window.innerWidth > 768) return;
  const sb = document.getElementById('sidebar');
  if (!sb || !sb.classList.contains('mobile-open')) return;
  // Don't dismiss if the tap originated inside the sidebar (let internal
  // controls work) UNLESS it was on a ticker row — those should navigate
  // and close.
  const ticker = e.target.closest('.tk');
  const insideSb = e.target.closest('#sidebar');
  const onMenuBtn = e.target.closest('#mobile-menu');
  if (onMenuBtn) return;
  if (ticker || !insideSb) sb.classList.remove('mobile-open');
}, true);

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
    <div class="regime-panel-header">Volatility Regime Structure</div>
    <div class="empty" style="padding:10px 0;font-size:11px">Loading regime...</div>
  </div>\`;
  const cryptoRegimePlaceholder = \`<div id="crypto-regime-panel" class="regime-panel">
    <div class="regime-panel-header">Crypto Regime Structure</div>
    <div class="empty" style="padding:10px 0;font-size:11px">Loading crypto regime...</div>
  </div>\`;
  ov.innerHTML = regimePlaceholder + cryptoRegimePlaceholder + (html || '<div class="empty">No market data yet — pipeline runs at 9:00 AM ET</div>') + newsHtml;
  loadNewsSection('overview-news', null);
  loadRegime();
  loadCryptoRegime();
}

// ── Regime Panel ─────────────────────────────────────────────────────────────
// "Volatility Regime Structure" — single consolidated card. State + features +
// position_scale + duration come from the intraday HMM (6-feature, 5-min
// cadence, scored against hmm_intraday_latest.pkl). A few daily-only fields
// (Stress, RORO, VIX 5d Δ, SPX 20d RV, HY/IG spread) are pulled from
// /api/regime — these have no intraday equivalent. Next-day transition
// probabilities removed 2026-05-23: HMM is not predictive, the daily
// transition matrix gave operators a false sense of forecasting.
// loadRegime() fetches both endpoints in parallel and composes the panel;
// re-polled every 60s so freshness drift below the 5-min cron is visible.
const _STATE_COLOR  = {LOW_VOL:'var(--green)',TRANSITIONING:'var(--yellow)',HIGH_VOL:'var(--orange)',CRISIS:'var(--red)'};
const _STATE_SHORT  = {LOW_VOL:'Low Vol',TRANSITIONING:'Transit.',HIGH_VOL:'High Vol',CRISIS:'Crisis'};
const _STATE_CLASS  = {LOW_VOL:'regime-state-LOW_VOL',TRANSITIONING:'regime-state-TRANSITIONING',
                       HIGH_VOL:'regime-state-HIGH_VOL',CRISIS:'regime-state-CRISIS'};

function _ageStr(tsUtc) {
  if (!tsUtc) return '—';
  const ageMs = Date.now() - new Date(tsUtc).getTime();
  const m = Math.round(ageMs / 60000);
  if (m < 1)  return 'just now';
  if (m < 60) return \`\${m}m ago\`;
  const h = Math.floor(m/60), r = m%60;
  return r ? \`\${h}h \${r}m ago\` : \`\${h}h ago\`;
}

function _durationStr(min) {
  if (min == null) return '—';
  if (min < 60) return \`\${min}m\`;
  const h = Math.floor(min/60);
  if (h < 24) return \`\${h}h \${min%60}m\`;
  const d = Math.floor(h/24);
  const hr = h % 24;
  return hr ? \`\${d}d \${hr}h\` : \`\${d}d\`;
}

// Single source of truth for the 6 intraday-HMM feature labels — used by
// both the subtitle line and the feature-tile grid. Order matches
// HMM_INPUT_COLS in scripts/run_intraday_market_state.py + train_intraday_hmm.py:
// [vix_synth_30d, vix_synth_90d, vix_term_slope, rr_25d, spy_gk_vol_daily, vvix_level].
const _INTRADAY_FEATURE_TILES = [
  ['VIX',        'vix_synth_30d',    2],
  ['VIX3M',      'vix_synth_90d',    2],
  ['Term Slope', 'vix_term_slope',   3],
  ['SPY GK Vol', 'spy_gk_vol_daily', 3],
  ['VVIX',       'vvix_level',       2],
  ['25Δ RR',     'rr_25d',           3],
];

function _renderRegimeStructure(intraday, daily) {
  if (!intraday || !intraday.available) {
    return \`<div class="regime-panel-header" style="margin-bottom:8px">
      Volatility Regime Structure <span style="font-size:9px;color:var(--dim);text-transform:none;letter-spacing:0">intraday HMM</span>
    </div>
    <div class="empty" style="padding:8px 0;font-size:11px">No intraday ticks yet — collector starts at 9:00 AM ET on market days.</div>\`;
  }
  const L = intraday.latest || {};
  const D = intraday.derived || {};
  const dly = (daily && daily.available) ? daily : null;

  const stateClass = _STATE_CLASS[L.state] || 'regime-state-NO_DATA';
  const conf       = L.confidence != null ? Math.round(L.confidence*100)+'%' : '—';
  const age        = _ageStr(L.ts_utc);
  const posScale   = D.position_scale_pct != null ? Math.round(D.position_scale_pct*100)+'%' : '—';
  const duration   = _durationStr(D.duration_minutes);

  // Stale: last tick > 15 min old (5-min cron + 1 tolerance tick + slack).
  const ageMin = L.ts_utc ? (Date.now() - new Date(L.ts_utc).getTime())/60000 : Infinity;
  const stale = ageMin > 15;
  const staleBadge = stale ? \`<span class="regime-alert-badge" title="last tick &gt;15 min ago — cron may be paused (out of schedule window or service down)">STALE · \${age}</span>\` : '';
  // Carry-fwd: tick is fresh but lands outside 9:30-16:15 ET (option market closed).
  const tickEt = L.ts_utc ? new Date(new Date(L.ts_utc).toLocaleString('en-US', { timeZone: 'America/New_York' })) : null;
  const optionMktOpen = tickEt && (() => {
    const m = tickEt.getHours() * 60 + tickEt.getMinutes();
    const dow = tickEt.getDay();
    return dow >= 1 && dow <= 5 && m >= 570 && m <= 975;
  })();
  const carryFwdBadge = (!stale && tickEt && !optionMktOpen)
    ? \`<span class="regime-alert-badge" style="background:var(--border2);color:var(--muted);border-color:var(--border)" title="Option market closed (9:30-16:15 ET). Synthetic VIX/RR features reflect prior-close quotes, not fresh signal.">CARRY-FWD QUOTES</span>\`
    : '';
  const priorBadge = (L.prior_state && L.prior_state !== L.state)
    ? \`<span class="regime-alert-badge">⚠ \${L.prior_state} → \${L.state}</span>\` : '';

  // Intraday-derived features (6-feature HMM input).
  const f = L.features_json || {};
  const fmt = (v, dp) => v == null || (typeof v === 'number' && isNaN(v)) ? '—' : (typeof v === 'number' ? v.toFixed(dp) : String(v));
  const featHtml = _INTRADAY_FEATURE_TILES
    .map(([lbl, key, dp]) => \`<div class="regime-feat"><div class="regime-feat-label">\${lbl}</div><div class="regime-feat-val">\${fmt(f[key], dp)}</div></div>\`)
    .join('');

  // 24h sparkline — exactly 96 fixed-width buckets, each representing one
  // 15-min window (matches the 3-tick hysteresis confirmation gate, so a
  // colored bucket = "a regime stable enough to have triggered a redeploy"
  // and a streak of N same-color buckets = N × 15 min of confirmed regime).
  // Raw 5-min rows from intraday_regime_states get bucketed by their UTC
  // timestamp; buckets with no data render as dim placeholders so the
  // operator can see gaps (overnight, weekends, service down).
  const _BUCKETS = 96;
  const _BUCKET_MS = 15 * 60 * 1000;
  const _now = Date.now();
  const _bucketStart = _now - _BUCKETS * _BUCKET_MS;
  const _buckets = Array.from({ length: _BUCKETS }, (_, i) => ({
    start: _bucketStart + i * _BUCKET_MS,
    end:   _bucketStart + (i + 1) * _BUCKET_MS,
    rows: [],
  }));
  for (const r of (intraday.history || [])) {
    const t = new Date(r.ts_utc).getTime();
    const idx = Math.floor((t - _bucketStart) / _BUCKET_MS);
    if (idx >= 0 && idx < _BUCKETS) _buckets[idx].rows.push(r);
  }
  const sparkHtml = _buckets.map(b => {
    // UNKNOWN is treated as no-data: the detector recorded a row but the
    // classifier couldn't assign a real regime (model not loaded, bootstrap
    // mode, etc.), so it carries no actionable information. Merge with
    // truly-empty buckets — one visual group, one tooltip.
    const useful = b.rows.filter(r => r.state && r.state !== 'UNKNOWN');
    const winLbl = \`\${new Date(b.start).toISOString().slice(11,16)}–\${new Date(b.end).toISOString().slice(11,16)} UTC\`;
    if (useful.length === 0) {
      // Matches the exact look UNKNOWN ticks had in the pre-bucketing
      // sparkline: var(--dim) at opacity 0.5 (the floor that fell out of
      // Math.max(0.25, Number(r.confidence) || 0.5) when confidence=0).
      return \`<span class="regime-spark-tick" style="background:var(--dim);opacity:0.5" title="\${winLbl} · no data"></span>\`;
    }
    const states = useful.map(r => r.state);
    const allSame = states.every(s => s === states[0]);
    const color = allSame ? (_STATE_COLOR[states[0]] || 'var(--dim)') : 'var(--muted)';
    const avgConf = useful.reduce((a, r) => a + (Number(r.confidence) || 0), 0) / useful.length;
    const op = Math.max(0.3, Math.min(1, avgConf));
    const stateLbl = allSame ? states[0] : 'mixed (' + states.join('/') + ')';
    const tt = \`\${winLbl} · \${stateLbl} · \${useful.length}/3 ticks · avg conf \${Math.round(avgConf*100)}%\`;
    return \`<span class="regime-spark-tick" style="background:\${color};opacity:\${op}" title="\${tt}"></span>\`;
  }).join('');

  // Daily-only fields kept on the card. Since 2026-07-12 the intraday
  // detector PURGES the retired daily keys (stress_score/roro_score/features
  // froze at the 2026-06-08 false-CRISIS values once the daily HMM stopped
  // writing the file) — absent keys must render as '—', not as a
  // current-looking 0/green gauge.
  const df = dly ? (dly.features || {}) : {};
  const _hasStress = dly != null && dly.stress_score != null;
  const _hasRoro   = dly != null && dly.roro_score != null;
  const stress    = _hasStress ? Math.min(100, Math.max(0, dly.stress_score)) : 0;
  const stressLbl = _hasStress ? \`\${stress}/100\` : '—';
  const stressClr = !_hasStress ? 'var(--dim)' : stress >= 70 ? 'var(--red)' : stress >= 40 ? 'var(--orange)' : 'var(--green)';
  const roro      = _hasRoro ? Math.min(50, Math.max(-50, dly.roro_score)) : 0;
  const roroPct   = _hasRoro ? (Math.abs(roro)/50*50).toFixed(1) : 0;
  const roroLeft  = roro < 0 ? \`left:\${50-roroPct}%;width:\${roroPct}%\` : \`left:50%;width:\${roroPct}%\`;
  const roroClr   = !_hasRoro ? 'var(--dim)' : roro >= 0 ? 'var(--green)' : 'var(--red)';
  const roroLbl   = !_hasRoro ? '—' : (roro >= 0 ? \`+\${(dly.roro_score||0).toFixed(1)} risk-on\` : \`\${(dly.roro_score||0).toFixed(1)} risk-off\`);

  // Daily-block freshness: flag frozen date/stress/roro so the gauge greys them
  // instead of presenting stale values as current (e.g. 2026-06-08 operator resync).
  const _stale = dly && dly.daily_stale;
  const _asOf  = dly && dly.daily_date ? \` (as-of \${dly.daily_date}\${_stale ? ', stale' : ''})\` : '';
  const stressClrEff = _stale ? 'var(--dim)' : stressClr;
  const roroClrEff   = _stale ? 'var(--dim)' : roroClr;

  const macroHtml = [
    ['VIX 5d Δ',     df.vix_5d_chg   != null ? (df.vix_5d_chg>=0?'+':'')+df.vix_5d_chg.toFixed(2) : '—'],
    ['SPX 20d RV',   df.spx_rv_20d   != null ? df.spx_rv_20d.toFixed(2)+'%'                       : '—'],
    ['HY/IG Spread', df.hy_ig_spread != null ? df.hy_ig_spread.toFixed(4)                         : '—'],
  ].map(([lbl,val])=>\`<div class="regime-feat"><div class="regime-feat-label">\${lbl}</div><div class="regime-feat-val">\${val}</div></div>\`).join('');

  const modelTag = intraday.model && intraday.model.trained_at
    ? \`model retrained \${new Date(intraday.model.trained_at).toISOString().slice(0,10)}\`
    : '';

  return \`
    <div class="regime-panel-header" style="margin-bottom:10px">
      Volatility Regime Structure
      <span style="font-size:9px;color:var(--dim);text-transform:none;letter-spacing:0">
        intraday HMM · 6 features (\${_INTRADAY_FEATURE_TILES.map(t => t[0]).join(', ')}) · last tick \${age}\${modelTag ? ' · ' + modelTag : ''}
      </span>
    </div>

    <!-- HERO: current state + key implications + alert badges -->
    <div class="regime-hero">
      <span class="regime-state-badge \${stateClass}">\${L.state || 'UNKNOWN'}</span>
      <div class="regime-meta-item"><div class="regime-meta-label">Confidence</div><div class="regime-meta-val">\${conf}</div></div>
      <div class="regime-meta-item"><div class="regime-meta-label">Position Scale</div><div class="regime-meta-val">\${posScale}</div></div>
      <div class="regime-meta-item"><div class="regime-meta-label">Duration</div><div class="regime-meta-val">\${duration}</div></div>
      <div style="flex:1"></div>
      \${priorBadge}\${carryFwdBadge}\${staleBadge}
    </div>

    <!-- TWO-COLUMN BODY: what's driving (left) + what's happening (right) -->
    <div class="regime-body-grid">

      <!-- LEFT: model inputs + corroborating macro -->
      <div>
        <div class="regime-section">
          <div class="regime-section-header">HMM Model Inputs <span style="font-size:9px;color:var(--dim);text-transform:none;font-weight:normal">(6 features driving the classification)</span></div>
          <div class="regime-feat-grid">\${featHtml}</div>
        </div>

        <div class="regime-section">
          <div class="regime-section-header">Corroborating Macro <span style="font-size:9px;color:var(--dim);text-transform:none;font-weight:normal">(daily-only, not in the HMM)</span></div>
          <div class="regime-feat-grid">\${macroHtml}</div>
        </div>
      </div>

      <!-- RIGHT: recent path + market stress -->
      <div>
        <div class="regime-section">
          <div class="regime-section-header">24h State Path</div>
          <div style="display:flex;gap:1px;align-items:center;height:18px;padding:2px 0">\${sparkHtml}</div>
          <div style="display:flex;gap:10px;font-size:9px;color:var(--muted);margin-top:6px;flex-wrap:wrap">
            \${['LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'].map(s =>
              \`<span><span style="display:inline-block;width:8px;height:8px;background:\${_STATE_COLOR[s]};border-radius:1px;margin-right:3px;vertical-align:middle"></span>\${_STATE_SHORT[s]}</span>\`
            ).join('')}
          </div>
        </div>

        <div class="regime-section">
          <div class="regime-section-header">Market Stress\${_stale ? \` <span style="font-size:9px;color:var(--dim);text-transform:none;font-weight:normal;letter-spacing:0">\${_asOf}</span>\` : ''}</div>
          <div class="regime-bar-group" style="margin-bottom:12px">
            <div class="regime-bar-label" style="\${_stale ? 'color:var(--dim)' : ''}"><span>Stress <span style="text-transform:none;letter-spacing:0;color:var(--dim);font-weight:normal">(daily percentile of VIX, credit spread, momentum)</span></span><span>\${stressLbl}</span></div>
            <div class="regime-bar-track"><div class="regime-bar-fill" style="width:\${stress}%;background:\${stressClrEff}"></div></div>
          </div>
          <div class="regime-bar-group">
            <div class="regime-bar-label" style="\${_stale ? 'color:var(--dim)' : ''}"><span>RORO</span><span>\${roroLbl}</span></div>
            <div class="regime-roro-track">
              <div class="regime-roro-center"></div>
              <div class="regime-roro-fill" style="\${roroLeft};background:\${roroClrEff}"></div>
            </div>
          </div>
        </div>
      </div>

    </div>\`;
}

function _renderCryptoRegimeStructure(data) {
  if (!data || !data.available) {
    return \`<div class="regime-panel-header" style="margin-bottom:8px">
      Crypto Regime Structure <span style="font-size:9px;color:var(--dim);text-transform:none;letter-spacing:0">24/7 HMM</span>
    </div>
    <div class="empty" style="padding:8px 0;font-size:11px">Crypto regime detector inactive — enable OPENCLAW_CRYPTO_REGIME + run backfill.</div>\`;
  }
  const L = data.latest || {};

  const stateClass = _STATE_CLASS[L.state] || 'regime-state-NO_DATA';
  const conf       = L.confidence != null ? Math.round(L.confidence * 100) + '%' : '—';
  const streak     = L.hysteresis_streak != null ? L.hysteresis_streak + ' ticks' : '—';
  const age        = _ageStr(L.ts_utc);

  // Stale: last tick > 2h old (24/7 detector, hourly cadence).
  const stale = L.ts_utc ? (Date.now() - new Date(L.ts_utc).getTime()) > 2 * 3600 * 1000 : true;
  const staleBadge = stale
    ? \`<span class="regime-alert-badge" title="last tick &gt;2 hours ago — crypto detector may be paused or backfill not yet applied">STALE · \${age}</span>\`
    : '';
  const priorBadge = (L.prior_state && L.prior_state !== L.state)
    ? \`<span class="regime-alert-badge">⚠ \${L.prior_state} → \${L.state}</span>\` : '';

  // Feature tiles — features_json may arrive as a JSON string (JSONB → depends on driver).
  const rawF = L.features_json;
  const f = rawF ? (typeof rawF === 'string' ? JSON.parse(rawF) : rawF) : {};
  const fmt = (v, dp) => v == null || (typeof v === 'number' && isNaN(v)) ? '—' : (typeof v === 'number' ? v.toFixed(dp) : String(v));
  const _CRYPTO_FEATURE_TILES = [
    ['BTC RV 24h',    'btc_rv_24h',          2],
    ['BTC RV 7d',     'btc_rv_168h',         2],
    ['Vol Term Slope','btc_vol_term_slope',   3],
    ['BTC Ret 24h',   'btc_ret_24h',         2],
    ['ETH–BTC Disp',  'eth_btc_dispersion',  2],
  ];
  const featHtml = _CRYPTO_FEATURE_TILES
    .map(([lbl, key, dp]) => \`<div class="regime-feat"><div class="regime-feat-label">\${lbl}</div><div class="regime-feat-val">\${fmt(f[key], dp)}</div></div>\`)
    .join('');

  // 24h state-path sparkline — hourly cadence → 24 buckets of 60 min each.
  const _C_BUCKETS  = 24;
  const _C_BUCKET_MS = 60 * 60 * 1000;
  const _c_now = Date.now();
  const _c_bucketStart = _c_now - _C_BUCKETS * _C_BUCKET_MS;
  const _c_buckets = Array.from({ length: _C_BUCKETS }, (_, i) => ({
    start: _c_bucketStart + i * _C_BUCKET_MS,
    end:   _c_bucketStart + (i + 1) * _C_BUCKET_MS,
    rows: [],
  }));
  for (const r of (data.history || [])) {
    const t = new Date(r.ts_utc).getTime();
    const idx = Math.floor((t - _c_bucketStart) / _C_BUCKET_MS);
    if (idx >= 0 && idx < _C_BUCKETS) _c_buckets[idx].rows.push(r);
  }
  const sparkHtml = _c_buckets.map(b => {
    const useful = b.rows.filter(r => r.state && r.state !== 'UNKNOWN');
    const winLbl = \`\${new Date(b.start).toISOString().slice(11,16)}–\${new Date(b.end).toISOString().slice(11,16)} UTC\`;
    if (useful.length === 0) {
      return \`<span class="regime-spark-tick" style="background:var(--dim);opacity:0.5" title="\${winLbl} · no data"></span>\`;
    }
    const states = useful.map(r => r.state);
    const allSame = states.every(s => s === states[0]);
    const color = allSame ? (_STATE_COLOR[states[0]] || 'var(--dim)') : 'var(--muted)';
    const avgConf = useful.reduce((a, r) => a + (Number(r.confidence) || 0), 0) / useful.length;
    const op = Math.max(0.3, Math.min(1, avgConf));
    const stateLbl = allSame ? states[0] : 'mixed (' + states.join('/') + ')';
    const tt = \`\${winLbl} · \${stateLbl} · avg conf \${Math.round(avgConf * 100)}%\`;
    return \`<span class="regime-spark-tick" style="background:\${color};opacity:\${op}" title="\${tt}"></span>\`;
  }).join('');

  return \`
    <div class="regime-panel-header" style="margin-bottom:10px">
      Crypto Regime Structure
      <span style="font-size:9px;color:var(--dim);text-transform:none;letter-spacing:0">
        24/7 crypto HMM · 5 features (BTC RV/term-slope/return + ETH–BTC disp) · gated (OPENCLAW_CRYPTO_REGIME) · last tick \${age}
      </span>
    </div>

    <!-- HERO: current state + key metrics + alert badges -->
    <div class="regime-hero">
      <span class="regime-state-badge \${stateClass}">\${L.state || 'UNKNOWN'}</span>
      <div class="regime-meta-item"><div class="regime-meta-label">Confidence</div><div class="regime-meta-val">\${conf}</div></div>
      <div class="regime-meta-item"><div class="regime-meta-label">Duration</div><div class="regime-meta-val">\${streak}</div></div>
      <div style="flex:1"></div>
      \${priorBadge}\${staleBadge}
    </div>

    <!-- TWO-COLUMN BODY: model inputs (left) + 24h path (right) -->
    <div class="regime-body-grid">

      <!-- LEFT: model inputs -->
      <div>
        <div class="regime-section">
          <div class="regime-section-header">HMM Model Inputs <span style="font-size:9px;color:var(--dim);text-transform:none;font-weight:normal">(5 features driving the classification)</span></div>
          <div class="regime-feat-grid">\${featHtml}</div>
        </div>
      </div>

      <!-- RIGHT: 24h state path -->
      <div>
        <div class="regime-section">
          <div class="regime-section-header">24h State Path</div>
          <div style="display:flex;gap:1px;align-items:center;height:18px;padding:2px 0">\${sparkHtml}</div>
          <div style="display:flex;gap:10px;font-size:9px;color:var(--muted);margin-top:6px;flex-wrap:wrap">
            \${['LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'].map(s =>
              \`<span><span style="display:inline-block;width:8px;height:8px;background:\${_STATE_COLOR[s]};border-radius:1px;margin-right:3px;vertical-align:middle"></span>\${_STATE_SHORT[s]}</span>\`
            ).join('')}
          </div>
        </div>
      </div>

    </div>\`;
}

async function loadRegime() {
  const el = document.getElementById('regime-panel');
  if (!el) return;
  const [intraday, daily] = await Promise.all([
    _safeFetch('/api/regime/intraday', { available: false }, { critical: true, label: 'regime-intraday' }),
    _safeFetch('/api/regime',          { available: false, state: 'NO_DATA' }, { critical: true, label: 'regime-daily' }),
  ]);
  el.innerHTML = _renderRegimeStructure(intraday, daily);
}

async function loadCryptoRegime() {
  const el = document.getElementById('crypto-regime-panel');
  if (!el) return;
  const data = await _safeFetch('/api/regime/crypto', { available: false }, { label: 'regime-crypto' });
  el.innerHTML = _renderCryptoRegimeStructure(data);
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
      <tr><th>#</th><th>Started (ET)</th><th>Dur</th><th>Prices</th><th>Options</th><th>Fund</th><th>API (FMP)</th><th>Errors</th><th>Status</th></tr>
      \${cycles.map(c => \`<tr class="cycle-row">
        <td>\${c.id}</td>
        <td>\${new Date(c.started_at).toLocaleString('en-US',{timeZone:'America/New_York',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false})}</td>
        <td>\${dur(c.duration_ms)}</td>
        <td class="num">\${parseInt(c.price_rows||0).toLocaleString()}</td>
        <td class="num">\${parseInt(c.options_contracts||0).toLocaleString()}</td>
        <td class="num">\${c.fundamental_records||0}</td>
        <td class="num">\${c.fmp_calls||0}</td>
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
let pnlChart       = null;
let pnlRange       = '90d';   // '90d' | '1y' | 'life'
let pnlCandlesData = null;    // [{date, open, high, low, close, pct_change, intraday}]
let valueCurveData = null;    // /api/portfolio/value-curve?period=1A — feeds 1y view + lifetime AAR
let lifetimeData   = null;    // /api/portfolio/value-curve?period=all
let regime90dData  = null;    // [{date, regime}]
let regime1yData   = null;    // [{date, regime}]

function _setNavActive(which) {
  for (const k of ['market','portfolio','strategies','research','pipelines']) {
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
  const rp = document.getElementById('research-page');
  if (rp) rp.style.display = 'none';
  const pl = document.getElementById('pipeline-page');
  if (pl) pl.style.display = 'none';
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


// ── Research page ─────────────────────────────────────────────────────────────
let _researchState = {
  currentSessionId: null,
  initialized:      false,
  sessions:         [],
};

async function showResearch() {
  _hideAllPages();
  document.getElementById('research-page').style.display = 'block';
  _setNavActive('research');
  if (!_researchState.initialized) await _initResearch();
  else await _refreshResearch();
}

async function _initResearch() {
  _researchState.initialized = true;
  document.getElementById('btn-new-session').onclick = _createSession;
  document.getElementById('btn-sessions-drawer').onclick = _openSessionsDrawer;
  document.getElementById('btn-close-drawer').onclick = _closeSessionsDrawer;
  document.getElementById('sessions-backdrop').onclick = _closeSessionsDrawer;
  document.getElementById('chat-send').onclick = _sendMessage;
  document.getElementById('chat-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _sendMessage(); }
  });
  document.getElementById('papers-status').addEventListener('change', _refreshPapers);
  document.getElementById('pm-close').onclick = _closePaperModal;
  document.getElementById('paper-modal').onclick = (e) => { if (e.target.id === 'paper-modal') _closePaperModal(); };
  document.getElementById('chat-fab').onclick = _openChatOverlay;
  document.getElementById('chat-close').onclick = _closeChatOverlay;
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (document.getElementById('paper-modal').classList.contains('open')) _closePaperModal();
      else if (document.getElementById('sessions-drawer').classList.contains('open')) _closeSessionsDrawer();
      else if (document.getElementById('rs-chat-overlay').classList.contains('open')) _closeChatOverlay();
    }
  });
  let qTimer;
  document.getElementById('papers-q').addEventListener('input', () => {
    clearTimeout(qTimer); qTimer = setTimeout(_refreshPapers, 250);
  });
  await _refreshResearch();
  setInterval(() => {
    if (document.getElementById('research-page').style.display === 'block') {
      _refreshQueue(); _refreshStaging(); _refreshRunsHist(); _refreshCampaigns();
    }
  }, 5000);
}

async function _refreshResearch() {
  await Promise.all([
    _refreshSessions(),
    _refreshCampaigns(),
    _refreshQueue(),
    _refreshPapers(),
    _refreshStaging(),
    _refreshRunsHist(),
  ]);
}

function _rsEsc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function _fetchJSON(url, opts) {
  try { const r = await fetch(url, opts); return await r.json(); }
  catch (e) { return { error: e.message }; }
}

// Minimal markdown renderer — handles the subset assistants actually produce.
function _renderMarkdown(src) {
  if (!src) return '';
  // Extract fenced code blocks first so we don't mangle them.
  const codeBlocks = [];
  let s = String(src).replace(/\`\`\`([a-zA-Z0-9_-]*)\\n([\\s\\S]*?)\`\`\`/g, (_, lang, code) => {
    const idx = codeBlocks.length;
    codeBlocks.push({ lang, code });
    return '\\u0000CB' + idx + '\\u0000';
  });
  s = _rsEsc(s);
  // Headings
  s = s.replace(/^###\\s+(.+)$/gm, '<h3>$1</h3>')
       .replace(/^##\\s+(.+)$/gm,  '<h2>$1</h2>')
       .replace(/^#\\s+(.+)$/gm,   '<h1>$1</h1>');
  // Blockquote (one level)
  s = s.replace(/^&gt;\\s+(.+)$/gm, '<blockquote>$1</blockquote>');
  // Bold / italic / inline code (order matters)
  s = s.replace(/\`([^\`\\n]+)\`/g, '<code>$1</code>');
  s = s.replace(/\\*\\*([^*\\n]+)\\*\\*/g, '<strong>$1</strong>');
  s = s.replace(/(?<!\\*)\\*([^*\\n]+)\\*(?!\\*)/g, '<em>$1</em>');
  // Links  [text](url)
  s = s.replace(/\\[([^\\]]+)\\]\\(([^\\s)]+)\\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  // Lists — collapse runs of  - / * / digit.  lines into <ul>/<ol>.
  const lines = s.split('\\n');
  const out = [];
  let inUl = false, inOl = false;
  for (const ln of lines) {
    const ul = /^\\s*[-*]\\s+(.*)$/.exec(ln);
    const ol = /^\\s*\\d+\\.\\s+(.*)$/.exec(ln);
    if (ul) {
      if (inOl) { out.push('</ol>'); inOl = false; }
      if (!inUl) { out.push('<ul>'); inUl = true; }
      out.push('<li>' + ul[1] + '</li>');
    } else if (ol) {
      if (inUl) { out.push('</ul>'); inUl = false; }
      if (!inOl) { out.push('<ol>'); inOl = true; }
      out.push('<li>' + ol[1] + '</li>');
    } else {
      if (inUl) { out.push('</ul>'); inUl = false; }
      if (inOl) { out.push('</ol>'); inOl = false; }
      out.push(ln);
    }
  }
  if (inUl) out.push('</ul>');
  if (inOl) out.push('</ol>');
  s = out.join('\\n');
  // Paragraphs — split on blank lines, skip block-level children.
  s = s.split(/\\n{2,}/).map(block => {
    const t = block.trim();
    if (!t) return '';
    if (/^<(h[1-3]|ul|ol|blockquote|pre)/i.test(t)) return t;
    return '<p>' + t.replace(/\\n/g, '<br>') + '</p>';
  }).join('');
  // Restore code blocks
  s = s.replace(/\\u0000CB(\\d+)\\u0000/g, (_, i) => {
    const cb = codeBlocks[+i];
    return '<pre><code>' + _rsEsc(cb.code) + '</code></pre>';
  });
  return s;
}

async function _refreshSessions() {
  const { sessions = [] } = await _fetchJSON('/api/research/sessions');
  _researchState.sessions = sessions;
  _renderSessionHeaderStrip();
  _renderSessionsDrawer();
  if (!_researchState.currentSessionId && sessions.length) {
    await _loadSession(sessions[0].id);
  }
}

function _renderSessionHeaderStrip() {
  const sessions = _researchState.sessions || [];
  const cur = sessions.find(s => s.id === _researchState.currentSessionId);
  const nameEl = document.getElementById('chat-session-name');
  const costEl = document.getElementById('chat-session-cost');
  if (cur) {
    const label = cur.title || ('session ' + cur.id.slice(0,8));
    nameEl.textContent = label;
    nameEl.title = cur.id;
    const cost = Number(cur.total_cost_usd || 0);
    const tok = Number(cur.total_tokens || 0);
    costEl.textContent = (tok ? _fmtTokens(tok) + ' · ' : '') + '$' + cost.toFixed(2);
  } else {
    nameEl.textContent = 'MasterMindJohn — Chat';
    costEl.textContent = '';
  }
}

function _renderSessionsDrawer() {
  const host = document.getElementById('sessions-drawer-body');
  const sessions = _researchState.sessions || [];
  if (!sessions.length) { host.innerHTML = '<div style="color:var(--muted);padding:14px;font-size:11px">No sessions yet. Click "+ new" to start.</div>'; return; }
  host.innerHTML = sessions.map(s => {
    const active = s.id === _researchState.currentSessionId;
    const label = _rsEsc(s.title || ('session ' + s.id.slice(0,8)));
    const when = s.last_active_at ? new Date(s.last_active_at).toLocaleString() : '—';
    const cost = '$' + Number(s.total_cost_usd || 0).toFixed(2);
    return \`<div class="session-item \${active?'active':''}" data-sid="\${_rsEsc(s.id)}">
      <div class="si-title">\${label}</div>
      <div class="si-meta"><span>\${_rsEsc(when)}</span><span>\${cost}</span></div>
    </div>\`;
  }).join('');
  for (const row of host.querySelectorAll('.session-item')) {
    row.addEventListener('click', async () => {
      await _loadSession(row.dataset.sid);
      _closeSessionsDrawer();
    });
  }
}

function _openSessionsDrawer() {
  document.getElementById('sessions-drawer').classList.add('open');
  document.getElementById('sessions-backdrop').classList.add('open');
  _renderSessionsDrawer();
}
function _closeSessionsDrawer() {
  document.getElementById('sessions-drawer').classList.remove('open');
  document.getElementById('sessions-backdrop').classList.remove('open');
}

function _fmtTokens(n) {
  if (n < 1000) return n + 't';
  if (n < 10_000) return (n/1000).toFixed(1) + 'kt';
  if (n < 1_000_000) return Math.round(n/1000) + 'kt';
  return (n/1_000_000).toFixed(2) + 'Mt';
}

async function _createSession() {
  const r = await fetch('/api/research/sessions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: new Date().toISOString().slice(0, 16).replace('T', ' ') }),
  });
  const data = await r.json();
  if (!r.ok) { alert('create failed: ' + (data.error || r.statusText)); return; }
  _researchState.currentSessionId = data.id;
  document.getElementById('chat-scroll').innerHTML = '<div style="color:var(--muted);padding:14px;font-size:11px">New session. Ask anything — MasterMindJohn has the dashboard snapshot loaded.</div>';
  await _refreshSessions();
}

async function _loadSession(id) {
  _researchState.currentSessionId = id;
  const { messages = [] } = await _fetchJSON(\`/api/research/sessions/\${encodeURIComponent(id)}/history\`);
  const scroll = document.getElementById('chat-scroll');
  scroll.innerHTML = '';
  for (const m of messages) _renderChatMessage(m.role, m.content);
  scroll.scrollTop = scroll.scrollHeight;
  _renderSessionHeaderStrip();
  _renderSessionsDrawer();
}

function _renderChatMessage(role, content) {
  const scroll = document.getElementById('chat-scroll');
  const el = document.createElement('div');
  if (role === 'user') {
    el.className = 'chat-msg user';
    el.textContent = (content && content.text) || '';
  } else if (role === 'assistant') {
    el.className = 'chat-msg assistant';
    const body = document.createElement('div');
    body.className = 'chat-md';
    body.innerHTML = _renderMarkdown((content && content.text) || '');
    el.appendChild(body);
  } else if (role === 'tool_use') {
    el.className = 'chat-msg tool';
    _fillToolBubble(el, content, /*isResult*/ false);
    el.addEventListener('click', () => el.classList.toggle('expanded'));
  } else if (role === 'tool_result') {
    el.className = 'chat-msg tool result';
    _fillToolBubble(el, content, /*isResult*/ true);
    el.addEventListener('click', () => el.classList.toggle('expanded'));
  } else {
    el.className = 'chat-msg tool';
    el.textContent = role + ': ' + (JSON.stringify(content) || '').slice(0, 240);
  }
  scroll.appendChild(el);
  scroll.scrollTop = scroll.scrollHeight;
  return el;
}

function _fillToolBubble(el, content, isResult) {
  if (isResult) {
    const raw = (content && content.result);
    const txt = typeof raw === 'string' ? raw
      : (raw != null ? JSON.stringify(raw, null, 2) : JSON.stringify(content || {}, null, 2));
    const preview = (typeof raw === 'string' ? raw : JSON.stringify(raw || content || {})).slice(0, 120).replace(/\\s+/g, ' ');
    el.innerHTML = \`<div class="chat-tool-summary"><span class="chat-tool-badge">↳ result</span><span>\${_rsEsc(preview)}</span></div>
      <div class="chat-tool-body">\${_rsEsc(txt).slice(0, 10_000)}</div>\`;
    return;
  }
  const name = (content && content.name) || 'tool';
  const input = (content && content.input) || {};
  let summary = '';
  if (name === 'Task' && input.subagent_type) {
    const p = (input.prompt || '').slice(0, 80).replace(/\\s+/g, ' ');
    summary = \`<span class="chat-tool-badge">⚙ Task → \${_rsEsc(input.subagent_type)}</span><span>\${_rsEsc(p)}</span>\`;
  } else if (name === 'Bash' && input.command) {
    const cmd = input.command.slice(0, 120).replace(/\\s+/g, ' ');
    summary = \`<span class="chat-tool-badge">⚙ Bash</span><span>\${_rsEsc(cmd)}</span>\`;
  } else if ((name === 'Read' || name === 'Edit' || name === 'Write') && input.file_path) {
    summary = \`<span class="chat-tool-badge">⚙ \${_rsEsc(name)}</span><span>\${_rsEsc(input.file_path)}</span>\`;
  } else {
    const keys = Object.keys(input || {}).length;
    summary = \`<span class="chat-tool-badge">⚙ \${_rsEsc(name)}</span><span>\${keys} key\${keys===1?'':'s'}</span>\`;
  }
  el.innerHTML = \`<div class="chat-tool-summary">\${summary}</div>
    <div class="chat-tool-body">\${_rsEsc(JSON.stringify(input, null, 2)).slice(0, 10_000)}</div>\`;
}

function _openChatOverlay() {
  const ov = document.getElementById('rs-chat-overlay');
  ov.classList.add('open');
  document.body.classList.add('rs-chat-locked');
  document.getElementById('chat-fab').classList.add('hidden');
  setTimeout(() => { try { document.getElementById('chat-input').focus(); } catch (_) {} }, 50);
  const scroll = document.getElementById('chat-scroll');
  if (scroll) scroll.scrollTop = scroll.scrollHeight;
}

function _closeChatOverlay() {
  document.getElementById('rs-chat-overlay').classList.remove('open');
  document.body.classList.remove('rs-chat-locked');
  document.getElementById('chat-fab').classList.remove('hidden');
}

// Single-line description of what a tool_use is currently doing — drives the
// in-place progress text under the shimmer dots. Mirrors how Claude/ChatGPT
// surface the active step ("Reading X…", "Running Y…") without papering the
// thread with one bubble per tool call.
function _progressLabelFor(toolUse) {
  const name = (toolUse && toolUse.name) || 'tool';
  const input = (toolUse && toolUse.input) || {};
  if (name === 'Task' && input.subagent_type) return \`Spawning \${input.subagent_type}…\`;
  if (name === 'Bash' && input.command) {
    const c = input.command.replace(/\\s+/g, ' ').slice(0, 80);
    return \`Running: \${c}\`;
  }
  if (name === 'Read'  && input.file_path) return \`Reading \${input.file_path.split('/').pop()}\`;
  if (name === 'Edit'  && input.file_path) return \`Editing \${input.file_path.split('/').pop()}\`;
  if (name === 'Write' && input.file_path) return \`Writing \${input.file_path.split('/').pop()}\`;
  if ((name === 'Grep' || name === 'Glob') && (input.pattern || input.path)) {
    return \`\${name}: \${input.pattern || input.path}\`;
  }
  if (name === 'WebFetch' && input.url)    return \`Fetching \${input.url.replace(/^https?:\\/\\//,'').slice(0,60)}…\`;
  if (name === 'WebSearch' && input.query) return \`Searching: \${String(input.query).slice(0,60)}\`;
  return \`Calling \${name}…\`;
}

function _ensureProgressRow() {
  let row = document.getElementById('chat-progress-row');
  if (row) return row;
  row = document.createElement('div');
  row.className = 'chat-progress';
  row.id = 'chat-progress-row';
  row.innerHTML = '<span class="chat-progress-dots"><span></span><span></span><span></span></span><span class="chat-progress-text">Thinking…</span>';
  document.getElementById('chat-scroll').appendChild(row);
  return row;
}

function _setProgressLabel(label) {
  const row = _ensureProgressRow();
  const t = row.querySelector('.chat-progress-text');
  if (t) t.textContent = label;
  const scroll = document.getElementById('chat-scroll');
  if (scroll) scroll.scrollTop = scroll.scrollHeight;
}

function _removeProgressRow() {
  const row = document.getElementById('chat-progress-row');
  if (row) row.remove();
}

// After the turn ends, fold the silent tool log into a compact pill on the
// final assistant bubble. Click the pill to expand the per-call list.
function _attachToolLogFooter(assistantEl, toolUses) {
  if (!assistantEl || !toolUses || !toolUses.length) return;
  const wrap = document.createElement('div');
  wrap.className = 'chat-tool-log';
  const header = document.createElement('span');
  header.className = 'chat-tool-log-header';
  header.textContent = \`🔧 \${toolUses.length} tool\${toolUses.length===1?'':'s'} used · expand\`;
  const list = document.createElement('div');
  list.className = 'chat-tool-log-list';
  for (const t of toolUses) {
    const r = document.createElement('div');
    r.className = 'chat-tool-log-row';
    r.textContent = _progressLabelFor(t).replace(/…$/, '');
    r.addEventListener('click', (e) => {
      e.stopPropagation();
      r.classList.toggle('expanded');
      r.textContent = r.classList.contains('expanded')
        ? JSON.stringify(t.input || {}, null, 2).slice(0, 4_000)
        : _progressLabelFor(t).replace(/…$/, '');
    });
    list.appendChild(r);
  }
  header.addEventListener('click', () => wrap.classList.toggle('expanded'));
  wrap.appendChild(header);
  wrap.appendChild(list);
  assistantEl.appendChild(wrap);
}

async function _sendMessage() {
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;
  if (!_researchState.currentSessionId) await _createSession();
  const sid = _researchState.currentSessionId;
  input.value = '';
  document.getElementById('chat-send').disabled = true;
  _renderChatMessage('user', { text });
  _setProgressLabel('Thinking…');

  let assistantBuf = '';
  let assistantEl = null;
  const toolLog = []; // captured tool_uses for the post-turn footer
  try {
    const resp = await fetch(\`/api/research/sessions/\${encodeURIComponent(sid)}/message\`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    });
    if (!resp.ok || !resp.body) throw new Error('stream start failed: ' + resp.status);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const frames = buf.split(/\\n\\n/);
      buf = frames.pop();
      for (const f of frames) _handleSSE(f, (ev) => {
        if (ev.type === 'assistant' && ev.message && ev.message.content) {
          const texts = ev.message.content.filter(c => c.type === 'text').map(c => c.text).join('');
          const tools = ev.message.content.filter(c => c.type === 'tool_use');
          if (texts) {
            assistantBuf += texts;
            if (!assistantEl) {
              // Promote: progress row stays after the bubble for the next tool.
              const row = document.getElementById('chat-progress-row');
              assistantEl = _renderChatMessage('assistant', { text: '' });
              if (row) document.getElementById('chat-scroll').appendChild(row);
            }
            const md = assistantEl.querySelector('.chat-md');
            if (md) md.innerHTML = _renderMarkdown(assistantBuf);
            else assistantEl.textContent = assistantBuf;
          }
          for (const t of tools) {
            toolLog.push(t);
            _setProgressLabel(_progressLabelFor(t));
            // Tool break ends the current text stream — next text starts a new bubble.
            assistantEl = null;
          }
        } else if (ev.type === 'user' && ev.message && ev.message.content) {
          // tool_result events are silent now; the progress line moves to the
          // NEXT tool_use or to the final assistant text. We still bump the
          // label briefly so the user sees forward motion on long tools.
          const hasResult = ev.message.content.some(c => c.type === 'tool_result');
          if (hasResult) _setProgressLabel('Processing…');
          assistantEl = null;
        } else if (ev.type === 'result' && ev.total_cost_usd != null) {
          const m = document.createElement('div');
          m.className = 'chat-meta';
          m.textContent = \`turn cost $\${Number(ev.total_cost_usd).toFixed(3)} · \${ev.duration_ms}ms\`;
          document.getElementById('chat-scroll').appendChild(m);
        } else if (ev._sseEvent === 'error') {
          _removeProgressRow();
          const err = document.createElement('div');
          err.className = 'chat-msg err';
          err.textContent = 'error: ' + (ev.message || JSON.stringify(ev));
          document.getElementById('chat-scroll').appendChild(err);
        }
      });
    }
    // Find the last assistant bubble in the scroll for the tool-log footer
    // (assistantEl may have been nulled mid-stream).
    const scroll = document.getElementById('chat-scroll');
    const allAsst = scroll.querySelectorAll('.chat-msg.assistant');
    const lastAsst = allAsst[allAsst.length - 1] || null;
    _attachToolLogFooter(lastAsst, toolLog);
  } catch (e) {
    const err = document.createElement('div');
    err.className = 'chat-msg err';
    err.textContent = 'stream error: ' + e.message;
    document.getElementById('chat-scroll').appendChild(err);
  } finally {
    _removeProgressRow();
    document.getElementById('chat-send').disabled = false;
    _refreshSessions();
  }
}

function _handleSSE(frame, onEvent) {
  const lines = frame.split(/\\n/);
  let eventName = 'message';
  const dataLines = [];
  for (const l of lines) {
    if (l.startsWith('event:')) eventName = l.slice(6).trim();
    else if (l.startsWith('data:')) dataLines.push(l.slice(5).trim());
  }
  if (!dataLines.length) return;
  const raw = dataLines.join('\\n');
  try {
    const obj = JSON.parse(raw);
    obj._sseEvent = eventName;
    if (!obj.type) obj.type = eventName;
    onEvent(obj);
  } catch (_) { onEvent({ _sseEvent: eventName, raw }); }
}

async function _refreshCampaigns() {
  const { campaigns = [] } = await _fetchJSON('/api/research/campaigns');
  const active = campaigns.filter(c => ['awaiting_ack','running','planning'].includes(c.status));
  document.getElementById('camp-count').textContent = active.length ? \`\${active.length} active · \${campaigns.length} total\` : \`\${campaigns.length}\`;
  if (!campaigns.length) { document.getElementById('camp-body').innerHTML = '<div style="color:var(--muted);font-size:11px">— no campaigns yet —</div>'; return; }
  document.getElementById('camp-body').innerHTML = campaigns.slice(0, 20).map(c => {
    const plan = c.plan_json || {};
    const items = Array.isArray(plan.items) ? plan.items : [];
    const progress = c.progress_json || {};
    const drafted = progress.drafted || c.candidates_inserted || 0;
    const total = items.length || drafted;
    const pct = total > 0 ? Math.min(100, Math.round(drafted * 100 / total)) : 0;
    const cost = progress.cost_usd != null ? '$' + Number(progress.cost_usd).toFixed(2) : '—';
    const est = plan.total_est_cost_usd != null ? '$' + Number(plan.total_est_cost_usd).toFixed(2) + ' est' : '';
    return \`<div class="camp-row" data-cid="\${_rsEsc(c.id)}">
      <div class="camp-head">
        <span class="camp-name">\${_rsEsc(c.name)}</span>
        <span class="rs-pill camp-pill \${_rsEsc(c.status)}">\${_rsEsc(c.status.replace('_',' '))}</span>
      </div>
      <div class="camp-meta">
        <span>\${drafted}/\${total || '?'} drafted</span>
        \${progress.deduped ? '<span style="color:var(--purple)">' + progress.deduped + ' deduped</span>' : ''}
        <span>cost \${cost}</span>
        \${est ? '<span>' + est + '</span>' : ''}
        <span>\${new Date(c.created_at).toLocaleString()}</span>
      </div>
      <div class="camp-progress-bar"><div class="camp-progress-fill" style="width:\${pct}%"></div></div>
    </div>\`;
  }).join('');
  for (const row of document.getElementById('camp-body').querySelectorAll('.camp-row')) {
    row.addEventListener('click', () => _toggleCampaignDetail(row));
  }
}

function _renderCampaignDag(plan, candidates) {
  const items = Array.isArray(plan && plan.items) ? plan.items : [];
  const tierItems = (plan && Array.isArray(plan.tiers) && plan.selected_tier)
    ? (plan.tiers.find(t => t.name === plan.selected_tier) || {}).items : null;
  const slugList = (tierItems && tierItems.length ? tierItems : items).map(it => {
    if (typeof it === 'string') return { slug: it, name: it };
    return { slug: it.slug || it.name, name: it.name || it.slug, thesis: it.thesis };
  }).filter(x => x.slug);
  if (!slugList.length && (!candidates || !candidates.length)) return '';
  const candBySlug = new Map();
  for (const c of (candidates || [])) {
    const key = (c.slug || c.staging_name || c.strategy_id || '').toLowerCase();
    if (key) candBySlug.set(key, c);
  }
  // Rows: planned items first, then any straggler candidate slugs not in plan.
  const rows = [];
  const seen = new Set();
  for (const it of slugList) {
    const key = (it.slug || '').toLowerCase();
    seen.add(key);
    rows.push({ slug: it.slug, cand: candBySlug.get(key) || null });
  }
  for (const [k, c] of candBySlug.entries()) {
    if (!seen.has(k)) rows.push({ slug: c.slug || c.staging_name || k, cand: c });
  }
  if (!rows.length) return '';
  const cells = rows.map(r => {
    const c = r.cand;
    const drafted = !!c;
    const qbt = c && c.quick_backtest_json;
    const qbtStatus = qbt ? qbt.status : null;
    let btDot = 'off', btLabel = '—';
    if (qbtStatus === 'ok') {
      const s = Number(qbt.sharpe || 0);
      btDot = s >= 0.5 ? 'on' : s <= 0 ? 'neg' : 'partial';
      btLabel = \`sh \${s>=0?'+':''}\${s.toFixed(2)}\`;
    } else if (qbtStatus === 'deferred') { btDot = 'partial'; btLabel = 'deferred'; }
    else if (qbtStatus === 'error')      { btDot = 'neg';     btLabel = 'error'; }
    else if (drafted)                     { btLabel = 'pending'; }
    const approved = c && (c.staging_status === 'approved' || c.staging_status === 'promoted');
    const live     = c && (c.registry_status === 'approved' || c.staging_status === 'promoted');
    const pnlDot   = live ? 'blue' : 'off';
    return \`<tr>
      <td class="dag-slug">\${_rsEsc(r.slug)}</td>
      <td><span class="dag-cell"><span class="dag-dot \${drafted?'on':'off'}"></span><span class="dag-cell-label">\${drafted?'drafted':'planned'}</span></span></td>
      <td><span class="dag-cell"><span class="dag-dot \${btDot}"></span><span class="dag-cell-label">\${_rsEsc(btLabel)}</span></span></td>
      <td><span class="dag-cell"><span class="dag-dot \${approved?'on':'off'}"></span><span class="dag-cell-label">\${approved ? _rsEsc(c.staging_status) : (c ? _rsEsc(c.staging_status || 'pending') : '—')}</span></span></td>
      <td><span class="dag-cell"><span class="dag-dot \${pnlDot}"></span><span class="dag-cell-label">\${live ? 'live' : '—'}</span></span></td>
    </tr>\`;
  }).join('');
  return \`<div class="camp-dag"><table>
    <thead><tr><th>strategy</th><th>drafted</th><th>backtest</th><th>staging</th><th>live</th></tr></thead>
    <tbody>\${cells}</tbody>
  </table></div>\`;
}

async function _toggleCampaignDetail(row) {
  const existing = row.querySelector('.camp-detail');
  if (existing) { existing.remove(); return; }
  const cid = row.dataset.cid;
  const { campaign, candidates = [], error } = await _fetchJSON(\`/api/research/campaigns/\${encodeURIComponent(cid)}\`);
  const detail = document.createElement('div');
  detail.className = 'camp-detail';
  if (error || !campaign) { detail.textContent = 'error: ' + (error || 'not found'); row.appendChild(detail); return; }
  const plan = campaign.plan_json || {};
  const items = Array.isArray(plan.items) ? plan.items : [];
  const itemLines = items.slice(0, 30).map(it => {
    const match = candidates.find(c => (c.strategy_id && c.strategy_id === it.slug) || (c.source_url && c.source_url.endsWith(it.slug)));
    const status = match ? match.staging_status || match.status : 'pending';
    return \`<div class="detail-item">• \${_rsEsc(it.slug || it.name || '')} — <span style="color:var(--text)">\${_rsEsc(status)}</span></div>\`;
  }).join('');
  const canCancel = !campaign.cancel_requested && ['awaiting_ack','running','planning'].includes(campaign.status);
  const progress = campaign.progress_json || {};
  const dedupNotes = Array.isArray(progress.dedup_notes) ? progress.dedup_notes : [];
  const dedupHtml = dedupNotes.length
    ? '<div class="detail-item" style="color:var(--purple);margin-top:6px"><strong>Deduped:</strong> ' +
      dedupNotes.map(n => _rsEsc(typeof n === 'string' ? n : (n.slug + ' → ' + (n.match_id || n.matched_id || '?')))).join(', ') +
      '</div>' : '';
  const tiers = Array.isArray(plan.tiers) ? plan.tiers : [];
  const selTier = plan.selected_tier || null;
  const tiersHtml = tiers.length
    ? '<div class="detail-item" style="margin-top:6px"><strong style="color:var(--text)">Tiers:</strong> ' +
      tiers.map(t => {
        const sel = selTier && t.name === selTier;
        const color = sel ? 'var(--blue)' : 'var(--muted)';
        const weight = sel ? '700' : '400';
        const prefix = sel ? '▸ ' : '';
        return \`<span style="color:\${color};font-weight:\${weight};margin-right:8px">\${prefix}\${_rsEsc(t.name)} $\${Number(t.est_cost_usd||0).toFixed(0)} · \${(t.items||[]).length}×</span>\`;
      }).join('') + '</div>'
    : '';
  const dagHtml = _renderCampaignDag(plan, candidates);
  detail.innerHTML = \`<div class="detail-item" style="color:var(--muted);margin-bottom:4px">\${_rsEsc(plan.summary || campaign.request_text || '').slice(0,240)}</div>
    \${tiersHtml}
    \${dagHtml || itemLines}
    \${dedupHtml}
    \${canCancel ? '<button class="camp-cancel-btn" data-cid="' + _rsEsc(cid) + '">Cancel campaign</button>' : ''}\`;
  row.appendChild(detail);
  const cb = detail.querySelector('.camp-cancel-btn');
  if (cb) cb.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (!confirm('Cancel this campaign? MasterMindJohn will halt at next item boundary.')) return;
    cb.disabled = true;
    const r = await fetch(\`/api/research/campaigns/\${encodeURIComponent(cid)}/cancel\`, { method: 'POST' });
    if (!r.ok) alert('cancel failed');
    await _refreshCampaigns();
  });
}

async function _refreshQueue() {
  const { queued = [], recent_runs = [] } = await _fetchJSON('/api/research/queue');
  document.getElementById('queue-count').textContent = \`\${queued.length} queued · \${recent_runs.length} recent\`;
  const rows = queued.map(q => {
    const title = _rsEsc((q.title || q.source_url || '').slice(0, 90));
    const status = _rsEsc(q.status);
    const cls = q.status === 'pending' ? 'warn' : (q.status && q.status.startsWith('blocked')) ? 'muted' : 'ok';
    const kindPill = q.kind === 'internal'
      ? '<span class="rs-pill" style="background:rgba(188,140,255,0.15);color:var(--purple)">internal</span>'
      : '';
    return \`<div class="rs-row">
      <div class="rs-title">\${title}</div>
      <div class="rs-meta">\${kindPill}<span class="rs-pill \${cls}">\${status}</span>p=\${_rsEsc(q.priority)} · \${_rsEsc(q.submitted_by)} · \${q.submitted_at ? new Date(q.submitted_at).toLocaleString() : ''}</div>
    </div>\`;
  }).join('');
  document.getElementById('queue-body').innerHTML = rows || '<div style="color:var(--muted);font-size:11px">— no pending research —</div>';
}

async function _refreshPapers() {
  const status = document.getElementById('papers-status').value;
  const q = document.getElementById('papers-q').value;
  const url = \`/api/research/papers?\${new URLSearchParams({ status, q })}\`;
  const { papers = [] } = await _fetchJSON(url);
  document.getElementById('papers-count').textContent = \`\${papers.length}\`;
  if (!papers.length) { document.getElementById('papers-body').innerHTML = '<div style="color:var(--muted);font-size:11px">— no papers —</div>'; return; }
  document.getElementById('papers-body').innerHTML = papers.map(p => {
    const title = _rsEsc((p.title || p.source_url || '').slice(0, 110));
    const status = _rsEsc(p.status);
    const cls = p.status === 'done' ? 'ok' : (p.status && p.status.startsWith('blocked')) ? 'muted' : 'warn';
    const conf = p.confidence != null ? Number(p.confidence).toFixed(2) : '—';
    return \`<div class="rs-row" data-cid="\${_rsEsc(p.candidate_id)}" style="cursor:pointer">
      <div class="rs-title">\${title}</div>
      <div class="rs-meta"><span class="rs-pill \${cls}">\${status}</span>conf=\${conf} · \${_rsEsc(p.venue || '—')} · \${p.submitted_at ? new Date(p.submitted_at).toLocaleDateString() : ''}</div>
    </div>\`;
  }).join('');
  for (const row of document.getElementById('papers-body').querySelectorAll('.rs-row[data-cid]')) {
    row.addEventListener('click', () => _openPaperModal(row.dataset.cid));
  }
}

async function _openPaperModal(candidateId) {
  const modal = document.getElementById('paper-modal');
  const bodyEl = document.getElementById('pm-body');
  const titleEl = document.getElementById('pm-title');
  titleEl.textContent = 'Loading…';
  bodyEl.innerHTML = '<div style="color:var(--muted);font-size:11px">Loading…</div>';
  modal.classList.add('open');
  const { paper, gate_decisions = [], error } = await _fetchJSON(\`/api/research/papers/\${encodeURIComponent(candidateId)}\`);
  if (error || !paper) {
    titleEl.textContent = 'Error';
    bodyEl.innerHTML = '<div style="color:var(--red);font-size:11px">' + _rsEsc(error || 'not found') + '</div>';
    return;
  }
  titleEl.textContent = paper.title || paper.source_url || 'paper';
  const authors = Array.isArray(paper.authors) ? paper.authors.join(', ') : (paper.authors || '—');
  const gatesHtml = gate_decisions.length ? gate_decisions.map(g => \`
    <div class="pm-gate-row">
      <span>\${_rsEsc(g.gate_name)}</span>
      <span class="pm-gate-outcome \${_rsEsc(g.outcome)}">\${_rsEsc(g.outcome)}</span>
      <span>\${_rsEsc(g.reason_code || '')}\${g.reason_detail ? ' — ' + _rsEsc(g.reason_detail) : ''}</span>
    </div>\`).join('') : '<div style="color:var(--muted);font-size:10px">no gate decisions yet</div>';
  const huntJson = paper.hunter_result_json ? JSON.stringify(paper.hunter_result_json, null, 2) : null;
  bodyEl.innerHTML = \`
    <div class="pm-section">
      <h4>Meta</h4>
      <div class="pm-meta">
        <span><strong>status</strong> \${_rsEsc(paper.status || '—')}</span>
        <span><strong>kind</strong> \${_rsEsc(paper.kind || '—')}</span>
        <span><strong>venue</strong> \${_rsEsc(paper.venue || '—')}</span>
        <span><strong>published</strong> \${paper.published_date ? new Date(paper.published_date).toLocaleDateString() : '—'}</span>
        <span><strong>submitted_by</strong> \${_rsEsc(paper.submitted_by || '—')}</span>
        <span><strong>authors</strong> \${_rsEsc(authors)}</span>
      </div>
    </div>
    <div class="pm-section">
      <h4>Source</h4>
      <a href="\${_rsEsc(paper.source_url)}" target="_blank" rel="noopener" style="color:var(--blue);font-size:11px">\${_rsEsc(paper.source_url)}</a>
    </div>
    \${paper.abstract ? '<div class="pm-section"><h4>Abstract</h4><div class="pm-abstract">' + _rsEsc(paper.abstract) + '</div></div>' : ''}
    <div class="pm-section">
      <h4>Gate decisions</h4>
      <div class="pm-gates">\${gatesHtml}</div>
    </div>
    \${huntJson ? '<div class="pm-section"><h4>Hunter / spec JSON</h4><div class="pm-json">' + _rsEsc(huntJson) + '</div></div>' : ''}
  \`;
}

function _closePaperModal() {
  document.getElementById('paper-modal').classList.remove('open');
}

function _renderQbtBadges(it) {
  const r = it.quick_backtest_json;
  const started = it.quick_backtest_started_at;
  if (!r) {
    if (started) return '<span class="qbt-badge pending">⟳ backtest running…</span>';
    return '<span class="qbt-badge">no backtest</span>';
  }
  if (r.status === 'deferred') {
    return \`<span class="qbt-badge deferred" title="\${_rsEsc(r.message || '')}">deferred · needs strategycoder</span>\`;
  }
  if (r.status === 'error') {
    return \`<span class="qbt-badge neg" title="\${_rsEsc(r.reason || '')}">bt error</span>\`;
  }
  const s = Number(r.sharpe || 0);
  const dd = Number(r.max_dd || 0) * 100;
  const ret = Number(r.total_return_pct || 0);
  const cls = s >= 0.5 ? 'ok' : s <= 0 ? 'neg' : '';
  return \`<span class="qbt-metrics">
    <span class="qbt-badge \${cls}">sharpe \${s>=0?'+':''}\${s.toFixed(2)}</span>
    <span class="qbt-badge">dd \${dd.toFixed(0)}%</span>
    <span class="qbt-badge">ret \${ret>=0?'+':''}\${ret.toFixed(0)}%</span>
  </span>\`;
}

function _renderQbtDetail(it) {
  const r = it.quick_backtest_json;
  if (!r) return '';
  return \`<div style="margin-top:4px"><strong style="color:var(--text)">quick backtest</strong><pre>\${_rsEsc(JSON.stringify(r, null, 2))}</pre></div>\`;
}

async function _refreshStaging() {
  const { items = [] } = await _fetchJSON('/api/research/staging');
  const pending = items.filter(i => i.status === 'pending');
  document.getElementById('staging-count').textContent = \`\${pending.length} pending / \${items.length} total\`;
  const _stagingNote = '<div style="color:var(--muted);font-size:11px;margin-bottom:6px">Review-only — ✓/✗ records a decision; it does NOT promote to live (promotion is a separate manual step).</div>';
  if (!items.length) { document.getElementById('staging-body').innerHTML = _stagingNote + '<div style="color:var(--muted);font-size:11px">— nothing staged —</div>'; return; }
  document.getElementById('staging-body').innerHTML = _stagingNote + items.map(it => {
    const actions = it.status === 'pending'
      ? \`<div class="stage-actions">
          <button class="stage-btn ok" data-id="\${_rsEsc(it.id)}" data-action="approved">✓</button>
          <button class="stage-btn err" data-id="\${_rsEsc(it.id)}" data-action="rejected">✗</button>
        </div>\`
      : \`<span class="rs-pill muted">\${_rsEsc(it.status)}</span>\`;
    const params = it.parameters ? JSON.stringify(it.parameters, null, 2) : '{}';
    const regime = it.regime_conditions ? JSON.stringify(it.regime_conditions, null, 2) : null;
    const uni = Array.isArray(it.universe) ? it.universe.join(', ') : (it.universe || '');
    const qbtBadges = _renderQbtBadges(it);
    return \`<div class="stage-row" data-sid="\${_rsEsc(it.id)}">
      <div>
        <div class="rs-title">\${_rsEsc(it.name)}</div>
        <div class="rs-meta">\${qbtBadges}\${_rsEsc((it.thesis || '').slice(0, 160))}</div>
        <div class="rs-meta">by \${_rsEsc(it.proposed_by)} · \${new Date(it.created_at).toLocaleString()}\${uni ? ' · ' + _rsEsc(uni) : ''}\${it.signal_frequency ? ' · ' + _rsEsc(it.signal_frequency) : ''}</div>
      </div>
      \${actions}
      <div class="stage-expanded-body" style="display:none">
        \${it.thesis ? '<div><strong style="color:var(--text)">thesis</strong> ' + _rsEsc(it.thesis) + '</div>' : ''}
        <div style="margin-top:4px"><strong style="color:var(--text)">parameters</strong><pre>\${_rsEsc(params)}</pre></div>
        \${regime ? '<div style="margin-top:4px"><strong style="color:var(--text)">regime_conditions</strong><pre>' + _rsEsc(regime) + '</pre></div>' : ''}
        \${_renderQbtDetail(it)}
        <button class="stage-ask-btn" data-name="\${_rsEsc(it.name)}" data-id="\${_rsEsc(it.id)}">Ask MasterMindJohn about this →</button>
      </div>
    </div>\`;
  }).join('');
  for (const row of document.getElementById('staging-body').querySelectorAll('.stage-row')) {
    row.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      row.classList.toggle('expanded');
      const body = row.querySelector('.stage-expanded-body');
      if (body) body.style.display = row.classList.contains('expanded') ? 'block' : 'none';
    });
  }
  for (const btn of document.getElementById('staging-body').querySelectorAll('.stage-ask-btn')) {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const input = document.getElementById('chat-input');
      input.value = \`Review staged strategy: \${btn.dataset.name} (id: \${btn.dataset.id})\`;
      input.focus();
    });
  }
  for (const btn of document.getElementById('staging-body').querySelectorAll('button[data-id][data-action]')) {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      if (!confirm(\`Mark this proposal \${action.toUpperCase()} (review only — does NOT promote to live)?\`)) return;
      btn.disabled = true;
      const r = await fetch(\`/api/research/staging/\${encodeURIComponent(btn.dataset.id)}/decision\`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, by: 'operator' }),
      });
      const data = await r.json();
      if (!r.ok) { alert('error: ' + (data.error || r.statusText)); btn.disabled = false; return; }
      await _refreshStaging();
    });
  }
}

async function _refreshRunsHist() {
  const { runs = [] } = await _fetchJSON('/api/research/runs');
  document.getElementById('runs-hist-count').textContent = \`\${runs.length}\`;
  if (!runs.length) { document.getElementById('runs-hist-body').innerHTML = '<div style="color:var(--muted);font-size:10px">— no runs yet —</div>'; return; }
  document.getElementById('runs-hist-body').innerHTML = runs.map(r => {
    const stats = r.input_stats || {};
    const cls = (r.status === 'ok' || r.status === 'completed' || r.status === 'applied') ? 'ok'
              : (r.status === 'pending' || r.status === 'running' || r.status === 'partial') ? 'warn'
              : (r.status === 'failed' || r.status === 'ignored' || r.status === 'superseded') ? 'err'
              : 'muted';
    const extras = [];
    if (stats.strategy_id)      extras.push(_rsEsc(String(stats.strategy_id).slice(0, 20)));
    if (stats.papers_imported != null) extras.push(stats.papers_imported + 'p imp');
    if (stats.size_delta_pct != null) extras.push((stats.size_delta_pct >= 0 ? '+' : '') + Number(stats.size_delta_pct).toFixed(2) + '%Δ');
    if (stats.input_count != null)  extras.push(stats.input_count + 'in');
    if (stats.output_count != null) extras.push(stats.output_count + 'out');
    return \`<span style="color:var(--muted);font-size:10px;margin-right:14px;font-family:'SF Mono',monospace">
      \${_rsEsc(r.mode)} \${_rsEsc(String(r.run_date).slice(0,10))} <span class="rs-pill \${cls}">\${_rsEsc(r.status)}</span>\${r.cost_usd ? '$' + Number(r.cost_usd).toFixed(2) : ''}\${extras.length ? ' · ' + extras.join(' · ') : ''}
    </span>\`;
  }).join('');
}

// _safeFetch: wraps fetch+JSON parse with toast-on-failure for critical
// endpoints. Returns the fallback value on any error (network, non-2xx,
// JSON parse). Pass critical:true to also surface a red toast — operator
// otherwise reads an empty placeholder as "no data" instead of "fetch broke".
async function _safeFetch(url, fallback, opts = {}) {
  const { critical = false, label = url } = opts;
  try {
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + r.statusText);
    return await r.json();
  } catch (e) {
    console.warn('[fetch failed]', label, e.message);
    if (critical) {
      try { toast('Failed: ' + label + ' — ' + e.message, 'error', 6000); } catch (_) {}
    }
    return fallback;
  }
}

// Account equity for tile dollar-P&L computation. Cached at module
// scope so re-renders triggered by sort/expand don't need it re-passed.
let _navCache = null;
// Lambda — fetched on portfolio load + updated on slider drag. Sizer
// reads this server-side from pipeline_config; the dashboard value is
// purely cosmetic + the source of the PUT.
let _lambdaCache = 2.0;
// _grossLevCache removed: gross leverage is now passed explicitly to renderRiskPanel.

async function loadPortfolio() {
  const [summary, positions, history, candles, account, valCurve, lifeCurve, reg90, reg1y] = await Promise.all([
    _safeFetch('/api/portfolio/summary',           {}, { label: 'portfolio summary' }),
    _safeFetch('/api/portfolio/positions',         [], { critical: true, label: 'portfolio positions' }),
    _safeFetch('/api/portfolio/history',           [], { label: 'portfolio history' }),
    _safeFetch('/api/portfolio/pnl-candles?days=90', [], { label: 'pnl candles' }),
    _safeFetch('/api/portfolio/account',           {}, { critical: true, label: 'portfolio account' }),
    _safeFetch('/api/portfolio/value-curve?period=1A', {}, { label: 'value curve 1y' }),
    _safeFetch('/api/portfolio/value-curve?period=all', {}, { label: 'value curve all' }),
    _safeFetch('/api/portfolio/regime-history?days=90', [], { label: 'regime history 90d' }),
    _safeFetch('/api/portfolio/regime-history?days=365', [], { label: 'regime history 1y' }),
  ]);
  if (account && account.equity != null && isFinite(parseFloat(account.equity))) {
    _navCache = parseFloat(account.equity);
  }
  // Pull risk-sizing config — bundles global λ with per-regime liquidity_param
  // so the Risk & Sizing panel renders the full operator surface in one call.
  let riskCfg = null;
  try {
    riskCfg = await fetch('/api/config/regime-sizing').then(r => r.json());
    if (riskCfg && riskCfg.global && isFinite(parseFloat(riskCfg.global.lambda))) {
      _lambdaCache = parseFloat(riskCfg.global.lambda);
    }
  } catch (_) { /* keep cached value */ }
  renderAccountRow(account);
  valueCurveData = valCurve;
  renderPortfolioSummary(summary, valCurve);
  renderRiskPanel(riskCfg, account ? (account.gross_leverage ?? null) : null);
  renderPositions(positions);
  renderHistory(history);
  pnlCandlesData = Array.isArray(candles) ? candles : [];
  lifetimeData   = lifeCurve;
  regime90dData  = Array.isArray(reg90) ? reg90 : [];
  regime1yData   = Array.isArray(reg1y) ? reg1y : [];
  renderChartForRange();
}

function setPnlRange(range) {
  pnlRange = range;
  document.getElementById('btn-range-90d').classList.toggle('active',  range === '90d');
  document.getElementById('btn-range-1y').classList.toggle('active',   range === '1y');
  document.getElementById('btn-range-life').classList.toggle('active', range === 'life');
  renderChartForRange();
}

function renderChartForRange() {
  if (pnlRange === '90d') {
    document.getElementById('pf-chart-title').textContent = 'Portfolio P&L Curve (90d) — Daily OHLC';
    _renderRegimeLegend(regime90dData || []);
    renderCandleChart(pnlCandlesData || [], regime90dData || []);
  } else if (pnlRange === '1y') {
    const rows = (valueCurveData && valueCurveData.rows) || [];
    document.getElementById('pf-chart-title').textContent =
      'Portfolio Value — ' + _rangeLabel(rows.length);
    _renderRegimeLegend(regime1yData || []);
    renderValueChart(rows, regime1yData || []);
  } else {
    const rows = (lifetimeData && lifetimeData.rows) || [];
    document.getElementById('pf-chart-title').textContent =
      'Portfolio Value — Lifetime (' + rows.length + (rows.length === 1 ? ' day' : ' days') + ')';
    _renderRegimeLegend([]);
    renderValueChart(rows, []);
  }
}

// Show the regime legend only when the current view paints bands. Items
// fade for regimes that don't occur in the visible window so the legend
// reflects what's actually on screen.
function _renderRegimeLegend(rows) {
  const el = document.getElementById('pf-regime-legend');
  if (!el) return;
  if (!Array.isArray(rows) || rows.length === 0) { el.style.display = 'none'; return; }
  const present = new Set(rows.map(r => r && r.regime).filter(Boolean));
  el.style.display = 'flex';
  for (const item of el.querySelectorAll('.rl-item')) {
    item.classList.toggle('rl-on', present.has(item.getAttribute('data-state')));
  }
}

function _rangeLabel(days) {
  return days >= 230 ? '1 Year'
       : days >= 105 ? '6 Months'
       : days >=  42 ? '3 Months'
       : days >=  15 ? '1 Month'
       : days >    0 ? (days + (days === 1 ? ' Day' : ' Days'))
       : '';
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

  // Show "actual N.NN×" sub under Invested so the operator can see the
  // realized broker exposure (not just the config-intent target lambda).
  const _actLev = (a.gross_leverage == null) ? '—' : a.gross_leverage.toFixed(2) + '×';
  document.getElementById('pf-invested').textContent = fmt(invested);
  const investedSubParts = [];
  if (a.short_market_value) investedSubParts.push('short ' + fmt(a.short_market_value));
  investedSubParts.push('λ actual ' + _actLev);
  document.getElementById('pf-invested-sub').textContent = investedSubParts.join(' · ');
}

function renderPortfolioSummary(s, valCurve) {
  document.getElementById('pf-open').textContent    = s.open_count ?? '—';
  document.getElementById('pf-closed').textContent  = s.closed_count ?? '—';
  // Win Rate card now shows BOTH windows: 30d on the left, lifetime on
  // the right. Mirrors the Annualized Return card pattern. Subtitle
  // shows lifetime denominator so the operator can sanity-check.
  const wr30 = s.win_rate_30d       != null ? s.win_rate_30d + '%'      : '—';
  const wrL  = s.win_rate_lifetime  != null ? s.win_rate_lifetime + '%' : '—';
  document.getElementById('pf-winrate').textContent = wr30 + ' | ' + wrL;
  if (s.closed_count) {
    document.getElementById('pf-winrate-sub').textContent =
      s.wins_lifetime + ' of ' + s.closed_count + ' positions profitable lifetime';
  } else {
    document.getElementById('pf-winrate-sub').textContent = 'No closed positions';
  }

  // Annualized Equity Realization % — Predicted | Lifetime.
  //   Lifetime  = annualization of the entire observed equity curve
  //               (anchor = Alpaca base_value, else first row).
  //   Predicted = annualization of just the last 30 trading days. While
  //               we have <30 days of history this collapses to the same
  //               window as Lifetime, so the two values are identical
  //               until the curve crosses one month.
  const rows      = (valCurve && valCurve.rows) || [];
  const baseValue = valCurve && valCurve.base_value != null ? parseFloat(valCurve.base_value) : null;
  const firstEq   = rows.length ? parseFloat(rows[0].equity) : null;
  const latestEq  = rows.length ? parseFloat(rows[rows.length - 1].equity) : null;
  const lifeAnchor= (baseValue != null && baseValue > 0) ? baseValue : firstEq;
  const lifeRet   = (lifeAnchor && latestEq != null) ? (latestEq - lifeAnchor) / lifeAnchor : null;
  const lifeDays  = rows.length;
  const lifeAar   = _annualizePct(lifeRet, lifeDays);

  const predRows  = rows.slice(-30);
  const predFirst = predRows.length ? parseFloat(predRows[0].equity) : null;
  // Use base_value as the anchor only when the predicted window starts at
  // the very beginning of the curve — otherwise the first row of the
  // 30-day slice is the correct anchor.
  const predAnchor= (predRows.length === rows.length && baseValue != null && baseValue > 0)
                    ? baseValue : predFirst;
  const predRet   = (predAnchor && latestEq != null) ? (latestEq - predAnchor) / predAnchor : null;
  const predDays  = predRows.length;
  const predAar   = _annualizePct(predRet, predDays);

  const el        = document.getElementById('pf-avgpnl');
  const subEl     = document.getElementById('pf-pnl-sub');
  const fmt = v => v == null ? '—' : ((v >= 0 ? '+' : '') + v.toFixed(2) + '%');
  if (lifeAar != null || predAar != null) {
    el.innerHTML = '<span class="' + pnlCls(predAar, 'positive', 'negative', 'neutral') + '">' + fmt(predAar) + '</span>'
                 + '<span style="color:var(--dim);font-weight:400">&nbsp;|&nbsp;</span>'
                 + '<span class="' + pnlCls(lifeAar, 'positive', 'negative', 'neutral') + '">' + fmt(lifeAar) + '</span>';
    el.className  = 'pf-stat-value';
    const lifeTxt = ((lifeRet * 100) >= 0 ? '+' : '') + (lifeRet * 100).toFixed(2) + '%';
    const equityTxt = '$' + latestEq.toLocaleString('en-US', {maximumFractionDigits: 0});
    subEl.textContent = lifeTxt + ' over ' + lifeDays + (lifeDays === 1 ? ' day' : ' days')
                      + '  ·  Equity: ' + equityTxt;
  } else {
    el.textContent = '—';
    subEl.textContent = rows.length ? 'Insufficient equity history' : 'No portfolio history yet';
  }
}

// ── Positions / History — grouped by ticker ────────────────────────────────
// Tables aggregate at the *ticker* level: one parent row per ticker with
// rollup metrics, click-to-expand reveals per-strategy sub-rows including
// signed contribution (+ winners, − losers).
//
// Contribution % = pnl_pct × size_pct, expressed as % of NAV. This is the
// honest "what this strategy added to (or subtracted from) the book" — it's
// size-weighted P&L. A 5% position up 10% contributes +0.50% to NAV.
//
// Direction normalization: six raw values exist in the DB
//   LONG / BUY / BUY_VOL   → LONG-side
//   SHORT / SELL / SELL_VOL → SHORT-side
// Net Dir at the ticker level is LONG, SHORT, or MIXED.
function _normalizeDir(d) {
  const u = String(d == null ? '' : d).toUpperCase();
  if (u === 'LONG' || u === 'BUY' || u === 'BUY_VOL') return 'LONG';
  if (u === 'SHORT' || u === 'SELL' || u === 'SELL_VOL') return 'SHORT';
  return u;
}

// Expansion state — preserved across re-renders (sort, collapse) so toggling
// a column header doesn't fold the user's open tickers back up.
//   { tableId: Set<ticker> }
const _expandedTickers = {};

// Raw signal rows, cached separately from _tableDataCache (which holds the
// *grouped* array for sort/collapse). On any re-render triggered by
// _bindSortable / _bindCollapse / _bindGroupClicks, the renderer needs the
// raw rows back — feeding groups through _groupByTicker a second time
// would silently produce empty rollups (the metrics-vanish + no-children
// bug from the first ship).
const _rawDataCache = {};
function _isExpanded(tableId, ticker) {
  const s = _expandedTickers[tableId];
  return !!(s && s.has(ticker));
}
function _toggleExpanded(tableId, ticker, renderFn) {
  let s = _expandedTickers[tableId];
  if (!s) { s = new Set(); _expandedTickers[tableId] = s; }
  if (s.has(ticker)) s.delete(ticker); else s.add(ticker);
  renderFn(_rawDataCache[tableId] || []);
}

// Group raw signal rows by ticker. \`mode\` is 'open' or 'closed' — picks the
// P&L column and the close-side field. Returns array of group objects with
// rollup keys (ticker, n, net_dir, net_pnl, contrib_pct, total_size_pct,
// avg_entry, current/close, avg_days, last_closed) and a \`signals\` array of
// the underlying rows (each augmented with \`dir_norm\` and \`contrib_pct\`).
function _groupByTicker(rows, mode) {
  const isOpen = mode === 'open';
  const pnlKey = isOpen ? 'unrealized_pnl_pct' : 'realized_pnl_pct';
  const priceKey = isOpen ? 'current_price' : 'closed_price';
  const map = new Map();
  for (const r of rows) {
    const tk = r.ticker || '—';
    let g = map.get(tk);
    if (!g) { g = { ticker: tk, signals: [] }; map.set(tk, g); }
    g.signals.push(r);
  }
  const groups = [];
  for (const g of map.values()) {
    let wEntrySum = 0, wEntryDen = 0;     // size-weighted entry
    let priceSum = 0, priceN = 0;          // mean price (current/close)
    let totalSize = 0;
    let contribSum = 0;                    // Σ pnl × size  (NAV-fraction)
    let sizeWPnlSum = 0;                   // Σ pnl × size  (same — net %)
    let pnlSizeDen = 0;                    // Σ size, restricted to rows where pnl exists
    let daysSum = 0, daysN = 0;
    let wins = 0;
    let lastClosed = null;
    const dirs = new Set();
    for (const s of g.signals) {
      const sz = s.position_size_pct != null ? parseFloat(s.position_size_pct) : null;
      const en = s.entry_price != null ? parseFloat(s.entry_price) : null;
      const px = s[priceKey] != null ? parseFloat(s[priceKey]) : null;
      let pn = s[pnlKey] != null ? parseFloat(s[pnlKey]) : null;
      const dh = s.days_held != null ? parseFloat(s.days_held) : null;
      const dn = _normalizeDir(s.direction);
      // Open: directional return off the broker's COST BASIS (avg_entry_price =
      // the first entry into the position) vs its live current price, so the
      // per-strategy P&L matches Alpaca's reported ticker P&L exactly. The
      // signal's own entry_price is the *intended* entry, not the actual fill,
      // and diverges (e.g. MU signal 949 vs broker basis 861). Each strategy's
      // own direction signs it (a short on a net-long, rising ticker shows red).
      // DB marks are null/stale intraday; the broker leg is the live basis.
      // Closed keeps the authoritative realized_pnl_pct.
      if (isOpen) {
        const bp = s.broker_position || null;
        const basis = (bp && bp.avg_entry_price != null && isFinite(parseFloat(bp.avg_entry_price)))
          ? parseFloat(bp.avg_entry_price)
          : ((en != null && isFinite(en)) ? en : null);   // broker cost basis; fallback to signal entry
        const bpx = (bp && bp.current_price != null && isFinite(parseFloat(bp.current_price)))
          ? parseFloat(bp.current_price)
          : ((px != null && isFinite(px)) ? px : null);
        const dirSign = dn === 'LONG' ? 1 : (dn === 'SHORT' ? -1 : 0);
        if (basis != null && basis !== 0 && bpx != null && dirSign !== 0) pn = dirSign * (bpx - basis) / basis;
      }
      if (dn) dirs.add(dn);
      s.dir_norm = dn;
      if (sz != null && isFinite(sz)) totalSize += sz;
      if (sz != null && en != null && isFinite(sz) && isFinite(en)) {
        wEntrySum += en * sz; wEntryDen += sz;
      }
      if (px != null && isFinite(px)) { priceSum += px; priceN += 1; }
      if (sz != null && pn != null && isFinite(sz) && isFinite(pn)) {
        const c = pn * sz;
        contribSum += c;
        sizeWPnlSum += c;
        pnlSizeDen += sz;
        s.contrib_pct = c;
      } else {
        s.contrib_pct = null;
      }
      if (dh != null && isFinite(dh)) { daysSum += dh; daysN += 1; }
      if (!isOpen && pn != null && isFinite(pn) && pn > 0) wins += 1;
      if (!isOpen && s.closed_at) {
        const t = new Date(s.closed_at).getTime();
        if (!isNaN(t) && (lastClosed == null || t > lastClosed)) lastClosed = t;
      }
    }
    g.n = g.signals.length;
    g.net_dir = dirs.size === 0 ? '—' : (dirs.size === 1 ? [...dirs][0] : 'MIXED');
    // Open header entry = broker cost basis (avg_entry_price = first entry into
    // the position) so entry→current matches Alpaca's reported P&L; closed uses
    // the size-weighted signal entry (no broker leg once closed).
    const _brokerAvg = (g.signals[0] && g.signals[0].broker_position
                        && g.signals[0].broker_position.avg_entry_price != null)
      ? parseFloat(g.signals[0].broker_position.avg_entry_price) : null;
    g.avg_entry = (isOpen && _brokerAvg != null && isFinite(_brokerAvg))
      ? _brokerAvg
      : (wEntryDen > 0 ? wEntrySum / wEntryDen : null);
    // Open prefers the broker's live current price: the DB mark (close_price)
    // is null OR stale (=entry, written at open) intraday until the 16:15
    // compute, which would make the header read no movement while the bars
    // (computed off the live broker price) show plenty. Closed has no broker
    // leg and uses the DB closed_price mean.
    const _brokerCur = (g.signals[0] && g.signals[0].broker_position
                        && g.signals[0].broker_position.current_price != null)
      ? parseFloat(g.signals[0].broker_position.current_price) : null;
    g.price = (isOpen && _brokerCur != null && isFinite(_brokerCur))
      ? _brokerCur
      : (priceN > 0 ? priceSum / priceN : null);
    // Broker truth (2026-05-19): if any strategy-intent row carries a
    // broker_position block, the broker holds this ticker — use its size +
    // entry + return for the parent row, overriding the intent-rollup. All
    // signals for a ticker carry the same broker_position so signals[0] is
    // representative. When the broker doesn't hold the ticker (drift case),
    // broker stays null and the parent row falls back to intent rollup
    // (legacy behaviour, for stale 'open' rows still in the table).
    g.broker = (g.signals[0] && g.signals[0].broker_position) || null;
    // Resting exit legs, attached per-ticker by /api/portfolio/positions. Same
    // representative-row contract as broker_position: every signal for a ticker
    // carries the identical block, because the broker holds ONE consolidated
    // position per ticker and therefore ONE bracket — no matter how many
    // strategies want a piece of it.
    g.broker_bracket = (g.signals[0] && g.signals[0].broker_bracket) || null;
    g.total_size_pct = (g.broker && g.broker.size_pct != null)
      ? g.broker.size_pct                // broker-truth $ size / NAV
      : totalSize;                       // fallback to summed intents
    g.contrib_pct = sizeWPnlSum;
    g.net_pnl = (g.broker && g.broker.unrealized_plpc != null)
      ? g.broker.unrealized_plpc         // broker-truth unrealized P/L %
      : (pnlSizeDen > 0 ? sizeWPnlSum / pnlSizeDen : null);
    if (g.broker && g.broker.market_value != null) g.market_value = g.broker.market_value;
    // Corr-adjusted cumulative Sharpe (S_adj) at last sizing — attached
    // per-ticker by /api/portfolio/positions (all rows carry the same value).
    const _sadj = g.signals[0] && g.signals[0].corr_cum_sharpe != null
      ? parseFloat(g.signals[0].corr_cum_sharpe) : null;
    g.corr_cum_sharpe = (_sadj != null && isFinite(_sadj)) ? _sadj : null;
    // The sizer's actual per-strategy contributions for this ticker (attached by
    // /api/portfolio/positions from cycle_contributing_strategies). The expanded
    // view prefers these over raw open intents. All signal rows for a ticker
    // carry the same array; read it off the first, mirroring corr_cum_sharpe.
    g.sizer_contributions = (g.signals[0] && Array.isArray(g.signals[0].sizer_contributions))
      ? g.signals[0].sizer_contributions : null;
    g.avg_days = daysN > 0 ? daysSum / daysN : null;
    g.is_open = isOpen;
    g.wins = wins;
    g.losses = isOpen ? null : (g.n - wins);
    g.last_closed = lastClosed;
    // Pre-sort sub-rows: largest size first (so the heaviest contributor
    // shows up under the chevron). Sub-row order doesn't bind to parent
    // sort — sort is at the ticker level only.
    g.signals.sort((a, b) => {
      const sa = a.position_size_pct != null ? parseFloat(a.position_size_pct) : -1;
      const sb = b.position_size_pct != null ? parseFloat(b.position_size_pct) : -1;
      return sb - sa;
    });
    groups.push(g);
  }
  return groups;
}

function _dirCls(d) {
  if (d === 'LONG')  return 'dir-long';
  if (d === 'SHORT') return 'dir-short';
  if (d === 'MIXED') return 'dir-mixed';
  return '';
}
// Signed percent formatter — input is already in display percent units
// (e.g. 0.84 → "+0.84%"). Distinct from the older _fmtPct(v) defined
// later in this file which multiplies by 100 internally; the two would
// collide (later function wins via JS hoisting) so this one carries a
// unique name.
function _fmtPctSigned(p, withSign) {
  if (p == null || !isFinite(p)) return '—';
  const s = (withSign && p > 0 ? '+' : '') + p.toFixed(2) + '%';
  return s;
}

// Compact dollar formatter with optional sign. Examples:
//   _fmtDollar(234)         → "$234"
//   _fmtDollar(234, true)   → "+$234"
//   _fmtDollar(-1234)       → "-$1.2k"
//   _fmtDollar(12345, true) → "+$12.3k"
//   _fmtDollar(2345000)     → "$2.3M"
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

// Tile background color for the heatmap. \`pct\` is a P&L percent already
// multiplied to its display scale (e.g. 1.5 for +1.5%). Anchored at ±5%
// for full saturation; values beyond that just stay at peak intensity.
// Returns an rgba() so the tile keeps a dark base on zero, gradient-fades
// to green for winners and red for losers.
function _pnlColor(pct) {
  // Snap to grey for any P&L that displays as "0.00%" — matches the
  // 2-decimal formatting in _fmtPctSigned. Prevents the faint green/red
  // tint on rounding-noise positions where the visible number reads zero.
  if (pct == null || !isFinite(pct) || Math.abs(pct) < 0.005) return 'rgba(48,54,61,0.55)';
  const t = Math.max(-1, Math.min(1, pct / 5));
  if (t > 0) return 'rgba(63,185,80,'  + (0.18 + 0.72 * t).toFixed(2) + ')';
  return         'rgba(248,81,73,'  + (0.18 + 0.72 * -t).toFixed(2) + ')';
}

// Vertical stack of signed per-strategy NAV-contribution bars for a ticker.
// Bars are centered on a zero-line, scaled to max |contribution|; entry +
// current/close prices come from the already-loaded group object. See the
// in-function comment for the contribution definition and data source.
function _buildAlphaBarsHtml(group) {
  // Per-strategy bars. OPEN positions render the SIZER's actual contributions
  // (group.sizer_contributions, from cycle_contributing_strategies) — the
  // strategies that truly sized the position, as signed sizing weight — so the
  // panel matches the true book instead of the raw open-intent list (which also
  // carries gated-out + stale-but-unclosed phantoms). CLOSED trades (History)
  // keep the per-signal realized-P&L bars built here, which ARE legitimately
  // per-strategy. The signals→P&L build below is the closed-trade path and the
  // fallback; the sizer override is applied after the price header.
  const byStrat = new Map();
  for (const s of (group.signals || [])) {
    const sid = s.strategy_id || 'unknown';
    let e = byStrat.get(sid);
    if (!e) { e = { strategy_id: sid, contribution: 0, hasVal: false, dirs: new Set() }; byStrat.set(sid, e); }
    const cc = (s.contrib_pct != null && isFinite(s.contrib_pct)) ? s.contrib_pct : null;
    if (cc != null) { e.contribution += cc; e.hasVal = true; }
    if (s.dir_norm) e.dirs.add(s.dir_norm);
  }
  let contributions = [...byStrat.values()].map(e => ({
    strategy_id: e.strategy_id,
    contribution: e.hasVal ? e.contribution * 100 : null,   // NAV-fraction → display %
    direction: e.dirs.size === 1 ? [...e.dirs][0] : (e.dirs.size > 1 ? 'MIXED' : ''),
  })).sort((a, b) => {
    if (a.contribution == null) return 1;
    if (b.contribution == null) return -1;
    return b.contribution - a.contribution;   // winners (green) top, losers (red) bottom
  });
  // Price header (entry + current/close) — from the already-loaded position.
  const isOpen = group.is_open !== false;
  const entry  = group.avg_entry != null ? parseFloat(group.avg_entry) : null;
  const nowPx  = group.price != null ? parseFloat(group.price) : null;  // already resolved: current if open, close if closed
  const pxFmt  = (v) => v == null || !isFinite(v) ? '—' : '$' + v.toFixed(2);
  // Exit legs — broker truth (what will actually fire), not strategy intent.
  // The % is measured from ENTRY, so it reads as the bracket's designed shape
  // (the -8%/+5% the sizer chose) and stays put as the market moves; a
  // current-price gap would drift every tick and never match the sizer's numbers.
  // Sign is raw price direction (stop below a long = negative), so a long reads
  // stop −8% / target +5% and a short reads stop +8% / target −5%.
  // null bracket = the broker leg failed, so we do not KNOW; an empty-but-present
  // bracket = we asked and there is genuinely nothing resting. Never render the
  // former as "none" — that would invent an unprotected book out of an API blip.
  const bracket = group.broker_bracket || null;
  const bracketKnown = bracket != null;
  const legPct  = (lvl) => (entry != null && isFinite(entry) && entry !== 0)
    ? ((lvl - entry) / entry) * 100 : null;
  // Leg % color is SEMANTIC, not sign-based: stop = red (risk), target =
  // green (reward). Sign-based coloring read backwards on shorts, where the
  // stop sits ABOVE entry (+% → green) and the target below (−% → red).
  // The signed values themselves stay raw price direction.
  const legHtml = (label, lvl, title, cls) => {
    if (!bracketKnown) {
      return \`<span class="ab-px" title="could not read resting orders from the broker — \${label} unknown, not necessarily absent">\${label} <b>—</b></span>\`;
    }
    if (lvl == null || !isFinite(lvl)) {
      // Absence IS the finding — an unprotected position must not render as a
      // blank gap that reads like "nothing to report".
      return \`<span class="ab-px ab-px-none" title="no resting \${label} order at the broker for \${escapeHtml(group.ticker)} — this position is not bracketed">\${label} <b>none</b></span>\`;
    }
    const gap = legPct(lvl);
    const gapTxt = gap == null ? '' :
      \` <span class="\${cls}">\${gap >= 0 ? '+' : ''}\${gap.toFixed(1)}%</span>\`;
    return \`<span class="ab-px" title="\${title}">\${label} <b>\${pxFmt(lvl)}</b>\${gapTxt}</span>\`;
  };
  // A position being LIQUIDATED (past-stop emergency exit / ext-hours monitor
  // exit) must not read as bracketed or as silently naked — it gets its own
  // chip. "exit $x pending" = the liquidation order is resting at the broker;
  // "exit queued" = the monitor journal owes the exit at its next tick
  // (10-min cadence, 04:00–19:50 ET).
  const exitChip = !bracketKnown ? '' :
    (bracket.exiting != null && isFinite(bracket.exiting))
      ? \`<span class="ab-px" title="liquidation order resting at the broker — this position is being closed (past-stop emergency exit or monitor exit), not bracketed">exit <b>\${pxFmt(bracket.exiting)}</b> <span class="pf-pnl-neg">pending</span></span>\`
      : (bracket.queued
        ? \`<span class="ab-px" title="past its stop with no session open — exit journaled for the ext-hours monitor, fires at its next tick (10-min cadence, 04:00–19:50 ET)">exit <b class="pf-pnl-neg">queued</b></span>\`
        : '');
  const bracketHdr = !isOpen ? '' :
    legHtml('stop', bracket && bracket.stop,
            'stop-loss resting at the broker · % is measured from the entry price',
            'pf-pnl-neg')
    + legHtml('target', bracket && bracket.target,
              'take-profit resting at the broker · % is measured from the entry price',
              'pf-pnl-pos')
    + exitChip;
  const priceHdr = \`<div class="ab-prices">
      <span class="ab-px">entry <b>\${pxFmt(entry)}</b></span>
      <span class="ab-px">\${isOpen ? 'current' : 'close'} <b>\${pxFmt(nowPx)}</b></span>
      \${bracketHdr}
    </div>\`;

  // Prefer the sizer's ACTUAL per-strategy contributions (the strategies that
  // truly SIZED this position) over the raw open-intent list. Fall back to the
  // intent-P&L bars ONLY for closed trades (History); an OPEN position with no
  // sizer attribution renders "not re-sized", never the phantom intent list.
  let contribUnit = 'pnl';
  if (Array.isArray(group.sizer_contributions) && group.sizer_contributions.length) {
    contributions = group.sizer_contributions.map(c => ({
      strategy_id: c.strategy_id,
      contribution: (c.contribution != null && isFinite(Number(c.contribution))) ? Number(c.contribution) : null,
      direction: Number(c.direction) > 0 ? 'LONG' : (Number(c.direction) < 0 ? 'SHORT' : ''),
    })).sort((a, b) => {
      if (a.contribution == null) return 1;
      if (b.contribution == null) return -1;
      return b.contribution - a.contribution;
    });
    contribUnit = 'sizing';
  } else if (isOpen) {
    contributions = [];          // suppress phantom open-intent bars
    contribUnit = 'unsized';
  }

  if (!contributions.length) {
    const emptyMsg = contribUnit === 'unsized'
      ? \`<b>\${escapeHtml(group.ticker)}</b> has not been re-sized since its last sizing stamp — no current per-strategy attribution.\`
      : \`No strategy signals recorded for <b>\${escapeHtml(group.ticker)}</b>.\`;
    return \`<div class="alpha-bars">\${priceHdr}
      <div class="alpha-bars empty">\${emptyMsg}</div>
    </div>\`;
  }
  const haveValues = contributions.some(c => c.contribution != null && isFinite(c.contribution));
  const maxAbs = haveValues
    ? (Math.max(...contributions.map(c => Math.abs(c.contribution || 0))) || 1)
    : 1;

  const rows = contributions.map(c => {
    const val   = (c.contribution != null && isFinite(c.contribution)) ? c.contribution : null;
    if (val == null) {
      return \`<div class="ab-row ab-unranked">
        <div class="ab-label">\${c.strategy_id}</div>
        <div class="ab-track muted"><div class="ab-zero"></div></div>
        <div class="ab-value muted">n/a</div>
      </div>\`;
    }
    const widthPct = (Math.abs(val) / maxAbs) * 50;
    const sign     = val >= 0 ? 'pos' : 'neg';
    const valCls   = val >= 0 ? 'pf-pnl-pos' : 'pf-pnl-neg';
    const dirNorm  = c.direction || '';
    const valTxt   = (val >= 0 ? '+' : '') + val.toFixed(2) + (contribUnit === 'sizing' ? '' : '%');
    const labTitle = contribUnit === 'sizing'
      ? c.strategy_id + ' · sizing contribution (conviction-weighted allocation: sleeve Sharpe × size_scalar × direction)'
      : c.strategy_id + ' · NAV contribution = size% × P&L%';
    return \`<div class="ab-row">
      <div class="ab-label" title="\${labTitle}">\${c.strategy_id}</div>
      <div class="ab-dir \${_dirCls(dirNorm)}">\${dirNorm}</div>
      <div class="ab-track">
        <div class="ab-zero"></div>
        <div class="ab-fill \${sign}" style="width:\${widthPct.toFixed(2)}%"></div>
      </div>
      <div class="ab-value \${valCls}">\${valTxt}</div>
    </div>\`;
  }).join('');

  const net = haveValues ? contributions.reduce((s, c) => s + (c.contribution || 0), 0) : null;
  let badge = '';
  if (net != null && isFinite(net)) {
    const netTxt = (net >= 0 ? '+' : '') + net.toFixed(2) + (contribUnit === 'sizing' ? '' : '%');
    const netCls = net >= 0 ? 'pf-pnl-pos' : 'pf-pnl-neg';
    const netLabel = contribUnit === 'sizing' ? 'net sizing' : 'net contrib';
    badge = \`<span class="ab-badge">\${netLabel} = <span class="\${netCls}">\${netTxt}</span></span>\`;
  }
  // Acting strategies (2026-08-22 conviction gate): distinct strategies whose
  // sizing contribution points the way the position nets. This is the count
  // the sizer compares against the regime's min_acting_strategies slider.
  if (contribUnit === 'sizing' && net != null && isFinite(net) && net !== 0) {
    const side = net > 0 ? 'LONG' : 'SHORT';
    const acting = new Set(contributions.filter(c => c.direction === side).map(c => c.strategy_id)).size;
    badge += \` <span class="ab-badge" title="distinct strategies acting \${side} on \${escapeHtml(group.ticker)} at the last sizing — the per-regime Conviction Gate (Strategies page) requires at least N of these">acting = \${acting}</span>\`;
  }
  // Conviction the position was last sized at = |S_adj|, the MAGNITUDE of the
  // correlation-adjusted cumulative Sharpe. S_adj itself is signed by trade
  // direction (long +, short −) and the row is already visibly long/short, so
  // a signed value here only reads as a spurious "negative Sharpe". Show the
  // magnitude, and label it "conviction" so it is never mistaken for a raw
  // Sharpe ratio. S_adj SIZES the position (and its (|S|+1) cap); ticker
  // selection is the acting-strategy gate above. (Kept in this expanded
  // per-position view only; the main heatmap/treemap tiles no longer carry
  // it — declutter, 2026-07-17.)
  if (group.corr_cum_sharpe != null && isFinite(group.corr_cum_sharpe)) {
    const conv = Math.abs(group.corr_cum_sharpe);
    badge += \` <span class="ab-badge" title="conviction |S_adj| — magnitude of the correlation-adjusted cumulative Sharpe the position was last sized at; drives the sizing weight and the per-ticker (|S|+1) cap">conviction |S_adj| = \${conv.toFixed(2)}</span>\`;
  } else if (isOpen) {
    // Render the gap rather than dropping the badge. The sizer stamps S_adj
    // only on cycles that emit orders, so a silently absent badge is
    // indistinguishable from "this feature was never built" — which is exactly
    // how it read when the sizer went several cycles without emitting.
    badge += \` <span class="ab-badge ab-badge-warn" title="no S_adj stamped for \${escapeHtml(group.ticker)}: the sizer records it only on cycles that emit orders, so this position has not been re-sized since the stamp was added">conviction |S_adj| = —</span>\`;
  }
  return \`<div class="alpha-bars">
    \${priceHdr}
    <div class="ab-title"><span class="ab-ticker">\${escapeHtml(group.ticker)}</span>\${badge}</div>
    \${rows}
  </div>\`;
}

// Squares-only heatmap layout (2026-05-21). Each tile is a 1:1 square
// whose AREA encodes total_size_pct (so side = √(share) × k, clamped
// to [MIN_SIDE, MAX_SIDE]). Tiles wrap left-to-right via flexbox; the
// arrangement does NOT tile into a larger square — the operator
// accepted gaps as the price of consistent aspect ratios on every
// ticker. Color encodes net_pnl. Click a tile to reveal the
// per-strategy contribution bars below.
//
// Two display modes: compact (top 12) and expanded (all tickers,
// scrollable within max-height). Replaces the prior slice-and-dice
// row-based layout that gave the long-tail tiles unreadable aspect
// ratios.
const _heatmapSelected = {};   // { tableId: ticker | null }
const _heatmapExpanded = {};   // { tableId: bool }

// Open positions — two-column LONG/SHORT grid of uniform tiles.
// Replaces the variable-area heatmap + custom packer for the live
// positions view: CSS Grid auto-fill handles layout natively, tile
// size is uniform (color still encodes P&L), columns are split by
// broker.side. Closed-trade history (renderHistory) still uses
// _buildHeatmapHtml below since the variable-area view conveys past
// position size more usefully there.
// Squarified-treemap algorithm (Bruls/Huijing/van Wijk 2000). Lays out
// rectangles in a container so each has area proportional to its weight
// AND aspect ratios are kept as close to 1 as possible. Pure function;
// returns [{...item, x, y, w, h}]. The dashboard runs this per-column
// after the column's actual width is known (ResizeObserver re-runs on
// resize), then absolute-positions each tile from the result.
function _squarifiedTreemap(items, width, height) {
  if (!items.length || width <= 0 || height <= 0) return [];
  const total = items.reduce((s, i) => s + Math.max(0, i.value || 0), 0);
  if (total <= 0) return items.map(i => ({ ...i, x: 0, y: 0, w: 0, h: 0 }));
  const scale = (width * height) / total;
  const scaled = items
    .map(i => ({ ...i, area: Math.max(0, i.value || 0) * scale }))
    .filter(i => i.area > 0)
    .sort((a, b) => b.area - a.area);
  const output = [];
  _squarifyStep(scaled, [], { x: 0, y: 0, w: width, h: height }, output);
  return output;
}
function _worstAspect(row, sideLen) {
  if (!row.length) return Infinity;
  const sum = row.reduce((s, i) => s + i.area, 0);
  const rmax = row.reduce((m, i) => Math.max(m, i.area), 0);
  const rmin = row.reduce((m, i) => Math.min(m, i.area), Infinity);
  const s2 = sum * sum, l2 = sideLen * sideLen;
  return Math.max((l2 * rmax) / s2, s2 / (l2 * rmin));
}
function _layoutRow(row, rect, output) {
  if (!row.length) return rect;
  const sum = row.reduce((s, i) => s + i.area, 0);
  if (rect.w >= rect.h) {
    const colW = sum / rect.h;
    let cy = rect.y;
    for (const item of row) {
      const itemH = item.area / colW;
      output.push({ ...item, x: rect.x, y: cy, w: colW, h: itemH });
      cy += itemH;
    }
    return { x: rect.x + colW, y: rect.y, w: Math.max(0, rect.w - colW), h: rect.h };
  } else {
    const rowH = sum / rect.w;
    let cx = rect.x;
    for (const item of row) {
      const itemW = item.area / rowH;
      output.push({ ...item, x: cx, y: rect.y, w: itemW, h: rowH });
      cx += itemW;
    }
    return { x: rect.x, y: rect.y + rowH, w: rect.w, h: Math.max(0, rect.h - rowH) };
  }
}
function _squarifyStep(items, row, rect, output) {
  if (!items.length) { _layoutRow(row, rect, output); return; }
  const next = items[0];
  const sideLen = Math.min(rect.w, rect.h);
  if (sideLen <= 0) { return; }
  const candidate = [...row, next];
  if (row.length === 0 || _worstAspect(candidate, sideLen) <= _worstAspect(row, sideLen)) {
    _squarifyStep(items.slice(1), candidate, rect, output);
  } else {
    const remaining = _layoutRow(row, rect, output);
    _squarifyStep(items, [], remaining, output);
  }
}

function _buildPositionsTwoColumnHtml(groups, selectedTicker, nav) {
  const longs  = groups.filter(g => g.broker && g.broker.side === 'long');
  const shorts = groups.filter(g => g.broker && g.broker.side === 'short');
  // No broker side (degraded mode) → bucket by DB direction as fallback so
  // the panel never collapses to empty when Alpaca is unreachable.
  for (const g of groups) {
    if (!g.broker || !g.broker.side) {
      const sigs = (g.signals || []);
      const longCnt  = sigs.filter(s => (s.direction || '').toUpperCase() === 'LONG').length;
      const shortCnt = sigs.filter(s => (s.direction || '').toUpperCase() === 'SHORT').length;
      (longCnt >= shortCnt ? longs : shorts).push(g);
    }
  }
  // Note: order doesn't matter for output rendering — the treemap algorithm
  // sorts internally. We do sort here for the side counts only.

  const buildCol = (label, rows) => {
    const totalMv = rows.reduce((s, g) => s + Math.abs((g.broker && g.broker.market_value) || 0), 0);
    const sideClass = label === 'LONG' ? 'pf-poscol-long' : 'pf-poscol-short';
    const meta = \`\${rows.length} · \${totalMv > 0 ? _fmtDollar(totalMv, false) : '—'}\`;
    const body = rows.length
      ? \`<div class="pf-poscol-tm" data-side="\${label}"></div>\`
      : '<div class="empty" style="padding:12px;font-size:11px">none</div>';
    return \`<div class="pf-poscol \${sideClass}">
      <div class="pf-poscol-header"><span>\${label}</span><span class="pf-poscol-meta">\${meta}</span></div>
      \${body}
    </div>\`;
  };

  // Stash per-side row data for the post-render treemap populator. Keyed
  // by side label so _populateTreemap can pull the right rows + selection
  // state. _navCache is the closure capture; selectedTicker passed through.
  _pendingTreemap = {
    LONG:  { rows: longs,  selectedTicker, nav },
    SHORT: { rows: shorts, selectedTicker, nav },
  };

  return \`<div class="pf-pos-twocol">\${buildCol('LONG', longs)}\${buildCol('SHORT', shorts)}</div>\`;
}

// Module-scoped staging slot — _buildPositionsTwoColumnHtml fills this
// during render so _populateTreemap can read the row data + selection
// after the placeholder containers are in the DOM and their widths are
// measurable. One render cycle at a time; no race because both writes
// and reads happen synchronously in renderPositions().
let _pendingTreemap = null;

function _sizeClassForTile(w, h) {
  const s = Math.min(w, h);
  if (s >= 130) return 'huge';
  if (s >= 80)  return 'large';
  if (s >= 50)  return 'medium';
  if (s >= 28)  return 'small';
  return 'tiny';
}

function _populateTreemap(container, label) {
  const stash = _pendingTreemap && _pendingTreemap[label];
  if (!stash) return;
  const { rows, selectedTicker, nav } = stash;
  const w = container.clientWidth;
  const h = container.clientHeight;
  if (!w || !h || !rows.length) return;

  // Weight = ABS broker market value (degraded → total_size_pct fallback).
  // Filter out zero-weight items so the algorithm doesn't try to lay them out.
  const items = rows.map(g => {
    const mv = g.broker && Number.isFinite(g.broker.market_value)
      ? Math.abs(g.broker.market_value)
      : Math.abs(g.total_size_pct || 0);
    return { ...g, value: mv };
  });

  const placed = _squarifiedTreemap(items, w, h);
  container.innerHTML = placed.map(g => {
    const pnl = (g.net_pnl != null && isFinite(g.net_pnl)) ? g.net_pnl * 100 : null;
    // Shorts display the TICKER's move, not the position P&L sign — a short
    // losing 2.2% reads "+2.2%" (the stock rose 2.2%). Color stays keyed to
    // real P&L (_pnlColor(pnl)): red tile + positive number = losing short.
    const dispPnl = (pnl != null && g.net_dir === 'SHORT') ? -pnl : pnl;
    const dollarPnl = (g.broker && Number.isFinite(g.broker.unrealized_pl))
      ? g.broker.unrealized_pl
      : ((nav && isFinite(nav) && g.contrib_pct != null) ? g.contrib_pct * nav : null);
    const sharePct = (g.broker && g.broker.size_pct != null)
      ? g.broker.size_pct * 100
      : (g.total_size_pct || 0) * 100;
    const isSel = g.ticker === selectedTicker;
    const days = g.avg_days != null ? g.avg_days.toFixed(0) + 'd' : '';
    const sizeClass = _sizeClassForTile(g.w, g.h);
    const pnlTip = pnl == null ? '' :
      (dispPnl !== pnl
        ? ' · px ' + _fmtPctSigned(dispPnl, true) + ' · pnl ' + _fmtPctSigned(pnl, true)
        : ' · pnl ' + _fmtPctSigned(pnl, true));
    const tip = \`\${g.ticker} · \${sharePct.toFixed(2)}% of NAV · \${g.n} strateg\${g.n === 1 ? 'y' : 'ies'}\${pnlTip}\${dollarPnl != null ? ' · ' + _fmtDollar(dollarPnl, true) : ''}\${g.avg_days != null ? ' · ' + days : ''}\`;
    return \`<div class="pf-postile\${isSel ? ' selected' : ''}" data-ticker="\${g.ticker}" data-size="\${sizeClass}" style="left:\${g.x.toFixed(1)}px;top:\${g.y.toFixed(1)}px;width:\${g.w.toFixed(1)}px;height:\${g.h.toFixed(1)}px;background:\${_pnlColor(pnl)}" title="\${tip}">
      <div class="pf-postile-sym">\${g.ticker}</div>
      <div class="pf-postile-bottom">
        <span class="pf-postile-pnl">\${dispPnl != null ? _fmtPctSigned(dispPnl, true) : '—'}</span>
        <span class="pf-postile-share">\${sharePct.toFixed(1)}%</span>
      </div>
    </div>\`;
  }).join('');
}

function _buildHeatmapHtml(groups, selectedTicker, expanded, nav) {
  const sorted = [...groups].sort((a, b) => (b.total_size_pct || 0) - (a.total_size_pct || 0));
  const shown  = expanded ? sorted : sorted.slice(0, 12);
  // Size denominator: only tickers the broker actually holds. Phantom
  // intent-rollups from stale execution_signals (broker doesn't hold them)
  // would otherwise dilute the share %. 2026-05-19: GLW at 194% of NAV
  // displayed as 8.95% because 272 phantom intent tickers padded the
  // denominator to 21.73. Fallback to all groups if no broker-held rows
  // exist (e.g. flat book) so tiles still render proportionally.
  const heldGroups = sorted.filter(g => g.broker && g.broker.size_pct != null);
  const sizeDenom = (heldGroups.length
    ? heldGroups.reduce((s, g) => s + (g.total_size_pct || 0), 0)
    : sorted.reduce((s, g) => s + (g.total_size_pct || 0), 0)) || 1;
  // Square tiles (2026-05-21): each tile's AREA is proportional to its
  // share of the book, so the SIDE = sqrt(share). Clamp the largest tile
  // to MAX_SIDE so it doesn't dominate the panel, and the smallest to
  // MIN_SIDE so micro-positions stay readable. Gaps via CSS flex-wrap;
  // tiles do NOT tile into a larger square — per design, that constraint
  // would force unreadable aspect ratios on the long tail.
  const MIN_SIDE = 56;
  const MAX_SIDE = 168;
  const maxShare = shown.reduce((m, g) => Math.max(m, (g.total_size_pct || 0) / sizeDenom), 0) || 1;
  // Pick a sqrt-scale such that the largest visible tile lands at MAX_SIDE.
  // side(g) = sqrt(share(g)) × k, where k = MAX_SIDE / sqrt(maxShare).
  const k = MAX_SIDE / Math.sqrt(maxShare);
  const tilesHtml = shown.map(g => {
      const pnl = (g.net_pnl != null && isFinite(g.net_pnl)) ? g.net_pnl * 100 : null;
      // Shorts display the TICKER's move (see treemap render above); color
      // stays keyed to real P&L.
      const dispPnl = (pnl != null && g.net_dir === 'SHORT') ? -pnl : pnl;
      const share       = (g.total_size_pct || 0) / sizeDenom;
      const sharePct    = share * 100;
      const rawNotional = (g.total_size_pct || 0) * 100;
      // Prefer the broker's live unrealized_pl ($) over the DB-derived
      // nav × contrib_pct fallback. The DB path uses signal_pnl marks
      // (updated only by the daily reconcile cron) and the position_size_pct
      // at signal time, so it diverges from broker truth as the day progresses
      // AND can show the wrong sign on multi-strategy tickers where intent
      // sizes don't sum to the actual broker share. unrealized_pl is the
      // exact figure Alpaca reports for the consolidated position.
      const dollarPnl   = (g.broker && Number.isFinite(g.broker.unrealized_pl))
                            ? g.broker.unrealized_pl
                            : ((nav && isFinite(nav) && g.contrib_pct != null)
                                ? g.contrib_pct * nav : null);
      const side  = Math.max(MIN_SIDE, Math.min(MAX_SIDE, Math.round(Math.sqrt(share) * k)));
      // Size tier drives per-element font/visibility via CSS rules:
      // tiny (<70) shows ticker + pnl% only, small (70..99) drops $,
      // medium (100..139) is the base layout, large (>=140) scales up.
      // Cutoffs chosen so each tier's content fits without truncation.
      const tier  = side >= 140 ? 'large' : side >= 100 ? 'medium' : side >= 70 ? 'small' : 'tiny';
      const isSel = g.ticker === selectedTicker;
      const days  = g.avg_days != null ? g.avg_days.toFixed(0) + 'd' : '';
      const pnlTip = pnl == null ? '' :
        (dispPnl !== pnl
          ? ' · px ' + _fmtPctSigned(dispPnl, true) + ' · pnl ' + _fmtPctSigned(pnl, true)
          : ' · pnl ' + _fmtPctSigned(pnl, true));
      const tip = \`\${g.ticker} · \${sharePct.toFixed(2)}% of book (notional \${rawNotional.toFixed(1)}%) · \${g.n} strateg\${g.n === 1 ? 'y' : 'ies'}\${pnlTip}\${dollarPnl != null ? ' · ' + _fmtDollar(dollarPnl, true) : ''}\${g.avg_days != null ? ' · ' + days : ''}\`;
      // data-side carries the computed side length for the client-side
      // shelf packer (_packHeatmapShelves) to read after DOM insertion.
      // Position becomes absolute via packer; until then tiles render
      // off-screen so the user never sees the pre-pack stacked layout.
      return \`<div class="pf-tile pf-tile-\${tier} \${isSel ? 'selected' : ''}"
                   data-ticker="\${g.ticker}"
                   data-side="\${side}"
                   style="position:absolute;left:-9999px;top:0;width:\${side}px;height:\${side}px;background:\${_pnlColor(pnl)};"
                   title="\${tip}">
        <div class="pf-tile-hero">
          <div class="tk-symbol">\${g.ticker}</div>
          <div class="tk-row">
            <span class="tk-pnl">\${dispPnl != null ? _fmtPctSigned(dispPnl, true) : '—'}</span>
            \${dollarPnl != null ? '<span class="tk-dollar">' + _fmtDollar(dollarPnl, true) + '</span>' : ''}
          </div>
        </div>
        <div class="pf-tile-strip">
          <span class="tk-share">\${sharePct.toFixed(1)}%</span>
          <span class="tk-days">\${days}</span>
        </div>
      </div>\`;
    }).join('');
  return \`<div class="pf-heatmap \${expanded ? 'expanded' : ''}">\${tilesHtml}</div>\`;
}

// Pack squares using Skyline Bottom-Left placement. For each tile (in
// descending side order) we find the (x, y) where the local skyline is
// LOWEST — ties broken by leftmost x — and place it there. The skyline
// is the per-x upper boundary of placed tiles, represented as a sorted
// list of {x, w, y} segments tiling [0, container_width). This packs
// tighter than FFDH because small tiles slot into vertical gaps below
// medium tiles within an existing shelf instead of spilling to a fresh
// shelf at the bottom. For the typical Active Positions panel (1 huge
// tile + several medium + a long tail of small), this is the
// difference between "1 row of giants + 1 mostly-empty row of dwarfs"
// and "1 row that absorbs all dwarfs into the giants' shadow."
function _packHeatmapShelves(container) {
  if (!container) return;
  const padding = parseFloat(getComputedStyle(container).paddingLeft) || 0;
  const width   = (container.clientWidth || 0) - padding * 2;
  if (width <= 0) return;
  const gap     = 6;
  const tileEls = Array.from(container.querySelectorAll('.pf-tile'));
  if (!tileEls.length) { container.style.height = '0px'; return; }
  const items = tileEls.map(el => ({ el, side: parseFloat(el.dataset.side) || 56 }));
  items.sort((a, b) => b.side - a.side);

  // Skyline = sorted segments covering [0, width). Each segment.y is the
  // upper boundary at that x range. Gaps between placed tiles are
  // absorbed into the segment's width via the +gap below.
  let skyline = [{ x: 0, w: width, y: 0 }];

  for (const it of items) {
    const s = it.side;
    if (s > width) {
      // Tile wider than container — stack at the topmost current y.
      const maxY = skyline.reduce((m, seg) => Math.max(m, seg.y), 0);
      it.x = 0; it.y = maxY;
      skyline = [{ x: 0, w: width, y: maxY + s + gap }];
      continue;
    }
    // Try each segment's left edge as a candidate anchor. For each, the
    // placement's y = max segment.y over [x, x+s). Pick lowest y, ties
    // broken by leftmost x.
    let bestX = -1, bestY = Infinity;
    for (let i = 0; i < skyline.length; i++) {
      const x = skyline[i].x;
      if (x + s > width) continue;
      let maxY = 0;
      for (let j = i; j < skyline.length; j++) {
        if (skyline[j].x >= x + s) break;
        if (skyline[j].y > maxY) maxY = skyline[j].y;
      }
      if (maxY < bestY || (maxY === bestY && (bestX < 0 || x < bestX))) {
        bestY = maxY; bestX = x;
      }
    }
    if (bestX < 0) {
      // No valid placement (shouldn't happen since s ≤ width). Stack
      // at the current ceiling so nothing overlaps.
      const maxY = skyline.reduce((m, seg) => Math.max(m, seg.y), 0);
      bestX = 0; bestY = maxY;
    }
    it.x = bestX; it.y = bestY;

    // Update skyline: the placed tile + a trailing gap occupy
    // [bestX, bestX + s + gap) horizontally, with upper boundary
    // bestY + s + gap (reserves vertical room for a next tile below).
    const x1  = bestX;
    const x2  = Math.min(bestX + s + gap, width);
    const newY = bestY + s + gap;
    const next = [];
    for (const seg of skyline) {
      const segEnd = seg.x + seg.w;
      if (segEnd <= x1 || seg.x >= x2) {
        next.push(seg);
      } else {
        if (seg.x < x1)  next.push({ x: seg.x, w: x1 - seg.x,        y: seg.y });
        if (segEnd > x2) next.push({ x: x2,    w: segEnd - x2,       y: seg.y });
      }
    }
    next.push({ x: x1, w: x2 - x1, y: newY });
    next.sort((a, b) => a.x - b.x);
    // Coalesce adjacent same-y segments
    skyline = [];
    for (const seg of next) {
      const last = skyline[skyline.length - 1];
      if (last && last.y === seg.y && last.x + last.w === seg.x) {
        last.w += seg.w;
      } else {
        skyline.push({ ...seg });
      }
    }
  }

  // Right-compaction post-pass. Skyline-BL's "lowest y, leftmost x"
  // placement can leave secondary tiles isolated within a wider column
  // above them — e.g. WBD landing under APA's left edge while JBHT sits
  // 27px to the right under CSCO. For each tile (processed right-to-left
  // so the rightmost is settled before its leftward neighbor reads its
  // position), find the nearest vertically-overlapping tile to its right
  // and slide our tile rightward to gap distance from that neighbor.
  // Tiles with no right neighbor on the same horizontal band stay put —
  // we don't slide them to the wall because operator wants them anchored
  // to whatever column above them placed them there.
  const byXDesc = [...items].sort((a, b) => b.x - a.x);
  for (const it of byXDesc) {
    let nearestRight = Infinity;
    for (const other of items) {
      if (other === it) continue;
      if (other.x < it.x + it.side + 0.5) continue;  // not strictly to the right
      const vOverlap = !(other.y + other.side <= it.y || other.y >= it.y + it.side);
      if (!vOverlap) continue;
      if (other.x < nearestRight) nearestRight = other.x;
    }
    if (nearestRight < Infinity) {
      const newX = nearestRight - gap - it.side;
      if (newX > it.x) it.x = newX;
    }
  }

  for (const it of items) {
    it.el.style.left = (padding + it.x) + 'px';
    it.el.style.top  = (padding + it.y) + 'px';
  }
  // Total content height = highest skyline y minus the trailing gap that
  // the bottommost tile's update added (we don't need padding below the
  // last tile beyond the container's own bottom padding).
  const maxY = skyline.reduce((m, seg) => Math.max(m, seg.y), 0);
  const total = Math.max(maxY - gap, 0) + padding * 2;
  container.style.height = total + 'px';
}

// Browser-side mirror of src/channels/api/positions_grouped.js#groupByStrategy.
// The Node-side helper still serves /api/portfolio/positions?group_by=strategy
// and the unit tests; this client-side twin exists so DOM renderers can share
// the same bucketing/subtotal contract without duplicating it inline. Keep
// the two in lock-step: null/missing strategy_id collapses to '(unattributed)',
// subtotal sums Number(r.day_pnl_usd) || 0, input order preserved per group.
function _groupByStrategyClient(rows) {
  const buckets = new Map();
  for (const r of rows) {
    const key = r.strategy_id || '(unattributed)';
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(r);
  }
  const out = [];
  for (const [strategy_id, positions] of buckets) {
    const subtotal = positions.reduce((s, r) => s + (Number(r.day_pnl_usd) || 0), 0);
    out.push({ strategy_id, positions, subtotal_day_pnl_usd: subtotal });
  }
  return out;
}

// "By strategy" view of the active positions table (Phase 1E — concept lifted
// from achannarasappa/ticker AssetGroup primitive). Renders a section header
// per strategy_id with a per-row table beneath it. Subtotal is the sum of
// day_pnl_usd, computed server-side via positions_grouped#computeDayPnlUsd
// (Phase 1 follow-up #11) — rows whose mark history is too short or whose
// position_size_pct is NaN ship as null and route through the em-dash
// fallback so a brand-new position does not fake a real $0 print.
function _renderPositionsByStrategy(el, rows) {
  const groups = _groupByStrategyClient(rows);
  const countEl = document.getElementById('pf-pos-count');
  if (countEl) {
    countEl.textContent = rows.length
      ? \`\${groups.length} \${groups.length === 1 ? 'strategy' : 'strategies'} · \${rows.length} position\${rows.length === 1 ? '' : 's'}\`
      : '';
  }
  if (!groups.length) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  const headers = \`<thead><tr>
      <th>Ticker</th><th>Dir</th>
      <th class="num">Entry</th><th class="num">Current</th>
      <th class="num">Size %</th><th class="num">P&amp;L %</th>
      <th class="num">Day $</th>
      <th class="num">Days</th>
    </tr></thead>\`;
  const html = groups.map(g => {
    // Em-dash when EVERY row has missing day_pnl_usd — matches the rest of the
    // dashboard's missing-data convention so the operator doesn't misread a
    // silent zero as a real flat-pnl day. With the server-side computation in
    // place this only fires when an entire strategy has only fresh-today
    // positions (no prev mark) or the NAV fetch failed.
    const allMissing = g.positions.every(r =>
      r.day_pnl_usd == null || Number.isNaN(Number(r.day_pnl_usd))
    );
    const subFmt = allMissing ? '—' : _fmtDollar(g.subtotal_day_pnl_usd, true);
    const header = \`<div class="pf-strategy-group-header">
        <span class="pf-sg-name">\${g.strategy_id}</span>
        <span class="pf-sg-meta">\${g.positions.length} position\${g.positions.length === 1 ? '' : 's'} · day P&amp;L \${subFmt}</span>
      </div>\`;
    const body = g.positions.map(p => {
      const pnl = p.unrealized_pnl_pct != null ? Number(p.unrealized_pnl_pct) * 100 : null;
      const dayUsd = p.day_pnl_usd == null || Number.isNaN(Number(p.day_pnl_usd))
        ? null : Number(p.day_pnl_usd);
      return \`<tr>
        <td><span class="tk-name" style="color:var(--blue);cursor:pointer" onclick="showMarket();selectTicker('\${p.ticker}')">\${p.ticker}</span></td>
        <td class="\${_dirCls(p.direction)}">\${p.direction || '—'}</td>
        <td class="num">\${p.entry_price != null ? '$' + Number(p.entry_price).toFixed(2) : '—'}</td>
        <td class="num">\${p.current_price != null ? '$' + Number(p.current_price).toFixed(2) : '—'}</td>
        <td class="num">\${p.position_size_pct != null ? (Number(p.position_size_pct) * 100).toFixed(2) + '%' : '—'}</td>
        <td class="num \${pnlCls(pnl)}">\${_fmtPctSigned(pnl, true)}</td>
        <td class="num \${dayUsd == null ? '' : pnlCls(dayUsd)}">\${dayUsd == null ? '—' : _fmtDollar(dayUsd, true)}</td>
        <td class="num">\${p.days_held != null ? p.days_held : '—'}</td>
      </tr>\`;
    }).join('');
    return header + \`<table class="db-table" style="min-width:560px">\${headers}<tbody>\${body}</tbody></table>\`;
  }).join('');
  el.innerHTML = html;
}

// ── Risk & Sizing panel ────────────────────────────────────────────────────
// Single most-consequential operator-tunable surface: global λ + per-regime
// liquidity_param + safety floors. Daily target gross exposure =
// λ × liquidity_param[current_regime] × NAV. Full-width slider with
// risk-preference framing (Conservative ↔ Balanced ↔ Aggressive); regime
// cards underneath show effective leverage live.
let _riskCfgCache = null;
function renderRiskPanel(cfg, grossLev) {
  const el = document.getElementById('pf-risk-panel');
  if (!el) return;
  if (cfg && cfg.global) _riskCfgCache = cfg;
  cfg = _riskCfgCache;
  if (!cfg || !cfg.global) {
    el.innerHTML = '<div class="empty">Risk config unavailable — DB may be down.</div>';
    return;
  }
  const lam = cfg.global.lambda;
  const stampEl = document.getElementById('pf-risk-saved-stamp');
  if (stampEl) stampEl.textContent = cfg.global.updated_at
    ? 'Leverage saved ' + new Date(cfg.global.updated_at).toLocaleString('en-US', {month:'numeric',day:'numeric',hour:'numeric',minute:'2-digit'})
    : '';
  // 2026-05-19: Daily slider cap dropped from 3.00 → 2.00 (Reg T overnight
  // max). Tick set now ends at the cap so labels don't render off-track.
  const ticks = [
    { v: 0.25, label: 'Defensive' },
    { v: 0.50, label: 'Conservative' },
    { v: 1.00, label: 'Cautious' },
    { v: 1.50, label: 'Balanced' },
    { v: 2.00, label: 'Reg T Max' },
  ];
  const currentRegime = (cfg.current_regime || '').toUpperCase();
  const current = (cfg.regimes || []).find(r => r.state === currentRegime);
  const currentEff = current ? lam * current.liquidity_param : lam;
  // Position each tick at its true location along the slider track
  // (left% = (v - min) / (max - min)). Without this the ticks bunch at
  // the edges because flex space-between maps tick #1 to 0% regardless
  // of value.
  const lmin = cfg.global.min, lmax = cfg.global.max;
  const tickHtml = ticks.map(t => {
    const pct = ((t.v - lmin) / (lmax - lmin)) * 100;
    return \`<span class="pf-tick" data-v="\${t.v}" style="left:\${pct.toFixed(2)}%"><span class="pf-tick-val">\${t.v.toFixed(2)}×</span><span class="pf-tick-label">\${t.label}</span></span>\`;
  }).join('');
  const regimeCards = (cfg.regimes || []).map(r => {
    const active = r.state === currentRegime;
    const eff = lam * r.liquidity_param;
    const css = r.state.toLowerCase();
    return \`<div class="pf-regime-card \${active ? 'regime-active' : ''}" data-regime="\${r.state}">
      <div class="pf-regime-card-head">
        <span class="pf-regime-name \${css}">\${r.state.replace('_', ' ')}</span>
        \${active ? '<span class="pf-regime-active-badge">LIVE</span>' : ''}
      </div>
      <div class="pf-regime-eff" id="pf-eff-\${r.state}">\${eff.toFixed(2)}×</div>
      <div class="pf-regime-eff-label">target leverage</div>
      <div class="pf-regime-eff-formula">= Leverage × <span id="pf-liq-display-\${r.state}">\${r.liquidity_param.toFixed(2)}</span></div>
      <div class="pf-regime-param-row">
        <span class="pf-regime-param-label-block">
          <span class="pf-regime-param-label">liquidity_param</span>
          <span class="pf-regime-param-hint">Throttle — fraction of global Leverage that actually deploys in this regime.</span>
        </span>
        <span><input type="number" class="pf-regime-param-input" data-regime="\${r.state}" data-field="liquidity_param" min="0" max="1" step="0.05" value="\${r.liquidity_param.toFixed(2)}" /></span>
      </div>
      <div class="pf-regime-param-row">
        <span class="pf-regime-param-label-block">
          <span class="pf-regime-param-label">circuit_breaker</span>
          <span class="pf-regime-param-hint">Auto-liquidate a position once its unrealized loss exceeds this % of NAV.</span>
        </span>
        <span><input type="number" class="pf-regime-param-input" data-regime="\${r.state}" data-field="position_circuit_breaker_pct" data-percent="1" min="0" max="50" step="0.1" value="\${(r.position_circuit_breaker_pct * 100).toFixed(1)}" /><span class="pf-regime-param-suffix">%</span></span>
      </div>
    </div>\`;
  }).join('');
  // Risk switches (operator 2026-07-27: only the LEVERAGE slider remains a
  // slider — the corr de-gross and the net-exposure cap are switch+value
  // rows, all consumed by the sizer from pipeline_config every cycle).
  const acThr      = (cfg.global.asset_corr_thr != null) ? cfg.global.asset_corr_thr : 0.70;
  const acThrMin   = (cfg.global.asset_corr_thr_min != null) ? cfg.global.asset_corr_thr_min : 0.50;
  const acThrMax   = (cfg.global.asset_corr_thr_max != null) ? cfg.global.asset_corr_thr_max : 0.90;
  const acEnabled  = !!cfg.global.asset_corr_enabled;
  const acPct      = (cfg.global.asset_corr_cap_pct != null) ? cfg.global.asset_corr_cap_pct : 0.20;
  const ncEnabled  = !!cfg.global.net_cap_enabled;
  const ncFrac     = (cfg.global.max_net_frac != null) ? cfg.global.max_net_frac : 0.6;
  // Side-by-side switch cards (operator 2026-07-27): pill toggle top-right,
  // hint prose, value inputs on a footer row. Card dims when OFF.
  const _switchCard = (id, label, hint, enabled, valueCells) => \`
    <div class="pf-risk-switch-card \${enabled ? '' : 'pf-off'}" id="\${id}-card">
      <div class="pf-rsw-head">
        <span class="pf-rsw-title">\${label}
          <span class="pf-rsw-state \${enabled ? 'on' : 'off'}">\${enabled ? 'LIVE' : 'OFF'}</span>
        </span>
        <label class="pf-toggle" title="\${enabled ? 'Click to disable' : 'Click to enable'} — applies next sizer cycle">
          <input type="checkbox" id="\${id}-switch" \${enabled ? 'checked' : ''} />
          <span class="pf-toggle-track"><span class="pf-toggle-knob"></span></span>
        </label>
      </div>
      <div class="pf-rsw-hint">\${hint}</div>
      <div class="pf-rsw-vals">\${valueCells}</div>
    </div>\`;
  const acCells = \`
    <span class="pf-rsw-val"><span class="pf-rsw-val-label">Pearson thr</span>
      <input type="number" class="pf-regime-param-input" id="pf-ac-thr" min="\${acThrMin}" max="\${acThrMax}" step="0.05" value="\${acThr.toFixed(2)}" /></span>
    <span class="pf-rsw-val"><span class="pf-rsw-val-label">cluster cap ×NAV</span>
      <input type="number" class="pf-regime-param-input" id="pf-ac-pct" min="0.05" max="0.50" step="0.05" value="\${acPct.toFixed(2)}" /></span>\`;
  const ncCells = \`
    <span class="pf-rsw-val"><span class="pf-rsw-val-label">max |net| ÷ gross</span>
      <input type="number" class="pf-regime-param-input" id="pf-nc-frac" min="0.10" max="1.00" step="0.05" value="\${ncFrac.toFixed(2)}" /></span>\`;

  el.innerHTML = \`<div class="pf-risk-panel">
    <div class="pf-risk-headline">
      <div class="pf-risk-title">Risk &amp; Sizing</div>
      <div class="pf-risk-help"><strong>Daily (Reg T)</strong> controls today's daily/multi-day process: gross exposure = <code>Leverage × regime.liquidity_param × NAV</code>, hard-capped at 2× per Reg T overnight rules. <strong>Asset-Corr De-Gross</strong> caps each cluster of correlated same-direction positions at <code>cap</code> × NAV (thr = Pearson clustering cutoff; lower clusters harder — 0.60 catches the semi sector). <strong>Net-Exposure Cap</strong> de-levers the heavy side so |net| ≤ <code>max net</code> × gross (an all-long book scales to that fraction). Switches + values apply next sizer cycle.</div>
    </div>

    <div class="pf-lambda-block">
      <div class="pf-lambda-block-head"><span class="pf-lr-label">Daily Leverage (Reg T)</span><span class="pf-lr-cap">cap \${cfg.global.max.toFixed(2)}× · active</span></div>
      <div class="pf-lambda-mega">
        <input type="range" id="pf-lambda-slider" min="\${cfg.global.min}" max="\${cfg.global.max}" step="0.05" value="\${lam.toFixed(2)}" />
        <div class="pf-lambda-ticks">\${tickHtml}</div>
      </div>
      <div class="pf-lambda-readout">
        <span class="pf-lr-value" id="pf-lambda-readout">\${lam.toFixed(2)}×</span>
        <span class="pf-lr-sub">→ target in <strong style="color:var(--text)">\${currentRegime.replace('_',' ') || '—'}</strong>: <span id="pf-current-eff" style="color:var(--blue);font-weight:700">\${currentEff.toFixed(2)}×</span> NAV · <span title="Gross broker exposure (|long|+|short|)/equity from last account refresh" style="color:var(--muted)">actual <strong style="color:var(--text)">\${grossLev != null ? grossLev.toFixed(2) + '×' : '—'}</strong></span></span>
      </div>
    </div>

    <div class="pf-risk-switch-grid">
      \${_switchCard('pf-ac', 'Asset-Corr De-Gross', 'Caps each cluster of correlated same-direction positions at <b>cluster cap</b> × NAV and releases the rest. Lower threshold clusters harder — 0.60 catches the semi sector, 0.70 mostly ETF/market-beta.', acEnabled, acCells)}
      \${_switchCard('pf-nc', 'Net-Exposure Cap', 'De-levers the heavy side so |net| ≤ <b>max net</b> × intended gross — an all-long book scales down to that fraction. The June one-way-momentum guardrail; parked OFF 2026-07-27.', ncEnabled, ncCells)}
    </div>

    <div class="pf-regime-grid" style="margin-top:14px">\${regimeCards}</div>
  </div>\`;

  // Risk switch + value wiring — switches PUT immediately; numeric values
  // debounce 500ms. All land in pipeline_config; sizer picks up next cycle.
  const _putRisk = async (payload, okMsg) => {
    try {
      const r = await fetch('/api/config/risk-toggles', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (r.ok) toast(okMsg + ' — next cycle picks it up', 'ok', 3500);
      else toast('Risk config update failed', 'error', 4000);
      return r.ok;
    } catch (e) { toast('Risk config error: ' + e.message, 'error', 4000); return false; }
  };
  const _wireSwitch = (id, key, cfgSync) => {
    const sw = el.querySelector('#' + id + '-switch');
    if (!sw) return;
    sw.addEventListener('change', async () => {
      const on = sw.checked;
      const ok = await _putRisk({ [key]: on }, (on ? 'Enabled ' : 'Disabled ') + key.replace(/_/g, ' '));
      if (ok) { cfgSync(on); renderRiskPanel(cfg, grossLev); }
      else sw.checked = !on;
    });
  };
  _wireSwitch('pf-ac', 'asset_corr_cap_enabled', on => { cfg.global.asset_corr_enabled = on; });
  _wireSwitch('pf-nc', 'net_exposure_cap_enabled', on => { cfg.global.net_cap_enabled = on; });
  const _wireNum = (id, key, cfgKey, viaThrEndpoint) => {
    const input = el.querySelector('#' + id);
    if (!input) return;
    let t = null;
    input.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(async () => {
        const v = parseFloat(input.value);
        if (!isFinite(v)) return;
        let ok;
        if (viaThrEndpoint) {
          try {
            const r = await fetch('/api/config/asset-corr-thr', {
              method: 'PUT', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ value: v }) });
            ok = r.ok;
            toast(ok ? 'De-gross threshold saved: ' + v.toFixed(2) + ' — next cycle picks it up'
                     : 'De-gross threshold update failed', ok ? 'ok' : 'error', 3500);
          } catch (e) { toast('De-gross threshold error: ' + e.message, 'error', 4000); ok = false; }
        } else {
          ok = await _putRisk({ [key]: v }, key.replace(/_/g, ' ') + ' saved: ' + v.toFixed(2));
        }
        if (ok) cfg.global[cfgKey] = v;
      }, 500);
    });
  };
  _wireNum('pf-ac-thr', 'asset_corr_cap_thr', 'asset_corr_thr', true);
  _wireNum('pf-ac-pct', 'asset_corr_cap_pct', 'asset_corr_cap_pct', false);
  _wireNum('pf-nc-frac', 'max_net_frac', 'max_net_frac', false);

  // Wire slider — drag with live readout, debounce 400ms before PUT.
  const slider   = el.querySelector('#pf-lambda-slider');
  const readout  = el.querySelector('#pf-lambda-readout');
  const effReadout = el.querySelector('#pf-current-eff');
  const _updateEffective = (newLam) => {
    (cfg.regimes || []).forEach(r => {
      const card = el.querySelector(\`[data-regime="\${r.state}"].pf-regime-card\`);
      if (!card) return;
      const liqInput = card.querySelector('[data-field="liquidity_param"]');
      const liq = liqInput ? parseFloat(liqInput.value) : r.liquidity_param;
      const eff = newLam * liq;
      const effEl = document.getElementById('pf-eff-' + r.state);
      if (effEl) effEl.textContent = eff.toFixed(2) + '×';
    });
    const cur = (cfg.regimes || []).find(r => r.state === currentRegime);
    if (cur && effReadout) {
      const liqInput = el.querySelector(\`[data-regime="\${currentRegime}"] [data-field="liquidity_param"]\`);
      const liq = liqInput ? parseFloat(liqInput.value) : cur.liquidity_param;
      effReadout.textContent = (newLam * liq).toFixed(2) + '×';
    }
  };
  if (slider && readout) {
    let timer = null;
    slider.addEventListener('input', () => {
      const v = parseFloat(slider.value);
      readout.textContent = v.toFixed(2) + '×';
      _updateEffective(v);
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          const r = await fetch('/api/config/lambda', {
            method: 'PUT', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ value: v }),
          });
          if (r.ok) {
            _lambdaCache = v;
            cfg.global.lambda = v;
            cfg.global.updated_at = new Date().toISOString();
            if (stampEl) stampEl.textContent = 'Leverage saved just now';
            toast('Daily leverage saved: ' + v.toFixed(2) + '× — next cycle picks up the new value', 'ok', 4000);
          } else {
            toast('Daily leverage update failed', 'error', 4000);
          }
        } catch (e) {
          toast('Daily leverage update error: ' + e.message, 'error', 4000);
        }
      }, 400);
    });
  }

  // (Asset-corr threshold slider removed 2026-07-27 — thr/cap are numeric
  // inputs on the switch rows above; only the leverage slider remains.)

  // Click-to-jump on tick labels.
  el.querySelectorAll('.pf-lambda-ticks .pf-tick').forEach(tick => {
    tick.addEventListener('click', () => {
      const v = parseFloat(tick.dataset.v);
      if (!isFinite(v) || !slider) return;
      slider.value = String(v);
      slider.dispatchEvent(new Event('input', { bubbles: true }));
    });
  });

  // Wire regime param inputs — each input is independent; blur or
  // Enter triggers PUT for that single field.
  el.querySelectorAll('.pf-regime-param-input').forEach(input => {
    const orig = input.value;
    input.addEventListener('input', () => {
      input.classList.toggle('dirty', input.value !== orig);
      // Re-compute effective leverage live as liquidity_param changes.
      if (input.dataset.field === 'liquidity_param') {
        const lamNow = parseFloat(slider ? slider.value : lam);
        _updateEffective(lamNow);
        const liqDisp = document.getElementById('pf-liq-display-' + input.dataset.regime);
        if (liqDisp) liqDisp.textContent = parseFloat(input.value || '0').toFixed(2);
      }
    });
    const commit = async () => {
      const raw = parseFloat(input.value);
      if (!isFinite(raw) || input.value === orig) {
        input.classList.remove('dirty');
        return;
      }
      // Inputs marked data-percent="1" are displayed in % but stored as a
      // decimal in the DB (2.0% in UI → 0.02 on the wire).
      const v = input.dataset.percent === '1' ? raw / 100 : raw;
      input.classList.remove('dirty');
      input.classList.add('saving');
      try {
        const r = await fetch('/api/config/regime-sizing/' + input.dataset.regime, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ [input.dataset.field]: v }),
        });
        if (r.ok) {
          input.classList.remove('saving');
          input.defaultValue = String(raw);
          const human = input.dataset.percent === '1' ? raw.toFixed(2) + '%' : String(raw);
          toast(input.dataset.regime + '.' + input.dataset.field + ' saved: ' + human, 'ok', 3000);
        } else {
          input.classList.remove('saving');
          input.classList.add('dirty');
          const j = await r.json().catch(() => ({}));
          toast('Save failed: ' + (j.error || r.statusText), 'error', 4000);
        }
      } catch (e) {
        input.classList.remove('saving');
        input.classList.add('dirty');
        toast('Save error: ' + e.message, 'error', 4000);
      }
    };
    input.addEventListener('blur', commit);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { input.blur(); } });
  });
}

function renderPositions(rows) {
  const el = document.getElementById('pf-positions');
  if (rows.length && rows[0] && Array.isArray(rows[0].signals)) {
    rows = rows.flatMap(g => g.signals);
  }
  _rawDataCache['pf-positions'] = rows;
  const toggle = document.getElementById('pf-pos-group-toggle');
  // Wire the toggle once — onchange just re-renders from the cache.
  if (toggle && !toggle.dataset.bound) {
    toggle.dataset.bound = '1';
    toggle.addEventListener('change', () => renderPositions(_rawDataCache['pf-positions'] || []));
  }
  if (toggle && toggle.value === 'strategy') {
    return _renderPositionsByStrategy(el, rows);
  }
  const groups = _groupByTicker(rows, 'open');
  // Count = actual broker positions (consolidated per ticker), NOT the
  // per-strategy intent rows. A 56-position book with ~6 strategies per
  // ticker has ~343 intent rows behind it; showing "343 positions" is
  // misleading — the operator wants the broker-truth count.
  const longCnt  = groups.filter(g => g.broker && g.broker.side === 'long').length;
  const shortCnt = groups.filter(g => g.broker && g.broker.side === 'short').length;
  document.getElementById('pf-pos-count').textContent =
    groups.length ? \`\${groups.length} open · \${longCnt} long · \${shortCnt} short\` : '';
  if (!groups.length) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  const selectedTicker = _heatmapSelected['pf-positions'] || null;
  // Re-validate selection — if the operator's previously-selected ticker
  // dropped out of the recent slice (closed, fell past the 90d window),
  // clear it instead of rendering a stale bar chart.
  const selectedGroup = selectedTicker ? groups.find(g => g.ticker === selectedTicker) : null;
  if (!selectedGroup) _heatmapSelected['pf-positions'] = null;

  const twocol = _buildPositionsTwoColumnHtml(groups, selectedGroup ? selectedGroup.ticker : null, _navCache);
  const bars   = selectedGroup ? _buildAlphaBarsHtml(selectedGroup) : '';
  el.innerHTML = twocol + bars;

  // Populate each treemap container now that its width is measurable.
  // Re-run on resize so the layout stays squarified at any column width.
  el.querySelectorAll('.pf-poscol-tm').forEach(tm => {
    const label = tm.dataset.side;
    _populateTreemap(tm, label);
    if (!tm._tmResizeObserver) {
      let lastW = tm.clientWidth, lastH = tm.clientHeight;
      tm._tmResizeObserver = new ResizeObserver(() => {
        if (tm.clientWidth !== lastW || tm.clientHeight !== lastH) {
          lastW = tm.clientWidth; lastH = tm.clientHeight;
          _populateTreemap(tm, label);
          _attachTileClickHandlers(el);
        }
      });
      tm._tmResizeObserver.observe(tm);
    }
  });
  _attachTileClickHandlers(el);
}

// Tile click → toggle per-ticker selection → fetch alpha decomposition
// → re-render to surface the bars panel below. Factored out so it can
// be re-attached after a ResizeObserver-triggered treemap re-layout.
function _attachTileClickHandlers(el) {
  el.querySelectorAll('.pf-postile').forEach(tile => {
    if (tile._tileClickBound) return;
    tile._tileClickBound = true;
    tile.addEventListener('click', () => {
      const tk = tile.dataset.ticker;
      const cur = _heatmapSelected['pf-positions'];
      if (cur === tk) {
        _heatmapSelected['pf-positions'] = null;
        renderPositions(_rawDataCache['pf-positions']);
        return;
      }
      _heatmapSelected['pf-positions'] = tk;
      // Bars come straight from the group's own signals (synchronous) — a
      // single render surfaces them; no fetch/second render needed.
      renderPositions(_rawDataCache['pf-positions']);
    });
  });
}

function renderHistory(rows) {
  const el = document.getElementById('pf-history');
  if (rows.length && rows[0] && Array.isArray(rows[0].signals)) {
    rows = rows.flatMap(g => g.signals);
  }
  _rawDataCache['pf-history'] = rows;
  const groups = _groupByTicker(rows, 'closed');
  document.getElementById('pf-hist-count').textContent =
    rows.length ? \`\${groups.length} ticker\${groups.length === 1 ? '' : 's'} · \${rows.length} trade\${rows.length === 1 ? '' : 's'}\` : '';
  if (!groups.length) { el.innerHTML = '<div class="empty">No closed trades yet</div>'; return; }
  if (!_sortState['pf-history'] || !_sortState['pf-history'].key) {
    groups.sort((a, b) => (b.last_closed || 0) - (a.last_closed || 0));
  }
  const sorted = _applySort('pf-history', groups);
  const { shown, footer } = _collapseRows('pf-history', sorted);
  const headers = \`<thead><tr>
      <th data-sort-key="ticker" data-sort-type="str">Ticker</th>
      <th class="num" data-sort-key="n" data-sort-type="num">#</th>
      <th data-sort-key="net_dir" data-sort-type="str">Dir</th>
      <th class="num" data-sort-key="avg_entry" data-sort-type="num">Entry</th>
      <th class="num" data-sort-key="price" data-sort-type="num">Close</th>
      <th class="num" data-sort-key="net_pnl" data-sort-type="num">P&amp;L %</th>
      <th class="num" data-sort-key="contrib_pct" data-sort-type="num" title="Contribution to NAV = Σ(P&amp;L% × Size%). Signed.">Contrib %</th>
      <th class="num" data-sort-key="avg_days" data-sort-type="num">Days</th>
      <th data-sort-key="last_closed" data-sort-type="num">Closed</th>
    </tr></thead>\`;
  const body = shown.map(g => {
    const expanded = _isExpanded('pf-history', g.ticker);
    const netPnl = g.net_pnl != null ? g.net_pnl * 100 : null;
    const contrib = g.contrib_pct != null ? g.contrib_pct * 100 : null;
    const chev = expanded ? '▾' : '▸';
    const lc = g.last_closed ? new Date(g.last_closed).toLocaleDateString('en-US',{month:'numeric',day:'numeric',year:'2-digit'}) : '—';
    const wlBadge = g.n > 0 ? \`<span class="wl-badge" title="\${g.wins}W / \${g.losses}L">\${g.wins}W·\${g.losses}L</span>\` : '';
    const parent = \`<tr class="ticker-row \${expanded ? 'expanded' : ''}" data-ticker="\${g.ticker}">
        <td class="tk-cell">
          <span class="chev">\${chev}</span>
          <span class="tk-name" onclick="event.stopPropagation();showMarket();selectTicker('\${g.ticker}')">\${g.ticker}</span>
        </td>
        <td class="num">\${g.n} \${wlBadge}</td>
        <td class="\${_dirCls(g.net_dir)}">\${g.net_dir}</td>
        <td class="num">\${g.avg_entry != null ? '$' + g.avg_entry.toFixed(2) : '—'}</td>
        <td class="num">\${g.price != null ? '$' + g.price.toFixed(2) : '—'}</td>
        <td class="num \${pnlCls(netPnl)}">\${_fmtPctSigned(netPnl, true)}</td>
        <td class="num \${pnlCls(contrib)}">\${_fmtPctSigned(contrib, true)}</td>
        <td class="num">\${g.avg_days != null ? g.avg_days.toFixed(0) : '—'}</td>
        <td style="color:var(--dim)">\${lc}</td>
      </tr>\`;
    // Expanded sub-row: inline alpha-bar chart inside a 9-column-span cell.
    // Replaces the older per-strategy detail table.
    const barsCell = expanded
      ? \`<tr class="bars-row"><td colspan="9" class="bars-cell">\${_buildAlphaBarsHtml(g)}</td></tr>\`
      : '';
    return \`<tbody class="tk-group">\${parent}\${barsCell}</tbody>\`;
  }).join('');
  el.innerHTML = \`<table class="db-table grouped" style="min-width:680px">\${headers}\${body}</table>\${footer}\`;
  _bindGroupClicks('pf-history', renderHistory);
  _bindSortable('pf-history', renderHistory);
  _bindCollapse('pf-history', renderHistory);
}

// Click a ticker row → toggle its child strat-rows. The click target is the
// chevron + ticker name area only — clicking the ticker text itself drills
// into the Market view (stopPropagation guards that).
function _bindGroupClicks(tableId, renderFn) {
  const host = document.getElementById(tableId);
  if (!host) return;
  host.querySelectorAll('tr.ticker-row').forEach(tr => {
    tr.style.cursor = 'pointer';
    tr.addEventListener('click', () => {
      const tk = tr.dataset.ticker;
      if (!tk) return;
      // _toggleExpanded re-renders; the expanded row's bars come from the
      // group's own signals (synchronous), so no fetch/second render needed.
      _toggleExpanded(tableId, tk, renderFn);
    });
  });
}

// ── Strategies page ─────────────────────────────────────────────────────────
// 3-section layout:
//   Active Stack        = state ∈ {live, monitoring}; sub-status Live/Stale/Waiting
//                           Live    — regime_active + last_signal ≤ 7d
//                           Stale   — regime_active + no signal > 7d
//                           Waiting — !regime_active (regime conditions not met)
//   Inactive Stack      = state ∈ {deprecated, archived, orphan} — historical metrics
//   Research Candidates = state ∈ {paper, candidate} — backtest metrics, Approve/Reject
let strategiesData = [];
// Current-regime strategy-similarity matrix (sizer's S_adj store) — powers the
// expanded panel's pairwise-ρ ordering of Similar Active Strategies.
let _stSimMatrix = {};
let _stSimRegime = null;
let _stActiveJobs = {};   // strategy_id → {job_id, phase, progress, payload}
let _stLastFailures = {}; // strategy_id → {job_id, reason}  (persisted server-side; survives reload + restart)
let _stDataUsage = null; // {parameters, strategies_total, categories_total}

// Per-regime conviction-gate slider state. Debounced PUTs avoid spamming
// the API while the operator drags; one in-flight PUT per regime is
// enough since the server's UPDATE is idempotent on regime_state.
const _sharpeGateDebounce = {};   // { LOW_VOL: timeoutId, ... }
const _sharpeGateInflight = {};   // { LOW_VOL: bool }

async function _loadSharpeGates() {
  const host = document.getElementById('st-sharpe-gates');
  if (!host) return;
  let cfg;
  try {
    cfg = await fetch('/api/config/regime-sizing').then(r => r.json());
  } catch (e) {
    host.innerHTML = '<div class="st-sharpe-card-empty">Failed to load gate config</div>';
    return;
  }
  const regimes = Array.isArray(cfg.regimes) ? cfg.regimes : [];
  const current = (cfg.current_regime || '').toUpperCase();
  if (!regimes.length) {
    host.innerHTML = '<div class="st-sharpe-card-empty">No regime sizing rows in DB</div>';
    return;
  }
  host.innerHTML = regimes.map(r => {
    const vc = (r.min_acting_strategies != null && isFinite(r.min_acting_strategies))
      ? Math.max(1, Math.min(10, Math.round(Number(r.min_acting_strategies)))) : 1;
    const tag = r.state === current ? '<span class="st-sharpe-card-tag">CURRENT</span>' : '';
    const klass = 'st-sharpe-card' + (r.state === current ? ' current-regime' : '');
    return \`<div class="\${klass}" data-regime="\${r.state}">
      <div class="st-sharpe-card-head">
        <span class="st-sharpe-card-regime">\${r.state}</span>
        \${tag}
      </div>
      <div class="st-sharpe-card-value" id="st-cg-val-\${r.state}" title="minimum distinct strategies acting in the net direction">\${vc} <span class="st-sub-label">acting</span></div>
      <input type="range" class="st-sharpe-card-slider" id="st-cg-slider-\${r.state}"
             min="1" max="10" step="1" value="\${vc}" data-regime="\${r.state}" />
      <div class="st-sharpe-card-range"><span>1 · any</span><span>10 · consensus</span></div>
      <div class="st-sharpe-card-status" id="st-cg-status-\${r.state}"></div>
    </div>\`;
  }).join('');
  // Wire each acting-strategy slider — live label update on input, debounced
  // PUT (min_acting_strategies) on change. 300ms is short enough to feel
  // responsive without firing on every pixel of drag.
  for (const r of regimes) {
    const cslider = document.getElementById('st-cg-slider-' + r.state);
    const cvalEl  = document.getElementById('st-cg-val-' + r.state);
    const cstatEl = document.getElementById('st-cg-status-' + r.state);
    if (!cslider || !cvalEl) continue;
    cslider.addEventListener('input', () => {
      cvalEl.innerHTML = parseInt(cslider.value, 10) + ' <span class="st-sub-label">acting</span>';
    });
    cslider.addEventListener('change', () => {
      if (_sharpeGateDebounce['corr_' + r.state]) clearTimeout(_sharpeGateDebounce['corr_' + r.state]);
      _sharpeGateDebounce['corr_' + r.state] = setTimeout(
        () => _corrSharpeGatePut(r.state, parseInt(cslider.value, 10), cstatEl), 300);
    });
  }
}

async function _corrSharpeGatePut(regime, value, statEl) {
  if (_sharpeGateInflight['corr_' + regime]) return;
  _sharpeGateInflight['corr_' + regime] = true;
  if (statEl) statEl.textContent = 'saving…';
  try {
    const resp = await fetch('/api/config/regime-sizing/' + encodeURIComponent(regime), {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ min_acting_strategies: value }),
    });
    _noteDashboardBuild(resp);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (statEl) statEl.textContent = '✗ ' + (err.error || resp.statusText);
    } else if (statEl) {
      statEl.textContent = '✓ saved · next cycle requires ≥' + value + ' acting strateg' + (value === 1 ? 'y' : 'ies');
      setTimeout(() => { if (statEl) statEl.textContent = ''; }, 4000);
    }
  } catch (e) {
    if (statEl) statEl.textContent = '✗ ' + (e.message || 'network error');
  } finally {
    _sharpeGateInflight['corr_' + regime] = false;
  }
}

// ── Strategy Activation slider + dry-run preview ────────────────────────────
// Slider binds pipeline_config.strategy_activation_min_sharpe (debounced PUT,
// same UX as the conviction gates above). The Preview button POSTs
// /api/activation/dry-run — the server only ever shells the assigner with
// --dry-run (the LIVE apply is the daily-cycle 'activation' step at the next
// compute, plus the weekly Mon 00:00 ET run), and we render the
// parsed prior→new eligibility diff: per-regime "eligible now" is DB
// reality, everything else is hypothetical.
let _actSaveTimer   = null;
let _actPutInflight = false;
let _actWired       = false;

// ── Activation min-TRADES slider ────────────────────────────────────────────
// Sibling of the min-Sharpe slider: the per-regime SAMPLE-SIZE floor. Own
// timer/inflight/wired state so a save on one card can't cancel the other's.
let _actnSaveTimer   = null;
let _actnPutInflight = false;
let _actnWired       = false;

async function _loadActivationTradesCard() {
  const slider = document.getElementById('st-actn-slider');
  const valEl  = document.getElementById('st-actn-val');
  const statEl = document.getElementById('st-actn-status');
  if (!slider || !valEl) return;
  let cfg;
  try {
    const r = await fetch('/api/config/activation-min-trades');
    cfg = await r.json();
    if (!r.ok) throw new Error(cfg.error || r.statusText);
  } catch (e) {
    if (statEl) statEl.textContent = '✗ load failed: ' + (e.message || 'network error');
    return;
  }
  const v = Number.isFinite(Number(cfg.value)) ? Math.round(Number(cfg.value)) : 100;
  slider.value    = v;
  slider.disabled = false;
  valEl.textContent = String(v);
  if (statEl && !statEl.textContent) {
    // Name the live SOURCE. An unset key is not "0" — the assigner defers to the
    // class gate, and saying so prevents reading the slider as an active floor
    // that was never written.
    statEl.textContent = cfg.row_exists && cfg.source === 'pipeline_config'
      ? _actApplyStatus(cfg)
      : 'row not set — assigner defers to the class gate (100)';
  }
  if (_actnWired) return;
  _actnWired = true;
  slider.addEventListener('input', () => { valEl.textContent = String(parseInt(slider.value, 10)); });
  slider.addEventListener('change', () => {
    if (_actnSaveTimer) clearTimeout(_actnSaveTimer);
    _actnSaveTimer = setTimeout(() => {
      _actnSaveTimer = null;
      _actnPut(parseInt(slider.value, 10), statEl);
    }, 300);
  });
}

async function _actnPut(value, statEl) {
  if (_actnPutInflight) return;
  _actnPutInflight = true;
  if (statEl) statEl.textContent = 'saving…';
  try {
    const resp = await fetch('/api/config/activation-min-trades', {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ value }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (statEl) statEl.textContent = '✗ ' + (err.error || resp.statusText);
    } else if (statEl) {
      statEl.textContent = '✓ saved ' + value + ' · pending — applies at the next daily compute (15:00 ET)';
      _noteDashboardBuild(resp);
      setTimeout(() => { if (statEl.textContent.startsWith('✓')) statEl.textContent = ''; }, 6000);
    }
  } catch (e) {
    if (statEl) statEl.textContent = '✗ ' + (e.message || 'network error');
  } finally {
    _actnPutInflight = false;
  }
}

async function _loadActivationCard() {
  const slider = document.getElementById('st-act-slider');
  const valEl  = document.getElementById('st-act-val');
  const statEl = document.getElementById('st-act-status');
  if (!slider || !valEl) return;
  let cfg;
  try {
    const r = await fetch('/api/config/activation-min-sharpe');
    cfg = await r.json();
    if (!r.ok) throw new Error(cfg.error || r.statusText);
  } catch (e) {
    if (statEl) statEl.textContent = '✗ load failed: ' + (e.message || 'network error');
    return;
  }
  const v = (cfg.value != null && isFinite(cfg.value)) ? Number(cfg.value) : 0.5;
  slider.value    = v;
  slider.disabled = false;
  valEl.textContent = v.toFixed(2);
  if (statEl && !statEl.textContent) {
    statEl.textContent = cfg.row_exists
      ? _actApplyStatus(cfg)
      : 'row not set — assigner fail-safe default 0.50';
  }
  if (_actWired) return;   // re-loads refresh the value; listeners wire once
  _actWired = true;
  slider.addEventListener('input', () => {
    valEl.textContent = parseFloat(slider.value).toFixed(2);
  });
  slider.addEventListener('change', () => {
    if (_actSaveTimer) clearTimeout(_actSaveTimer);
    _actSaveTimer = setTimeout(() => {
      _actSaveTimer = null;
      _actPut(parseFloat(slider.value), statEl);
    }, 300);
  });
}

async function _actPut(value, statEl) {
  if (_actPutInflight) return;
  _actPutInflight = true;
  if (statEl) statEl.textContent = 'saving…';
  try {
    const resp = await fetch('/api/config/activation-min-sharpe', {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ value }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      if (statEl) statEl.textContent = '✗ ' + (err.error || resp.statusText);
    } else if (statEl) {
      statEl.textContent = '✓ saved ' + value.toFixed(2) + ' · pending — applies at the next daily compute (15:00 ET)';
      _noteDashboardBuild(resp);
      setTimeout(() => {
        if (statEl && statEl.textContent.indexOf('✓') === 0) statEl.textContent = '';
      }, 6000);
    }
  } catch (e) {
    if (statEl) statEl.textContent = '✗ ' + (e.message || 'network error');
  } finally {
    _actPutInflight = false;
  }
}

async function _actPreviewDryRun() {
  const btn = document.getElementById('st-act-preview-btn');
  const out = document.getElementById('st-act-preview-out');
  if (!btn || !out || btn.disabled) return;
  // Flush any pending debounced threshold save first — the assigner reads
  // pipeline_config fresh, so the preview must reflect the slider position.
  const slider = document.getElementById('st-act-slider');
  const statEl = document.getElementById('st-act-status');
  if (_actSaveTimer) {
    clearTimeout(_actSaveTimer);
    _actSaveTimer = null;
    if (slider && !slider.disabled) await _actPut(parseFloat(slider.value), statEl);
  }
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = 'running dry-run…';
  out.innerHTML = '<div class="st-act-preview-empty">Running assigner --dry-run over all strategies (nice -19, hard 120s cap)…</div>';
  try {
    const resp = await fetch('/api/activation/dry-run', { method: 'POST' });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      out.innerHTML = '<div class="st-act-preview-empty">✗ ' + escapeHtml(data.error || resp.statusText) +
        (data.stderr_tail ? '<br><span style="color:var(--dim)">' + escapeHtml(String(data.stderr_tail).slice(-300)) + '</span>' : '') +
        '</div>';
      return;
    }
    _actRenderPreview(data, out);
  } catch (e) {
    out.innerHTML = '<div class="st-act-preview-empty">✗ ' + escapeHtml(e.message || 'network error') + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

// Coerce a server-supplied count to a finite number for safe HTML
// interpolation (everything else goes through escapeHtml).
function _actNum(v, fallback) {
  const n = Number(v);
  return isFinite(n) ? n : fallback;
}

function _actRenderPreview(data, host) {
  const regs = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
  const pr = data.per_regime || {};
  const rows = regs.map(r => {
    const c = pr[r] || {};
    const act   = _actNum(c.activated, 0);
    const deact = _actNum(c.deactivated, 0);
    const eligNow  = _actNum(c.eligible_now, null);
    const eligPrev = _actNum(c.eligible_preview, null);
    return '<tr><td>' + r + '</td>' +
      '<td>' + (eligNow  != null ? eligNow  : '—') + '</td>' +
      '<td>' + (eligPrev != null ? eligPrev : '—') + '</td>' +
      '<td class="' + (act ? 'st-act-up' : '') + '">'   + (act   ? '+' + act : '0') + '</td>' +
      '<td class="' + (deact ? 'st-act-down' : '') + '">' + (deact ? '−' + deact : '0') + '</td></tr>';
  }).join('');
  const nd    = Array.isArray(data.newly_dormant) ? data.newly_dormant : [];
  const ndCnt = _actNum(data.newly_dormant_count, nd.length);
  const cells = data.cells || {};
  const errs  = _actNum(data.errors, 0);
  const warns = (data.warnings || []).length
    ? '<div class="st-act-preview-empty">⚠ ' + escapeHtml((data.warnings || []).join(' · ')) + '</div>' : '';
  host.innerHTML =
    '<div class="st-act-summary"><span class="st-act-hypo">Preview — hypothetical, nothing written</span>' +
      'threshold <b>' + (data.threshold != null ? _actNum(data.threshold, 0).toFixed(2) : '?') + '</b> · ' +
      _actNum(data.evaluated, 0) + ' evaluated · ' +
      _actNum(data.skipped, 0) + ' skipped (no corrected backtest) · ' +
      '<span style="color:var(--green)">' + _actNum(cells.activated, 0) + ' cells would activate</span> · ' +
      '<span style="color:var(--red)">' + _actNum(cells.deactivated, 0) + ' would deactivate</span> · ' +
      '<b>' + ndCnt + '</b> strateg' + (ndCnt === 1 ? 'y' : 'ies') + ' newly fully-dormant' +
      (errs ? ' · <span style="color:var(--red)">' + errs + ' assigner errors</span>' : '') +
      ' · ' + Math.round(_actNum(data.duration_ms, 0) / 1000) + 's</div>' +
    '<table class="st-act-table"><thead><tr>' +
      '<th>Regime</th><th>eligible now (DB reality)</th><th>eligible after (preview)</th>' +
      '<th>would activate</th><th>would deactivate</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>' +
    '<div class="st-sub-label">counts cover evaluated strategies only — skipped strategies keep their current rows untouched</div>' +
    (nd.length
      ? '<div class="st-sub-label">Newly fully-dormant (' + nd.length + ') — eligible somewhere today, would lose all 4 regimes:</div>' +
        '<div class="st-act-dormant">' + nd.map(s => '<span>' + escapeHtml(s) + '</span>').join('') + '</div>'
      : '<div class="st-act-preview-empty">No strategy would go fully dormant at this threshold.</div>') +
    warns;
}

async function loadStrategies() {
  try {
    const [rows, jobs, failures, dataUsage, proposals, driftResp, appliedResp, notedResp, simResp] = await Promise.all([
      _safeFetch('/api/strategies',                  [], { critical: true, label: 'strategies list' }),
      _safeFetch('/api/approvals/active',            [], { label: 'active approvals' }),
      _safeFetch('/api/approvals/recent-failures',   [], { label: 'recent failures' }),
      _safeFetch('/api/data/usage',                  null, { label: 'data usage' }),
      _safeFetch('/api/regime-proposals?status=pending', { proposals: [] }, { label: 'regime proposals' }),
      _safeFetch('/api/regime-drift',                { signals: [] }, { label: 'regime drift' }),
      _safeFetch('/api/regime-proposals/applied?days=7', { applied: [] }, { label: 'applied adjustments' }),
      _safeFetch('/api/regime-proposals/noted?days=21', { proposals: [], sizing_recs: [] }, { label: 'noted adjustments' }),
      _safeFetch('/api/strategy-similarity',         { regime: null, matrix: {} }, { label: 'similarity matrix' }),
    ]);
    _stSimMatrix = (simResp && simResp.matrix) ? simResp.matrix : {};
    _stSimRegime = simResp ? simResp.regime : null;
    // Fire-and-forget — the gates section renders independently of the
    // strategy list below. Errors are reflected inline in the section.
    _loadSharpeGates();
    _loadActivationCard();
    _loadActivationTradesCard();
    _rpRender(proposals?.proposals || [], notedResp || {});
    _saRenderApplied(appliedResp?.applied || []);
    // 2026-05-19: calibration-addenda panel removed (operator no longer
    // edits Mastermind's weekly prompt — research-page-only entry).
    _rdIndex(driftResp?.signals || []);   // index drift by (strategy, regime) for cell badges
    strategiesData = Array.isArray(rows) ? rows : [];
    _stActiveJobs = {};
    for (const j of (Array.isArray(jobs) ? jobs : [])) {
      _stActiveJobs[j.strategy_id] = j;
    }
    // Don't clobber in-flight session failures captured since last reload —
    // just fill in anything the server remembers that we didn't.
    for (const f of (Array.isArray(failures) ? failures : [])) {
      if (!_stActiveJobs[f.strategy_id]) {
        _stLastFailures[f.strategy_id] = { job_id: f.job_id, reason: _stFailReason(f.result || {}) };
      }
    }
    _stDataUsage = dataUsage && Array.isArray(dataUsage.parameters) ? dataUsage : null;
  } catch {
    strategiesData = [];
    _stActiveJobs = {};
  }
  _renderStrategyPage();
}

const _ST_PHASE_META = {
  data_pipeline_setup: { icon: '📡', label: 'setting up data collection' },
  awaiting_snapshot:   { icon: '⏳', label: 'awaiting first snapshot'     },
  strategycoder:       { icon: '🔨', label: 'strategycoder building'      },
  validate:            { icon: '🧪', label: 'validating contract'         },
  backtest:            { icon: '📈', label: 'running 3-window backtest'   },
  promoting:           { icon: '🚀', label: 'promoting to paper'          },
};
function _stJobChipHTML(sid) {
  const j = _stActiveJobs[sid];
  if (!j) return '';
  const meta = _ST_PHASE_META[j.phase] || { icon: '⏳', label: j.phase || 'running' };
  const pct = typeof j.progress === 'number' ? j.progress : 0;
  const missing = j.payload && j.payload.missing_sources;
  const missingHint = Array.isArray(missing) && missing.length
    ? ' (' + missing.length + ' src)' : '';
  return (
    '<div class="st-job-wrap" title="job ' + j.job_id + ' — ' + meta.label + '">' +
      '<div class="st-job-chip">' +
        '<span class="st-job-fill" style="width:' + pct + '%"></span>' +
        '<span class="st-job-text">' + meta.icon + ' ' + meta.label + ' · ' + pct + '%' + missingHint + '</span>' +
      '</div>' +
      '<button class="st-action-btn st-cancel-btn" onclick="stCancelApproval(\\'' + sid + '\\')" title="Cancel this approval job">Cancel</button>' +
    '</div>'
  );
}

function _stFailureBannerHTML(sid) {
  const fail = _stLastFailures[sid];
  if (!fail) return '';
  const reason = fail.reason || fail;   // legacy string payload during in-flight sessions
  const safe = String(reason).replace(/[<&>]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'})[c]);
  return (
    '<div class="st-fail-banner" title="' + _escStr(String(reason)) + '">' +
      '<span class="st-fail-ico">❌</span>' +
      '<span class="st-fail-msg">' + safe + '</span>' +
      '<button class="st-action-btn st-fail-dismiss" onclick="stDismissFailure(\\'' + sid + '\\')" title="Clear this failure badge">✕</button>' +
    '</div>'
  );
}

function _stRenderJobChips() {
  // Candidates section removed 2026-07-13 — approval-job progress now only
  // reaches the operator via toasts + the SSE log; nothing to re-render.
}

// The engine TRADES by registry status='approved', NOT by manifest state.
// A row is "trading" iff the registry has approved it; such rows belong in
// the Active Stack regardless of what the manifest state still says. This is
// a DISPLAY-ONLY reclassification — it does NOT write manifest.json or the
// strategy_registry; promoting the manifest is a separate governance decision.
// The three predicates below are mutually exclusive (each row matches at most
// one): _isTrading routes to Active Stack and gates the other two off; among
// non-trading rows the manifest-state sets {live,monitoring} /
// {deprecated,archived,orphan} / {paper,candidate,staging} are disjoint.
function _isTrading(r)     { return String(r.registry_status || '').toLowerCase() === 'approved'; }
function _inActiveStack(r) { return _isTrading(r) || r.state === 'live' || r.state === 'monitoring'; }
function _inInactive(r)    { return !_isTrading(r) && (r.state === 'deprecated' || r.state === 'archived' || r.state === 'orphan'); }
function _inCandidate(r)   { return !_isTrading(r) && (r.state === 'paper' || r.state === 'candidate' || r.state === 'staging'); }

// Sub-status for Active Stack rows: 'live' | 'stale' | 'waiting'
function _activeSub(r) {
  if (!r.regime_active) return 'waiting';
  if (r.is_stale)       return 'stale';
  return 'live';
}

function _renderStrategyPage() {
  const rows = strategiesData;
  const active    = rows.filter(_inActiveStack);
  const inactive  = rows.filter(_inInactive);
  const candidate = rows.filter(_inCandidate);

  const live    = active.filter(r => _activeSub(r) === 'live').length;
  const stale   = active.filter(r => _activeSub(r) === 'stale').length;
  const waiting = active.filter(r => _activeSub(r) === 'waiting').length;

  document.getElementById('st-total').textContent         = rows.length;
  document.getElementById('st-active-tile').textContent   = active.length;
  document.getElementById('st-active-sub').textContent    = live + ' live / ' + stale + ' stale / ' + waiting + ' waiting';
  document.getElementById('st-inactive-tile').textContent = inactive.length;
  document.getElementById('st-candidate-tile').textContent= candidate.length;
  document.getElementById('st-active-count').textContent  = active.length + ' strategies';
  document.getElementById('st-inactive-count').textContent= inactive.length + ' strategies';

  // Data tile: count of fine-grained financial parameters ingested
  // (open, close, implied_volatility, ebitda, …) from /api/data/usage.
  const paramCount = _stDataUsage && _stDataUsage.parameters ? _stDataUsage.parameters.length : 0;
  document.getElementById('st-data-tile').textContent = paramCount || '—';
  document.getElementById('st-data-sub').textContent  = 'financial parameters ingested';

  // Drift + fresh summary header: counts across all sections.
  const n_trading_not_shown     = rows.filter(r => r.drift === 'trading_not_shown').length;
  const n_shown_live_not_trading = rows.filter(r => r.drift === 'shown_live_not_trading').length;
  const n_fresh                 = rows.filter(r => r.fresh).length;
  const driftSummaryEl = document.getElementById('st-drift-summary');
  if (driftSummaryEl) {
    const parts = [];
    if (n_trading_not_shown > 0 || n_shown_live_not_trading > 0) {
      parts.push('<div class="st-drift-header">⚠️ ' + n_trading_not_shown + ' trading but not shown live \xb7 ' + n_shown_live_not_trading + ' shown live but not trading</div>');
    }
    if (n_fresh > 0) {
      parts.push('<div class="st-drift-header" style="color:var(--green);border-color:var(--green)">✨ ' + n_fresh + ' fresh strateg' + (n_fresh === 1 ? 'y' : 'ies') + ' (live < 1 week)</div>');
    }
    driftSummaryEl.innerHTML = parts.join('');
  }

  _renderActiveStack(active);
  _renderInactiveStack(inactive);
  // Candidates section removed 2026-07-13 — auto-approval owns the flow.
}

// ── Data Usage (ranked horizontal bar list, modal) ────────────────────────
function _stOpenDataUsage() {
  const modal = document.getElementById('data-usage-modal');
  if (!modal) return;
  modal.classList.add('open');
  _stRenderDataUsage();
}
function _stCloseDataUsage() {
  const modal = document.getElementById('data-usage-modal');
  if (modal) modal.classList.remove('open');
}
// Close on backdrop click + escape, mirroring the paper-modal pattern.
document.addEventListener('click', (e) => {
  const modal = document.getElementById('data-usage-modal');
  if (modal && modal.classList.contains('open') && e.target === modal) _stCloseDataUsage();
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const modal = document.getElementById('data-usage-modal');
  if (modal && modal.classList.contains('open')) _stCloseDataUsage();
});

function _fmtHistoryLen(days) {
  if (!days || days <= 0) return '—';
  if (days >= 365) {
    const y = days / 365;
    return (y >= 10 ? y.toFixed(0) : y.toFixed(1)) + 'y';
  }
  if (days >= 30) return Math.round(days / 30) + 'mo';
  return days + 'd';
}

function _stRenderDataUsage() {
  const body = document.getElementById('data-usage-body');
  if (!body) return;
  const u = _stDataUsage;
  if (!u || !u.parameters || !u.parameters.length) {
    body.innerHTML = '<div style="padding:24px;color:var(--muted);font-size:11px">No data parameters reported by the server. Try refreshing the page.</div>';
    return;
  }
  // Server already sorted by user_count desc; use the first row as the
  // bar-scale max so empty parameters render as a thin track.
  const params  = u.parameters;
  const maxUsers = Math.max(1, ...params.map(p => p.user_count || 0));
  const totalStrats = u.strategies_total || 0;
  const usedAny = params.filter(p => (p.user_count || 0) > 0).length;
  const knownCats = new Set(['prices','options_eod','financials','earnings','insider','macro','computed']);
  const rowsHtml = params.map(p => {
    const pct  = Math.round(((p.user_count || 0) / maxUsers) * 100);
    const hist = _fmtHistoryLen(p.history_days);
    const cat  = knownCats.has(p.category) ? p.category : 'other';
    const ttl  = p.name + ' · ' + p.category
               + (p.description ? '\\n' + p.description : '')
               + (p.provider ? '\\nprovider: ' + p.provider : '')
               + '\\nhistory: ' + hist + (p.min_date && p.max_date ? ' (' + p.min_date + ' → ' + p.max_date + ')' : '')
               + '\\nused by ' + (p.user_count || 0) + ' / ' + totalStrats + ' strategies';
    const empty = (p.user_count || 0) === 0;
    return '<div class="du-row' + (empty ? ' du-zero' : '') + '" title="' + _escStr(ttl) + '">'
         +   '<span class="du-name">' + p.name + '</span>'
         +   '<span class="du-cat du-cat-' + cat + '">' + p.category + '</span>'
         +   '<span class="du-history">' + hist + (p.provider ? ' · ' + p.provider : '') + '</span>'
         +   '<span class="du-desc">' + (p.description || '') + '</span>'
         +   '<div class="du-bar-track">'
         +     '<div class="du-bar-fill" style="width:' + pct + '%"></div>'
         +     (empty ? '<div class="du-bar-empty">no strategy declares this category</div>' : '')
         +   '</div>'
         +   '<div class="du-count">'
         +     (p.user_count || 0)
         +     '<small>/ ' + totalStrats + '</small>'
         +   '</div>'
         + '</div>';
  }).join('');
  const computedCount = params.filter(p => p.category === 'computed').length;
  body.innerHTML = ''
    + '<div class="du-wrap">'
    +   '<div class="du-summary">'
    +     '<div class="du-summary-top">'
    +       '<span><b>' + params.length + '</b> financial parameters across <b>' + (u.categories_total || 0) + '</b> categories · <b>' + computedCount + '</b> computed</span>'
    +       '<span><b>' + usedAny + '</b> declared by ≥1 strategy · <b>' + totalStrats + '</b> strategies registered</span>'
    +     '</div>'
    +     '<div class="du-summary-explain">Strategies declare data at the category level today; counts roll up to every column inside that category.</div>'
    +   '</div>'
    +   '<div class="du-list-head">'
    +     '<span>Parameter</span><span>Category</span><span>History</span><span>Description</span><span>Strategies using</span><span>Count</span>'
    +   '</div>'
    +   '<div class="du-list">' + rowsHtml + '</div>'
    + '</div>';
}

// Per-regime breakdown cell rendered for every active strategy. Shows all
// four regimes as compact chips so the operator can see at a glance how
// the strategy performs across the regime axis. Active regimes (declared
// in active_in_regimes) get a blue accent; the current market regime
// gets a small dot indicator. Cells without backtest trades render as 'na'.
//
// Source: BACKTEST only — backtest_regime_breakdown[regime] = {sharpe,
// trade_count, total_return_pct, hit_rate}. Shows backtest Sharpe per regime.
const _REGIME_AXIS = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
const _REGIME_TAGS = { LOW_VOL: 'LV', TRANSITIONING: 'TR', HIGH_VOL: 'HV', CRISIS: 'CR' };
// Per-regime effective Sharpe — since 2026-08-29 (benchmark-relative sizing
// spec D2) this IS the raw sleeve Sharpe: strategy_weights.daily_weight ==
// effective_sharpe, no sqrt(avg holding days) divisor. Kept as a function so
// the column keeps its name and sort key.
function _effSharpeOf(b) {
  return (b && b.sharpe != null) ? parseFloat(b.sharpe) : null;
}
function _regimeBreakdown(r) {
  const breakdown = r.backtest_regime_breakdown || {};
  const eligible = Array.isArray(r.eligible_regimes)
    ? r.eligible_regimes
    : (r.active_in_regimes && r.active_in_regimes.length ? r.active_in_regimes : _REGIME_AXIS);
  const current  = r.current_regime || null;
  const sid      = r.strategy_id;
  const editable = r.state === 'live';
  const cells = _REGIME_AXIS.map(rg => {
    const isEligible = eligible.includes(rg);
    const b      = breakdown[rg];
    const trades = b && b.trade_count ? parseInt(b.trade_count) : 0;
    // Cell value = raw per-regime BACKTEST Sharpe — the quantity the
    // activation slider gates on. Eff.Sharpe lives in the expanded
    // "Backtest by Regime" grid only (operator 2026-07-27).
    const sharpe = (b && b.sharpe != null) ? parseFloat(b.sharpe) : null;
    const klass = ['st-regime-cell', \`st-rg-\${rg}\`];
    if (current === rg) klass.push('st-rg-current');
    if (!trades)        klass.push('st-rg-na');
    if (!isEligible)    klass.push('st-rg-ineligible');
    if (editable)       klass.push('st-rg-editable');
    const valTxt = sharpe != null ? sharpe.toFixed(2) : '—';
    // NOTE: the /api/strategies per-regime object emits the return field as
    // total_return_pct (server.js ~1238), not return_pct as the plan's
    // Step 3b snippet assumed — read the key the API actually surfaces so the
    // tooltip's Ret segment renders instead of always showing a dash.
    const retPct = b.total_return_pct != null ? b.total_return_pct : b.return_pct;
    const statsLine = !trades ? 'no backtest trades'
      : 'Sharpe ' + valTxt
        + ' · Ret ' + (retPct != null ? (retPct >= 0 ? '+' : '') + parseFloat(retPct).toFixed(1) + '%' : '—')
        + ' · Win ' + (b.hit_rate != null ? Math.round(b.hit_rate * 100) + '%' : '—')
        + ' · ' + trades + ' trades';
    const eligLine = isEligible ? 'eligible' : 'NOT eligible';
    const editHint = editable ? ' · click to toggle' : '';
    const ttl = rg + ' [' + eligLine + ']: ' + statsLine + editHint;
    const onclick = editable ? \` onclick="_stToggleRegimeEligibility(event, '\${_escStr(sid)}', '\${rg}')"\` : '';
    return \`<span class="\${klass.join(' ')}" title="\${_escStr(ttl)}"\${onclick}>
              <span class="st-rg-tag">\${rg}</span>
              <span class="st-rg-val">\${valTxt}</span>
            </span>\`;
  }).join('');
  const gridTitle = editable
    ? 'Per-regime BACKTEST Sharpe · click a regime to toggle eligibility'
    : "Per-regime BACKTEST Sharpe · operator toggle only for state='live'";
  return \`<div class="st-regime-grid" title="\${gridTitle}">\${cells}</div>\`;
}

// Mirror of _regimeBreakdown for the research-candidates table: shows
// per-regime BACKTEST Sharpe (from strategy_backtest_regimes via
// /api/strategies → r.backtest_regime_breakdown) instead of live ARR.
// Used in candidates pre-promotion to give the operator the same
// per-regime view the regime-blended sizer will see.
function _regimeBacktestSharpe(r) {
  const breakdown = r.backtest_regime_breakdown || {};
  const eligible  = Array.isArray(r.eligible_regimes)
    ? r.eligible_regimes
    : (r.active_in_regimes && r.active_in_regimes.length ? r.active_in_regimes : _REGIME_AXIS);
  const current = r.current_regime || null;
  const cells = _REGIME_AXIS.map(rg => {
    const b      = breakdown[rg] || null;
    // Source-of-truth for "declared": backend now sets b.declared per regime.
    // Fall back to manifest eligible_regimes for old rows missing the flag.
    const isDeclared = (b && b.declared != null) ? !!b.declared : eligible.includes(rg);
    const trades = b && b.trade_count != null ? b.trade_count : 0;
    const sharpe = (b && b.sharpe != null) ? parseFloat(b.sharpe) : null;
    const klass = ['st-regime-cell', \`st-rg-\${rg}\`];
    if (current === rg) klass.push('st-rg-current');
    if (sharpe == null) klass.push('st-rg-na');
    // NOT-declared uses a light dashed-border treatment (st-rg-not-declared)
    // — the BT Sharpe value stays fully readable. Heavy st-rg-ineligible
    // (grey + strikethrough) is reserved for the LIVE active-stack cells
    // where the regime is actually turned off in the sizer.
    if (!isDeclared)   klass.push('st-rg-not-declared');
    const valTxt = sharpe != null ? sharpe.toFixed(2) : '—';
    const eligLine = isDeclared ? 'declared eligible' : 'NOT declared (outside literature scope)';
    const note = b && b.note ? \` (\${b.note})\` : '';
    const statsLine = sharpe == null
      ? (trades > 0 ? \`\${trades} trades, sharpe NULL (trade_count < 5)\` : \`no backtest trades\${note}\`)
      : \`sharpe=\${sharpe.toFixed(2)} · \${trades} trades · dd=\${b.max_dd != null ? (b.max_dd * 100).toFixed(1) + '%' : '—'}\`;
    const ttl = rg + ' [' + eligLine + ']: ' + statsLine;
    return \`<span class="\${klass.join(' ')}" title="\${_escStr(ttl)}">
              <span class="st-rg-tag">\${rg}</span>
              <span class="st-rg-val">\${valTxt}</span>
            </span>\`;
  }).join('');
  return \`<div class="st-regime-grid" title="Per-regime BACKTEST Sharpe (from unified_backtest). Dashed border = not in literature-declared regimes. Tooltip = trades + drawdown.">\${cells}</div>\`;
}

// Backwards-compat shim — left in case other call sites reference it; the
// active-stack table uses _regimeBreakdown now.
function _regimesCell(r) {
  const regs = r.active_in_regimes || [];
  if (!regs.length) return '<span style="color:var(--dim)">—</span>';
  return regs.map(rg => \`<span class="regime-state-\${rg}" style="padding:2px 6px;border-radius:4px;font-size:9px;font-weight:600;margin-right:3px;letter-spacing:.04em">\${rg}</span>\`).join('');
}

function _fmtPct(v) {
  if (v == null) return '—';
  const n = parseFloat(v) * 100;
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}
function _fmtRate(v) { return v == null ? '—' : Math.round(parseFloat(v) * 100) + '%'; }
function _fmtDate(v) { return v ? new Date(v).toLocaleDateString('en-US',{month:'numeric',day:'numeric',year:'2-digit'}) : '—'; }
function _fmtNum(v, d) {
  if (v == null || isNaN(parseFloat(v))) return '—';
  return parseFloat(v).toFixed(d == null ? 2 : d);
}
function _escStr(s) { return String(s == null ? '' : s).replace(/"/g, '&quot;').replace(/'/g, '&#39;'); }

// Returns an inline ⚠️ badge when the strategy has manifest↔registry drift.
// r.drift is one of 'none' | 'trading_not_shown' | 'shown_live_not_trading'.
// The manifest state (r.state) remains the PRIMARY displayed status; this
// badge is purely additive.
function _freshBadge(r) {
  // ✨ fresh — went live within the last 7 days (auto-promotion batch).
  // Additive, same pattern as the drift badge; self-expires via state_since.
  if (!r.fresh) return '';
  return '<span class="st-fresh-badge" title="Newly promoted — live for less than a week (auto-approval batch)">✨ fresh</span>';
}
// Universe ladder campaign W4 (2026-07-21): chip showing the CHOSEN universe
// tier the strategy trades (universe shrink). Non-null universe_tier also
// means every backtest metric on the row is that tier's shrunk number, not
// the full-universe run's.
const _UNI_LABELS = { sp500: 'SP500', tier_r1000: 'R1000',
                      tier_r3000: 'R3000', tier_liquid: 'LIQUID' };
function _universeBadge(r) {
  const t = r.universe_tier;
  if (!t) return '';
  return '<span class="st-uni-badge" title="Chosen universe: ' + t
       + ' (ladder shrink) — Sharpe/DD/trade metrics on this row are for THIS universe, not the full-universe backtest">'
       + (_UNI_LABELS[t] || t) + '</span>';
}
function _driftBadge(r) {
  if (!r.drift || r.drift === 'none') return '';
  if (r.drift === 'trading_not_shown') {
    return '<span class="st-drift-badge" title="Engine IS trading this (registry: approved); manifest shows ' + _escStr(r.state) + '">⚠️ trading</span>';
  }
  if (r.drift === 'shown_live_not_trading') {
    return '<span class="st-drift-badge" title="Manifest shows ' + _escStr(r.state) + ' but engine is NOT trading it (registry: ' + _escStr(r.registry_status || '—') + ')">⚠️ not trading</span>';
  }
  return '';
}

// ── Section 1: Active Stack ────────────────────────────────────────────────
const _SUB_ORDER = { live: 0, stale: 1, waiting: 2 };

// Persisted expansion state. Survives table re-renders triggered by
// sort changes, polling refreshes, and approval-job updates so the
// operator's selection isn't lost on every redraw.
let _stExpandedSid = null;
// Chart instances (_stEqChart / _stCprChart) are declared inline beside their
// render functions below.
let _stArrCache    = {};     // sid → backtest-curve payload (cached for the session)
let _stArrFetching = {};     // sid → in-flight Promise (de-dupe concurrent expands)
let _stRegimeFilter = 'ELIGIBLE'; // 'ELIGIBLE' | 'ALL' | 'LOW_VOL' | 'TRANSITIONING' | 'HIGH_VOL' | 'CRISIS'

function _stSetRegimeFilter(rg) {
  _stRegimeFilter = rg;
  // Toggle the active button class.
  const bar = document.getElementById('st-regime-filter');
  if (bar) {
    bar.querySelectorAll('.srf-btn').forEach(b => {
      b.classList.toggle('active', b.dataset.regime === rg);
    });
    const hint = document.getElementById('srf-hint');
    if (hint) {
      hint.textContent = rg === 'ALL'
        ? 'showing all-regime backtest stats'
        : rg === 'ELIGIBLE'
          ? 'showing eligible-regimes blend (regimes the strategy is approved to trade)'
          : 'showing ' + rg + ' backtest stats';
    }
  }
  _renderActiveStack(strategiesData.filter(_inActiveStack));
}

function _stToggleExpand(sid) {
  if (_stExpandedSid === sid) {
    _stCloseExpand();
  } else {
    _stExpandedSid = sid;
    _renderActiveStack(strategiesData.filter(_inActiveStack));
    _stPaintExpand(sid);
  }
}

// Click on a regime cell → toggle eligibility for a live strategy. Stops
// propagation so the row-expand handler doesn't also fire. Writes through
// the existing /api/regime-eligibility endpoint (audit + atomic manifest
// rewrite); regime_gate picks up the change on the next is_eligible() call
// with no service restart.
// Phase 2C — drift signal index (populated on every loadStrategies pass)
let _rdSignals = {};   // { 'strategy_id|regime': drift_signal_obj }

function _rdIndex(signals) {
  _rdSignals = {};
  for (const s of (signals || [])) {
    _rdSignals[s.strategy_id + '|' + s.regime_state] = s;
  }
}

function _rdDriftFor(strategyId, regime) {
  return _rdSignals[strategyId + '|' + regime] || null;
}

// Phase 2B / 2026-07-14 full-auto Saturday — render the suggestion queue:
// pending proposals (transient — the Saturday auto-apply decides them within
// the run) + noted low-confidence proposals (operator-decidable until the
// next weekly review supersedes them) + noted low-confidence sizing recs
// (informational; their brackets are still backtest-coupled).
function _rpRender(proposals, noted) {
  const section = document.getElementById('rp-section');
  const tbody = document.querySelector('#rp-table tbody');
  const countEl = document.getElementById('rp-count');
  if (!section || !tbody) return;
  section.style.display = '';
  const notedProps = (noted && noted.proposals) || [];
  const notedRecs  = (noted && noted.sizing_recs) || [];
  const total = proposals.length + notedProps.length + notedRecs.length;
  if (countEl) countEl.textContent = total ? '(' + total + ')' : '(none)';
  tbody.innerHTML = '';
  const _badge = (txt, bg) =>
    '<span style="padding:1px 6px;border-radius:3px;font-size:10px;background:' + bg + ';color:#fff">' + txt + '</span> ';
  const _row = (p, kind) => {
    const tr = document.createElement('tr');
    // Proposals carry ONLY size + eligibility (2026-05-31). Stop/target/
    // max_hold are decided by the backtest-coupling step, never approved here.
    const proposedBits = [];
    if (p.proposed_eligible !== null && p.proposed_eligible !== undefined) proposedBits.push('elig=' + p.proposed_eligible);
    if (p.proposed_size_scalar !== null && p.proposed_size_scalar !== undefined) proposedBits.push('size=' + Number(p.proposed_size_scalar).toFixed(2));
    const conf = p.confidence != null ? Number(p.confidence).toFixed(2) : '?';
    const why = escapeHtml((p.reasoning || '') + (p.decision_reason ? ' — ' + p.decision_reason : ''));
    tr.innerHTML =
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' + escapeHtml(p.strategy_id) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + escapeHtml(p.regime_state) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' +
        (kind === 'pending' ? _badge('PENDING', '#b08800') : _badge('NOTED', '#57606a')) + proposedBits.join(' · ') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + conf + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-size:11px;color:var(--muted)">' + why + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right;white-space:nowrap">' +
        '<button class="rp-btn" data-id="' + p.id + '" data-action="approve" style="padding:3px 8px;font-size:11px;background:#2ea043;color:white;border:none;border-radius:3px;cursor:pointer;margin-right:4px">Approve</button>' +
        '<button class="rp-btn" data-id="' + p.id + '" data-action="reject" style="padding:3px 8px;font-size:11px;background:#a04040;color:white;border:none;border-radius:3px;cursor:pointer">Reject</button>' +
      '</td>';
    tbody.appendChild(tr);
  };
  for (const p of proposals) _row(p, 'pending');
  for (const p of notedProps) _row(p, 'noted');
  for (const r of notedRecs) {
    // Sizing recs have no approve path (size only flows at confidence > 0.8);
    // shown so the operator sees what was suggested + the coupling outcome.
    const tr = document.createElement('tr');
    const bits = [];
    if (r.size_delta_pct != null)   bits.push('Δsize=' + Number(r.size_delta_pct).toFixed(3) + '%');
    if (r.stop_delta_pct != null)   bits.push('Δstop=' + Number(r.stop_delta_pct).toFixed(3));
    if (r.target_delta_pct != null) bits.push('Δtgt=' + Number(r.target_delta_pct).toFixed(3));
    if (r.hold_days_delta != null)  bits.push('Δhold=' + r.hold_days_delta + 'd');
    const conf = r.confidence != null ? Number(r.confidence).toFixed(2) : '?';
    const cpl = r.coupling_outcome
      ? ' · brackets ' + (r.coupling_outcome === 'applied' ? 'APPLIED via backtest' : 'rejected by backtest')
      : '';
    tr.innerHTML =
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' + escapeHtml(r.strategy_id) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);color:var(--muted)">all</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' +
        _badge('NOTED · SIZING', '#57606a') + bits.join(' · ') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + conf + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-size:11px;color:var(--muted)">' + escapeHtml((r.reasoning || '').slice(0, 160)) + cpl + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right;color:var(--muted);font-size:11px">re-eval Sat</td>';
    tbody.appendChild(tr);
  }
  // Wire buttons
  for (const btn of tbody.querySelectorAll('.rp-btn')) {
    btn.onclick = () => _rpDecide(btn.dataset.id, btn.dataset.action);
  }
}

function _saRenderApplied(applied) {
  const tbody = document.querySelector('#sa-applied-table tbody');
  const countEl = document.getElementById('sa-applied-count');
  if (!tbody) return;
  tbody.innerHTML = '';
  if (countEl) countEl.textContent = '(' + applied.length + ')';
  if (!applied.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="padding:6px 8px;color:var(--muted)">No adjustments applied in the last 7 days.</td></tr>';
    return;
  }
  const fmt = (v) => (v == null ? '—' : Number(v).toFixed(3));
  const _delta = (label, b, af, f) => {
    if (af == null || String(b) === String(af)) return null;
    return label + ' ' + f(b) + ' → ' + f(af);
  };
  for (const a of applied) {
    const d = (a.bt_sharpe_after != null && a.bt_sharpe_before != null)
      ? (a.bt_sharpe_after - a.bt_sharpe_before) : null;
    // One compact string listing only what actually changed.
    const changes = [
      _delta('stop',   a.stop_before,     a.stop_after,     fmt),
      _delta('target', a.target_before,   a.target_after,   fmt),
      _delta('size',   a.size_before,     a.size_after,     (v) => (v == null ? '—' : Number(v).toFixed(2))),
      _delta('hold',   a.hold_before,     a.hold_after,     (v) => (v == null ? '—' : v + 'd')),
      _delta('elig',   a.eligible_before, a.eligible_after, (v) => (v == null ? '—' : String(v))),
    ].filter(Boolean).join(' · ') || '(no field delta)';
    const via = a.source === 'saturday_coupling' ? ['backtest', '#1f6feb']
      : a.source === 'auto-approval' ? ['auto conf', '#8250df']
      : ['manual', '#57606a'];
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' + escapeHtml(a.strategy_id) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + escapeHtml(a.regime_state) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)"><span style="padding:1px 6px;border-radius:3px;font-size:10px;background:' + via[1] + ';color:#fff">' + via[0] + '</span></td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' + escapeHtml(changes) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right;color:' + (d != null && d >= 0 ? '#2ea043' : 'var(--muted)') + '" title="backtest Sharpe ' + fmt(a.bt_sharpe_before) + ' → ' + fmt(a.bt_sharpe_after) + '">' + (d != null ? (d >= 0 ? '+' : '') + d.toFixed(2) : '—') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right">' + (a.bt_n_trades != null ? a.bt_n_trades : '—') + '</td>';
    tbody.appendChild(tr);
  }
}

// 2026-05-19: Phase 2F calibration-addenda render/load/decide/wire helpers
// removed. The operator no longer interacts with MastermindJohn's weekly
// review prompt — research-page is the single entry point for paper /
// source / hand-developed-strategy additions.

async function _rpDecide(id, action) {
  let actor = window.localStorage.getItem('rg_operator');
  if (!actor) {
    actor = prompt('Operator name (saved for the session):', '');
    if (!actor) return;
    window.localStorage.setItem('rg_operator', actor);
  }
  const reason = prompt('Reason for ' + action + ' of proposal #' + id + '?', '');
  if (reason === null) return;
  try {
    const res = await fetch('/api/regime-proposals/' + encodeURIComponent(id) + '/' + action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actor: 'operator:' + actor, reason }),
    });
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.error || res.statusText);
    }
    await loadStrategies();
  } catch (err) {
    alert('Proposal ' + action + ' failed: ' + err.message);
  }
}

async function _stToggleRegimeEligibility(event, sid, regime) {
  if (event && event.stopPropagation) event.stopPropagation();
  const row = (strategiesData || []).find(r => r.strategy_id === sid);
  if (!row) return;
  if (row.state !== 'live') {
    alert("Eligibility editing is restricted to state='live' strategies.");
    return;
  }
  const _ALL = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
  const current = Array.isArray(row.eligible_regimes)
    ? row.eligible_regimes.slice()
    : _ALL.slice();
  const next = new Set(current);
  if (next.has(regime)) next.delete(regime);
  else next.add(regime);
  if (next.size === 0) {
    alert('Strategy must remain eligible in at least one regime. Trim a different regime first, or expand another before removing this one.');
    return;
  }
  const reason = prompt(
    'Reason for changing ' + sid + ' eligibility?\\n' +
    'Before: ' + JSON.stringify(current) + '\\n' +
    'After:  ' + JSON.stringify([...next]),
    ''
  );
  if (reason === null) return;
  let actor = window.localStorage.getItem('rg_operator');
  if (!actor) {
    actor = prompt('Operator name (saved for the session):', '');
    if (!actor) return;
    window.localStorage.setItem('rg_operator', actor);
  }
  try {
    // Phase 2A: POST to per-regime endpoint instead of the legacy list-set
    // endpoint. We only need to flip the cell the operator clicked.
    const eligibleAfter = next.has(regime);
    const res = await fetch(
      '/api/regime-params/' + encodeURIComponent(sid) + '/' + encodeURIComponent(regime),
      {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          eligible: eligibleAfter,
          actor:    'operator:' + actor,
          reason,
          source:   'strategies-page',
        }),
      },
    );
    if (!res.ok) {
      const e = await res.json().catch(() => ({}));
      throw new Error(e.error || res.statusText);
    }
    await loadStrategies(); // refresh the row, including its eligibility marks
  } catch (err) {
    alert('Eligibility update failed: ' + err.message);
  }
}
function _stCloseExpand() {
  if (_stEqChart)  { try { _stEqChart.destroy();  } catch (_) {} _stEqChart  = null; }
  if (_stCprChart) { try { _stCprChart.destroy(); } catch (_) {} _stCprChart = null; }
  _stExpandedSid = null;
  _renderActiveStack(strategiesData.filter(_inActiveStack));
}

function _renderActiveStack(rows) {
  const el = document.getElementById('st-active-wrap');
  // Regime filter: scopes the headline metric columns to ALL / ELIGIBLE / a
  // single regime via each row's metrics_by_scope. Show the control.
  const _rf = document.getElementById('st-regime-filter');
  if (_rf) _rf.style.display = '';
  if (!rows.length) {
    el.innerHTML = '<div class="empty">No strategies in the Active Stack.</div>';
    return;
  }
  // Compute derived columns for sorting.
  //   _oue_total       = O+U+E (sort key on # O/U/E)
  //   _active_rank     = Waiting(0) < Stale(1) < Live(2) — so ascending
  //                       surfaces most-activated strategies last;
  //                       descending surfaces LIVE rows first.
  //
  // All per-row metrics (sharpe / effective_sharpe / closed_count / win_rate /
  // arr_pct / adr_pct / act_days) now arrive backtest-sourced straight from
  // the /api/strategies payload — no live recompute. The only derived sort
  // keys are _oue_total (sum of the backtest OUE counts) and _active_rank
  // (Waiting<Stale<Live, drives the Status column sort).
  // Resolve which metric scope to display (regime filter). Fall back to the
  // row's own default_scope, then to ALL, then to the legacy top-level fields.
  const _scope = _stRegimeFilter;
  const _pickScope = (r) => {
    const mbs = r.metrics_by_scope;
    if (!mbs) return null;
    return mbs[_scope] || mbs[r.default_scope] || mbs.ALL || null;
  };
  const enriched = rows.map(r => {
    const m = _pickScope(r);
    const overlay = m ? {
      sharpe:           m.sharpe,
      effective_sharpe: m.effective_sharpe,
      closed_count:     m.closed_count,
      win_rate:         m.win_rate,
      arr_pct:          m.arr_pct,
      adr_pct:          m.adr_pct,
      act_days:         m.act_days,
    } : {};
    return Object.assign({}, r, overlay, {
      _oue_total:   (r.oue_over || 0) + (r.oue_under || 0) + (r.oue_expected || 0),
      _active_rank: _activeRankFor(r),
    });
  });
  // Default sort: status sub-group then strategy_id. Operator clicks override.
  let sorted;
  const s = _sortState['st-active-wrap'];
  if (s && s.key) {
    sorted = _applySort('st-active-wrap', enriched);
  } else {
    _tableDataCache['st-active-wrap'] = enriched;
    sorted = enriched.slice().sort((a, b) => {
      const d = (_SUB_ORDER[_activeSub(a)] ?? 9) - (_SUB_ORDER[_activeSub(b)] ?? 9);
      if (d !== 0) return d;
      return String(a.strategy_id).localeCompare(String(b.strategy_id));
    });
  }
  const { shown, footer } = _collapseRows('st-active-wrap', sorted);
  // Active strategy lookup for cross-references (similar-strategies panel).
  const activeStrategiesById = {};
  for (const er of enriched) activeStrategiesById[er.strategy_id] = er;
  // Persist for the expansion-panel renderer to read without re-prop.
  _stActiveSnapshot = activeStrategiesById;
  const expandedSid = _stExpandedSid;
  const _scopeLabel = _scope === 'ALL' ? 'All regimes'
    : _scope === 'ELIGIBLE' ? 'Eligible regimes'
    : _scope + ' only';
  el.innerHTML = \`<div class="st-scope-caption" style="font-size:9.5px;color:var(--dim);padding:2px 4px 6px;letter-spacing:.04em">Metrics scope: <b style="color:var(--muted)">\${_scopeLabel}</b> — Sharpe / Eff / Closed / Win / ARR / ADR / ACT reflect this regime selection. "By Regime" always shows all four.</div>
  <table class="db-table st-active-table" style="min-width:1180px">
    <tr>
      <th data-sort-key="strategy_id" data-sort-type="str">Strategy</th>
      <th data-sort-key="_active_rank" data-sort-type="num" title="Waiting(0)<Stale(1)<Live(2)">Status</th>
      <th title="Per-regime BACKTEST Sharpe; dot=current regime; blue=declared">By Regime</th>
      <th class="num" data-sort-key="sharpe" data-sort-type="num" title="Backtest Sharpe (primary window)">Sharpe</th>
      <th class="num" data-sort-key="effective_sharpe" data-sort-type="num" title="Sleeve Sharpe (cadence normalization retired 2026-08-29)">Eff.Sharpe</th>
      <th class="num" data-sort-key="closed_count" data-sort-type="num" title="Backtest trade count">Closed</th>
      <th class="num" data-sort-key="win_rate" data-sort-type="num" title="Backtest hit rate">Win&nbsp;%</th>
      <th class="num" data-sort-key="arr_pct" data-sort-type="num" title="Backtest mean trade return %">ARR&nbsp;%</th>
      <th class="num" data-sort-key="adr_pct" data-sort-type="num" title="Backtest ARR / ACT">ADR&nbsp;%</th>
      <th class="num" data-sort-key="act_days" data-sort-type="num" title="Backtest avg holding days">ACT</th>
      <th class="num" data-sort-key="_oue_total" data-sort-type="num" title="BACKTEST OUE (GBM σ): O=realized>expectation by ≥2σ, U=below, E=within. O+U+E = backtest trades.">#&nbsp;O/U/E</th>
      <th data-sort-key="last_signal_date" data-sort-type="date">Last Signal</th>
      <th>Actions</th>
    </tr>
    \${shown.map(r => {
      const sub = _activeSub(r);
      const subLabel = sub.toUpperCase();
      const title = sub === 'waiting'
        ? 'Regime ' + (r.current_regime || '?') + ' not in active_in_regimes: ' + ((r.active_in_regimes || []).join(', ') || '?')
        : (sub === 'stale' ? 'Regime active but no signal in 7+ days' : 'Trading actively');
      const arr = r.arr_pct, adr = r.adr_pct, act = r.act_days;
      const o = r.oue_over || 0, u = r.oue_under || 0, e = r.oue_expected || 0;
      const oueEmpty = (o + u + e) === 0;
      const ourCell = oueEmpty
        ? '<span style="color:var(--dim)">—</span>'
        : \`<span style="color:#4ade80">\${o}</span>/<span style="color:#f87171">\${u}</span>/<span style="color:#94a3b8">\${e}</span>\`;
      const sh = r.sharpe, esh = r.effective_sharpe;
      const closedTxt = r.closed_count || 0;
      const winTxt = r.win_rate != null ? Math.round(parseFloat(r.win_rate) * 100) + '%' : '—';
      const adrTitle = 'ARR ' + (arr != null ? ((arr >= 0 ? '+' : '') + arr.toFixed(2) + '%') : '—')
                     + ' / ACT ' + (act != null ? act.toFixed(1) + ' days' : '—');
      const actTxt = act != null ? act.toFixed(1) + (act === 1 ? ' day' : ' days') : '—';
      // Governance lever for rows the engine already trades but the manifest
      // still files as candidate (drift='trading_not_shown'). This reclass is
      // display-only; the button reaches the SAME promote path the candidate
      // "✅ Approve" uses (stApprove → POST /api/strategies/:id/approve, no
      // gate override) so the operator can reconcile manifest→live when ready.
      const promoteBtn = r.drift === 'trading_not_shown'
        ? \`<button class="st-action-btn" onclick="event.stopPropagation();stApprove('\${_escStr(r.strategy_id)}', false)" title="engine already trades this; promote manifest candidate→live to reconcile">⚠️ Promote in manifest</button>\`
        : '';
      const isOpen = r.strategy_id === expandedSid;
      const expandRow = isOpen ? \`
        <tr class="st-expand-row" data-sid="\${_escStr(r.strategy_id)}">
          <td colspan="13">
            <div class="st-expand-shell" id="st-expand-shell-\${_escStr(r.strategy_id)}">
              <div class="st-expand-pad" id="st-expand-pad-\${_escStr(r.strategy_id)}">
                <div class="st-expand-head">
                  <div class="st-expand-title">\${r.strategy_id}
                    <small>\${_escStr(r.description || '')}</small>
                  </div>
                  <button class="st-expand-close" onclick="event.stopPropagation();_stCloseExpand()">Close ✕</button>
                </div>
                <div class="st-expand-body" id="st-expand-body-\${_escStr(r.strategy_id)}">
                  <div class="st-arr-empty">Loading…</div>
                </div>
              </div>
            </div>
          </td>
        </tr>\` : '';
      return \`<tr class="st-row-expandable \${isOpen ? 'st-row-open' : ''}" data-sid="\${_escStr(r.strategy_id)}">
        <td class="st-name-cell" style="font-weight:600" title="\${_escStr(r.description)}" onclick="_stToggleExpand('\${_escStr(r.strategy_id)}')">
          <span class="st-chevron">▶</span>\${r.strategy_id}\${_universeBadge(r)}
        </td>
        <td><span class="sg-status sg-status-\${sub}" title="\${_escStr(title)}">\${subLabel}</span>\${_driftBadge(r)}\${_freshBadge(r)}</td>
        <td>\${_regimeBreakdown(r)}</td>
        <td class="num">\${sh != null ? parseFloat(sh).toFixed(2) : '—'}</td>
        <td class="num">\${esh != null ? parseFloat(esh).toFixed(2) : '—'}</td>
        <td class="num">\${closedTxt}</td>
        <td class="num">\${winTxt}</td>
        <td class="num \${pnlCls(arr)}">\${arr != null ? ((arr >= 0 ? '+' : '') + arr.toFixed(2) + '%') : '—'}</td>
        <td class="num \${pnlCls(adr)}" title="\${adrTitle}">\${adr != null ? ((adr >= 0 ? '+' : '') + adr.toFixed(2) + '%') : '—'}</td>
        <td class="num" style="color:var(--muted)">\${actTxt}</td>
        <td class="num" title="BACKTEST OUE (GBM σ): O=realized>expectation by ≥2σ, U=below, E=within.">\${ourCell}</td>
        <td style="color:var(--dim)">\${_fmtDate(r.last_signal_date)}</td>
        <td>\${promoteBtn}<button class="st-action-btn st-unstack-btn" onclick="event.stopPropagation();stUnstack('\${_escStr(r.strategy_id)}')">Unstack</button></td>
      </tr>\${expandRow}\`;
    }).join('')}
  </table>\${footer}\`;
  _bindSortable('st-active-wrap', _renderActiveStack);
  _bindCollapse('st-active-wrap', _renderActiveStack);
  // If a row is currently expanded, paint its detail panel (chart +
  // similar strategies) and animate the shell open.
  if (expandedSid) _stPaintExpand(expandedSid);
}

let _stActiveSnapshot = {};

// Paint the expansion panel for a strategy. The panel content (regime
// stats + similar-strategies heatmap) renders synchronously so it's
// visible immediately. The equity-vs-SP500 chart then fills in after the
// /api/strategies/:id/backtest-curve fetch (cached after first call). The
// canvas wrapper has a fixed CSS height (.st-arr-canvas-wrap) so Chart.js
// sizes correctly even while the shell's max-height transition runs — no
// need to wait for transitionend.
async function _stPaintExpand(sid) {
  const shell = document.getElementById('st-expand-shell-' + sid);
  const body  = document.getElementById('st-expand-body-' + sid);
  if (!shell || !body) return;
  // Defer the class flip to the next frame so the transition runs from
  // the baseline 0 max-height.
  requestAnimationFrame(() => shell.classList.add('st-open'));

  const r = (strategiesData || []).find(x => x.strategy_id === sid);
  if (!r) return;

  // ── Backtest per-regime stats grid (Sharpe · Return · Win · Trades) ─────
  const breakdown = r.backtest_regime_breakdown || {};
  const active    = new Set(r.active_in_regimes || []);
  const current   = r.current_regime || null;
  const cellsHtml = _REGIME_AXIS.map(rg => {
    const b = breakdown[rg];
    const trades = (b && b.trade_count != null) ? parseInt(b.trade_count) : 0;
    const has = trades > 0;
    const tagBg = ({
      LOW_VOL:       'rgba(63,185,80,0.18)',
      TRANSITIONING: 'rgba(210,153,34,0.22)',
      HIGH_VOL:      'rgba(240,136,62,0.28)',
      CRISIS:        'rgba(248,81,73,0.34)',
    })[rg];
    const dot = current === rg ? '<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--blue);margin-left:6px" title="Current market regime"></span>' : '';
    const star = active.has(rg) ? '<span style="color:var(--blue);font-size:9px;margin-left:4px">●ACTIVE</span>' : '';
    const head = '<div class="st-rs-head" style="background:' + tagBg + ';margin:-7px -8px 6px;padding:5px 8px;border-radius:5px 5px 0 0">'
               + '<span>' + rg + '</span><span>' + star + dot + '</span></div>';
    if (!has) {
      const why = active.has(rg)
        ? 'no backtest trades under this regime yet'
        : 'not in active_in_regimes';
      return '<div class="st-rs-cell">' + head + '<div class="st-rs-na">' + why + '</div></div>';
    }
    // API emits the return field as total_return_pct (server.js ~1238), not
    // return_pct — read the key the API surfaces, falling back for old rows.
    const retSrc = b.total_return_pct != null ? b.total_return_pct : b.return_pct;
    const retPct = retSrc != null ? parseFloat(retSrc) : null;
    const sharpe = b.sharpe != null ? parseFloat(b.sharpe) : null;
    const winPct = b.hit_rate != null ? Math.round(b.hit_rate * 100) : null;
    // Additional per-regime risk metrics (operator 2026-07-27): effective
    // Sharpe — since 2026-08-29 (spec D2) the three sources (this cell,
    // blendScope's row Eff.Sharpe, strategy_weights.daily_weight) agree
    // again: all raw sleeve Sharpe by default, all reverting to sharpe /
    // sqrt(avg holding days) together under
    // OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM=1 — Calmar (the DD-gate
    // escape-hatch metric) and Max DD (the ceiling it escapes).
    const effSh  = _effSharpeOf(b);
    const calmar = b.calmar != null ? parseFloat(b.calmar) : null;
    const maxDd  = b.max_dd_pct != null ? parseFloat(b.max_dd_pct) : null;
    const inner = ''
      + '<div class="st-rs-row"><span>Sharpe</span><b>' + (sharpe != null ? sharpe.toFixed(2) : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>Eff.Sharpe</span><b>' + (effSh != null ? effSh.toFixed(2) : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>Calmar</span><b' + (calmar != null ? ' style="color:' + (calmar >= 0.5 ? 'var(--green)' : 'var(--red)') + '"' : '') + '>'
      +   (calmar != null ? calmar.toFixed(2) : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>Max DD</span><b' + (maxDd != null ? ' style="color:' + (maxDd <= 20 ? 'var(--muted)' : maxDd <= 50 ? 'var(--orange, #f0883e)' : 'var(--red)') + '"' : '') + '>'
      +   (maxDd != null ? maxDd.toFixed(1) + '%' : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>Return</span><b' + (retPct != null ? ' style="color:' + (retPct >= 0 ? 'var(--green)' : 'var(--red)') + '"' : '') + '>'
      +   (retPct != null ? ((retPct >= 0 ? '+' : '') + retPct.toFixed(1) + '%') : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>Win</span><b>' + (winPct != null ? winPct + '%' : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>Trades</span><b>' + trades + '</b></div>';
    return '<div class="st-rs-cell">' + head + inner + '</div>';
  }).join('');

  // ── Similar strategies (overlap on at least one regime) ────────────────
  // Ordered by PAIRWISE similarity ρ to the focused strategy (operator
  // 2026-07-27) — the same current-regime matrix the sizer's S_adj reads.
  // ρ shown first, then the per-regime backtest Sharpes. Missing pair =
  // matrix sparse-default territory → sorts to the bottom.
  const myRegimes = new Set(r.active_in_regimes || []);
  const _rhoTo = (other) => {
    const a = (_stSimMatrix[sid] || {})[other];
    const b2 = (_stSimMatrix[other] || {})[sid];
    const v = a != null ? a : b2;
    return v != null ? parseFloat(v) : null;
  };
  const similar = Object.values(_stActiveSnapshot)
    .filter(o => o.strategy_id !== sid)
    .filter(o => (o.active_in_regimes || []).some(rg => myRegimes.has(rg)))
    .sort((a, b) => {
      const ra = _rhoTo(a.strategy_id), rb = _rhoTo(b.strategy_id);
      if (ra == null && rb == null) return String(a.strategy_id).localeCompare(String(b.strategy_id));
      if (ra == null) return 1;
      if (rb == null) return -1;
      return rb - ra;
    })
    .slice(0, 12);
  const similarHtml = similar.length ? similar.map(o => {
    const rho = _rhoTo(o.strategy_id);
    const rhoCls = rho == null ? 'empty' : (rho >= 0.85 ? 'neg' : rho >= 0.4 ? '' : 'pos');
    const rhoTtl = rho == null
      ? 'pairwise similarity: not in the ' + (_stSimRegime || 'current') + ' matrix (treated ~independent)'
      : 'pairwise similarity ρ=' + rho.toFixed(2) + ' (' + (_stSimRegime || 'current') + ' matrix — the S_adj input; ≥0.85 = near-clone)';
    const rhoCell = '<span class="st-sm-cell ' + rhoCls + '" style="font-weight:600" title="' + _escStr(rhoTtl) + '">'
                  + (rho != null ? rho.toFixed(2) : '—') + '</span>';
    const cells = _REGIME_AXIS.map(rg => {
      const b = (o.backtest_regime_breakdown || {})[rg];
      const bt = (b && b.trade_count != null) ? parseInt(b.trade_count) : 0;
      if (!b || !bt) return '<span class="st-sm-cell empty" title="' + rg + ': no backtest trades">—</span>';
      const sharpe = b.sharpe != null ? parseFloat(b.sharpe) : null;
      const cls = sharpe == null ? 'empty' : (sharpe > 0 ? 'pos' : sharpe < 0 ? 'neg' : '');
      const txt = sharpe == null ? '—' : sharpe.toFixed(2);
      const ttl = rg + ': Sharpe ' + txt + ' · ' + bt + ' trades';
      return '<span class="st-sm-cell ' + cls + '" title="' + _escStr(ttl) + '">' + txt + '</span>';
    }).join('');
    return '<div class="st-similar-row" title="Open ' + o.strategy_id + '">'
         + '<span class="st-sm-name" onclick="_stToggleExpand(\\'' + _escStr(o.strategy_id) + '\\')" style="cursor:pointer">' + o.strategy_id + '</span>'
         + rhoCell + cells + '</div>';
  }).join('') : '<div class="st-rs-na" style="font-size:10.5px;padding:8px 0">No other active strategies share these regimes.</div>';

  // Render the synchronous content + a stable canvas wrapper. The wrapper
  // has explicit height (CSS .st-arr-canvas-wrap) so Chart.js' responsive
  // sizing has a fixed reference frame.
  body.innerHTML = \`
    <div class="st-expand-grid">
      <div class="st-expand-section">
        <div class="st-expand-section-title"><span>Backtest by Regime</span><span style="color:var(--dim);font-size:9px">Sharpe · Eff · Calmar · MaxDD · Return · Win · Trades</span></div>
        <div class="st-regime-stats">\${cellsHtml}</div>
        <div class="st-expand-section-title" style="margin-top:14px"><span>Similar Active Strategies</span><span style="color:var(--dim);font-size:9px">sorted by pairwise ρ · then backtest Sharpe per regime · click to open</span></div>
        <div class="st-similar">
          <div class="st-similar-row" style="background:transparent">
            <span class="st-sm-name" style="color:var(--dim);font-size:9px;text-transform:uppercase;letter-spacing:.06em">Strategy</span>
            <span class="st-sm-cell" style="background:transparent;color:var(--dim);font-size:9px" title="pairwise similarity to this strategy (current-regime matrix, the S_adj input)">ρ</span>
            \${_REGIME_AXIS.map(rg => '<span class="st-sm-cell" style="background:transparent;color:var(--dim);font-size:9px">' + _REGIME_TAGS[rg] + '</span>').join('')}
          </div>
          \${similarHtml}
        </div>
      </div>
      <div class="st-expand-section st-arr-wrap">
        <div class="st-expand-section-title"><span>Backtest equity vs SP500</span><span style="color:var(--dim);font-size:9px">regime-shaded</span></div>
        <div class="st-arr-canvas-wrap"><canvas id="st-eq-chart-\${sid}"></canvas></div>
        <div class="st-flow-row">
          <div class="st-flow-meta"><span style="color:var(--dim);font-size:9.5px">Backtest positions closed per regime</span><span></span></div>
          <div class="st-flow-canvas-wrap"><canvas id="st-cpr-chart-\${sid}"></canvas></div>
        </div>
      </div>
    </div>\`;

  // Fetch (or read cached) backtest equity curve. The canvas wrapper has
  // fixed CSS height (.st-arr-canvas-wrap) so Chart.js can size correctly
  // even while the shell's max-height transition is still running — no need
  // to wait for transitionend.
  let payload = _stArrCache[sid];
  if (!payload) {
    if (!_stArrFetching[sid]) {
      _stArrFetching[sid] = fetch('/api/strategies/' + encodeURIComponent(sid) + '/backtest-curve')
        .then(r => r.ok ? r.json() : { rows: [] })
        .catch(() => ({ rows: [] }));
    }
    payload = await _stArrFetching[sid];
    delete _stArrFetching[sid];
    _stArrCache[sid] = payload;
  }
  // If the user closed or jumped to another row in the interim, bail out.
  if (_stExpandedSid !== sid) return;
  _stRenderEquityChart(sid, payload);
  _stRenderClosedPerRegimeChart(sid, r);   // r.backtest_regime_breakdown
}

// Backtest equity-vs-SP500 line chart. Two datasets (strategy solid-blue
// fill + SP500 dashed grey) over the precomputed backtest equity curve, with
// the regimeBands plugin painting a per-point regime backdrop. Y-axis is
// formatted as a growth multiple (Nx).
let _stEqChart = null;
function _stRenderEquityChart(sid, payload) {
  const canvas = document.getElementById('st-eq-chart-' + sid);
  if (!canvas) return;
  if (_stEqChart) { try { _stEqChart.destroy(); } catch (_) {} _stEqChart = null; }
  const rows = (payload && payload.rows) || [];
  const wrap = canvas.parentElement;
  if (!rows.length) { if (wrap) wrap.style.display = 'none'; return; }
  if (wrap) wrap.style.display = '';
  const labels = rows.map(r => r.date);
  const strat  = rows.map(r => r.strat_equity);
  const spx    = rows.map(r => r.spx_equity);
  const regimeMap = {};
  for (const r of rows) if (r.regime) regimeMap[r.date] = r.regime;
  _stEqChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets: [
      { label:'strategy', data:strat, borderColor:'#58a6ff', backgroundColor:'rgba(88,166,255,0.10)',
        borderWidth:1.6, pointRadius:0, fill:true, tension:0.15 },
      { label:'SP500', data:spx, borderColor:'#8b949e', borderWidth:1.2, pointRadius:0,
        borderDash:[4,3], fill:false, tension:0.15 },
    ]},
    options: {
      responsive:true, maintainAspectRatio:false, animation:false, resizeDelay:200,
      interaction:{ mode:'index', intersect:false },
      plugins: {
        legend:{ display:true, labels:{ color:'#8b949e', font:{size:9}, boxWidth:10 } },
        regimeBands:{ map: regimeMap },
        tooltip:{ backgroundColor:'#0d1117', borderColor:'#30363d', borderWidth:1,
          titleColor:'#c9d1d9', bodyColor:'#e6edf3', displayColors:true, padding:8,
          callbacks:{ label: ctx => {
            const r = rows[ctx.dataIndex];
            return ctx.dataset.label + ' ' + ctx.parsed.y.toFixed(3) + 'x' +
                   (ctx.datasetIndex===0 && r ? '  · '+(r.regime||'—') : '');
          }}},
      },
      scales: {
        x:{ ticks:{ color:'#484f58', maxTicksLimit:8, font:{size:9} }, grid:{ color:'rgba(48,54,61,0.45)' } },
        y:{ position:'right', ticks:{ color:'#484f58', font:{size:9}, callback:v=>v.toFixed(1)+'x', maxTicksLimit:5 },
            grid:{ color:'rgba(48,54,61,0.45)' } }
      }
    }
  });
}

// Backtest positions-closed-per-regime bar chart. One bar per regime over
// the fixed _CPR_AXIS, height = backtest_regime_breakdown[rg].trade_count,
// coloured by regime. Hidden entirely when no regime has any trades.
let _stCprChart = null;
const _CPR_AXIS = ['LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS'];
const _CPR_COLORS = { LOW_VOL:'rgba(63,185,80,0.9)', TRANSITIONING:'rgba(210,153,34,0.9)',
                      HIGH_VOL:'rgba(240,136,62,0.9)', CRISIS:'rgba(248,81,73,0.9)' };
function _stRenderClosedPerRegimeChart(sid, r) {
  const canvas = document.getElementById('st-cpr-chart-' + sid);
  if (!canvas) return;
  if (_stCprChart) { try { _stCprChart.destroy(); } catch (_) {} _stCprChart = null; }
  const bd = r.backtest_regime_breakdown || {};
  const counts = _CPR_AXIS.map(rg => (bd[rg] && bd[rg].trade_count) ? bd[rg].trade_count : 0);
  const wrap = canvas.parentElement;
  if (!counts.some(c => c > 0)) { if (wrap) wrap.style.display = 'none'; return; }
  if (wrap) wrap.style.display = '';
  _stCprChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: { labels: _CPR_AXIS, datasets: [{ data: counts,
      backgroundColor: _CPR_AXIS.map(rg => _CPR_COLORS[rg]), borderWidth: 0, barPercentage: 0.7 }]},
    options: {
      responsive:true, maintainAspectRatio:false, animation:false, resizeDelay:200,
      plugins: { legend:{ display:false },
        tooltip:{ backgroundColor:'#0d1117', borderColor:'#30363d', borderWidth:1,
          titleColor:'#c9d1d9', bodyColor:'#e6edf3', displayColors:false, padding:8,
          callbacks:{ label: ctx => 'closed: ' + ctx.parsed.y }}},
      scales: {
        x:{ ticks:{ color:'#484f58', font:{size:9} }, grid:{ display:false } },
        y:{ position:'right', beginAtZero:true, ticks:{ color:'#484f58', font:{size:9}, precision:0, maxTicksLimit:3 },
            grid:{ display:false } }
      }
    }
  });
}

// ── Section 2: Inactive Stack ──────────────────────────────────────────────
function _renderInactiveStack(rows) {
  const el = document.getElementById('st-inactive-wrap');
  if (!rows.length) {
    el.innerHTML = '<div class="empty">No decommissioned strategies.</div>';
    return;
  }
  let sorted;
  const s = _sortState['st-inactive-wrap'];
  if (s && s.key) {
    sorted = _applySort('st-inactive-wrap', rows);
  } else {
    _tableDataCache['st-inactive-wrap'] = rows;
    sorted = rows.slice().sort((a, b) => String(a.strategy_id).localeCompare(String(b.strategy_id)));
  }
  const { shown, footer } = _collapseRows('st-inactive-wrap', sorted);
  el.innerHTML = \`<table class="db-table" style="min-width:900px">
    <tr>
      <th data-sort-key="strategy_id" data-sort-type="str">Strategy</th>
      <th data-sort-key="state" data-sort-type="str">Status</th>
      <th>Regimes</th>
      <th data-sort-key="last_signal_date" data-sort-type="date">Last Signal</th>
    </tr>
    \${shown.map(r => {
      return \`<tr>
        <td style="font-weight:600" title="\${_escStr(r.description)}">\${r.strategy_id}\${_universeBadge(r)}</td>
        <td><span class="st-badge st-badge-\${r.state}">\${r.state.toUpperCase()}</span>\${_driftBadge(r)}\${_freshBadge(r)}</td>
        <td>\${_regimesCell(r)}</td>
        <td style="color:var(--dim)">\${_fmtDate(r.last_signal_date)}</td>
      </tr>\`;
    }).join('')}
  </table>\${footer}\`;
  _bindSortable('st-inactive-wrap', _renderInactiveStack);
  _bindCollapse('st-inactive-wrap', _renderInactiveStack);
}

// ── Section 3: Research Candidates ─────────────────────────────────────────
// Gate warning when backtest Sharpe < 0.5 or Max DD > 20%.
// _renderCandidates removed 2026-07-13 (operator directive): the Research
// Candidates approval table is gone — candidate→live flows through the
// automatic per-regime qualification gate (auto_approval.js Sunday stage +
// staging_approver finalize hook). Action-handler API wrappers (stApprove /
// stApproveGated / stReject) are retained for console/manual use.

// ── Action handlers ────────────────────────────────────────────────────────

// Non-blocking toast notification. types: 'ok' | 'error' | 'warn' | 'info'.
function toast(msg, type = 'info', ttlMs = 4_000) {
  let host = document.getElementById('toast-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toast-host';
    document.body.appendChild(host);
  }
  const el = document.createElement('div');
  el.className = 'toast toast-' + type;
  el.textContent = msg;
  el.onclick = () => el.remove();
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add('toast-in'));
  setTimeout(() => {
    el.classList.add('toast-out');
    setTimeout(() => el.remove(), 350);
  }, ttlMs);
}

async function _stTransition(sid, toState, force, reason, eligibleRegimes) {
  try {
    const body = { to_state: toState, force: !!force, reason: reason || '', actor: 'manual:dashboard' };
    if (Array.isArray(eligibleRegimes)) body.eligible_regimes = eligibleRegimes;
    const resp = await fetch('/api/strategies/' + encodeURIComponent(sid) + '/transition', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { toast('Transition failed: ' + (data.error || resp.statusText), 'error', 6_000); return false; }
    toast(sid + ' → ' + toState + (eligibleRegimes ? ' (regimes: ' + eligibleRegimes.join(',') + ')' : ''), 'ok', 3_500);
    await loadStrategies();
    return true;
  } catch (e) {
    toast('Network error: ' + e.message, 'error', 6_000);
    return false;
  }
}

async function stUnstack(sid) {
  if (!confirm('Unstack ' + sid + '? It will move to Inactive Stack (live/monitoring → deprecated).')) return;
  await _stTransition(sid, 'deprecated', false, 'Manual unstack via dashboard');
}

// Pop the regime-picker modal on Approve. Operator chooses:
//   • By Sharpe   — auto-select regimes with backtest Sharpe ≥ threshold
//   • Keep declared — use the strategy's literature-driven eligible_regimes
//   • Custom      — manual checkboxes (defaults to the declared set)
// Then calls /transition with the chosen eligible_regimes piggybacked.
async function stApprove(sid, gateWarn) {
  const row = (strategiesData || []).find(r => r.strategy_id === sid);
  if (!row) { toast('Strategy row not found', 'error'); return; }
  const decision = await _stOpenApprovalPicker(row, gateWarn);
  if (!decision) return;  // operator cancelled
  const reasonSuffix = ' [regimes via ' + decision.mode + ': ' + decision.eligible_regimes.join(',') + ']';
  if (gateWarn) {
    await _stTransition(sid, 'live', true,
      'Manual approve via dashboard (override)' + reasonSuffix,
      decision.eligible_regimes);
  } else {
    await _stTransition(sid, 'live', false,
      'Manual approve via dashboard' + reasonSuffix,
      decision.eligible_regimes);
  }
}

// Open a modal letting the operator pick the regime set for promotion.
// Returns a Promise resolving to {mode, eligible_regimes} or null on cancel.
function _stOpenApprovalPicker(row, gateWarn) {
  const _AXIS = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
  const breakdown = row.backtest_regime_breakdown || {};
  const declared  = Array.isArray(row.eligible_regimes) && row.eligible_regimes.length
    ? row.eligible_regimes.slice()
    : (row.active_in_regimes || []).slice();
  const declaredSet = new Set(declared);
  // Pre-compute the "by Sharpe" set at the default threshold so the panel
  // can show a live preview before the operator clicks Apply.
  const DEFAULT_SHARPE_MIN = 0.5;
  const sharpeFor = rg => {
    const b = breakdown[rg];
    return (b && b.sharpe != null) ? parseFloat(b.sharpe) : null;
  };
  const computeBySharpe = (threshold) => _AXIS.filter(rg => {
    const s = sharpeFor(rg);
    return s != null && s >= threshold;
  });

  return new Promise(resolve => {
    const backdrop = document.createElement('div');
    backdrop.className = 'st-approve-modal-backdrop';
    backdrop.innerHTML = \`
      <div class="st-approve-modal" role="dialog" aria-modal="true">
        <h3 style="margin:0 0 6px;font-size:14px">Approve <code>\${_escStr(row.strategy_id)}</code> → LIVE</h3>
        <p style="margin:0 0 14px;font-size:11px;color:var(--muted)">
          Pick which regimes the strategy is eligible for. The sizer will only allocate capital in regimes you select here. You can change this later from the Active Stack table.\${gateWarn ? '<br><b style="color:var(--orange)">⚠ Strategy is below Sharpe/DD gates — this will be logged as an override.</b>' : ''}
        </p>

        <table class="st-approve-bt-table">
          <tr><th>Regime</th><th class="num">BT Sharpe</th><th class="num">Trades</th><th>Declared?</th><th>Select</th></tr>
          \${_AXIS.map(rg => {
            const b = breakdown[rg] || {};
            const s = sharpeFor(rg);
            const tr = b.trade_count ?? 0;
            const dec = declaredSet.has(rg);
            const sCls = s == null ? '' : (s >= 0.5 ? 'pos' : (s < 0 ? 'neg' : 'low'));
            return \`<tr>
              <td><span class="st-rg-tag st-rg-\${rg}" style="padding:2px 6px;border-radius:3px;font-size:10px;font-weight:700">\${rg}</span></td>
              <td class="num \${sCls}">\${s != null ? s.toFixed(2) : '—'}</td>
              <td class="num" style="color:var(--muted)">\${tr || '—'}</td>
              <td>\${dec ? '<span style="color:var(--green)">✓</span>' : '<span style="color:var(--dim)">—</span>'}</td>
              <td><input type="checkbox" data-regime="\${rg}" \${dec ? 'checked' : ''}></td>
            </tr>\`;
          }).join('')}
        </table>

        <div class="st-approve-modes">
          <button class="st-approve-mode-btn" data-mode="literature">Keep declared (\${declared.join(', ') || 'none'})</button>
          <button class="st-approve-mode-btn" data-mode="sharpe">By Sharpe ≥ <input type="number" id="st-approve-sharpe-min" step="0.1" min="-5" max="5" value="\${DEFAULT_SHARPE_MIN}" style="width:48px;font-size:11px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:3px;padding:1px 4px"></button>
          <span class="st-approve-mode-hint">Or check boxes individually above.</span>
        </div>

        <div class="st-approve-footer">
          <span id="st-approve-summary" style="font-size:11px;color:var(--muted)"></span>
          <span style="flex:1"></span>
          <button class="st-approve-cancel">Cancel</button>
          <button class="st-approve-confirm">\${gateWarn ? '⚠ Approve (override)' : '✅ Approve'}</button>
        </div>
      </div>\`;
    document.body.appendChild(backdrop);

    let chosenMode = declaredSet.size ? 'literature' : 'custom';
    const summary = backdrop.querySelector('#st-approve-summary');
    const checkboxes = () => Array.from(backdrop.querySelectorAll('input[type="checkbox"][data-regime]'));
    const getSelected = () => checkboxes().filter(c => c.checked).map(c => c.dataset.regime);
    const updateSummary = () => {
      const sel = getSelected();
      summary.textContent = sel.length
        ? 'Will activate: ' + sel.join(', ')
        : 'Select at least one regime.';
      backdrop.querySelector('.st-approve-confirm').disabled = sel.length === 0;
    };
    const applyMode = (mode) => {
      chosenMode = mode;
      let target;
      if (mode === 'literature') target = new Set(declared);
      else if (mode === 'sharpe') {
        const t = parseFloat(backdrop.querySelector('#st-approve-sharpe-min').value);
        target = new Set(computeBySharpe(isNaN(t) ? DEFAULT_SHARPE_MIN : t));
      } else target = new Set(getSelected());  // custom: leave as-is
      for (const cb of checkboxes()) cb.checked = target.has(cb.dataset.regime);
      updateSummary();
    };
    applyMode(chosenMode);

    backdrop.querySelectorAll('.st-approve-mode-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        if (e.target.tagName === 'INPUT') return;  // don't trigger when typing threshold
        applyMode(btn.dataset.mode);
      });
    });
    backdrop.querySelector('#st-approve-sharpe-min').addEventListener('input', () => applyMode('sharpe'));
    checkboxes().forEach(cb => cb.addEventListener('change', () => { chosenMode = 'custom'; updateSummary(); }));

    const close = (result) => { backdrop.remove(); resolve(result); };
    backdrop.querySelector('.st-approve-cancel').addEventListener('click', () => close(null));
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(null); });
    backdrop.querySelector('.st-approve-confirm').addEventListener('click', () => {
      const sel = getSelected();
      if (sel.length === 0) return;
      close({ mode: chosenMode, eligible_regimes: sel });
    });
  });
}

async function stReject(sid) {
  // State-aware reject:
  //   staging   → archived  (abandon before approval)
  //   candidate → staging   (regress: needs additional data / re-backtest)
  //   paper     → archived  (legacy escape — no rows should be in paper post-migration)
  const row = strategiesData.find(r => r.strategy_id === sid);
  const state = row && row.state;
  let target = 'archived';
  let label  = 'Archive ' + sid + '? It will be removed from the active stack.';
  if (state === 'candidate') {
    target = 'staging';
    label = 'Reject ' + sid + '? It will regress to STAGING so the data pipeline can be reviewed and the strategy re-built.';
  }
  if (!confirm(label)) return;
  await _stTransition(sid, target, false, 'Manual reject via dashboard');
}

async function stApproveGated(sid, state) {
  // Under the fused-staging-approval flow this only fires for staging-state
  // strategies. The worker handles backfill + strategycoder + backtest and
  // promotes the manifest staging→candidate on success.
  const label =
    'This will run fused approval for ' + sid + ': backfill required data sources, ' +
    'invoke StrategyCoder to implement the strategy, and run the 3-window convergence ' +
    'backtest (typically 5–15 min). On success the strategy promotes to CANDIDATE ' +
    'with backtest metrics — you click Approve again from CANDIDATE to go LIVE. Continue?';
  if (!confirm(label)) return;
  // Dismiss any persisted failure banner server-side before retrying; if the
  // retry also fails, a fresh banner row will replace it.
  const prev = _stLastFailures[sid];
  if (prev && prev.job_id) {
    fetch('/api/approvals/' + encodeURIComponent(prev.job_id) + '/dismiss', {method:'POST'}).catch(() => {});
  }
  delete _stLastFailures[sid];
  try {
    const resp = await fetch('/api/strategies/' + encodeURIComponent(sid) + '/approve', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ actor: 'manual:dashboard' }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { toast('Approve failed: ' + (data.error || resp.statusText), 'error', 6_000); return; }
    _stActiveJobs[sid] = { job_id: data.job_id, phase: data.phase, progress: 0, strategy_id: sid };
    toast('🚀 Approval started for ' + sid, 'info', 3_000);
    await loadStrategies();
  } catch (e) { toast('Network error: ' + e.message, 'error', 6_000); }
}

async function stDismissFailure(sid) {
  const fail = _stLastFailures[sid];
  delete _stLastFailures[sid];
  // Persist the dismissal server-side so it also survives the next reload.
  if (fail && fail.job_id) {
    try {
      await fetch('/api/approvals/' + encodeURIComponent(fail.job_id) + '/dismiss', {method:'POST'});
    } catch (_) { /* banner is gone locally; retry on next click if it comes back */ }
  }
}

// Human-readable reason for a failed approval_job result payload. The
// candidate→paper path only fails now when the strategy can't execute
// (contract_violation / coding_failed / backtest_error / import error).
// Metric-based failures no longer happen — they promote and show up in the
// dashboard for manual judgment.
function _stFailReason(result) {
  if (!result) return 'unknown failure';

  // Staging-side fast fails (unsupported data source, missing spec).
  if (result.hint)    return result.hint;
  if (result.error === 'unsupported_source' && Array.isArray(result.unsupported)) {
    return 'Missing data source(s): ' + result.unsupported.join(', ') +
           ' — add a collector module to data/master/schema_registry.json before approving.';
  }
  if (result.error === 'missing_strategy_spec') {
    return 'No research_candidates or strategy_hypotheses row for this strategy — seed one first.';
  }

  // Execution errors from candidate_approver → _codeFromQueue. Prefer the
  // raw error string; fall back to reasonCode mapping if error is missing.
  const rc = result.reasonCode;
  const err = result.error || (result.backtest && result.backtest.error);
  if (err && typeof err === 'string' && err.length) {
    if (rc === 'contract_violation')   return 'Contract validation failed — ' + err;
    if (rc === 'coding_failed')        return 'StrategyCoder build failed — ' + err;
    if (rc === 'backtest_error')       return "Couldn't execute — " + err;
    return err;
  }
  if (rc) return 'Failed: ' + rc;
  return 'unknown failure';
}

async function stCancelApproval(sid) {
  if (!confirm('Cancel the in-flight approval job for ' + sid + '? Any data_ingestion_queue rows this job inserted will be rolled back.')) return;
  try {
    const resp = await fetch('/api/strategies/' + encodeURIComponent(sid) + '/approve/cancel', {method:'POST'});
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { toast('Cancel failed: ' + (data.error || resp.statusText), 'error', 6_000); return; }
    delete _stActiveJobs[sid];
    toast((data.child_killed ? '🛑 Killed subprocess for ' : '⚠️ Cancelled ') + sid, 'warn', 3_000);
    await loadStrategies();
  } catch (e) { toast('Network error: ' + e.message, 'error', 6_000); }
}

// ── Volatility-regime background bands ───────────────────────────────────────
// Chart.js plugin: paints transparent vertical bands behind the dataset
// area, one band per labelled day, coloured by regime state. Activates
// only when the chart's options carry a regimeBands.map (label→state)
// map). Skipped silently when the map is empty.
const REGIME_BAND_COLORS = {
  LOW_VOL:        'rgba(63,185,80,0.18)',
  TRANSITIONING:  'rgba(210,153,34,0.20)',
  HIGH_VOL:       'rgba(240,136,62,0.26)',
  CRISIS:         'rgba(248,81,73,0.32)',
};
const _regimeBandsPlugin = {
  id: 'regimeBands',
  beforeDatasetsDraw(chart, _args, opts) {
    const map = opts && opts.map;
    if (!map || !chart.scales.x) return;
    const { ctx, chartArea, scales: { x } } = chart;
    const labels = chart.data.labels || [];
    const N = labels.length;
    if (!N) return;
    // Each band extends to the midpoint between its label and its
    // neighbours' centres, so adjacent bands always abut with no gap
    // regardless of axis offset:
    //   offset:true  bar charts → label slots are evenly spaced and
    //                            non-edge midpoints land exactly at slot
    //                            boundaries (same as the previous
    //                            "half = width/N" math).
    //   offset:false line charts → labels span N-1 slots so the previous
    //                            "half = width/N" left a ~1/N-wide gap
    //                            between every pair of bands. Using the
    //                            actual neighbour midpoint closes that
    //                            gap. The leftmost/rightmost bands
    //                            extend to the chart-area edges.
    // A 0.5px overlap on each side defeats sub-pixel anti-alias seams
    // between adjacent same-colour fills.
    const center = i => x.getPixelForValue(i);
    ctx.save();
    for (let i = 0; i < N; i++) {
      const state = map[labels[i]];
      if (!state) continue;
      const color = REGIME_BAND_COLORS[state];
      if (!color) continue;
      const c = center(i);
      const left = i === 0
        ? chartArea.left
        : (center(i - 1) + c) / 2;
      const right = i === N - 1
        ? chartArea.right
        : (c + center(i + 1)) / 2;
      ctx.fillStyle = color;
      ctx.fillRect(
        left  - 0.5,
        chartArea.top,
        (right - left) + 1,
        chartArea.bottom - chartArea.top,
      );
    }
    ctx.restore();
  },
};
if (window.Chart && Chart.register) Chart.register(_regimeBandsPlugin);

function _regimeMap(rows) {
  // rows: [{date, regime}]; output: {date → state}
  const out = {};
  if (!Array.isArray(rows)) return out;
  for (const r of rows) { if (r && r.date) out[r.date] = r.regime || 'NO_DATA'; }
  return out;
}

function _buildChart(labels, values, yFmt, tooltipFmt, color, regimeMap) {
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
        regimeBands: { map: regimeMap || null },
        tooltip: { backgroundColor:'#161b22', borderColor:'#30363d', borderWidth:1,
          titleColor:'#8b949e', bodyColor:'#e6edf3',
          callbacks: { label: ctx => tooltipFmt(ctx.parsed.y) }
        }
      },
      layout: { padding: { bottom: 0 } },
      scales: {
        x: { ticks: { color:'#484f58', maxTicksLimit:6, font:{size:10}, padding: 0 }, grid: { color:'#21262d' } },
        y: { position:'right', ticks: { color:'#484f58', font:{size:10}, callback: yFmt }, grid: { color:'#21262d' } }
      }
    }
  });
}

// ── Candlestick (90d daily OHLC of account equity) ────────────────────────────
// Implementation note: Chart.js core has no candlestick type, so we use
// the standard "two-bar overlay" trick:
//   - dataset 0 (wick): narrow centred floating bar [low, high]
//   - dataset 1 (body): full-width floating bar [open, close], coloured
//                       green if up, red if down, grey if flat
// Bodies span the full category width so adjacent days touch; visual
// definition between candles comes from the up/down colour shift. The
// body's borderWidth is 0 so neighbouring same-direction candles merge
// into a single block — this avoids hairline gaps that show through at
// 1px between bars.
const _crosshairPlugin = {
  id: 'pfCrosshair',
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || !opts.enabled) return;
    const active = chart.tooltip?.getActiveElements?.() || [];
    if (!active.length) return;
    const x = active[0].element.x;
    const { top, bottom } = chart.chartArea;
    const ctx = chart.ctx;
    ctx.save();
    ctx.beginPath();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'rgba(139,148,158,0.45)';
    ctx.lineWidth = 1;
    ctx.moveTo(x, top);
    ctx.lineTo(x, bottom);
    ctx.stroke();
    ctx.restore();
  },
};
if (window.Chart && Chart.register) Chart.register(_crosshairPlugin);

// Per-candle OHLC end-caps: flat horizontal serifs at the high and low
// of each day's wick. Painted in afterDatasetsDraw so they sit on top of
// the wick + body bars. Width is ~55% of category width — visible without
// crowding the touching candle bodies. Drawn in a single grey hue so the
// wick assembly reads as a unit and the body's up/down colour stays the
// only direction signal.
const _ohlcCapsPlugin = {
  id: 'ohlcCaps',
  afterDatasetsDraw(chart, _args, opts) {
    if (!opts || !opts.enabled) return;
    const rows = opts.data || [];
    if (!rows.length) return;
    const { x, y } = chart.scales;
    if (!x || !y) return;
    const labels = chart.data.labels || [];
    const catW = x.width / Math.max(labels.length, 1);
    const capHalf = Math.max(1, catW * 0.55 / 2);
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = opts.color || '#8b949e';
    ctx.lineWidth = 1;
    ctx.lineCap = 'butt';
    for (let i = 0; i < rows.length; i++) {
      const r = rows[i];
      if (!r) continue;
      const cx = x.getPixelForValue(i);
      const yHi = y.getPixelForValue(parseFloat(r.high));
      const yLo = y.getPixelForValue(parseFloat(r.low));
      ctx.beginPath();
      ctx.moveTo(cx - capHalf, yHi + 0.5);
      ctx.lineTo(cx + capHalf, yHi + 0.5);
      ctx.moveTo(cx - capHalf, yLo - 0.5);
      ctx.lineTo(cx + capHalf, yLo - 0.5);
      ctx.stroke();
    }
    ctx.restore();
  },
};
if (window.Chart && Chart.register) Chart.register(_ohlcCapsPlugin);

function renderCandleChart(rows, regimeRows) {
  const wrap = document.getElementById('pnlChart');
  if (!wrap) return;
  if (pnlChart) { pnlChart.destroy(); pnlChart = null; }
  if (!rows || !rows.length) {
    if (wrap) wrap.getContext('2d').clearRect(0, 0, wrap.width, wrap.height);
    return;
  }
  const labels  = rows.map(r => r.date);
  const wicks   = rows.map(r => [parseFloat(r.low), parseFloat(r.high)]);
  const bodies  = rows.map(r => [parseFloat(r.open), parseFloat(r.close)]);
  // One solid colour per candle — same hue used for both wick and body so
  // each day reads as a single uniform mark. Direction picks the hue:
  //   up   → green
  //   down → red
  //   flat → grey
  const COLOR_UP    = 'rgb(46,160,67)';
  const COLOR_DOWN  = 'rgb(218,54,51)';
  const COLOR_FLAT  = 'rgb(125,133,144)';
  const candleColor = r => {
    const d = parseFloat(r.close) - parseFloat(r.open);
    if (Math.abs(d) < 1e-6) return COLOR_FLAT;
    return d >= 0 ? COLOR_UP : COLOR_DOWN;
  };
  const colors = rows.map(candleColor);
  const fmt$ = v => '$' + parseFloat(v).toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0});
  pnlChart = new Chart(wrap.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        // Wick: narrow floating bar from low → high, sharing the body's
        // colour so the candle reads as a single solid mark.
        {
          label: 'wick',
          data: wicks,
          backgroundColor: colors,
          borderColor:     colors,
          borderWidth:     0,
          barPercentage:      0.14,
          categoryPercentage: 1.0,
          order: 1,
        },
        // Body: full-width floating bar from open → close. categoryPercentage
        // and barPercentage both 1.0 → adjacent candles touch with no gap.
        {
          label: 'body',
          data: bodies,
          backgroundColor: colors,
          borderWidth:     0,
          barPercentage:      1.0,
          categoryPercentage: 1.0,
          order: 0,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 220 },
      interaction: { mode: 'index', intersect: false, axis: 'x' },
      plugins: {
        legend: { display: false },
        regimeBands: { map: _regimeMap(regimeRows) },
        pfCrosshair: { enabled: true },
        tooltip: {
          backgroundColor:'#0d1117', borderColor:'#30363d', borderWidth:1,
          titleColor:'#c9d1d9', bodyColor:'#e6edf3', titleFont:{weight:'600'},
          displayColors: false, padding: 10, cornerRadius: 4,
          // Custom multi-line tooltip; only emit it once (filter wick).
          // The candle body geometry uses continuous open/close so bars
          // join end-to-end, but the operator wants O/C numbers in the
          // tooltip to reflect the regular session (9:30-16:00 ET). We
          // surface r.market_open / r.market_close when present (set on
          // any day with session samples) and fall back to continuous
          // open/close on weekends/holidays where no session exists.
          filter: ctx => ctx.dataset.label === 'body',
          callbacks: {
            title: items => items[0]?.label || '',
            label: ctx => {
              const r   = rows[ctx.dataIndex];
              if (!r) return '';
              const pct = r.pct_change != null
                ? ((r.pct_change >= 0 ? '+' : '') + (r.pct_change * 100).toFixed(2) + '%')
                : '—';
              const o = r.market_open  != null ? r.market_open  : r.open;
              const c = r.market_close != null ? r.market_close : r.close;
              const noSession = (r.intraday && r.market_open == null);
              return [
                'Δ '   + pct + (r.intraday ? '' : '  (daily mark)'),
                'O '   + fmt$(o) + (noSession ? '  (no session)' : ''),
                'H '   + fmt$(r.high),
                'L '   + fmt$(r.low),
                'C '   + fmt$(c) + (noSession ? '  (no session)' : ''),
              ];
            },
          },
        },
      },
      layout: { padding: { bottom: 0 } },
      scales: {
        x: {
          stacked: true,
          ticks: { color:'#484f58', maxTicksLimit:8, font:{size:10}, maxRotation:0, autoSkip:true, padding: 0 },
          grid:  { display:false },
          border:{ color:'#30363d' },
        },
        y: {
          position:'right',
          // 4% grace keeps wicks off the top/bottom edges.
          grace: '4%',
          ticks: { color:'#484f58', font:{size:10}, callback: fmt$, maxTicksLimit: 5 },
          grid:  { color:'rgba(48,54,61,0.45)', drawTicks:false },
          border:{ display:false },
          beginAtZero: false,
        },
      },
    },
  });
}

function renderValueChart(rows, regimeRows) {
  if (!rows || !rows.length) {
    const wrap = document.getElementById('pnlChart');
    if (pnlChart) { pnlChart.destroy(); pnlChart = null; }
    if (wrap) wrap.getContext('2d').clearRect(0, 0, wrap.width, wrap.height);
    return;
  }
  const labels = rows.map(r => String(r.date).slice(0,10));
  const values = rows.map(r => parseFloat(r.equity) || 0);
  const fmt$   = v => '$' + v.toLocaleString('en-US', {minimumFractionDigits:0, maximumFractionDigits:0});
  _buildChart(labels, values, fmt$, fmt$, '#58a6ff', _regimeMap(regimeRows));
}

// ── Pipeline badge ────────────────────────────────────────────────────────────
// Activation slider apply-state line (2026-08-22). "pending" comes from the
// server's comparison of the slider row vs the assigner's last-applied marker.
function _actApplyStatus(cfg) {
  const setAt = cfg.updated_at ? new Date(cfg.updated_at).toLocaleString() : null;
  const appliedAt = cfg.applied_at ? new Date(cfg.applied_at).toLocaleString() : null;
  if (cfg.pending) {
    return '⏳ set ' + (setAt || '—') + ' · pending — applies at the next daily compute (15:00 ET)'
      + (appliedAt ? ' · last applied ' + appliedAt : '');
  }
  if (appliedAt) {
    const la = cfg.last_applied || {};
    const via = la.trigger ? ' via ' + la.trigger : '';
    return '✓ applied ' + appliedAt + via + (setAt ? ' · set ' + setAt : '');
  }
  return setAt ? 'last set ' + setAt : '';
}

// ── Stale-tab guard (2026-08-22) ─────────────────────────────────────────────
// The page was served with window.__DASHBOARD_BUILD__; every /api/* response
// carries X-Dashboard-Build. A mismatch means johnbot restarted with a newer
// server.js and THIS tab's JS is stale (it would keep posting retired keys).
// Show a banner and reload after a short countdown.
let _buildReloadArmed = false;
function _noteDashboardBuild(resp) {
  try {
    const b = resp && resp.headers && resp.headers.get('X-Dashboard-Build');
    if (b && window.__DASHBOARD_BUILD__ && b !== window.__DASHBOARD_BUILD__) _armBuildReload(b);
  } catch (_) { /* never let the guard break a handler */ }
}
function _armBuildReload(serverBuild) {
  if (_buildReloadArmed) return;
  _buildReloadArmed = true;
  let secs = 8;
  const bar = document.createElement('div');
  bar.id = 'stale-build-banner';
  bar.setAttribute('role', 'alert');
  bar.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:10000;padding:10px 16px;'
    + 'background:var(--yellow);color:#000;font:600 13px/1.4 system-ui,sans-serif;'
    + 'display:flex;gap:14px;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.4)';
  const txt = document.createElement('span');
  const btn = document.createElement('button');
  btn.textContent = 'Reload now';
  btn.style.cssText = 'padding:4px 10px;border:1px solid #000;border-radius:4px;background:#fff;color:#000;cursor:pointer;font-weight:600';
  btn.onclick = () => location.reload();
  bar.appendChild(txt); bar.appendChild(btn);
  document.body.appendChild(bar);
  const tick = () => {
    txt.textContent = 'Dashboard was updated on the server (build ' + serverBuild + ') — this tab is running old code. Reloading in ' + secs + 's…';
    if (secs <= 0) { location.reload(); return; }
    secs -= 1;
    setTimeout(tick, 1000);
  };
  tick();
}
async function _checkDashboardBuild() {
  try {
    const r = await fetch('/api/dashboard-build', { cache: 'no-store' });
    if (!r.ok) return;
    const j = await r.json();
    if (j && j.build && window.__DASHBOARD_BUILD__ && j.build !== window.__DASHBOARD_BUILD__) _armBuildReload(j.build);
  } catch (_) { /* offline / restarting — next poll */ }
}

async function refreshPipeline() {
  const data = await fetch('/api/pipeline/status').then(r=>r.json()).catch(()=>null);
  if (!data?.coverage) return;
  const cov = data.coverage;
  const total = Object.values(universeData).flat().length || 456;
  document.getElementById('pipeline-badge').textContent =
    \`📡 prices:\${cov.price_coverage} options:\${cov.options_coverage} tech:\${cov.tech_coverage} fund:\${cov.fund_coverage}\`;
}

// ── Pipeline Diagnostics ─────────────────────────────────────────────────────
let _pipelineState = {
  initialized: false,
  runs:        [],
  selected:    null,
  sse:         null,
  paused:      false,
  streamCount: 0,
  refreshTimer: null,
};

async function showPipelines() {
  _hideAllPages();
  document.getElementById('pipeline-page').style.display = 'block';
  _setNavActive('pipelines');
  if (!_pipelineState.initialized) _initPipelines();
  await _refreshPipelines();
}

function _initPipelines() {
  _pipelineState.initialized = true;
  document.getElementById('pl-refresh').onclick = () => _refreshPipelines();
  document.getElementById('pl-filter-graph').onchange = () => _refreshPipelines();
  document.getElementById('pl-filter-limit').onchange = () => _refreshPipelines();
  document.getElementById('pl-stream-toggle').onclick = _togglePipelineStream;
  document.getElementById('pl-stream-pause').onclick = _togglePipelineStreamPause;
  document.getElementById('pl-stream-clear').onclick = () => {
    document.getElementById('pl-stream-pane').innerHTML =
      '<div class="pl-stream-line sys"><span class="pl-stream-ts">—</span>Stream cleared.</div>';
    _pipelineState.streamCount = 0;
    _updateStreamCount();
  };
  document.getElementById('pl-detail-copy').onclick = () => {
    if (_pipelineState.selected) {
      navigator.clipboard?.writeText(_pipelineState.selected).catch(()=>{});
    }
  };
  // Soft polling so tiles stay current even without SSE connected
  _pipelineState.refreshTimer = setInterval(() => {
    if (document.getElementById('pipeline-page').style.display === 'block') {
      _refreshPipelineSummary();
    }
  }, 15000);
}

function _fmtDuration(ms) {
  if (ms == null || !isFinite(ms) || ms < 0) return '—';
  if (ms < 1000) return ms + 'ms';
  const s = ms / 1000;
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60);
  const rem = Math.round(s - m*60);
  if (m < 60) return m + 'm ' + rem + 's';
  const h = Math.floor(m/60);
  return h + 'h ' + (m - h*60) + 'm';
}
function _fmtAgo(ts) {
  if (!ts) return '—';
  const diff = (Date.now() - ts) / 1000;
  if (diff < 60) return Math.round(diff) + 's ago';
  if (diff < 3600) return Math.round(diff/60) + 'm ago';
  if (diff < 86400) return Math.round(diff/3600) + 'h ago';
  return Math.round(diff/86400) + 'd ago';
}
function _fmtShortThread(t) {
  if (!t) return '—';
  return t.length > 16 ? t.slice(0, 10) + '…' + t.slice(-4) : t;
}
function _tileState(el, n, kind) {
  // kind = 'failures' (warn>0, err≥5) | 'live' (any>0 = live) | 'ok' (always neutral-good)
  if (!el) return;
  let state = 'neutral';
  if (kind === 'failures') {
    if (n >= 5) state = 'err';
    else if (n >= 1) state = 'warn';
    else state = 'ok';
  } else if (kind === 'live') {
    state = n >= 1 ? 'live' : 'neutral';
  } else {
    state = 'ok';
  }
  el.setAttribute('data-state', state);
}

async function _refreshPipelineSummary() {
  try {
    const d = await fetch('/api/pipelines/summary').then(r=>r.json());
    const setTile = (id, val) => { const e = document.getElementById(id); if (e) e.textContent = (val ?? '—'); };
    setTile('pl-tile-active', d.active ?? 0);
    setTile('pl-tile-today', d.today ?? 0);
    setTile('pl-tile-failures', d.failures_24h ?? 0);
    setTile('pl-tile-durable', d.durable_total ?? '—');
    _tileState(document.getElementById('pl-tile-card-active'), d.active||0, 'live');
    _tileState(document.getElementById('pl-tile-card-today'), d.today||0, 'ok');
    _tileState(document.getElementById('pl-tile-card-failures'), d.failures_24h||0, 'failures');
    _tileState(document.getElementById('pl-tile-card-durable'), d.durable_total||0, 'ok');
    const wEl = document.getElementById('pl-count-window');
    if (wEl) { const lbl = d.live_window ? d.live_window.replace(/_/g,' ') : null; wEl.textContent = lbl ? 'counts '+lbl : ''; wEl.style.display = lbl ? '' : 'none'; }

    // Populate graph filter dropdown if not present yet
    const sel = document.getElementById('pl-filter-graph');
    const current = sel.value;
    const have = new Set([...sel.options].map(o => o.value));
    for (const g of (d.graphs || [])) {
      if (!have.has(g)) {
        const opt = document.createElement('option');
        opt.value = g; opt.textContent = g;
        sel.appendChild(opt);
      }
    }
    sel.value = current;

    const t = document.getElementById('pl-last-updated');
    if (t) t.textContent = 'last refreshed ' + new Date().toLocaleTimeString();
  } catch (e) { /* tile failure non-fatal */ }
}

async function _refreshPipelines() {
  const icon = document.getElementById('pl-refresh-icon');
  if (icon) icon.innerHTML = '<span class="pl-spinner"></span>';
  await _refreshPipelineSummary();
  _refreshUniverseInflation();
  _refreshBackfillHistory();
  const limit = document.getElementById('pl-filter-limit').value;
  const graph = document.getElementById('pl-filter-graph').value;
  const q = new URLSearchParams({ limit });
  if (graph) q.set('graph', graph);

  const tbody = document.getElementById('pl-runs-tbody');
  try {
    const d = await fetch('/api/pipelines/runs?' + q.toString()).then(r=>r.json());
    _pipelineState.runs = d.runs || [];
    document.getElementById('pl-runs-count').textContent =
      _pipelineState.runs.length ? '· ' + _pipelineState.runs.length + ' shown' : '';

    if (!_pipelineState.runs.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="pl-empty">' +
        '<div class="pl-empty-icon">🌱</div>' +
        '<div>No runs yet for this filter.</div>' +
        '<div class="pl-dim" style="margin-top:6px">Trigger a graph from the shell:</div>' +
        '<div style="margin-top:4px"><code>node bin/run-graph.js cycle \\'{"runDate":"\${new Date().toISOString().slice(0,10)}"}\\'</code></div>' +
        '</td></tr>';
      return;
    }
    tbody.innerHTML = _pipelineState.runs.map((r) => {
      const status = (r.status || 'persisted').toLowerCase();
      const graphName = r.graph || 'unknown';
      const activity = r.eventCount != null
        ? \`\${r.eventCount} <span class="pl-dim">evt</span>\`
        : (r.checkpointCount != null ? \`\${r.checkpointCount} <span class="pl-dim">cp</span>\` : '<span class="pl-dim">—</span>');
      return \`<tr data-thread="\${escapeHtml(r.threadId || '')}">
        <td><span class="pl-graph-pill" data-graph="\${escapeHtml(graphName)}">\${escapeHtml(graphName)}</span></td>
        <td class="pl-thread-mono" title="\${escapeHtml(r.threadId || '')}">\${_fmtShortThread(r.threadId)}</td>
        <td><span class="pl-status \${status}">\${status}</span></td>
        <td class="pl-mono pl-dim" style="font-size:10.5px">\${escapeHtml(r.lastNode || '—')}</td>
        <td>\${_fmtAgo(r.startedAt || r.updatedAt)}</td>
        <td>\${_fmtDuration(r.durationMs)}</td>
        <td>\${activity}</td>
      </tr>\`;
    }).join('');
    tbody.querySelectorAll('tr').forEach(tr => {
      tr.onclick = () => _selectPipelineRun(tr.dataset.thread);
    });
    // Re-highlight the selected run if it's still in the list
    if (_pipelineState.selected) {
      const sel = tbody.querySelector(\`tr[data-thread="\${_pipelineState.selected}"]\`);
      if (sel) sel.classList.add('selected');
    }
  } catch (e) {
    tbody.innerHTML = \`<tr><td colspan="7" class="pl-empty"><div class="pl-empty-icon">⚠</div>Failed to load runs: \${escapeHtml(e.message)}</td></tr>\`;
  } finally {
    if (icon) icon.textContent = '↺';
  }
}

async function _selectPipelineRun(threadId) {
  if (!threadId) return;
  document.querySelectorAll('#pl-runs-tbody tr').forEach(tr => tr.classList.remove('selected'));
  const tr = document.querySelector('#pl-runs-tbody tr[data-thread="'+threadId+'"]');
  if (tr) tr.classList.add('selected');
  _pipelineState.selected = threadId;
  document.getElementById('pl-detail-copy').disabled = false;

  const body = document.getElementById('pl-detail-body');
  body.innerHTML = '<div class="pl-empty"><div class="pl-spinner" style="margin-bottom:8px"></div>Loading run detail…</div>';

  try {
    const d = await fetch('/api/pipelines/runs/' + encodeURIComponent(threadId)).then(r=>r.json());
    if (d.error) {
      body.innerHTML = '<div class="pl-empty"><div class="pl-empty-icon">⚠</div>'+escapeHtml(d.error)+'</div>';
      return;
    }

    let html = '';
    const graphName = d.live?.graph || (d.checkpoints?.[0]?.metadata?.graph) || 'unknown';

    // Header: thread + graph + status + duration meta strip
    html += '<div class="pl-detail-head">';
    html += '<span class="pl-graph-pill" data-graph="'+escapeHtml(graphName)+'">'+escapeHtml(graphName)+'</span>';
    if (d.live) {
      html += '<span class="pl-status '+ escapeHtml(d.live.status || 'unknown') +'">'+ escapeHtml(d.live.status || 'unknown') +'</span>';
    } else {
      html += '<span class="pl-status persisted">persisted</span>';
    }
    html += '<span class="pl-thread-mono">'+escapeHtml(threadId)+'</span>';
    html += '<div class="pl-detail-meta">';
    if (d.live) {
      html += '<span>duration <b>'+_fmtDuration(d.live.durationMs)+'</b></span>';
      html += '<span>events <b>'+(d.live.events?.length || 0)+'</b></span>';
      if (d.live.lastNode) html += '<span>last node <b class="pl-mono">'+escapeHtml(d.live.lastNode)+'</b></span>';
    }
    if (d.checkpoints?.length) html += '<span>checkpoints <b>'+d.checkpoints.length+'</b></span>';
    html += '</div></div>';

    if (d.live?.error) {
      html += '<div class="pl-section-title" style="color:#fca5a5">Error</div>';
      html += '<div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.3);color:#fca5a5;padding:8px 10px;border-radius:4px;font-size:11px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace">'+escapeHtml(String(d.live.error))+'</div>';
    }

    // Node timeline (gantt-style)
    if (d.live) {
      const events = d.live.events || [];
      const byNode = new Map();
      for (const e of events) {
        if (!e.node) continue;
        if (!byNode.has(e.node)) byNode.set(e.node, { first: e.ts, last: e.ts, status: e.status });
        const n = byNode.get(e.node);
        n.last = e.ts;
        if (e.status) n.status = e.status;
      }
      if (byNode.size > 0) {
        // Compute max duration for bar scaling
        let maxMs = 0;
        for (const n of byNode.values()) maxMs = Math.max(maxMs, n.last - n.first);
        if (maxMs === 0) maxMs = 1;
        html += '<div class="pl-section-title">Node timeline</div>';
        html += '<div class="pl-timeline">';
        for (const [name, n] of byNode.entries()) {
          const dur = n.last - n.first;
          const widthPct = Math.max(2, (dur / maxMs) * 100);
          let klass = 'ok';
          if (n.status === 'error') klass = 'err';
          else if (n.status === 'running') klass = 'run';
          else if (n.status === 'skipped') klass = 'skip';
          html += '<div class="pl-node-row '+klass+'">';
          html += '<span class="pl-node-name">'+escapeHtml(name)+'</span>';
          html += '<div class="pl-node-bar-cell"><div class="pl-node-bar" style="width:'+widthPct.toFixed(1)+'%"></div></div>';
          html += '<span class="pl-node-time">'+_fmtDuration(dur)+'</span>';
          html += '</div>';
        }
        html += '</div>';
      } else {
        html += '<div class="pl-empty" style="padding:14px"><div class="pl-empty-icon">📝</div>No node events in memory for this run.</div>';
      }
    }

    // Durable checkpoints (collapsible)
    if (d.checkpoints?.length) {
      const lastTen = d.checkpoints.slice(-10).map(c => ({
        id: (c.checkpointId || '').slice(0, 18) + '…',
        type: c.type,
        channels: c.channels,
        metadata: c.metadata,
      }));
      html += '<div class="pl-collapsible collapsed" id="pl-cps-block">';
      html += '<div class="pl-collapsible-head" onclick="document.getElementById(\\'pl-cps-block\\').classList.toggle(\\'collapsed\\')">'+
              'Checkpoints · '+d.checkpoints.length+' total (showing last '+lastTen.length+')</div>';
      html += '<div class="pl-collapsible-body"><pre class="pl-state-pre">'+escapeHtml(JSON.stringify(lastTen, null, 2))+'</pre></div>';
      html += '</div>';
    }

    if (!html) html = '<div class="pl-empty"><div class="pl-empty-icon">📭</div>No detail available for this run.</div>';
    body.innerHTML = html;
  } catch (e) {
    body.innerHTML = '<div class="pl-empty"><div class="pl-empty-icon">⚠</div>Error: '+escapeHtml(e.message)+'</div>';
  }
}

function _updateStreamCount() {
  const el = document.getElementById('pl-stream-count');
  if (el) el.textContent = _pipelineState.streamCount > 0
    ? '· ' + _pipelineState.streamCount + ' event' + (_pipelineState.streamCount === 1 ? '' : 's')
    : '';
}

function _togglePipelineStreamPause() {
  _pipelineState.paused = !_pipelineState.paused;
  const btn = document.getElementById('pl-stream-pause');
  btn.textContent = _pipelineState.paused ? '▶ Resume' : '⏸ Pause';
  btn.classList.toggle('primary', _pipelineState.paused);
}

function _togglePipelineStream() {
  const btn = document.getElementById('pl-stream-toggle');
  const pauseBtn = document.getElementById('pl-stream-pause');
  const status = document.getElementById('pl-stream-status');
  const pane = document.getElementById('pl-stream-pane');

  if (_pipelineState.sse) {
    _pipelineState.sse.close();
    _pipelineState.sse = null;
    btn.textContent = '▶ Connect';
    btn.classList.add('primary');
    pauseBtn.disabled = true;
    status.textContent = 'disconnected';
    status.className = 'pl-status persisted';
    return;
  }

  const es = new EventSource('/api/pipelines/stream');
  _pipelineState.sse = es;
  _pipelineState.paused = false;
  btn.textContent = '⏹ Disconnect';
  btn.classList.remove('primary');
  pauseBtn.disabled = false;
  pauseBtn.textContent = '⏸ Pause';
  pauseBtn.classList.remove('primary');
  status.textContent = 'connecting…';
  status.className = 'pl-status warn';

  const append = (kls, ts, text) => {
    if (_pipelineState.paused) return;
    const div = document.createElement('div');
    div.className = 'pl-stream-line ' + kls;
    div.innerHTML = '<span class="pl-stream-ts">'+escapeHtml(ts)+'</span><span>'+escapeHtml(text)+'</span>';
    pane.appendChild(div);
    while (pane.childElementCount > 400) pane.removeChild(pane.firstChild);
    pane.scrollTop = pane.scrollHeight;
    _pipelineState.streamCount++;
    _updateStreamCount();
  };
  const tsNow = () => new Date().toLocaleTimeString();

  es.onopen = () => { status.textContent = 'connected'; status.className = 'pl-status running'; };
  es.onerror = () => { status.textContent = 'reconnecting…'; status.className = 'pl-status warn'; };
  es.onmessage = (m) => {
    try {
      const d = JSON.parse(m.data);
      if (d.type === 'connected') append('sys', tsNow(), 'stream connected');
    } catch (_) { append('evt', tsNow(), m.data); }
  };
  es.addEventListener('trace', (m) => {
    try {
      const d = JSON.parse(m.data);
      const ts = new Date(d.ts || Date.now()).toLocaleTimeString();
      const text = (d.graph || '?') + ':' + _fmtShortThread(d.runId) + ' → ' +
                   (d.node || '?') + (d.status ? ' [' + d.status + ']' : '');
      append('evt', ts, text);
    } catch (_) { append('evt', tsNow(), m.data); }
  });
  es.addEventListener('run', (m) => {
    try {
      const d = JSON.parse(m.data);
      const ts = new Date(d.updatedAt || Date.now()).toLocaleTimeString();
      const cls = (d.status === 'error' || d.status === 'aborted') ? 'err' : 'run';
      append(cls, ts, 'run ' + _fmtShortThread(d.runId) + ' (' + (d.graph || '?') + ') → ' + (d.status || '?'));
      if (document.getElementById('pipeline-page').style.display === 'block') {
        clearTimeout(_pipelineState._debounce);
        _pipelineState._debounce = setTimeout(() => _refreshPipelines(), 500);
      }
    } catch (_) {}
  });
}

async function _refreshUniverseInflation() {
  const body = document.getElementById('pl-uni-inf-body');
  const count = document.getElementById('pl-uni-inf-count');
  const ts    = document.getElementById('pl-uni-inf-ts');
  if (!body) return;
  try {
    const rows = await fetch('/api/pipelines/universe-inflation').then(r => r.json());
    if (rows.error) {
      body.innerHTML = '<div class="pl-empty"><div class="pl-empty-icon">⚠</div>' + escapeHtml(rows.error) + '</div>';
      return;
    }
    if (!Array.isArray(rows) || !rows.length) {
      body.innerHTML = '<div class="pl-empty"><div class="pl-empty-icon">🌱</div>No universe resolution runs yet.</div>';
      if (count) count.textContent = '';
      return;
    }
    if (count) count.textContent = '· ' + rows.length + ' days';
    if (ts)    ts.textContent    = 'refreshed ' + new Date().toLocaleTimeString();

    const latest = rows[0];
    const pct    = latest.pct != null ? latest.pct + '%' : '—';
    const tileHtml = '<div class="pl-tiles" style="margin-bottom:12px">' +
      '<div class="pl-tile"><div class="pl-tile-row"><span class="pl-tile-icon">🎯</span><span class="pl-tile-label">Union size (latest)</span></div>' +
      '<div class="pl-tile-value">' + escapeHtml(String(latest.u ?? '—')) + '</div>' +
      '<div class="pl-tile-sub">' + escapeHtml(String(latest.d ?? '')) + '</div></div>' +
      '<div class="pl-tile"><div class="pl-tile-row"><span class="pl-tile-icon">📐</span><span class="pl-tile-label">Alpaca total (latest)</span></div>' +
      '<div class="pl-tile-value">' + escapeHtml(String(latest.total ?? '—')) + '</div>' +
      '<div class="pl-tile-sub">broker universe</div></div>' +
      '<div class="pl-tile"><div class="pl-tile-row"><span class="pl-tile-icon">📊</span><span class="pl-tile-label">Inflation % (latest)</span></div>' +
      '<div class="pl-tile-value">' + escapeHtml(pct) + '</div>' +
      '<div class="pl-tile-sub">union / total × 100</div></div>' +
      '</div>';

    const tableRows = rows.map(r =>
      '<tr><td class="pl-mono" style="font-size:11px">' + escapeHtml(String(r.d ?? '')) + '</td>' +
      '<td class="pl-mono" style="font-size:11px">' + escapeHtml(String(r.u ?? '—')) + '</td>' +
      '<td class="pl-mono" style="font-size:11px">' + escapeHtml(String(r.total ?? '—')) + '</td>' +
      '<td class="pl-mono" style="font-size:11px">' + escapeHtml(r.pct != null ? r.pct + '%' : '—') + '</td></tr>'
    ).join('');
    const tableHtml = '<div style="overflow:auto;max-height:220px"><table class="pl-runs-table">' +
      '<thead><tr><th>Date</th><th>Union</th><th>Total</th><th>Inflation %</th></tr></thead>' +
      '<tbody>' + tableRows + '</tbody></table></div>';

    body.innerHTML = tileHtml + tableHtml;
  } catch (e) {
    if (body) body.innerHTML = '<div class="pl-empty"><div class="pl-empty-icon">⚠</div>Error: ' + escapeHtml(e.message) + '</div>';
  }
}

async function _refreshBackfillHistory() {
  const body  = document.getElementById('pl-bfh-body');
  const count = document.getElementById('pl-bfh-count');
  const ts    = document.getElementById('pl-bfh-ts');
  if (!body) return;
  try {
    const rows = await fetch('/api/pipelines/backfill-history').then(r => r.json());
    if (rows && rows.error) {
      body.innerHTML = '<div class="pl-empty"><div class="pl-empty-icon">⚠</div>' + escapeHtml(rows.error) + '</div>';
      if (count) count.textContent = '';
      return;
    }
    if (!Array.isArray(rows) || !rows.length) {
      body.innerHTML = '<div class="pl-empty"><div class="pl-empty-icon">🌱</div>No backfilled metadata snapshots yet (since 2021-05-01).</div>';
      if (count) count.textContent = '';
      return;
    }

    if (count) count.textContent = '· ' + rows.length + ' months';
    if (ts)    ts.textContent    = 'refreshed ' + new Date().toLocaleTimeString();

    // Sparkline (inline SVG) — months on x-axis, row_count on y.
    const W = 480, H = 60, PAD = 4;
    const counts = rows.map(r => Number(r.row_count) || 0);
    const maxCount = Math.max(1, ...counts);
    const stepX = rows.length > 1 ? (W - 2 * PAD) / (rows.length - 1) : 0;
    const pts = counts.map((c, i) => {
      const x = PAD + i * stepX;
      const y = H - PAD - (c / maxCount) * (H - 2 * PAD);
      return x.toFixed(1) + ',' + y.toFixed(1);
    }).join(' ');
    const last = rows[rows.length - 1];
    const first = rows[0];
    const total = counts.reduce((s, c) => s + c, 0);

    const spark =
      '<svg width="100%" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" style="display:block;height:80px">' +
        '<polyline fill="none" stroke="#6fd1a7" stroke-width="1.5" points="' + pts + '"/>' +
      '</svg>';

    const tableRows = rows.slice().reverse().map(r =>
      '<tr><td class="pl-mono" style="font-size:11px">' + escapeHtml(String(r.month)) + '</td>' +
      '<td class="pl-mono" style="font-size:11px;text-align:right">' + escapeHtml(String(r.row_count.toLocaleString())) + '</td></tr>'
    ).join('');

    body.innerHTML =
      '<div style="margin-bottom:8px;display:flex;gap:20px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--muted)">' +
        '<span><b style="color:var(--fg)">' + total.toLocaleString() + '</b> total rows</span>' +
        '<span><b style="color:var(--fg)">' + rows.length + '</b> months covered</span>' +
        '<span>first: <b style="color:var(--fg)">' + escapeHtml(String(first.month)) + '</b></span>' +
        '<span>latest: <b style="color:var(--fg)">' + escapeHtml(String(last.month)) + '</b> (' + last.row_count.toLocaleString() + ' rows)</span>' +
      '</div>' +
      '<div style="border:1px solid var(--border);border-radius:4px;padding:6px;margin-bottom:10px">' + spark + '</div>' +
      '<div style="overflow:auto;max-height:200px"><table class="pl-runs-table">' +
        '<thead><tr><th>Month</th><th style="text-align:right">Rows</th></tr></thead>' +
        '<tbody>' + tableRows + '</tbody></table></div>';
  } catch (e) {
    if (body) body.innerHTML = '<div class="pl-empty"><div class="pl-empty-icon">⚠</div>Error: ' + escapeHtml(e.message) + '</div>';
  }
}

function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
</script>
</body>
</html>`;
}

// Start server — unless the caller only wants the broadcast/app exports
// (e.g. run_collector_once.js or the orchestrator invoking this module
// transitively). Skipping the listen here lets helper processes reuse
// the SSE/broadcast helpers without colliding with johnbot on :3000.
const httpServer = process.env.OPENCLAW_NO_HTTP_LISTEN === '1'
  ? null
  : app.listen(PORT, '0.0.0.0', () => {
      console.log(`[dashboard] OpenClaw dashboard → http://0.0.0.0:${PORT}`);
    });

if (httpServer) {
  httpServer.on('error', (err) => {
    if (err.code === 'EADDRINUSE') {
      console.error(`[dashboard] Port ${PORT} already in use — stale process still holds the socket. Exiting cleanly so systemd can retry.`);
      process.exit(1);
    }
    console.error('[dashboard] http server error:', err);
  });

  // Pre-warm the portfolio caches so the first dashboard load doesn't pay
  // the full 15-30s Alpaca-intraday latency. Refreshed every minute (well
  // inside each cache's TTL) so the user never hits a cold cache while
  // the dashboard is open. Errors are swallowed — the route handler will
  // surface them on the next user request.
  const warmPortfolioCache = () => {
    // Sampler first: it feeds today's live candle, which _buildCandles reads.
    samplePnlCandle().catch(() => {});
    _cached('candles:90', PNL_CANDLES_TTL,  () => _buildCandles(90)).catch(() => {});
    _cached('vc:1A',      VALUE_CURVE_TTL,  () => _buildValueCurve('1A')).catch(() => {});
    _cached('vc:all',     VALUE_CURVE_TTL,  () => _buildValueCurve('all')).catch(() => {});
  };
  warmPortfolioCache();
  setInterval(warmPortfolioCache, 60_000).unref();
}

function shutdown(reason) {
  console.log(`[dashboard] shutdown(${reason}) — closing http server on :${PORT}`);
  return new Promise((resolve) => {
    const done = () => resolve();
    const t = setTimeout(done, 3000);
    if (httpServer) httpServer.close(() => { clearTimeout(t); done(); }); else done();
  });
}

module.exports.app = app;
module.exports.httpServer = httpServer;
module.exports.shutdown = shutdown;
