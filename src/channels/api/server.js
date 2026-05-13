'use strict';

require('dotenv').config({ path: require('path').join(__dirname, '../../../.env') });

const express = require('express');
const { getAllSubagentStatuses, getBucketStatus } = require('../../database/redis');
const { verdictCache, query: dbQuery } = require('../../database/postgres');
const { readParquet } = require('../../data/parquet_store');
const fs = require('fs');
const { runAlpaca } = require('./alpaca_cli');
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
    // Cap to recent open signals. execution_signals.status='open' currently
    // matches ~4.6k rows because stale paper signals from prior cycles never
    // get flipped to 'closed' — that returned a 1.66 MB JSON payload that
    // dominated the portfolio page's perceived load time. The dashboard
    // collapses to 10 visible rows by default, so restricting to the most
    // recent 500 (within 90d) is more than the UI can ever surface.
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
        AND es.signal_date >= CURRENT_DATE - INTERVAL '90 days'
      ORDER BY es.signal_date DESC
      LIMIT 500
    `);
    res.json(result.rows);
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
             sp.closed_price, sp.realized_pnl_pct, sp.days_held,
             sp.close_reason, sp.closed_at, sp.signal_id::text
      FROM signal_pnl sp
      JOIN execution_signals es ON es.id = sp.signal_id
      ${where}
      ORDER BY sp.closed_at DESC, sp.signal_id DESC
      LIMIT 500
    `, params);
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
               ROUND(AVG(realized_pnl_pct)::numeric, 4) AS avg_pnl,
               ROUND(MAX(realized_pnl_pct)::numeric, 4) AS best,
               ROUND(MIN(realized_pnl_pct)::numeric, 4) AS worst,
               ROUND(AVG(NULLIF(days_held, 0))::numeric, 2) AS avg_days_held
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
      avg_realized:   closed.avg_pnl,
      best_trade:     closed.best,
      worst_trade:    closed.worst,
      avg_days_held:  closed.avg_days_held,
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

    // Per-strategy declared categories (used at category-level since
    // that's the granularity strategies emit today).
    const stratRes = await dbQuery(`
      SELECT id AS strategy_id,
             parameters->'required_columns'   AS req_cols,
             data_requirements_planned        AS planned
      FROM strategy_registry
    `);
    const usersByCategory = {};
    for (const s of stratRes.rows) {
      let cats = [];
      if (Array.isArray(s.req_cols)) cats = s.req_cols.filter(c => typeof c === 'string');
      if (!cats.length && Array.isArray(s.planned)) {
        cats = s.planned.map(p => p && (p.column || p.data_type)).filter(c => typeof c === 'string');
      }
      for (const c of new Set(cats)) {
        if (!usersByCategory[c]) usersByCategory[c] = [];
        usersByCategory[c].push(s.strategy_id);
      }
    }
    for (const k of Object.keys(usersByCategory)) usersByCategory[k].sort();

    const strategiesTotal = stratRes.rows.length;

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
    const statsRows = (await dbQuery(`SELECT * FROM strategy_stats`)).rows;
    const statsById = Object.fromEntries(statsRows.map(r => [r.strategy_id, r]));

    // Load regime_conditions + backtest/live metrics from strategy_registry DB.
    // Saturday-brain additions (058): data_requirements_planned (the missing
    // data spec the dashboard renders for STAGING strategies) and
    // staging_approved_at (operator's data-fetch approval timestamp).
    const srRows = (await dbQuery(`
      SELECT id, regime_conditions,
             backtest_sharpe, backtest_return_pct, backtest_max_dd_pct, backtest_trade_count,
             backtest_regime_breakdown,
             live_days, live_sharpe, live_return_pct,
             data_requirements_planned, staging_approved_at
      FROM strategy_registry
    `)).rows;
    const srById = Object.fromEntries(srRows.map(r => [r.id, r]));

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
    let currentRegime = 'TRANSITIONING';
    try {
      const latestPath = path.join(__dirname, '../../../.agents/market-state/latest.json');
      const latestJson = JSON.parse(fs.readFileSync(latestPath, 'utf8'));
      currentRegime = latestJson.state || 'TRANSITIONING';
    } catch (_) {}

    // Cumulative per-strategy O/U/R counts across all daily cycles.
    //   O = performance_outliers WHERE kind='over'
    //   U = performance_outliers WHERE kind='under'
    //   R = veto_log (excluding legacy 'missing_field' from pre-Phase-2
    //       lint_memo writer — those aren't trade rejections)
    // Grows monotonically as each daily cycle's trade_handoff_builder +
    // trade_agent_llm append rows to the two source tables.
    const d1StrategyStats = {};
    try {
      const [ouRes, rRes] = await Promise.all([
        dbQuery(`SELECT strategy_id, kind, COUNT(*)::int AS n
                   FROM performance_outliers
                  WHERE strategy_id IS NOT NULL
                  GROUP BY strategy_id, kind`).catch(() => ({ rows: [] })),
        dbQuery(`SELECT strategy_id, COUNT(*)::int AS n
                   FROM veto_log
                  WHERE strategy_id IS NOT NULL
                    AND veto_reason <> 'missing_field'
                  GROUP BY strategy_id`).catch(() => ({ rows: [] })),
      ]);
      for (const row of ouRes.rows) {
        const s = d1StrategyStats[row.strategy_id] || (d1StrategyStats[row.strategy_id] = { overperf: 0, underperf: 0, rejected: 0 });
        if (row.kind === 'over')  s.overperf  = row.n;
        if (row.kind === 'under') s.underperf = row.n;
      }
      for (const row of rRes.rows) {
        const s = d1StrategyStats[row.strategy_id] || (d1StrategyStats[row.strategy_id] = { overperf: 0, underperf: 0, rejected: 0 });
        s.rejected = row.n;
      }
    } catch (_) {}

    // Live per-strategy per-regime aggregates, computed from closed trades.
    // Joined to execution_signals for the regime tag captured at signal entry.
    // Keyed by strategy_id → regime_state → {trades, arr, adr, act, win_rate}.
    // arr is a fraction (0.05 = 5%); the dashboard converts to percent for
    // display, matching the convention used elsewhere on the page.
    const liveRegimeBreakdown = {};
    try {
      const lrRes = await dbQuery(`
        SELECT es.strategy_id,
               es.regime_state                                                  AS regime,
               COUNT(*)::int                                                    AS trades,
               AVG(sp.realized_pnl_pct)                                         AS arr,
               AVG(sp.days_held)                                                AS act,
               COUNT(*) FILTER (WHERE sp.realized_pnl_pct > 0)::numeric
                 / NULLIF(COUNT(*), 0)                                          AS win_rate,
               MIN(sp.realized_pnl_pct)                                         AS worst,
               MAX(sp.realized_pnl_pct)                                         AS best
        FROM signal_pnl sp
        JOIN execution_signals es ON sp.signal_id = es.id
        WHERE sp.status = 'closed'
          AND sp.realized_pnl_pct IS NOT NULL
          AND es.regime_state IS NOT NULL
        GROUP BY es.strategy_id, es.regime_state
      `);
      for (const row of lrRes.rows) {
        const arr  = row.arr != null ? parseFloat(row.arr) : null;
        const act  = row.act != null ? parseFloat(row.act) : null;
        const adr  = (arr != null && act != null && act > 0) ? (arr / act) : null;
        if (!liveRegimeBreakdown[row.strategy_id]) liveRegimeBreakdown[row.strategy_id] = {};
        liveRegimeBreakdown[row.strategy_id][row.regime] = {
          trades:    parseInt(row.trades) || 0,
          arr,                          // fraction (0.05 = 5%)
          adr,                          // fraction per day
          act,                          // days
          win_rate:  row.win_rate != null ? parseFloat(row.win_rate) : null,
          best:      row.best != null ? parseFloat(row.best) : null,
          worst:     row.worst != null ? parseFloat(row.worst) : null,
        };
      }
    } catch (_) { /* leave breakdown empty if join fails */ }

    // is_stale: manifest-active, regime-active, but hasn't produced a signal in N days.
    const STALE_DAYS = 7;
    const staleCutoff = Date.now() - STALE_DAYS * 24 * 3600 * 1000;

    const rows = [];
    const seen = new Set();
    for (const [sid, rec] of Object.entries(manifest.strategies || {})) {
      seen.add(sid);
      const s   = statsById[sid] || {};
      const sr  = srById[sid]    || {};
      const rc  = sr.regime_conditions || {};
      const activeRegimes = rc.active_in_regimes || ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL'];
      const regimeActive  = activeRegimes.includes(currentRegime);
      const lastTs   = s.last_signal_date ? new Date(s.last_signal_date).getTime() : 0;
      const isStale  = regimeActive && (!lastTs || lastTs < staleCutoff);
      const d1 = d1StrategyStats[sid] || null;
      rows.push({
        strategy_id:        sid,
        state:              rec.state || 'unknown',
        is_stale:           isStale,
        regime_active:      regimeActive,
        active_in_regimes:  activeRegimes,
        eligible_regimes:   Array.isArray(rec.eligible_regimes) ? rec.eligible_regimes : null,
        current_regime:     currentRegime,
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
        backtest_sharpe:           sr.backtest_sharpe           ?? null,
        backtest_return_pct:       sr.backtest_return_pct       ?? null,
        backtest_max_dd_pct:       sr.backtest_max_dd_pct       ?? null,
        backtest_trade_count:      sr.backtest_trade_count      ?? null,
        backtest_regime_breakdown: sr.backtest_regime_breakdown ?? null,
        live_regime_breakdown:     liveRegimeBreakdown[sid] || null,
        live_days:           sr.live_days           ?? null,
        live_sharpe:         sr.live_sharpe         ?? null,
        live_return_pct:     sr.live_return_pct     ?? null,
        d1_overperf:         d1 ? (d1.overperf  || 0) : 0,
        d1_underperf:        d1 ? (d1.underperf || 0) : 0,
        d1_rejected:         d1 ? (d1.rejected  || 0) : 0,
        // Saturday-brain Tier-B fields. Only populated for state='staging'
        // strategies pushed by Saturday brain. The dashboard's strategies
        // page renders these so the operator sees exactly what the Approve
        // click would fetch + when they last approved.
        data_requirements_planned: sr.data_requirements_planned || null,
        staging_approved_at:       sr.staging_approved_at || null,
        // Columns from data_requirements_planned that no collector/provider
        // knows about — would fail the staging worker's source-validation
        // step. Dashboard renders a ⚠ data badge on staging rows.
        unsupported_sources:       unsupportedByStrategyId[sid] || null,
      });
    }
    // Orphans: strategy_ids with signals but no manifest entry
    for (const s of statsRows) {
      if (seen.has(s.strategy_id)) continue;
      const sr = srById[s.strategy_id] || {};
      const d1 = d1StrategyStats[s.strategy_id] || null;
      rows.push({
        strategy_id:        s.strategy_id,
        state:              'orphan',
        is_stale:           false,
        regime_active:      true,
        active_in_regimes:  [],
        eligible_regimes:   null,
        current_regime:     currentRegime,
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
        backtest_sharpe:           sr.backtest_sharpe           ?? null,
        backtest_return_pct:       sr.backtest_return_pct       ?? null,
        backtest_max_dd_pct:       sr.backtest_max_dd_pct       ?? null,
        backtest_trade_count:      sr.backtest_trade_count      ?? null,
        backtest_regime_breakdown: sr.backtest_regime_breakdown ?? null,
        live_regime_breakdown:     liveRegimeBreakdown[s.strategy_id] || null,
        live_days:           sr.live_days           ?? null,
        live_sharpe:         sr.live_sharpe         ?? null,
        live_return_pct:     sr.live_return_pct     ?? null,
        d1_overperf:         d1 ? (d1.overperf  || 0) : 0,
        d1_underperf:        d1 ? (d1.underperf || 0) : 0,
        d1_rejected:         d1 ? (d1.rejected  || 0) : 0,
      });
    }
    res.json(rows);
  } catch (err) {
    res.status(500).json({ error: err.message });
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
const CANDIDATE_TO_LIVE_MIN_SHARPE = 0.5;
const CANDIDATE_TO_LIVE_MAX_DD_PCT = 20;   // DB stores as percent (e.g. 15.0 = 15%)

app.post('/api/strategies/:id/transition', async (req, res) => {
  const fs   = require('fs');
  const path = require('path');
  const sid     = req.params.id;
  const toState = req.body && req.body.to_state;
  const force   = req.body && req.body.force === true;
  const reason  = (req.body && req.body.reason) || '';
  const actor   = (req.body && req.body.actor)  || 'manual:dashboard';

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

  // Guard: candidate → live requires Sharpe/DD thresholds unless force=true.
  // Under the fused-approval lifecycle, this is the single remaining
  // operator-gated promotion step.
  const failedGates = [];
  if (tKey === 'candidate:live' && !force) {
    let sr = {};
    try {
      const srRes = await dbQuery(
        `SELECT backtest_sharpe, backtest_max_dd_pct FROM strategy_registry WHERE id = $1`,
        [sid]
      );
      sr = srRes.rows[0] || {};
    } catch (_) {}
    const sharpe = parseFloat(sr.backtest_sharpe);
    const maxDd  = parseFloat(sr.backtest_max_dd_pct);
    if (!isNaN(sharpe) && sharpe < CANDIDATE_TO_LIVE_MIN_SHARPE) failedGates.push('sharpe');
    if (!isNaN(maxDd)  && maxDd  > CANDIDATE_TO_LIVE_MAX_DD_PCT) failedGates.push('max_dd');
    if (failedGates.length > 0) {
      return res.status(422).json({
        error: `candidate→live blocked: ${failedGates.join(', ')} gate(s) failed`,
        failed_gates: failedGates,
        allow_override: true,
      });
    }
  }

  // Cross-process locked read-modify-write — see src/lib/manifest_lock.js.
  // Re-read the manifest under the lock so concurrent writers (lifecycle.py
  // auto_backtest, saturday_brain.js _stage, the approvals worker) can't be
  // clobbered by stale in-memory state from this request handler. The
  // validation above happened against the snapshot we read at line 540;
  // re-validate on the fresh disk view in case the strategy was archived
  // by another writer in the millisecond between read and write.
  const now = new Date().toISOString();
  const event = {
    from_state: fromState,
    to_state:   toState,
    timestamp:  now,
    actor,
    reason:     reason || STRATEGY_VALID_TRANSITIONS.get(tKey),
    metadata:   force ? { override: true, failed_gates: failedGates } : {},
  };
  try {
    const { withManifestLock } = require('../../lib/manifest_lock');
    await withManifestLock(manifestPath, (m) => {
      const r = (m.strategies || {})[sid];
      if (!r) throw new Error(`strategy ${sid} not in manifest`);
      r.state       = toState;
      r.state_since = now;
      r.history     = r.history || [];
      r.history.push(event);
      m.updated_at = now;
      return m;
    }, { actor: `dashboard:${actor || 'unknown'}` });
  } catch (e) {
    return res.status(500).json({ error: 'Manifest write failed: ' + e.message });
  }

  // Audit trail (non-fatal if DB unavailable).
  try {
    await dbQuery(
      `INSERT INTO lifecycle_events (strategy_id, from_state, to_state, actor, reason, metadata)
       VALUES ($1, $2, $3, $4, $5, $6)`,
      [sid, fromState, toState, actor, event.reason, JSON.stringify(event.metadata)]
    );
  } catch (e) {
    console.warn('lifecycle_events insert failed (non-fatal):', e.message);
  }

  // ── Sync strategy_registry.status ───────────────────────────────────────
  // The daily execution pipeline reads strategy_registry.status='approved'
  // as the gate for which strategies actually fire. Map lifecycle state →
  // registry status:
  //   live, monitoring         → 'approved' (runs daily)
  //   candidate, staging       → 'pending_approval'
  //   deprecated, archived     → 'deprecated'
  // (`paper` is legacy/frozen — no rows should be transitioning into it
  // post-rewrite. Map it defensively to 'pending_approval' for any orphan.)
  const REGISTRY_STATUS_FOR = {
    live:       'approved',
    monitoring: 'approved',
    paper:      'pending_approval',
    candidate:  'pending_approval',
    staging:    'pending_approval',
    deprecated: 'deprecated',
    archived:   'deprecated',
  };
  const targetStatus = REGISTRY_STATUS_FOR[toState];
  if (targetStatus) {
    try {
      if (targetStatus === 'approved') {
        await dbQuery(
          `UPDATE strategy_registry
              SET status      = $2,
                  approved_by = COALESCE(approved_by, $3),
                  approved_at = COALESCE(approved_at, NOW())
            WHERE id = $1`,
          [sid, targetStatus, actor],
        );
      } else {
        await dbQuery(
          `UPDATE strategy_registry SET status = $2 WHERE id = $1`,
          [sid, targetStatus],
        );
      }
    } catch (e) {
      console.warn('strategy_registry status sync failed (non-fatal):', e.message);
    }
  }

  // Broadcast SSE so the dashboard refreshes even without polling.
  try { broadcast({ type: 'strategy_transition', strategy_id: sid, from_state: fromState, to_state: toState, at: now }); } catch (_) {}

  res.json({ ok: true, strategy_id: sid, from_state: fromState, to_state: toState, at: now });
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
    res.json({
      equity:             parseFloat(a.equity)             || 0,
      cash:               parseFloat(a.cash)               || 0,
      buying_power:       parseFloat(a.buying_power)       || 0,
      last_equity:        parseFloat(a.last_equity)        || 0,
      long_market_value:  parseFloat(a.long_market_value)  || 0,
      short_market_value: parseFloat(a.short_market_value) || 0,
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
  // **session-bracketed Open/Close** while the **wicks span the full
  // 24-hour day** (capturing after-hours and overnight movement):
  //   continuous   → high / low across the full day, ensures adjacent
  //                  candles join end-to-end (today's intraday range
  //                  starts at yesterday's close)
  //   market_hours → only regular session (9:30-16:00 ET) samples, used
  //                  for the candle's Open (first session sample) and
  //                  Close (last session sample). Days with no market
  //                  samples (weekends/holidays) fall back to the
  //                  continuous-stream open/close so the candle still
  //                  renders as a flat mark.
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

  // Per-day continuous range: high, low, and a fallback open/close drawn
  // from the first/last continuous sample (used only if market_hours has
  // no sample for that day).
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
      const d = new Date(ts[i] * 1000)
        .toLocaleDateString('en-CA', { timeZone: 'America/New_York' });
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
      const slot = mktByDay.get(d) || { open: v, close: v };
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

  // Merge strategy:
  //   - Chart open/high/low/close come from the continuous stream so
  //     adjacent candles join end-to-end (today's open == yesterday's
  //     close, no after-hours discontinuities in the body or wicks).
  //   - market_open / market_close are surfaced as separate fields so
  //     the hover tooltip can report regular-session open/close even
  //     though the bar geometry uses continuous values. The client
  //     prefers market_* in the tooltip and falls back to open/close
  //     on weekends/holidays where the session has no samples.
  //   - Daily fallback mark is used only when the intraday window
  //     doesn't cover the day (older than ~29 days back).
  const allDays = new Set([...contByDay.keys(), ...mktByDay.keys(), ...dailyByDay.keys()]);
  const sorted  = [...allDays].sort();
  const candles = [];
  let prevClose = null;
  for (const d of sorted) {
    let row;
    const cont = contByDay.get(d);
    const mkt  = mktByDay.get(d);
    if (cont) {
      row = {
        date: d,
        open: cont.open,
        high: cont.high,
        low:  cont.low,
        close: cont.close,
        // Session-bracketed open/close (market hours only) — used by
        // the tooltip, not by the bar geometry.
        market_open:  mkt ? mkt.open  : null,
        market_close: mkt ? mkt.close : null,
        intraday: true,
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
      };
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
      .toISOString().slice(0, 10);
    const path = require('path');
    const stateDir = path.join(__dirname, '../../../.agents/market-state');

    const byDate = new Map();
    // Bulk: parquet
    const parq = await _loadRegimeParquet();
    for (const r of parq) {
      if (!r.date || r.date < cutoff) continue;
      byDate.set(r.date, r.regime || 'NO_DATA');
    }
    // Per-day overrides: regime_YYYY-MM-DD.json (newer than parquet refit).
    if (fs.existsSync(stateDir)) {
      const files = fs.readdirSync(stateDir)
        .filter(f => /^regime_\d{4}-\d{2}-\d{2}\.json$/.test(f));
      for (const f of files) {
        const d = f.slice(7, 17);
        if (d < cutoff) continue;
        try {
          const raw = fs.readFileSync(path.join(stateDir, f), 'utf8');
          const j = JSON.parse(raw);
          if (j.state) byDate.set(d, j.state);
        } catch (_) { /* skip malformed */ }
      }
    }
    // Forward-fill across every calendar day in the window so weekends,
    // holidays, and any single-day gaps inherit the prior trading day's
    // regime. The regime is a *state* — Friday's reading carries through
    // Saturday and Sunday until Monday's update — so chart backdrops
    // should paint continuously rather than leaving uncoloured strips
    // between trading sessions.
    const start = new Date(cutoff + 'T00:00:00Z');
    const today = new Date(new Date().toISOString().slice(0, 10) + 'T00:00:00Z');
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

// Per-strategy rolling ARR curve.
// ARR = Average Return Rate, the mean of `realized_pnl_pct` across closed
// trades. "ARR over time" plots a trailing-N-day rolling mean so the
// metric tracks how the strategy's per-trade economics shift across
// regimes. The response also carries the regime per day so the chart
// can paint the regime backdrop without a second fetch.
//
// Window default = 30d. The curve starts at the strategy's first close
// date (or first regime sample, whichever later) and runs to today.
// Days with no trades in the trailing window emit `rolling_arr_pct: null`
// so the line breaks correctly across long quiet stretches.
app.get('/api/strategies/:id/arr-curve', async (req, res) => {
  const sid    = String(req.params.id || '');
  const window = Math.min(Math.max(parseInt(req.query.window) || 30, 7), 365);
  if (!sid) return res.status(400).json({ error: 'strategy id required' });
  try {
    // Pull every closed trade for the strategy ordered by close date.
    // realized_pnl_pct is stored as a fraction (0.05 = 5%); we leave it
    // as a fraction in the response and let the client multiply by 100
    // for display, matching the convention used elsewhere.
    const trades = (await dbQuery(`
      SELECT closed_at::date AS d, realized_pnl_pct
      FROM signal_pnl
      WHERE strategy_id = $1 AND status = 'closed' AND closed_at IS NOT NULL
        AND realized_pnl_pct IS NOT NULL
      ORDER BY closed_at
    `, [sid])).rows;
    if (!trades.length) return res.json({ window_days: window, rows: [] });

    // Daily position-flow counts: # signals opened (signal_date) and
    // # signals closed (closed_at). Bucketed per day for the bar chart
    // stacked under the ARR curve so the operator can see trade flow
    // alongside per-trade economics.
    const openedByDay = new Map();
    const closedByDay = new Map();
    const [openedRes, closedRes] = await Promise.all([
      dbQuery(`SELECT signal_date::date AS d, COUNT(*)::int AS n
                 FROM execution_signals
                WHERE strategy_id = $1 AND signal_date IS NOT NULL
                GROUP BY signal_date`, [sid]).catch(() => ({ rows: [] })),
      dbQuery(`SELECT closed_at::date AS d, COUNT(*)::int AS n
                 FROM signal_pnl
                WHERE strategy_id = $1 AND status = 'closed' AND closed_at IS NOT NULL
                GROUP BY closed_at`, [sid]).catch(() => ({ rows: [] })),
    ]);
    for (const row of openedRes.rows) {
      openedByDay.set(row.d.toISOString().slice(0, 10), parseInt(row.n) || 0);
    }
    for (const row of closedRes.rows) {
      closedByDay.set(row.d.toISOString().slice(0, 10), parseInt(row.n) || 0);
    }

    // Bucket trades by close date, then sweep a rolling window across the
    // calendar from first close → today.
    const byDay = new Map();
    for (const t of trades) {
      const d = t.d.toISOString().slice(0, 10);
      if (!byDay.has(d)) byDay.set(d, []);
      byDay.get(d).push(parseFloat(t.realized_pnl_pct));
    }
    const startIso = trades[0].d.toISOString().slice(0, 10);
    const endIso   = new Date().toISOString().slice(0, 10);

    const regimeMap = new Map();
    for (const r of await _loadRegimeParquet()) {
      if (r.date >= startIso) regimeMap.set(r.date, r.regime || null);
    }
    // Daily JSON overrides win over parquet for the recent days.
    const stateDir = require('path').join(__dirname, '../../../.agents/market-state');
    if (fs.existsSync(stateDir)) {
      for (const f of fs.readdirSync(stateDir)
        .filter(f => /^regime_\d{4}-\d{2}-\d{2}\.json$/.test(f))) {
        const d = f.slice(7, 17);
        if (d < startIso) continue;
        try {
          const j = JSON.parse(fs.readFileSync(require('path').join(stateDir, f), 'utf8'));
          if (j.state) regimeMap.set(d, j.state);
        } catch (_) {}
      }
    }

    // Iterate calendar days [startIso, endIso]. We accumulate an explicit
    // window deque so the rolling mean is O(N) total rather than
    // O(N*window).
    const out  = [];
    const deque = [];   // [{d, ret}]
    let sum   = 0;
    let lastRegime = null; // forward-fill carry: weekends + gap days
                           // inherit the prior trading day's regime so
                           // the ARR chart's regime backdrop paints
                           // continuously rather than going blank
                           // across non-trading days.
    const start = new Date(startIso + 'T00:00:00Z');
    const end   = new Date(endIso   + 'T00:00:00Z');
    for (let cur = new Date(start); cur <= end; cur.setUTCDate(cur.getUTCDate() + 1)) {
      const dIso = cur.toISOString().slice(0, 10);
      // Push today's trades.
      const todays = byDay.get(dIso);
      if (todays) {
        for (const v of todays) { deque.push({ d: dIso, ret: v }); sum += v; }
      }
      // Drop trades that fell out of the trailing window.
      const cutoff = new Date(cur);
      cutoff.setUTCDate(cutoff.getUTCDate() - window);
      const cutoffIso = cutoff.toISOString().slice(0, 10);
      while (deque.length && deque[0].d < cutoffIso) {
        sum -= deque.shift().ret;
      }
      // Forward-fill regime: use today's reading if we have one, else
      // carry forward the most recent prior reading.
      if (regimeMap.has(dIso)) lastRegime = regimeMap.get(dIso);
      out.push({
        date:             dIso,
        rolling_arr_pct:  deque.length ? (sum / deque.length) : null,
        trade_count:      deque.length,
        regime:           lastRegime,
        opened:           openedByDay.get(dIso) || 0,
        closed:           closedByDay.get(dIso) || 0,
      });
    }
    res.json({ window_days: window, rows: out });
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

// ── Research page ──────────────────────────────────────────────────────────────
app.use('/api/research', require('./routes_research'));

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

// Phase 2F — Mastermind prompt recalibration loop
app.use('/api', require('./routes_recalibration'));

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
.st-similar-row{display:grid;grid-template-columns:1fr repeat(4,40px);gap:4px;align-items:center;font-size:10px;padding:3px 6px;border-radius:4px;background:var(--border2)}
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
  #st-active-wrap, #st-inactive-wrap, #st-candidate-wrap {
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
  /* Pf-positions has a 2-char Strategy column (already short) — give it
   * 60px instead of 78 so Ticker (col 2) gets more room. */
  #pf-positions .db-table th:first-child,
  #pf-positions .db-table td:first-child {
    width: 60px !important; max-width: 60px !important; min-width: 60px !important;
  }

  /* ── Table buttons — compact ───────────────────────────────────────── */
  .st-action-btn {
    padding: 3px 7px !important; font-size: 9.5px !important;
    min-height: 24px; border-radius: 4px; transition: transform .12s;
  }
  .st-action-btn:active { transform: scale(0.94); }

  /* ── Per-table column hides ──────────────────────────────────────── *
   * Active Stack: Strategy | Status | Regimes | Open | Closed | Win% | ARR% | ADR% | ACT | #O/U/R | Last Signal | Actions
   * Hide: 3 (Regimes), 11 (Last Signal). */
  #st-active-wrap .db-table th:nth-child(3),
  #st-active-wrap .db-table td:nth-child(3),
  #st-active-wrap .db-table th:nth-child(11),
  #st-active-wrap .db-table td:nth-child(11) { display: none; }

  /* Active positions: Strategy | Ticker | Dir | Entry | Current | P&L% | Size% | Days | Stop | Status
   * Hide: 4 (Entry), 9 (Stop). */
  #pf-positions .db-table th:nth-child(4),
  #pf-positions .db-table td:nth-child(4),
  #pf-positions .db-table th:nth-child(9),
  #pf-positions .db-table td:nth-child(9) { display: none; }

  /* Closed history — hide far-right columns. */
  #pf-history .db-table th:nth-child(n+8),
  #pf-history .db-table td:nth-child(n+8) { display: none; }

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
    <div class="pf-stat-card"><div class="pf-stat-label">Win Rate</div><div class="pf-stat-value" id="pf-winrate">—</div><div class="pf-stat-sub" id="pf-winrate-sub"></div></div>
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
      <div class="st-tile"><div class="st-tile-label">Total</div><div class="st-tile-value" id="st-total">—</div></div>
      <div class="st-tile"><div class="st-tile-label">Active Stack</div><div class="st-tile-value" id="st-active-tile">—</div><div class="st-tile-sub" id="st-active-sub">— live / — stale / — waiting</div></div>
      <div class="st-tile"><div class="st-tile-label">Inactive Stack</div><div class="st-tile-value" id="st-inactive-tile">—</div><div class="st-tile-sub">decommissioned</div></div>
      <div class="st-tile"><div class="st-tile-label">Research Candidates</div><div class="st-tile-value" id="st-candidate-tile">—</div><div class="st-tile-sub">awaiting approval</div></div>
      <div class="st-tile"><div class="st-tile-label">Data</div><div class="st-tile-value" id="st-data-tile">—</div><div class="st-tile-sub" id="st-data-sub">financial parameters ingested</div><a class="st-tile-link" id="st-data-usage-link" onclick="_stOpenDataUsage()">View Data Usage →</a></div>
    </div>

    <!-- Phase 2B — Mastermind proposals panel (collapsed if none pending) -->
    <div class="pf-section" id="rp-section" style="display:none">
      <div class="pf-section-header">
        <span>📋 Pending Regime Proposals <span class="st-sub-label" id="rp-count">—</span></span>
        <span class="st-sub-label">from MastermindJohn Saturday review</span>
      </div>
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

    <!-- Phase 2F — Mastermind calibration addenda (always visible; compact when empty).
         IDs use 'addenda-' prefix instead of 'ad-' because ad-blockers (uBlock,
         AdBlock, Brave shields) match id="ad-*" as advertisement containers
         and apply display:none !important cosmetic filters. -->
    <div class="pf-section" id="addenda-section">
      <div class="pf-section-header">
        <span>📝 Calibration Addenda <span class="st-sub-label" id="addenda-count">—</span></span>
        <span class="st-sub-label" style="display:flex;align-items:center;gap:8px">
          <span>prepended to next Saturday Mastermind prompt</span>
          <button id="addenda-add-btn" style="padding:3px 9px;font-size:11px;background:var(--accent,#2ea043);color:white;border:none;border-radius:3px;cursor:pointer">+ Add</button>
        </span>
      </div>
      <div id="addenda-add-form" style="display:none;padding:8px;border:1px solid var(--border2);border-radius:4px;margin-bottom:6px;background:var(--bg2,#f8f8f8)">
        <textarea id="addenda-add-text" placeholder="Addendum text (will be prepended verbatim to next Mastermind prompt)" rows="3" style="width:100%;font-size:12px;font-family:inherit;border:1px solid var(--border2);border-radius:3px;padding:6px;box-sizing:border-box"></textarea>
        <input id="addenda-add-rationale" type="text" placeholder="Rationale (operator-facing audit string)" style="width:100%;font-size:12px;border:1px solid var(--border2);border-radius:3px;padding:6px;box-sizing:border-box;margin-top:5px"/>
        <div style="display:flex;justify-content:flex-end;gap:6px;margin-top:5px">
          <button id="addenda-add-cancel" style="padding:3px 9px;font-size:11px;background:var(--bg2,#ccc);color:var(--fg,#000);border:1px solid var(--border2);border-radius:3px;cursor:pointer">Cancel</button>
          <button id="addenda-add-save" style="padding:3px 9px;font-size:11px;background:var(--accent,#2ea043);color:white;border:none;border-radius:3px;cursor:pointer">Save (active)</button>
        </div>
      </div>
      <table id="addenda-table" style="border-collapse:collapse;width:100%;font-size:12px">
        <thead>
          <tr style="text-align:left;color:var(--muted);font-size:11px">
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Status</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Source</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Addendum</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Rationale</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2)">Created</th>
            <th style="padding:6px 8px;border-bottom:1px solid var(--border2);text-align:right">Decide</th>
          </tr>
        </thead>
        <tbody><tr><td colspan="6" style="padding:8px;color:var(--muted);font-size:11px">Loading…</td></tr></tbody>
      </table>
    </div>

    <!-- Section 1: Active Stack (live + stale + waiting) -->
    <div class="pf-section">
      <div class="pf-section-header">
        <span>Active Stack <span class="st-sub-label">(live / stale / waiting on regime)</span></span>
        <span id="st-active-count" class="st-sub-label"></span>
      </div>
      <div class="st-regime-filter" id="st-regime-filter">
        <span class="srf-label">View by Regime</span>
        <button class="srf-btn active" data-regime="ALL"           onclick="_stSetRegimeFilter('ALL')">All</button>
        <button class="srf-btn srf-LOW_VOL"       data-regime="LOW_VOL"       onclick="_stSetRegimeFilter('LOW_VOL')">Low Vol</button>
        <button class="srf-btn srf-TRANSITIONING" data-regime="TRANSITIONING" onclick="_stSetRegimeFilter('TRANSITIONING')">Transitioning</button>
        <button class="srf-btn srf-HIGH_VOL"      data-regime="HIGH_VOL"      onclick="_stSetRegimeFilter('HIGH_VOL')">High Vol</button>
        <button class="srf-btn srf-CRISIS"        data-regime="CRISIS"        onclick="_stSetRegimeFilter('CRISIS')">Crisis</button>
        <span class="srf-hint" id="srf-hint"></span>
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

    <!-- Section 3: Research Candidates (paper / candidate) -->
    <div class="pf-section">
      <div class="pf-section-header">
        <span>Research Candidates <span class="st-sub-label">(passed research + coder + backtest — awaiting approval)</span></span>
        <span id="st-candidate-count" class="st-sub-label"></span>
      </div>
      <div class="pf-section-body"><div id="st-candidate-wrap"><div class="empty">Loading...</div></div></div>
    </div>
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
let pnlChart       = null;
let pnlRange       = '90d';   // '90d' | '1y' | 'life'
let pnlCandlesData = null;    // [{date, open, high, low, close, pct_change, intraday}]
let valueCurveData = null;    // /api/portfolio/value-curve?period=1A — feeds 1y view + lifetime AAR
let lifetimeData   = null;    // /api/portfolio/value-curve?period=all
let regime90dData  = null;    // [{date, regime}]
let regime1yData   = null;    // [{date, regime}]

function _setNavActive(which) {
  for (const k of ['market','portfolio','strategies','research']) {
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
  if (!items.length) { document.getElementById('staging-body').innerHTML = '<div style="color:var(--muted);font-size:11px">— nothing staged —</div>'; return; }
  document.getElementById('staging-body').innerHTML = items.map(it => {
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
      if (!confirm(\`\${action.toUpperCase()} this proposal?\`)) return;
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

async function loadPortfolio() {
  const [summary, positions, history, candles, account, valCurve, lifeCurve, reg90, reg1y] = await Promise.all([
    fetch('/api/portfolio/summary').then(r=>r.json()).catch(()=>({})),
    fetch('/api/portfolio/positions').then(r=>r.json()).catch(()=>[]),
    fetch('/api/portfolio/history').then(r=>r.json()).catch(()=>[]),
    fetch('/api/portfolio/pnl-candles?days=90').then(r=>r.json()).catch(()=>[]),
    fetch('/api/portfolio/account').then(r=>r.json()).catch(()=>({})),
    fetch('/api/portfolio/value-curve?period=1A').then(r=>r.json()).catch(()=>({})),
    fetch('/api/portfolio/value-curve?period=all').then(r=>r.json()).catch(()=>({})),
    fetch('/api/portfolio/regime-history?days=90').then(r=>r.json()).catch(()=>[]),
    fetch('/api/portfolio/regime-history?days=365').then(r=>r.json()).catch(()=>[]),
  ]);
  renderAccountRow(account);
  // Store valCurve first — renderPortfolioSummary needs it to compute
  // the portfolio-level Avg Annualized Realized P&L from the equity
  // curve (base_value → latest equity), not per-trade averages.
  valueCurveData = valCurve;
  renderPortfolioSummary(summary, valCurve);
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

  document.getElementById('pf-invested').textContent     = fmt(invested);
  document.getElementById('pf-invested-sub').textContent = a.short_market_value
    ? 'short ' + fmt(a.short_market_value) : '';
}

function renderPortfolioSummary(s, valCurve) {
  const wr  = s.win_rate != null ? s.win_rate + '%' : '—';
  document.getElementById('pf-open').textContent    = s.open_count ?? '—';
  document.getElementById('pf-closed').textContent  = s.closed_count ?? '—';
  document.getElementById('pf-winrate').textContent = wr;
  document.getElementById('pf-winrate-sub').textContent = s.closed_count
    ? s.win_rate + '% of ' + s.closed_count + ' trades' : 'No closed trades';

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

function renderPositions(rows) {
  const el = document.getElementById('pf-positions');
  document.getElementById('pf-pos-count').textContent = rows.length ? rows.length + ' open' : '';
  if (!rows.length) { el.innerHTML = '<div class="empty">No open positions</div>'; return; }
  const sorted = _applySort('pf-positions', rows);
  const { shown, footer } = _collapseRows('pf-positions', sorted);
  el.innerHTML = \`<table class="db-table" style="min-width:700px">
    <tr>
      <th data-sort-key="strategy_id" data-sort-type="str">Strategy</th>
      <th data-sort-key="ticker" data-sort-type="str">Ticker</th>
      <th data-sort-key="direction" data-sort-type="str">Dir</th>
      <th class="num" data-sort-key="entry_price" data-sort-type="num">Entry</th>
      <th class="num" data-sort-key="current_price" data-sort-type="num">Current</th>
      <th class="num" data-sort-key="unrealized_pnl_pct" data-sort-type="num">P&amp;L %</th>
      <th class="num" data-sort-key="position_size_pct" data-sort-type="num">Size %</th>
      <th class="num" data-sort-key="days_held" data-sort-type="num">Days</th>
      <th class="num" data-sort-key="stop_loss" data-sort-type="num">Stop</th>
      <th data-sort-key="status" data-sort-type="str">Status</th>
    </tr>
    \${shown.map(r => {
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
  </table>\${footer}\`;
  _bindSortable('pf-positions', renderPositions);
  _bindCollapse('pf-positions', renderPositions);
}

function renderHistory(rows) {
  const el = document.getElementById('pf-history');
  document.getElementById('pf-hist-count').textContent = rows.length ? rows.length + ' trades' : '';
  if (!rows.length) { el.innerHTML = '<div class="empty">No closed trades yet</div>'; return; }
  const sorted = _applySort('pf-history', rows);
  const { shown, footer } = _collapseRows('pf-history', sorted);
  el.innerHTML = \`<table class="db-table" style="min-width:680px">
    <tr>
      <th data-sort-key="strategy_id" data-sort-type="str">Strategy</th>
      <th data-sort-key="ticker" data-sort-type="str">Ticker</th>
      <th data-sort-key="direction" data-sort-type="str">Dir</th>
      <th class="num" data-sort-key="entry_price" data-sort-type="num">Entry</th>
      <th class="num" data-sort-key="closed_price" data-sort-type="num">Close</th>
      <th class="num" data-sort-key="realized_pnl_pct" data-sort-type="num">P&amp;L %</th>
      <th class="num" data-sort-key="days_held" data-sort-type="num">Days</th>
      <th data-sort-key="close_reason" data-sort-type="str">Reason</th>
      <th data-sort-key="closed_at" data-sort-type="date">Closed</th>
    </tr>
    \${shown.map(r => {
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
  </table>\${footer}\`;
  _bindSortable('pf-history', renderHistory);
  _bindCollapse('pf-history', renderHistory);
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
let _stActiveJobs = {};   // strategy_id → {job_id, phase, progress, payload}
let _stLastFailures = {}; // strategy_id → {job_id, reason}  (persisted server-side; survives reload + restart)
let _stDataUsage = null; // {parameters, strategies_total, categories_total}

async function loadStrategies() {
  try {
    const [rows, jobs, failures, dataUsage, proposals, driftResp] = await Promise.all([
      fetch('/api/strategies').then(r => r.json()).catch(() => []),
      fetch('/api/approvals/active').then(r => r.json()).catch(() => []),
      fetch('/api/approvals/recent-failures').then(r => r.json()).catch(() => []),
      fetch('/api/data/usage').then(r => r.json()).catch(() => null),
      fetch('/api/regime-proposals?status=pending').then(r => r.json()).catch(() => ({ proposals: [] })),
      fetch('/api/regime-drift').then(r => r.json()).catch(() => ({ signals: [] })),
    ]);
    _rpRender(proposals?.proposals || []);
    _addendaLoad();              // Phase 2F — calibration addenda panel
    _addendaWireForm();          // idempotent
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
  // Re-render the candidates section so chips update in place.
  const rows = strategiesData.filter(_inCandidate);
  _renderCandidates(rows);
}

function _inActiveStack(r) { return r.state === 'live' || r.state === 'monitoring'; }
function _inInactive(r)    { return r.state === 'deprecated' || r.state === 'archived' || r.state === 'orphan'; }
function _inCandidate(r)   { return r.state === 'paper' || r.state === 'candidate' || r.state === 'staging'; }

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
  document.getElementById('st-candidate-count').textContent = candidate.length + ' strategies';

  // Data tile: count of fine-grained financial parameters ingested
  // (open, close, implied_volatility, ebitda, …) from /api/data/usage.
  const paramCount = _stDataUsage && _stDataUsage.parameters ? _stDataUsage.parameters.length : 0;
  document.getElementById('st-data-tile').textContent = paramCount || '—';
  document.getElementById('st-data-sub').textContent  = 'financial parameters ingested';

  _renderActiveStack(active);
  _renderInactiveStack(inactive);
  _renderCandidates(candidate);
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
// gets a small dot indicator. Cells without live trade data render as 'na'.
//
// Source: live trades only — live_regime_breakdown[regime] = {trades,
// arr, adr, act, win_rate}. arr is a fraction (0.05 = 5%).
const _REGIME_AXIS = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'];
const _REGIME_TAGS = { LOW_VOL: 'LV', TRANSITIONING: 'TR', HIGH_VOL: 'HV', CRISIS: 'CR' };
function _regimeBreakdown(r) {
  const breakdown = r.live_regime_breakdown || {};
  // Eligibility source-of-truth = manifest.eligible_regimes (what
  // regime_gate.is_eligible() actually enforces). Fall back to
  // active_in_regimes from strategy_registry when manifest has no field
  // (legacy strategies — gate treats those as eligible-everywhere).
  const eligible = Array.isArray(r.eligible_regimes)
    ? r.eligible_regimes
    : (r.active_in_regimes && r.active_in_regimes.length ? r.active_in_regimes : _REGIME_AXIS);
  const current   = r.current_regime || null;
  const sid       = r.strategy_id;
  const editable  = r.state === 'live';
  const cells = _REGIME_AXIS.map(rg => {
    const isEligible = eligible.includes(rg);
    const b      = breakdown[rg];
    const trades = b && b.trades ? parseInt(b.trades) : 0;
    const arrPct = (b && b.arr != null) ? parseFloat(b.arr) * 100 : null;
    const klass = ['st-regime-cell', \`st-rg-\${rg}\`];
    if (current === rg) klass.push('st-rg-current');
    if (!trades)        klass.push('st-rg-na');
    if (!isEligible)    klass.push('st-rg-ineligible');
    if (editable)       klass.push('st-rg-editable');
    const valTxt = arrPct != null
      ? (arrPct >= 0 ? '+' : '') + arrPct.toFixed(2) + '%'
      : '—';
    const statsLine = !trades
      ? 'no live trades'
      : 'ARR ' + valTxt
        + ' · ADR ' + (b.adr != null ? ((b.adr * 100 >= 0 ? '+' : '') + (b.adr * 100).toFixed(3) + '%') : '—')
        + ' · ACT ' + (b.act != null ? b.act.toFixed(1) + 'd' : '—')
        + ' · Win ' + (b.win_rate != null ? Math.round(b.win_rate * 100) + '%' : '—')
        + ' · ' + trades + ' trades';
    const eligLine = isEligible ? 'eligible' : 'NOT eligible';
    const editHint = editable ? ' · click to toggle' : '';
    // Phase 2C drift badge — dot in top-left when drift signal present.
    const driftSig = _rdDriftFor(sid, rg);
    const driftBadge = driftSig && (driftSig.severity === 'WARN' || driftSig.severity === 'FAIL')
      ? \`<span class="st-rg-drift" style="position:absolute;top:1px;left:2px;width:5px;height:5px;border-radius:50%;background:\${driftSig.severity === 'FAIL' ? '#f85149' : '#d29922'}"></span>\`
      : '';
    const driftTtl = driftSig
      ? \` · drift=\${driftSig.severity} (\${driftSig.reason || ''})\`
      : '';
    const ttl = rg + ' [' + eligLine + ']: ' + statsLine + editHint + driftTtl;
    const onclick = editable
      ? \` onclick="_stToggleRegimeEligibility(event, '\${_escStr(sid)}', '\${rg}')"\`
      : '';
    return \`<span class="\${klass.join(' ')}" title="\${_escStr(ttl)}"\${onclick}>
              \${driftBadge}
              <span class="st-rg-tag">\${rg}</span>
              <span class="st-rg-val">\${valTxt}</span>
            </span>\`;
  }).join('');
  const gridTitle = editable
    ? 'Per-regime live ARR · click a regime to toggle eligibility'
    : "Per-regime live ARR · operator toggle available only for state='live'";
  return \`<div class="st-regime-grid" title="\${gridTitle}">\${cells}</div>\`;
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

// ── Section 1: Active Stack ────────────────────────────────────────────────
const _SUB_ORDER = { live: 0, stale: 1, waiting: 2 };

// Persisted expansion state. Survives table re-renders triggered by
// sort changes, polling refreshes, and approval-job updates so the
// operator's selection isn't lost on every redraw.
let _stExpandedSid = null;
let _stArrChart    = null;   // active Chart.js instance for the ARR curve
let _stFlowChart   = null;   // active Chart.js instance for the position-flow bars
let _stArrCache    = {};     // sid → arr-curve payload (cached for the session)
let _stArrFetching = {};     // sid → in-flight Promise (de-dupe concurrent expands)
let _stRegimeFilter = 'ALL'; // 'ALL' | 'LOW_VOL' | 'TRANSITIONING' | 'HIGH_VOL' | 'CRISIS'

function _stSetRegimeFilter(rg) {
  _stRegimeFilter = rg;
  // Update tab visuals.
  const filter = document.getElementById('st-regime-filter');
  if (filter) {
    for (const btn of filter.querySelectorAll('.srf-btn')) {
      btn.classList.toggle('active', btn.dataset.regime === rg);
    }
    const hint = document.getElementById('srf-hint');
    if (hint) hint.textContent = rg === 'ALL'
      ? ''
      : 'Showing live ARR / ADR / ACT / Win for ' + rg + ' trades only';
  }
  // Re-render the active stack with swapped metrics.
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

// Phase 2B — render pending Mastermind proposals
function _rpRender(proposals) {
  const section = document.getElementById('rp-section');
  const tbody = document.querySelector('#rp-table tbody');
  const countEl = document.getElementById('rp-count');
  if (!section || !tbody) return;
  if (!proposals.length) {
    section.style.display = 'none';
    return;
  }
  section.style.display = '';
  countEl.textContent = '(' + proposals.length + ' awaiting decision)';
  tbody.innerHTML = '';
  for (const p of proposals) {
    const tr = document.createElement('tr');
    const proposedBits = [];
    if (p.proposed_eligible !== null && p.proposed_eligible !== undefined) proposedBits.push('elig=' + p.proposed_eligible);
    if (p.proposed_size_scalar !== null) proposedBits.push('size=' + Number(p.proposed_size_scalar).toFixed(2));
    if (p.proposed_stop_pct !== null) proposedBits.push('stop=' + Number(p.proposed_stop_pct).toFixed(3));
    if (p.proposed_target_pct !== null) proposedBits.push('target=' + Number(p.proposed_target_pct).toFixed(3));
    if (p.proposed_max_hold_days !== null) proposedBits.push('hold=' + p.proposed_max_hold_days + 'd');
    const conf = p.confidence != null ? Number(p.confidence).toFixed(2) : '?';
    tr.innerHTML =
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' + p.strategy_id + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + p.regime_state + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' + proposedBits.join(' · ') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + conf + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-size:11px;color:var(--muted)">' + (p.reasoning || '') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right;white-space:nowrap">' +
        (_rpProposalHasPolicyFields(p)
          ? '<button class="rp-pathmc-btn" data-id="' + p.id + '" style="padding:3px 8px;font-size:11px;background:#555;color:white;border:none;border-radius:3px;cursor:pointer;margin-right:4px">Path-MC</button>'
          : '') +
        '<button class="rp-btn" data-id="' + p.id + '" data-action="approve" style="padding:3px 8px;font-size:11px;background:#2ea043;color:white;border:none;border-radius:3px;cursor:pointer;margin-right:4px">Approve</button>' +
        '<button class="rp-btn" data-id="' + p.id + '" data-action="reject" style="padding:3px 8px;font-size:11px;background:#a04040;color:white;border:none;border-radius:3px;cursor:pointer">Reject</button>' +
      '</td>';
    tbody.appendChild(tr);
    // Stash proposal data on Path-MC button if present
    const pathBtn = tr.querySelector('.rp-pathmc-btn');
    if (pathBtn) pathBtn._proposalData = p;
  }
  // Wire buttons
  for (const btn of tbody.querySelectorAll('.rp-btn')) {
    btn.onclick = () => _rpDecide(btn.dataset.id, btn.dataset.action);
  }
  for (const btn of tbody.querySelectorAll('.rp-pathmc-btn')) {
    btn.onclick = () => _rpRunPathMC(btn.dataset.id, btn);
  }
}

// Phase 2E — inline path-MC button on proposal rows
function _rpProposalHasPolicyFields(p) {
  return p.proposed_stop_pct != null || p.proposed_target_pct != null || p.proposed_max_hold_days != null;
}

async function _rpRunPathMC(id, btn) {
  const row = btn.closest('tr');
  if (!row) return;
  const p = btn._proposalData;
  if (!p) return;
  btn.disabled = true; btn.textContent = '…';
  // Surface a placeholder cell for the result if not already present
  let outRow = row.nextElementSibling;
  if (!outRow || !outRow.classList.contains('rp-mc-out-' + id)) {
    outRow = document.createElement('tr');
    outRow.className = 'rp-mc-out-' + id;
    outRow.innerHTML = '<td colspan="6" class="rp-mc-cell" style="padding:6px 12px;background:var(--bg2,#fafafa);border-bottom:1px solid var(--border2);font-size:11px;color:var(--muted)">Running path-MC…</td>';
    row.parentNode.insertBefore(outRow, row.nextSibling);
  }
  const out = outRow.querySelector('.rp-mc-cell');
  const body = {
    current_size:           Number(p.current_size_scalar ?? 1.0),
    proposed_size:          Number(p.proposed_size_scalar ?? p.current_size_scalar ?? 1.0),
    proposed_stop_pct:      Number(p.proposed_stop_pct ?? 0.05),
    proposed_target_pct:    Number(p.proposed_target_pct ?? 0.10),
    proposed_max_hold_days: Number(p.proposed_max_hold_days ?? 5),
    n_iter: 500, seed: 42, proposal_id: Number(p.id),
  };
  try {
    const res = await fetch('/api/regime-mc-intraday/' + encodeURIComponent(p.strategy_id) + '/' + encodeURIComponent(p.regime_state), {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    const j = await res.json();
    if (!res.ok || j.status !== 'OK') {
      out.textContent = 'Path-MC: ' + (j.note || j.error || ('status=' + (j.status || res.status)));
      btn.disabled = false; btn.textContent = 'Path-MC';
      return;
    }
    const fmt = (v, d=2) => (v == null ? '—' : Number(v).toFixed(d));
    const pct = (v) => (v == null ? '—' : (Number(v) * 100).toFixed(1) + '%');
    const diff = j.linear_mc_diff || {};
    const sharpeDelta = diff.sharpe_p50_delta;
    const ddDelta = diff.max_dd_p95_delta;
    const diffBadge = (sharpeDelta != null && Math.abs(sharpeDelta) > 0.3)
      ? ' <span style="background:#a04040;color:white;padding:1px 5px;border-radius:3px;font-size:10px">size assumption matters</span>'
      : '';
    out.innerHTML =
      '<b>Path-MC</b> (n=' + j.n_bootstrap_iter + ', source=' + j.path_source + ', trades=' + j.n_trades_sampled + ')' + diffBadge +
      ' &nbsp; Sharpe[' + fmt(j.sharpe_p05) + ', ' + fmt(j.sharpe_p50) + ', ' + fmt(j.sharpe_p95) + ']' +
      ' &nbsp; MaxDD[' + pct(j.max_dd_p05) + ', ' + pct(j.max_dd_p50) + ', ' + pct(j.max_dd_p95) + ']' +
      ' &nbsp; stop=' + pct(j.stop_hit_rate) +
      ' · target=' + pct(j.target_hit_rate) +
      ' · max-hold=' + pct(j.max_hold_hit_rate) +
      (sharpeDelta != null
         ? '<br><span style="color:var(--muted)">vs linear MC: Δsharpe_p50=' + fmt(sharpeDelta) + (ddDelta != null ? ' · Δmax_dd_p95=' + pct(ddDelta) : '') + '</span>'
         : '');
    btn.textContent = 'Re-run';
    btn.disabled = false;
  } catch (e) {
    out.textContent = 'Path-MC failed: ' + e.message;
    btn.disabled = false; btn.textContent = 'Path-MC';
  }
}

// Phase 2F — render addenda panel
function _addendaFormatTs(s) {
  if (!s) return '—';
  return String(s).slice(0, 16).replace('T', ' ');
}

function _addendaStatusBadge(status) {
  const colors = { pending: '#b08800', active: '#2ea043', expired: '#777',
                    rejected: '#a04040', superseded: '#555' };
  return '<span style="background:' + (colors[status] || '#888') + ';color:white;padding:1px 6px;border-radius:3px;font-size:10px">' + status + '</span>';
}

function _addendaRender(addenda) {
  const section = document.getElementById('addenda-section');
  const tbody = document.querySelector('#addenda-table tbody');
  const countEl = document.getElementById('addenda-count');
  if (!section || !tbody) return;
  // Filter to relevant (pending + active); historical states shown if asked
  const visible = addenda.filter(a => a.status === 'pending' || a.status === 'active');
  countEl.textContent = visible.length
    ? '(' + visible.length + ' affecting next Mastermind run)'
    : '(none active)';
  tbody.innerHTML = '';
  if (!visible.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="padding:8px;color:var(--muted);font-size:11px">No active addenda. Use + Add to author one, or wait for the bias detector to emit (currently dormant pending ≥10 outcomes/bucket).</td></tr>';
    return;
  }
  for (const a of visible) {
    const tr = document.createElement('tr');
    const decideBtns = a.status === 'pending'
      ? ('<button class="addenda-btn" data-id="' + a.id + '" data-action="approve" style="padding:3px 8px;font-size:11px;background:#2ea043;color:white;border:none;border-radius:3px;cursor:pointer;margin-right:4px">Approve</button>' +
         '<button class="addenda-btn" data-id="' + a.id + '" data-action="reject" style="padding:3px 8px;font-size:11px;background:#a04040;color:white;border:none;border-radius:3px;cursor:pointer">Reject</button>')
      : ('<button class="addenda-btn" data-id="' + a.id + '" data-action="expire" style="padding:3px 8px;font-size:11px;background:#777;color:white;border:none;border-radius:3px;cursor:pointer">Expire</button>');
    tr.innerHTML =
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2)">' + _addendaStatusBadge(a.status) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-family:ui-monospace,Menlo,monospace;font-size:11px">' + (a.source || '—') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-size:11px;max-width:380px;word-break:break-word">' + (a.addendum_text || '') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-size:11px;color:var(--muted);max-width:200px;word-break:break-word">' + (a.rationale || '') + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);font-size:11px;color:var(--muted)">' + _addendaFormatTs(a.created_at) + '</td>' +
      '<td style="padding:5px 8px;border-bottom:1px solid var(--border2);text-align:right;white-space:nowrap">' + decideBtns + '</td>';
    tbody.appendChild(tr);
  }
  for (const btn of tbody.querySelectorAll('.addenda-btn')) {
    btn.onclick = () => _addendaDecide(btn.dataset.id, btn.dataset.action);
  }
}

async function _addendaLoad() {
  try {
    const res = await fetch('/api/recalibration/addenda?status=all');
    const j = await res.json();
    _addendaRender(Array.isArray(j.addenda) ? j.addenda : []);
  } catch (e) {
    _addendaRender([]);
  }
}

async function _addendaDecide(id, action) {
  let actor = window.localStorage.getItem('rg_operator');
  if (!actor) {
    actor = prompt('Operator name (saved for the session):', '');
    if (!actor) return;
    window.localStorage.setItem('rg_operator', actor);
  }
  const reason = prompt('Reason for ' + action + ' of addendum #' + id + '?', '');
  if (reason === null) return;
  try {
    const res = await fetch('/api/recalibration/addenda/' + encodeURIComponent(id) + '/' + encodeURIComponent(action), {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ decided_by: actor, reason }),
    });
    const j = await res.json();
    if (!res.ok || j.status !== 'OK') throw new Error(j.error || j.status || res.statusText);
    await _addendaLoad();
  } catch (e) {
    alert('Addendum ' + action + ' failed: ' + e.message);
  }
}

async function _addendaAddSave() {
  const text = (document.getElementById('addenda-add-text').value || '').trim();
  const rationale = (document.getElementById('addenda-add-rationale').value || '').trim();
  if (!text) { alert('Addendum text is required.'); return; }
  let actor = window.localStorage.getItem('rg_operator');
  if (!actor) {
    actor = prompt('Operator name (saved for the session):', '');
    if (!actor) return;
    window.localStorage.setItem('rg_operator', actor);
  }
  try {
    const res = await fetch('/api/recalibration/addenda', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ addendum_text: text, rationale, decided_by: actor }),
    });
    const j = await res.json();
    if (!res.ok) throw new Error(j.error || res.statusText);
    document.getElementById('addenda-add-text').value = '';
    document.getElementById('addenda-add-rationale').value = '';
    document.getElementById('addenda-add-form').style.display = 'none';
    await _addendaLoad();
  } catch (e) {
    alert('Addendum save failed: ' + e.message);
  }
}

function _addendaWireForm() {
  const btn = document.getElementById('addenda-add-btn');
  const form = document.getElementById('addenda-add-form');
  const cancel = document.getElementById('addenda-add-cancel');
  const save = document.getElementById('addenda-add-save');
  if (!btn || !form || !cancel || !save || btn._wired) return;
  btn._wired = true;
  btn.onclick = () => { form.style.display = form.style.display === 'none' ? '' : 'none'; };
  cancel.onclick = () => { form.style.display = 'none'; };
  save.onclick = () => _addendaAddSave();
}

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
  if (_stArrChart)  { try { _stArrChart.destroy();  } catch (_) {} _stArrChart  = null; }
  if (_stFlowChart) { try { _stFlowChart.destroy(); } catch (_) {} _stFlowChart = null; }
  _stExpandedSid = null;
  _renderActiveStack(strategiesData.filter(_inActiveStack));
}

function _renderActiveStack(rows) {
  const el = document.getElementById('st-active-wrap');
  if (!rows.length) {
    el.innerHTML = '<div class="empty">No strategies in the Active Stack.</div>';
    return;
  }
  // Compute derived columns for sorting.
  //   d1_total         = O+U+R (click count on # O/U/R)
  //   _active_rank     = Waiting(0) < Stale(1) < Live(2) — so ascending
  //                       surfaces most-activated strategies last;
  //                       descending surfaces LIVE rows first.
  // ADR (Average Daily Return) = ARR / ACT, the natural decomposition of
  // per-trade average return into a daily rate using average closing time.
  // Filled for any strategy with closed trades. Annualization was rejected
  // because regime-gated strategies activate with variable density across
  // years — keeping the daily mean keeps the metric comparable.
  //
  // When the regime filter is set to a specific regime, the row's ARR /
  // ADR / ACT / Win % / Closed columns swap to that regime's values from
  // live_regime_breakdown so the operator can see how each strategy
  // performs under one regime at a time. 'ALL' uses the cross-regime
  // aggregates already on the row.
  const filterRg = _stRegimeFilter && _stRegimeFilter !== 'ALL' ? _stRegimeFilter : null;
  const enriched = rows.map(r => {
    let avgRet, actDays, winRate, closedCount;
    if (filterRg) {
      const b = (r.live_regime_breakdown || {})[filterRg];
      avgRet      = b && b.arr      != null ? parseFloat(b.arr)      : null;
      actDays     = b && b.act      != null ? parseFloat(b.act)      : null;
      winRate     = b && b.win_rate != null ? parseFloat(b.win_rate) : null;
      closedCount = b && b.trades   != null ? parseInt(b.trades)     : 0;
    } else {
      avgRet      = r.avg_realized_pct != null ? parseFloat(r.avg_realized_pct) : null;
      actDays     = r.avg_days_held    != null ? parseFloat(r.avg_days_held)    : null;
      winRate     = r.win_rate         != null ? parseFloat(r.win_rate)         : null;
      closedCount = r.closed_count     != null ? parseInt(r.closed_count)       : 0;
    }
    const arrPct = avgRet != null ? avgRet * 100 : null;        // per-trade %
    const adrPct = (avgRet != null && actDays != null && actDays > 0)
                   ? (avgRet * 100 / actDays) : null;            // per-day %
    return Object.assign({}, r, {
      d1_total:        (r.d1_overperf || 0) + (r.d1_underperf || 0) + (r.d1_rejected || 0),
      _active_rank:    _activeRankFor(r),
      _arr_pct:        arrPct,
      _adr_pct:        adrPct,
      _act_days:       actDays,
      _filtered_win:   winRate,
      _filtered_closed: closedCount,
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
  // Header tag on metric columns when the regime filter is non-ALL —
  // makes it obvious which subset of trades the numbers describe.
  const filterTag = filterRg
    ? \` <span class="srf-col-tag srf-col-tag-\${filterRg}" title="\${filterRg} only">\${_REGIME_TAGS[filterRg]}</span>\`
    : '';
  el.innerHTML = \`<table class="db-table st-active-table" style="min-width:1180px">
    <tr>
      <th data-sort-key="strategy_id" data-sort-type="str">Strategy</th>
      <th data-sort-key="_active_rank" data-sort-type="num" title="Sort: Waiting → Stale → Live (ascending)">Status</th>
      <th title="Per-regime LIVE ARR (mean realized P&amp;L per closed trade). Small dot = current market regime, blue border = declared in active_in_regimes">By Regime</th>
      <th class="num" data-sort-key="open_count" data-sort-type="num">Open</th>
      <th class="num" data-sort-key="_filtered_closed" data-sort-type="num">Closed\${filterTag}</th>
      <th class="num" data-sort-key="_filtered_win" data-sort-type="num">Win %\${filterTag}</th>
      <th class="num" data-sort-key="_arr_pct" data-sort-type="num" title="Average Return Rate: mean realized P&amp;L % across closed trades\${filterRg ? ' under '+filterRg : ''}.">ARR&nbsp;%\${filterTag}</th>
      <th class="num" data-sort-key="_adr_pct" data-sort-type="num" title="Average Daily Return: ARR / ACT. Per-trade average return broken down into a daily rate.\${filterRg ? ' '+filterRg+' trades only.' : ''}">ADR&nbsp;%\${filterTag}</th>
      <th class="num" data-sort-key="_act_days" data-sort-type="num" title="Average Closing Time: mean days_held across closed trades\${filterRg ? ' under '+filterRg : ''}.">ACT\${filterTag}</th>
      <th class="num" data-sort-key="d1_total" data-sort-type="num" title="Cumulative counts across all daily cycles: Overperformers / Underperformers / Rejected">#&nbsp;O/U/R</th>
      <th data-sort-key="last_signal_date" data-sort-type="date">Last Signal</th>
      <th>Actions</th>
    </tr>
    \${shown.map(r => {
      const sub = _activeSub(r);
      const subLabel = sub.toUpperCase();
      const title = sub === 'waiting'
        ? 'Regime ' + (r.current_regime || '?') + ' not in active_in_regimes: ' + ((r.active_in_regimes || []).join(', ') || '?')
        : (sub === 'stale' ? 'Regime active but no signal in 7+ days' : 'Trading actively');
      const arr = r._arr_pct;
      const adr = r._adr_pct;
      const act = r._act_days;
      const o = r.d1_overperf || 0, u = r.d1_underperf || 0, x = r.d1_rejected || 0;
      const ourEmpty = o === 0 && u === 0 && x === 0;
      const ourCell = ourEmpty
        ? '<span style="color:var(--dim)">—</span>'
        : \`<span style="color:#4ade80">\${o}</span>/<span style="color:#f87171">\${u}</span>/<span style="color:#94a3b8">\${x}</span>\`;
      const adrTitle = 'ARR ' + (arr != null ? ((arr >= 0 ? '+' : '') + arr.toFixed(2) + '%') : '—')
                     + ' / ACT ' + (act != null ? act.toFixed(1) + ' days' : '—');
      const actTxt = act != null ? act.toFixed(1) + (act === 1 ? ' day' : ' days') : '—';
      const isOpen = r.strategy_id === expandedSid;
      const expandRow = isOpen ? \`
        <tr class="st-expand-row" data-sid="\${_escStr(r.strategy_id)}">
          <td colspan="12">
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
      // Closed/Win cells respect the regime filter so the operator sees the
      // trade count + win rate that actually feeds the displayed ARR/ADR.
      const closedTxt = r._filtered_closed != null ? r._filtered_closed : (r.closed_count || 0);
      const winTxt    = r._filtered_win != null ? Math.round(r._filtered_win * 100) + '%'
                       : (r.win_rate != null ? Math.round(parseFloat(r.win_rate) * 100) + '%' : '—');
      const dimWhenFiltered = filterRg && (closedTxt === 0) ? 'style="color:var(--dim)"' : '';
      return \`<tr class="st-row-expandable \${isOpen ? 'st-row-open' : ''}" data-sid="\${_escStr(r.strategy_id)}">
        <td class="st-name-cell" style="font-weight:600" title="\${_escStr(r.description)}" onclick="_stToggleExpand('\${_escStr(r.strategy_id)}')">
          <span class="st-chevron">▶</span>\${r.strategy_id}
        </td>
        <td><span class="sg-status sg-status-\${sub}" title="\${_escStr(title)}">\${subLabel}</span></td>
        <td>\${_regimeBreakdown(r)}</td>
        <td class="num">\${r.open_count || 0}</td>
        <td class="num" \${dimWhenFiltered}>\${closedTxt}</td>
        <td class="num" \${dimWhenFiltered}>\${winTxt}</td>
        <td class="num \${pnlCls(arr)}" title="Mean realized P&amp;L % across closed trades\${filterRg ? ' under '+filterRg : ''}">\${arr != null ? ((arr >= 0 ? '+' : '') + arr.toFixed(2) + '%') : '—'}</td>
        <td class="num \${pnlCls(adr)}" title="\${adrTitle}">\${adr != null ? ((adr >= 0 ? '+' : '') + adr.toFixed(2) + '%') : '—'}</td>
        <td class="num" style="color:var(--muted)">\${actTxt}</td>
        <td class="num" title="Cumulative Over / Under / Rejected across all daily cycles">\${ourCell}</td>
        <td style="color:var(--dim)">\${_fmtDate(r.last_signal_date)}</td>
        <td><button class="st-action-btn st-unstack-btn" onclick="event.stopPropagation();stUnstack('\${_escStr(r.strategy_id)}')">Unstack</button></td>
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
// visible immediately. The ARR chart waits for two things in parallel:
//   1. /api/strategies/:id/arr-curve fetch (cached after first call)
//   2. The shell's max-height transition to *complete* — so Chart.js
//      reads the final container height when it sizes the canvas.
// Without #2 the canvas measured an in-flight transition height and
// shrank on every re-render — the "moving downwards / disappearing"
// glitch the operator reported.
async function _stPaintExpand(sid) {
  const shell = document.getElementById('st-expand-shell-' + sid);
  const body  = document.getElementById('st-expand-body-' + sid);
  if (!shell || !body) return;
  // Defer the class flip to the next frame so the transition runs from
  // the baseline 0 max-height.
  requestAnimationFrame(() => shell.classList.add('st-open'));

  const r = (strategiesData || []).find(x => x.strategy_id === sid);
  if (!r) return;

  // ── Live per-regime stats grid (was backtest, now live trades) ─────────
  const breakdown = r.live_regime_breakdown || {};
  const active    = new Set(r.active_in_regimes || []);
  const current   = r.current_regime || null;
  const cellsHtml = _REGIME_AXIS.map(rg => {
    const b = breakdown[rg];
    const has = b && (b.trades || 0) > 0;
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
        ? 'no closed trades under this regime yet'
        : 'not in active_in_regimes';
      return '<div class="st-rs-cell">' + head + '<div class="st-rs-na">' + why + '</div></div>';
    }
    const arrPct = b.arr * 100;
    const adrPct = b.adr != null ? b.adr * 100 : null;
    const winPct = b.win_rate != null ? Math.round(b.win_rate * 100) : null;
    const inner = ''
      + '<div class="st-rs-row"><span>ARR</span><b style="color:' + (arrPct >= 0 ? 'var(--green)' : 'var(--red)') + '">'
      +   (arrPct >= 0 ? '+' : '') + arrPct.toFixed(2) + '%</b></div>'
      + '<div class="st-rs-row"><span>ADR</span><b' + (adrPct != null ? ' style="color:' + (adrPct >= 0 ? 'var(--green)' : 'var(--red)') + '"' : '') + '>'
      +   (adrPct != null ? ((adrPct >= 0 ? '+' : '') + adrPct.toFixed(3) + '%') : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>ACT</span><b>' + (b.act != null ? b.act.toFixed(1) + 'd' : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>Win</span><b>' + (winPct != null ? winPct + '%' : '—') + '</b></div>'
      + '<div class="st-rs-row"><span>Trades</span><b>' + (b.trades || 0) + '</b></div>';
    return '<div class="st-rs-cell">' + head + inner + '</div>';
  }).join('');

  // ── Similar strategies (overlap on at least one regime) ────────────────
  // Heatmap cell shows live ARR % for that regime — directly comparable
  // to the focused strategy's per-regime cells above.
  const myRegimes = new Set(r.active_in_regimes || []);
  const similar = Object.values(_stActiveSnapshot)
    .filter(o => o.strategy_id !== sid)
    .filter(o => (o.active_in_regimes || []).some(rg => myRegimes.has(rg)))
    .sort((a, b) => String(a.strategy_id).localeCompare(String(b.strategy_id)))
    .slice(0, 12);
  const similarHtml = similar.length ? similar.map(o => {
    const cells = _REGIME_AXIS.map(rg => {
      const b = (o.live_regime_breakdown || {})[rg];
      if (!b || !b.trades) return '<span class="st-sm-cell empty" title="' + rg + ': no live trades">—</span>';
      const arrPct = b.arr != null ? b.arr * 100 : null;
      const cls = arrPct == null ? 'empty' : (arrPct > 0 ? 'pos' : arrPct < 0 ? 'neg' : '');
      const txt = arrPct == null ? '—' : (arrPct >= 0 ? '+' : '') + arrPct.toFixed(2) + '%';
      const ttl = rg + ': ARR ' + txt + ' · ' + b.trades + ' trades';
      return '<span class="st-sm-cell ' + cls + '" title="' + _escStr(ttl) + '">' + txt + '</span>';
    }).join('');
    return '<div class="st-similar-row" title="Open ' + o.strategy_id + '">'
         + '<span class="st-sm-name" onclick="_stToggleExpand(\\'' + _escStr(o.strategy_id) + '\\')" style="cursor:pointer">' + o.strategy_id + '</span>'
         + cells + '</div>';
  }).join('') : '<div class="st-rs-na" style="font-size:10.5px;padding:8px 0">No other active strategies share these regimes.</div>';

  // Render the synchronous content + a stable canvas wrapper. The wrapper
  // has explicit height (CSS .st-arr-canvas-wrap) so Chart.js' responsive
  // sizing has a fixed reference frame.
  body.innerHTML = \`
    <div class="st-expand-grid">
      <div class="st-expand-section">
        <div class="st-expand-section-title"><span>Live Trades by Regime</span><span style="color:var(--dim);font-size:9px">ARR · ADR · ACT · Win · Trades</span></div>
        <div class="st-regime-stats">\${cellsHtml}</div>
        <div class="st-expand-section-title" style="margin-top:14px"><span>Similar Active Strategies</span><span style="color:var(--dim);font-size:9px">live ARR % · share ≥1 regime · click to open</span></div>
        <div class="st-similar">
          <div class="st-similar-row" style="background:transparent">
            <span class="st-sm-name" style="color:var(--dim);font-size:9px;text-transform:uppercase;letter-spacing:.06em">Strategy</span>
            \${_REGIME_AXIS.map(rg => '<span class="st-sm-cell" style="background:transparent;color:var(--dim);font-size:9px">' + _REGIME_TAGS[rg] + '</span>').join('')}
          </div>
          \${similarHtml}
        </div>
      </div>
      <div class="st-expand-section st-arr-wrap">
        <div class="st-expand-section-title"><span>ARR over time</span><span class="st-arr-window" style="color:var(--dim);font-size:9px">30-day rolling</span></div>
        <div class="st-arr-meta" id="st-arr-meta-\${sid}"><span style="color:var(--dim)">Loading…</span></div>
        <div class="st-arr-canvas-wrap"><canvas id="st-arr-chart-\${sid}"></canvas></div>
        <div class="st-flow-row">
          <div class="st-flow-meta" id="st-flow-meta-\${sid}"><span style="color:var(--dim);font-size:9.5px;letter-spacing:.04em">Positions opened / closed per day</span><span></span></div>
          <div class="st-flow-canvas-wrap"><canvas id="st-flow-chart-\${sid}"></canvas></div>
        </div>
      </div>
    </div>\`;

  // Fetch (or read cached) ARR curve. The canvas wrapper has fixed CSS
  // height (.st-arr-canvas-wrap) so Chart.js can size correctly even
  // while the shell's max-height transition is still running — no need
  // to wait for transitionend.
  let payload = _stArrCache[sid];
  if (!payload) {
    if (!_stArrFetching[sid]) {
      _stArrFetching[sid] = fetch('/api/strategies/' + encodeURIComponent(sid) + '/arr-curve?window=30')
        .then(r => r.ok ? r.json() : { rows: [] })
        .catch(() => ({ rows: [] }));
    }
    payload = await _stArrFetching[sid];
    delete _stArrFetching[sid];
    _stArrCache[sid] = payload;
  }
  // If the user closed or jumped to another row in the interim, bail out.
  if (_stExpandedSid !== sid) return;
  _stRenderArrChart(sid, payload);
  _stRenderFlowChart(sid, payload);
}

function _stRenderArrChart(sid, payload) {
  const canvas = document.getElementById('st-arr-chart-' + sid);
  const meta   = document.getElementById('st-arr-meta-' + sid);
  if (!canvas) return;
  if (_stArrChart) { try { _stArrChart.destroy(); } catch (_) {} _stArrChart = null; }
  const rows = (payload && payload.rows) || [];
  if (!rows.length) {
    if (meta) meta.innerHTML = '<span style="color:var(--dim)">No closed trades yet — ARR curve will fill in once trades close.</span>';
    const wrap = canvas.parentElement;
    if (wrap) wrap.style.display = 'none';
    return;
  }
  const wrap = canvas.parentElement;
  if (wrap) wrap.style.display = '';
  const labels = rows.map(r => r.date);
  const values = rows.map(r => r.rolling_arr_pct != null ? r.rolling_arr_pct * 100 : null);
  const regimeMap = {};
  for (const r of rows) if (r.regime) regimeMap[r.date] = r.regime;
  const last  = [...values].reverse().find(v => v != null);
  const first = values.find(v => v != null);
  if (meta) {
    const lastTxt  = last  != null ? (last  >= 0 ? '+' : '') + last.toFixed(3) + '%' : '—';
    const firstTxt = first != null ? (first >= 0 ? '+' : '') + first.toFixed(3) + '%' : '—';
    meta.innerHTML = '<span>' + (payload.window_days || 30) + 'd rolling mean of realized P&amp;L per closed trade</span>'
                   + '<span>start <b style="color:var(--text)">' + firstTxt + '</b> · latest <b style="color:var(--text)">' + lastTxt + '</b></span>';
  }
  const fmt$ = v => (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  _stArrChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets: [{
      data: values,
      borderColor: '#58a6ff',
      backgroundColor: 'rgba(88,166,255,0.10)',
      borderWidth: 1.5,
      pointRadius: 0,
      fill: true,
      tension: 0.15,
      spanGaps: false,
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      // Disable initial animation — the chart's intro slide combined with
      // the expand transition was contributing to the perceived "drift".
      animation: false,
      // Debounce ResizeObserver callbacks so layout reflows during the
      // page's other live updates don't keep nudging the canvas.
      resizeDelay: 200,
      interaction: { mode: 'index', intersect: false },
      layout: { padding: { bottom: 0 } },
      plugins: {
        legend: { display: false },
        regimeBands: { map: regimeMap },
        tooltip: {
          backgroundColor:'#0d1117', borderColor:'#30363d', borderWidth:1,
          titleColor:'#c9d1d9', bodyColor:'#e6edf3',
          displayColors: false, padding: 8,
          callbacks: {
            label: ctx => {
              const r = rows[ctx.dataIndex];
              const v = ctx.parsed.y;
              const arr = v == null ? '—' : (v >= 0 ? '+' : '') + v.toFixed(3) + '%';
              return [
                'ARR ' + arr,
                'trades in window: ' + (r ? r.trade_count : 0),
                'regime: ' + (r && r.regime ? r.regime : '—'),
              ];
            },
          },
        },
      },
      scales: {
        x: { ticks: { color:'#484f58', maxTicksLimit: 8, font:{size:9}, padding: 0 }, grid: { color:'rgba(48,54,61,0.45)' } },
        y: { position:'right', ticks: { color:'#484f58', font:{size:9}, callback: fmt$, maxTicksLimit: 5 },
             grid:  { color:'rgba(48,54,61,0.45)' } }
      }
    }
  });
}

// Position-flow bar chart: positions opened (green) and closed (red) per
// day, rendered directly underneath the ARR curve. Shares x-axis dates
// with the ARR chart so the operator can correlate trade-flow density
// with the per-trade economics line above. Bars are grouped (not
// stacked) so a same-day open + close shows two distinguishable bars
// rather than a fused colour block.
function _stRenderFlowChart(sid, payload) {
  const canvas = document.getElementById('st-flow-chart-' + sid);
  const meta   = document.getElementById('st-flow-meta-' + sid);
  if (!canvas) return;
  if (_stFlowChart) { try { _stFlowChart.destroy(); } catch (_) {} _stFlowChart = null; }
  const rows = (payload && payload.rows) || [];
  if (!rows.length) {
    if (meta) meta.innerHTML = '<span style="color:var(--dim);font-size:9.5px">No trade flow yet.</span><span></span>';
    const wrap = canvas.parentElement;
    if (wrap) wrap.style.display = 'none';
    return;
  }
  const wrap = canvas.parentElement;
  if (wrap) wrap.style.display = '';
  const labels = rows.map(r => r.date);
  const opened = rows.map(r => r.opened || 0);
  const closed = rows.map(r => r.closed || 0);
  const totalOpened = opened.reduce((a, b) => a + b, 0);
  const totalClosed = closed.reduce((a, b) => a + b, 0);
  if (meta) {
    meta.innerHTML = '<span style="font-size:9.5px;letter-spacing:.04em">Positions <span style="color:var(--green);font-weight:600">opened</span> / <span style="color:var(--red);font-weight:600">closed</span> per day</span>'
                   + '<span style="font-size:9.5px"><b style="color:var(--green)">' + totalOpened + ' opened</b> · <b style="color:var(--red)">' + totalClosed + ' closed</b></span>';
  }
  // Bar geometry:
  //   barPercentage:      1.0  → opened + closed bars within the same day
  //                              touch with no gap
  //   categoryPercentage: 0.5  → each day's bar pair occupies half its
  //                              slot, leaving the other half as inter-
  //                              day whitespace so a month of bars reads
  //                              cleanly even at narrow chart widths
  _stFlowChart = new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        {
          label: 'opened',
          data: opened,
          backgroundColor: 'rgba(63,185,80,0.95)',
          borderWidth:     0,
          borderRadius:    0,
          barPercentage:      1.0,
          categoryPercentage: 0.5,
        },
        {
          label: 'closed',
          data: closed,
          backgroundColor: 'rgba(248,81,73,0.95)',
          borderWidth:     0,
          borderRadius:    0,
          barPercentage:      1.0,
          categoryPercentage: 0.5,
        },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: false,
      resizeDelay: 200,
      interaction: { mode: 'index', intersect: false },
      layout: { padding: { bottom: 0 } },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor:'#0d1117', borderColor:'#30363d', borderWidth:1,
          titleColor:'#c9d1d9', bodyColor:'#e6edf3',
          displayColors: false, padding: 8,
          callbacks: {
            title: items => items[0]?.label || '',
            label: ctx => {
              const r = rows[ctx.dataIndex];
              if (!r) return '';
              return [
                'opened: ' + (r.opened || 0),
                'closed: ' + (r.closed || 0),
              ];
            },
          },
        },
      },
      scales: {
        x: {
          ticks: { color:'#484f58', maxTicksLimit: 8, font:{size:9}, padding: 0, maxRotation: 0 },
          grid:  { display: false },
          border:{ color:'#30363d' },
        },
        y: {
          position:'right',
          beginAtZero: true,
          ticks: { color:'#484f58', font:{size:9}, maxTicksLimit: 3, precision: 0 },
          grid:  { display: false },
          border:{ display: false },
        },
      },
    },
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
      <th class="num" data-sort-key="live_days" data-sort-type="num">Live Days</th>
      <th class="num" data-sort-key="live_sharpe" data-sort-type="num">Live Sharpe</th>
      <th class="num" data-sort-key="live_return_pct" data-sort-type="num">Live Return</th>
      <th data-sort-key="last_signal_date" data-sort-type="date">Last Signal</th>
    </tr>
    \${shown.map(r => {
      const liveRet = r.live_return_pct != null ? parseFloat(r.live_return_pct) : null;
      return \`<tr>
        <td style="font-weight:600" title="\${_escStr(r.description)}">\${r.strategy_id}</td>
        <td><span class="st-badge st-badge-\${r.state}">\${r.state.toUpperCase()}</span></td>
        <td>\${_regimesCell(r)}</td>
        <td class="num" style="color:var(--muted)">\${r.live_days != null ? r.live_days : '—'}</td>
        <td class="num">\${_fmtNum(r.live_sharpe)}</td>
        <td class="num \${pnlCls(liveRet)}">\${liveRet != null ? (liveRet >= 0 ? '+' : '') + liveRet.toFixed(2) + '%' : '—'}</td>
        <td style="color:var(--dim)">\${_fmtDate(r.last_signal_date)}</td>
      </tr>\`;
    }).join('')}
  </table>\${footer}\`;
  _bindSortable('st-inactive-wrap', _renderInactiveStack);
  _bindCollapse('st-inactive-wrap', _renderInactiveStack);
}

// ── Section 3: Research Candidates ─────────────────────────────────────────
// Gate warning when backtest Sharpe < 0.5 or Max DD > 20%.
function _renderCandidates(rows) {
  const el = document.getElementById('st-candidate-wrap');
  if (!rows.length) {
    el.innerHTML = '<div class="empty">No candidates awaiting approval.</div>';
    return;
  }
  // Derived rank for the Status column: Staging < Candidate < Paper.
  const enriched = rows.map(r => Object.assign({}, r, {
    _state_rank: _candidateRankFor(r),
  }));
  let sorted;
  const s = _sortState['st-candidate-wrap'];
  if (s && s.key) {
    sorted = _applySort('st-candidate-wrap', enriched);
  } else {
    _tableDataCache['st-candidate-wrap'] = enriched;
    sorted = enriched.slice().sort((a, b) => String(a.strategy_id).localeCompare(String(b.strategy_id)));
  }
  const { shown, footer } = _collapseRows('st-candidate-wrap', sorted);
  el.innerHTML = \`<table class="db-table" style="min-width:1000px">
    <tr>
      <th data-sort-key="strategy_id" data-sort-type="str">Strategy</th>
      <th data-sort-key="_state_rank" data-sort-type="num" title="Sort: Staging → Candidate → Paper (ascending)">Status</th>
      <th>Regimes</th>
      <th class="num" data-sort-key="backtest_sharpe" data-sort-type="num">BT Sharpe</th>
      <th class="num" data-sort-key="backtest_return_pct" data-sort-type="num">BT Return</th>
      <th class="num" data-sort-key="backtest_max_dd_pct" data-sort-type="num">BT Max DD</th>
      <th class="num" data-sort-key="backtest_trade_count" data-sort-type="num">Backtest Trades</th>
      <th>Actions</th>
    </tr>
    \${shown.map(r => {
      const sharpe = r.backtest_sharpe;
      const maxDd  = r.backtest_max_dd_pct;
      const ret    = r.backtest_return_pct;
      // Backtest trade count from the convergence run, NOT live trade count
      // (which is r.total_count). 0 = strategy ran but emitted no signals;
      // null = never backtested. Both render as "—" but mean different
      // things — distinguish in the title.
      const trades = r.backtest_trade_count;
      const sharpeFail = sharpe != null && parseFloat(sharpe) < 0.5;
      const ddFail     = maxDd  != null && Math.abs(parseFloat(maxDd)) > 20;
      const gateWarn   = sharpeFail || ddFail;
      // Per-regime breakdown tooltip — built from backtest_regime_breakdown
      // (regime-stratified). Strategies without a breakdown render with a
      // generic tooltip; v1 metrics have been purged (NULL'd) elsewhere.
      const breakdown = r.backtest_regime_breakdown;
      const _bdLine = (lbl, b) => {
        if (!b) return lbl + ': —';
        if (b.note === 'not_declared') return lbl + ': not declared';
        if (b.note === 'no_oos_window') return lbl + ': no historical window meeting min_days';
        const sh = b.sharpe ?? 0;
        const dd = b.max_dd != null ? (b.max_dd * 100).toFixed(1) + '%' : '—';
        const tc = b.trade_count ?? 0;
        const od = b.oos_days ?? 0;
        return lbl + ': sharpe=' + (typeof sh === 'number' ? sh.toFixed(2) : sh)
                  + '  dd=' + dd + '  trades=' + tc + '  (' + od + ' OOS days)';
      };
      const sharpeTitle = breakdown
        ? ['LOW_VOL','TRANSITIONING','HIGH_VOL','CRISIS']
            .map(k => _bdLine(k.padEnd(14, ' '), breakdown[k]))
            .join('\\n')
        : 'No regime breakdown available';
      // Per-state approve button emoticon + label + click handler.
      // Under the fused-staging-approval lifecycle (2026-04-27):
      //   staging   → 📡 starts the fused worker (backfill + strategycoder + backtest;
      //                  auto-promotes to CANDIDATE on success)
      //   candidate → ✅ promotes to LIVE (synchronous /transition with sharpe/dd gate;
      //                  ⚠ on failing metrics — operator can override with force=true)
      //   paper     → legacy/frozen; no automatic Approve. Operator can archive via /transition.
      //   last failure? → ❌ Retry (tooltip carries the reason)
      const lastFail = _stLastFailures[r.strategy_id];
      const approveLbl = lastFail
        ? '❌ Retry'
        : (r.state === 'staging'   ? '📡 Approve'
        :  gateWarn                ? '⚠ Approve'
                                   : '✅ Approve');
      const approveTitle = lastFail
        ? ('Last run failed: ' + (lastFail.reason || lastFail) + '. Click to retry.')
        : (r.state === 'staging'
            ? 'Run fused approval: backfill required data + invoke StrategyCoder + 3-window convergence backtest. Auto-promotes to CANDIDATE on pass.'
            : r.state === 'candidate'
              ? (gateWarn
                  ? 'Metrics below gate thresholds (Sharpe ≥ 0.5, |Max DD| ≤ 20%) — approving will log an override.'
                  : 'Promote candidate → live (Alpaca paper/live trading).')
              : 'Promote to Active Stack');
      const approveCls = lastFail
        ? 'st-action-btn st-approve-btn st-approve-retry'
        : (r.state === 'staging'
            ? 'st-action-btn st-approve-btn st-approve-async'
            : 'st-action-btn st-approve-btn');
      const approveOnClick = r.state === 'staging'
        ? \`stApproveGated('\${r.strategy_id}', '\${r.state}')\`
        : \`stApprove('\${r.strategy_id}', \${gateWarn})\`;
      const jobChip = _stJobChipHTML(r.strategy_id);
      const failBanner = _stFailureBannerHTML(r.strategy_id);
      const actionsCell = jobChip
        ? jobChip
        : (failBanner +
           \`<button class="\${approveCls}" title="\${_escStr(approveTitle)}" onclick="\${approveOnClick}">\${approveLbl}</button>
            <button class="st-action-btn st-reject-btn" onclick="stReject('\${r.strategy_id}')">Reject</button>\`);
      // Warning badge for staging strategies whose Saturday-brain-planned
      // data columns include something no collector/provider can backfill.
      // The staging worker would reject Approve with unsupported_source.
      const dataWarn = (r.state === 'staging'
                       && Array.isArray(r.unsupported_sources)
                       && r.unsupported_sources.length > 0)
        ? \` <span class="st-data-warn" title="\${_escStr(
            'No collector/provider registered for: ' + r.unsupported_sources.join(', ')
            + '. Approve would fail with unsupported_source.\\nAdd these to data/master/schema_registry.json or data_columns first.')}">⚠ data</span>\`
        : '';
      return \`<tr>
        <td style="font-weight:600" title="\${_escStr(r.description)}">\${r.strategy_id}\${dataWarn}</td>
        <td><span class="st-badge st-badge-\${r.state}">\${r.state.toUpperCase()}</span></td>
        <td>\${_regimesCell(r)}</td>
        <td class="num\${sharpeFail ? ' st-gate-fail' : ''}" title="\${_escStr(sharpeTitle)}">\${_fmtNum(sharpe)}</td>
        <td class="num">\${ret != null ? (parseFloat(ret) >= 0 ? '+' : '') + parseFloat(ret).toFixed(2) + '%' : '—'}</td>
        <td class="num\${ddFail ? ' st-gate-fail' : ''}">\${maxDd != null ? parseFloat(maxDd).toFixed(2) + '%' : '—'}</td>
        <td class="num" style="color:var(--muted)" title="\${trades == null ? 'Never backtested' : (trades === 0 ? 'Backtest ran but emitted no signals' : trades + ' trades across the regime-stratified backtest windows')}">\${trades != null ? trades : '—'}</td>
        <td>\${actionsCell}</td>
      </tr>\`;
    }).join('')}
  </table>\${footer}\`;
  _bindSortable('st-candidate-wrap', _renderCandidates);
  _bindCollapse('st-candidate-wrap', _renderCandidates);
}

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

async function _stTransition(sid, toState, force, reason) {
  try {
    const resp = await fetch('/api/strategies/' + encodeURIComponent(sid) + '/transition', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ to_state: toState, force: !!force, reason: reason || '', actor: 'manual:dashboard' }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) { toast('Transition failed: ' + (data.error || resp.statusText), 'error', 6_000); return false; }
    toast(sid + ' → ' + toState, 'ok', 3_000);
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

async function stApprove(sid, gateWarn) {
  if (gateWarn) {
    if (!confirm('"' + sid + '" has metrics below gates (Sharpe < 0.5 or |Max DD| > 20%). Approve with override? This will be logged.')) return;
    await _stTransition(sid, 'live', true, 'Manual approve via dashboard (override)');
  } else {
    if (!confirm('Approve ' + sid + ' into the Active Stack?')) return;
    await _stTransition(sid, 'live', false, 'Manual approve via dashboard');
  }
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
  const rows = strategiesData.filter(_inCandidate);
  _renderCandidates(rows);
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
