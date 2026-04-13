'use strict';

const { query } = require('../database/postgres');

// ── Pipeline Config ───────────────────────────────────────────────────────────

let _configCache = null;
let _configCachedAt = 0;
const CONFIG_TTL_MS = 60_000; // re-read from DB at most every 60s

async function getConfig(key = null) {
  const now = Date.now();
  if (!_configCache || now - _configCachedAt > CONFIG_TTL_MS) {
    const res = await query(`SELECT key, value FROM pipeline_config`).catch(() => null);
    if (res) {
      _configCache = Object.fromEntries(res.rows.map(r => [r.key, r.value]));
      _configCachedAt = now;
    }
  }
  return key ? (_configCache?.[key] ?? null) : (_configCache ?? {});
}

async function setConfig(key, value) {
  await query(
    `INSERT INTO pipeline_config (key, value, updated_at) VALUES ($1, $2, NOW())
     ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()`,
    [key, String(value)]
  );
  _configCache = null; // bust cache
}

async function getAllConfig() {
  const res = await query(`SELECT key, value, description, updated_at FROM pipeline_config ORDER BY key`).catch(() => null);
  return res?.rows || [];
}

// ── DB Universe ───────────────────────────────────────────────────────────────

async function getUniverseTickers(indexFilter = null) {
  const res = await query(
    `SELECT ticker FROM universe_config
     WHERE active = true
     ${indexFilter ? `AND $1 = ANY(index_membership)` : ''}
     ORDER BY ticker`,
    indexFilter ? [indexFilter] : []
  ).catch(() => null);
  return res?.rows?.map(r => r.ticker) || [];
}

// Returns full universe rows with category metadata
async function getActiveUniverse() {
  const res = await query(
    `SELECT ticker, name, category, has_options, has_fundamentals, snapshot_24h, index_membership
     FROM universe_config WHERE active = true ORDER BY ticker`
  ).catch(() => null);
  return res?.rows || [];
}

async function addToUniverse(tickers, indexMembership = 'SP100') {
  for (const ticker of tickers) {
    await query(
      `INSERT INTO universe_config (ticker, index_membership, active, added_at)
       VALUES ($1, $2, true, NOW())
       ON CONFLICT (ticker) DO UPDATE SET
         active = true,
         index_membership = CASE
           WHEN $3 = ANY(universe_config.index_membership) THEN universe_config.index_membership
           ELSE array_append(universe_config.index_membership, $3)
         END`,
      [ticker, `{${indexMembership}}`, indexMembership]
    ).catch(() => null);
  }
}

// ── Universe ──────────────────────────────────────────────────────────────────

async function upsertUniverse(records) {
  if (!records.length) return;
  for (const r of records) {
    await query(
      `INSERT INTO universe (ticker, name, sector, industry, market_cap, index_membership, last_updated)
       VALUES ($1,$2,$3,$4,$5,$6,NOW())
       ON CONFLICT (ticker) DO UPDATE SET
         name=EXCLUDED.name, sector=EXCLUDED.sector, market_cap=EXCLUDED.market_cap, last_updated=NOW()`,
      [r.ticker, r.name, r.sector, r.industry, r.market_cap, r.index_membership || ['SP100']]
    ).catch(err => console.warn('[store] universe upsert:', err.message));
  }
}

// ── Price Data ────────────────────────────────────────────────────────────────

async function upsertPrices(ticker, bars, source = 'polygon') {
  if (!bars.length) return 0;
  let written = 0;
  for (const b of bars) {
    // Support both daily (date string) and sub-daily (unix ms timestamp b.t)
    const date = b.date || (b.t ? new Date(b.t).toISOString().slice(0, 10) : null);
    if (!date) continue;
    // datetime: use explicit timestamp if available, else midnight UTC for daily bars
    const datetime = b.datetime || (b.t ? new Date(b.t).toISOString() : `${date}T00:00:00.000Z`);
    await query(
      `INSERT INTO price_data (ticker, date, datetime, open, high, low, close, volume, vwap, transactions, source)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
       ON CONFLICT (ticker, datetime) DO UPDATE SET
         open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
         volume=EXCLUDED.volume, vwap=EXCLUDED.vwap, date=EXCLUDED.date`,
      [ticker, date, datetime,
       b.o ?? b.open, b.h ?? b.high, b.l ?? b.low, b.c ?? b.close,
       b.v ?? b.volume, b.vw ?? b.vwap, b.n ?? b.transactions, source]
    ).catch(() => null);
    written++;
  }
  return written;
}

async function getLatestPrice(ticker) {
  const res = await query(
    `SELECT * FROM price_data WHERE ticker=$1 ORDER BY date DESC LIMIT 1`, [ticker]
  );
  return res.rows[0] || null;
}

async function getPriceHistory(ticker, days = 252) {
  const res = await query(
    `SELECT * FROM price_data WHERE ticker=$1 ORDER BY date DESC LIMIT $2`, [ticker, days]
  );
  return res.rows.reverse();
}

// ── Snapshots ─────────────────────────────────────────────────────────────────

async function upsertSnapshot(ticker, snap) {
  const ts = new Date(Math.floor(Date.now() / 60000) * 60000); // round to minute
  await query(
    `INSERT INTO snapshots (ticker, snapshot_at, price, change_pct, volume, vwap, day_high, day_low, prev_close, market_cap, source)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
     ON CONFLICT (ticker, snapshot_at) DO UPDATE SET price=EXCLUDED.price, change_pct=EXCLUDED.change_pct`,
    [ticker, ts, snap.price, snap.change_pct, snap.volume, snap.vwap,
     snap.day_high, snap.day_low, snap.prev_close, snap.market_cap, snap.source || 'polygon']
  ).catch(() => null);
}

async function getLatestSnapshots(tickers) {
  const res = await query(
    `SELECT DISTINCT ON (ticker) ticker, price, change_pct, volume, snapshot_at
     FROM snapshots WHERE ticker = ANY($1)
     ORDER BY ticker, snapshot_at DESC`,
    [tickers]
  );
  return res.rows;
}

// ── Options / Greeks ──────────────────────────────────────────────────────────

async function upsertOptions(ticker, contracts, snapshotDate) {
  if (!contracts.length) return 0;
  let written = 0;
  for (const c of contracts) {
    const greeks = c.greeks || {};
    // Support both Polygon nested format (c.details.*) and flat format (c.expiry_date etc.)
    const expiry       = c.details?.expiration_date ?? c.expiry_date ?? null;
    const strike       = c.details?.strike_price    ?? c.strike_price ?? null;
    const contractType = c.details?.contract_type   ?? c.contract_type ?? null;
    const bid          = c.last_quote?.bid ?? c.bid ?? null;
    const ask          = c.last_quote?.ask ?? c.ask ?? null;
    if (!expiry || !strike || !contractType) continue;
    await query(
      `INSERT INTO options_data
         (ticker, snapshot_date, expiry, strike, contract_type, delta, gamma, theta, vega, rho, iv, open_interest, volume, last_price, bid, ask)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
       ON CONFLICT (ticker, snapshot_date, expiry, strike, contract_type) DO UPDATE SET
         delta=EXCLUDED.delta, gamma=EXCLUDED.gamma, theta=EXCLUDED.theta,
         vega=EXCLUDED.vega, rho=EXCLUDED.rho, iv=EXCLUDED.iv, open_interest=EXCLUDED.open_interest,
         volume=EXCLUDED.volume, last_price=EXCLUDED.last_price, bid=EXCLUDED.bid, ask=EXCLUDED.ask`,
      [ticker, snapshotDate, expiry, strike, contractType,
       greeks.delta ?? null, greeks.gamma ?? null, greeks.theta ?? null, greeks.vega ?? null, greeks.rho ?? null,
       c.implied_volatility ?? null, c.open_interest ?? null, c.day?.volume ?? null,
       c.day?.last_price ?? null, bid, ask]
    ).catch(() => null);
    written++;
  }
  return written;
}

// ── Fundamentals ──────────────────────────────────────────────────────────────

async function upsertFundamentals(ticker, records) {
  for (const r of records) {
    await query(
      `INSERT INTO fundamentals
         (ticker, period, period_end, revenue, gross_profit, ebitda, net_income, eps,
          gross_margin, operating_margin, net_margin, revenue_growth_yoy, pe_ratio, market_cap,
          roe, roic, debt_equity_ratio, p_fcf_ratio, source)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
       ON CONFLICT (ticker, period) DO UPDATE SET
         revenue=EXCLUDED.revenue, gross_profit=EXCLUDED.gross_profit, ebitda=EXCLUDED.ebitda,
         roe=EXCLUDED.roe, roic=EXCLUDED.roic,
         debt_equity_ratio=EXCLUDED.debt_equity_ratio, p_fcf_ratio=EXCLUDED.p_fcf_ratio`,
      [ticker, r.period, r.period_end, r.revenue, r.gross_profit, r.ebitda,
       r.net_income, r.eps, r.gross_margin, r.operating_margin, r.net_margin,
       r.revenue_growth_yoy, r.pe_ratio, r.market_cap,
       r.roe ?? null, r.roic ?? null, r.debt_equity_ratio ?? null, r.p_fcf_ratio ?? null,
       r.source || 'fmp']
    ).catch(() => null);
  }
}

// ── Coverage Registry ─────────────────────────────────────────────────────────

/**
 * Returns the date gaps that need fetching for a ticker+type.
 * requestedFrom/To are 'YYYY-MM-DD' strings.
 *
 * Returns array of { from, to } ranges to fetch (always 0, 1, or 2 entries):
 *   - []              → fully covered, skip entirely
 *   - [{ from, to }]  → one gap (tail or head only)
 *   - [head, tail]    → both ends missing (rare: we have middle but not edges)
 */
async function getGaps(ticker, dataType, requestedFrom, requestedTo) {
  const res = await query(
    `SELECT date_from, date_to FROM data_coverage WHERE ticker=$1 AND data_type=$2`,
    [ticker, dataType]
  ).catch(() => null);

  if (!res || !res.rows.length) {
    // No coverage at all — fetch everything
    return [{ from: requestedFrom, to: requestedTo }];
  }

  const known = res.rows[0];
  const kFrom = known.date_from.toISOString().slice(0, 10);
  const kTo   = known.date_to.toISOString().slice(0, 10);

  const gaps = [];

  // Need data before what we have?
  if (requestedFrom < kFrom) {
    // Fetch one day before known start to avoid off-by-one on weekends/holidays
    const gapTo = new Date(new Date(kFrom).getTime() - 86400_000).toISOString().slice(0, 10);
    if (requestedFrom <= gapTo) gaps.push({ from: requestedFrom, to: gapTo });
  }

  // Need data after what we have?
  if (requestedTo > kTo) {
    // Fetch from day after known end
    const gapFrom = new Date(new Date(kTo).getTime() + 86400_000).toISOString().slice(0, 10);
    if (gapFrom <= requestedTo) gaps.push({ from: gapFrom, to: requestedTo });
  }

  return gaps; // empty = fully covered
}

async function updateCoverage(ticker, dataType, dateFrom, dateTo, rowsAdded = 0) {
  await query(
    `INSERT INTO data_coverage (ticker, data_type, date_from, date_to, rows_stored, last_updated)
     VALUES ($1, $2, $3::date, $4::date, $5, NOW())
     ON CONFLICT (ticker, data_type) DO UPDATE SET
       date_from    = LEAST(EXCLUDED.date_from, data_coverage.date_from),
       date_to      = GREATEST(EXCLUDED.date_to, data_coverage.date_to),
       rows_stored  = data_coverage.rows_stored + $5,
       last_updated = NOW()`,
    [ticker, dataType, dateFrom, dateTo, rowsAdded]
  ).catch(() => null);
}

async function getAllCoverage(dataType) {
  const res = await query(
    `SELECT ticker, date_from, date_to, rows_stored, last_updated
     FROM data_coverage WHERE data_type=$1 ORDER BY ticker`,
    [dataType]
  ).catch(() => null);
  return res?.rows || [];
}

// ── Pre-cycle gap summary (batch — 4 queries, no per-ticker loops) ────────────

/**
 * Returns per-phase work-lists for the upcoming collection cycle.
 * "needs work" = gap exists between required range and stored coverage.
 *
 * Returns:
 *   {
 *     prices:      { covered, needed, tickers: string[] },
 *     options:     { covered, needed, tickers: string[] },
 *     fundamentals:{ covered, needed, tickers: string[], staleDays },
 *   }
 */
async function getGapSummary({ priceTickers, optionsTickers, fundTickers, fromDate, toDate, fundStaleDays = 45 }) {
  const yesterday = new Date(new Date(toDate).getTime() - 86400_000).toISOString().slice(0, 10);

  // ── Prices: needs update if coverage is absent or date_to < yesterday ────────
  const priceRes = await query(
    `SELECT u.ticker,
            CASE WHEN c.date_to >= $3::date THEN 'covered' ELSE 'needed' END AS status
     FROM   unnest($1::text[]) AS u(ticker)
     LEFT JOIN data_coverage c ON c.ticker = u.ticker AND c.data_type = 'prices'
     ORDER BY u.ticker`,
    [priceTickers, fromDate, yesterday]
  ).catch(() => null);

  // ── Options: needs update if no coverage record for today ────────────────────
  const optRes = await query(
    `SELECT u.ticker,
            CASE WHEN c.date_to >= $2::date THEN 'covered' ELSE 'needed' END AS status
     FROM   unnest($1::text[]) AS u(ticker)
     LEFT JOIN data_coverage c ON c.ticker = u.ticker AND c.data_type = 'options'
     ORDER BY u.ticker`,
    [optionsTickers, toDate]
  ).catch(() => null);

  // ── Fundamentals: needs update if last_updated > N days ago ─────────────────
  const fundRes = await query(
    `SELECT u.ticker,
            CASE WHEN c.last_updated >= NOW() - ($2 || ' days')::interval THEN 'covered' ELSE 'needed' END AS status
     FROM   unnest($1::text[]) AS u(ticker)
     LEFT JOIN data_coverage c ON c.ticker = u.ticker AND c.data_type = 'fundamentals'
     ORDER BY u.ticker`,
    [fundTickers, String(fundStaleDays)]
  ).catch(() => null);

  function toWorkList(rows) {
    const needed = [], covered = [];
    for (const r of (rows || [])) {
      (r.status === 'covered' ? covered : needed).push(r.ticker);
    }
    return { covered: covered.length, needed: needed.length, tickers: needed };
  }

  return {
    prices:       toWorkList(priceRes?.rows),
    options:      toWorkList(optRes?.rows),
    fundamentals: toWorkList(fundRes?.rows),
  };
}


// ── Pipeline Logging ──────────────────────────────────────────────────────────

async function logRun(ticker, runType, status, records = 0, errorMsg = null, durationMs = 0, apiCalls = 0) {
  await query(
    `INSERT INTO pipeline_runs (ticker, run_type, status, records_written, error_message, duration_ms, api_calls_used)
     VALUES ($1,$2,$3,$4,$5,$6,$7)`,
    [ticker, runType, status, records, errorMsg, durationMs, apiCalls]
  ).catch(() => null);
}

// ── Coverage Stats ────────────────────────────────────────────────────────────

async function getCoverageStats() {
  const res = await query(`
    SELECT
      -- All-time totals (permanent backtest archive)
      (SELECT COUNT(DISTINCT ticker) FROM price_data) AS price_coverage,
      (SELECT COUNT(*) FROM price_data) AS price_rows_total,
      (SELECT MIN(date) FROM price_data) AS price_earliest,
      (SELECT MAX(date) FROM price_data) AS price_latest,
      (SELECT COUNT(DISTINCT ticker) FROM options_data) AS options_coverage,
      (SELECT COUNT(*) FROM options_data) AS options_rows_total,
      (SELECT COUNT(DISTINCT ticker) FROM fundamentals) AS fund_coverage,
      (SELECT COUNT(DISTINCT ticker) FROM snapshots) AS snapshot_tickers,
      (SELECT COUNT(*) FROM snapshots) AS snapshot_rows_total,
      -- Recency (pipeline health indicators)
      (SELECT COUNT(DISTINCT ticker) FROM snapshots WHERE snapshot_at >= NOW() - INTERVAL '1 hour') AS live_coverage,
      (SELECT COUNT(DISTINCT ticker) FROM price_data WHERE date >= CURRENT_DATE - 3) AS recent_price_coverage,
      (SELECT COUNT(*) FROM pipeline_runs WHERE created_at >= NOW() - INTERVAL '1 hour') AS runs_last_hour,
      (SELECT COUNT(*) FROM pipeline_runs WHERE status='error' AND created_at >= NOW() - INTERVAL '1 hour') AS errors_last_hour
  `);
  return res.rows[0];
}

// ── API Call Stats (read from pipeline_runs — survives restarts) ─────────────

async function getTodayApiStats() {
  const res = await query(`
    SELECT
      run_type,
      COUNT(*) FILTER (WHERE status='success') AS success_runs,
      COUNT(*) FILTER (WHERE status='error')   AS error_runs,
      COALESCE(SUM(api_calls_used), 0)         AS api_calls,
      COALESCE(SUM(records_written), 0)        AS rows_written
    FROM pipeline_runs
    WHERE created_at >= CURRENT_DATE
    GROUP BY run_type
    ORDER BY run_type
  `).catch(() => null);

  const stats = { polygon: 0, fmp: 0, errors: 0, rows: 0 };
  if (!res) return stats;
  for (const row of res.rows) {
    stats.errors += parseInt(row.error_runs, 10);
    stats.rows   += parseInt(row.rows_written, 10);
    if (['prices', 'options', 'snapshot'].includes(row.run_type)) {
      stats.polygon += parseInt(row.api_calls, 10);
    }
    if (row.run_type === 'fundamentals') {
      stats.fmp += parseInt(row.api_calls, 10);
    }
  }
  return stats;
}

async function getDataFreshness(tickers) {
  const res = await query(`
    SELECT
      u.ticker,
      MAX(p.date) AS latest_price,
      MAX(s.snapshot_at) AS latest_snapshot,
      MAX(o.snapshot_date) AS latest_options
    FROM universe u
    LEFT JOIN price_data p ON p.ticker = u.ticker
    LEFT JOIN snapshots s ON s.ticker = u.ticker
    LEFT JOIN options_data o ON o.ticker = u.ticker
    WHERE u.ticker = ANY($1)
    GROUP BY u.ticker
    ORDER BY u.ticker
  `, [tickers]);
  return res.rows;
}

// ── Collection cycle tracking ─────────────────────────────────────────────────

async function startCycle() {
  const res = await query(
    `INSERT INTO collection_cycles (started_at, status) VALUES (NOW(), 'running') RETURNING id`
  );
  return res.rows[0].id;
}

async function completeCycle(id, metrics) {
  await query(
    `UPDATE collection_cycles SET
      completed_at        = NOW(),
      duration_ms         = $2,
      snapshot_tickers    = $3,
      price_rows          = $4,
      options_contracts   = $5,
      technical_rows      = $6,
      fundamental_records = $7,
      polygon_calls       = $8,
      fmp_calls           = $9,
      yfinance_calls      = $10,
      total_rows          = $3::integer + $4::integer + $5::integer + $6::integer + $7::integer,
      errors              = $11,
      status              = $12
    WHERE id = $1`,
    [
      id,
      metrics.durationMs,
      metrics.snapshotTickers   || 0,
      metrics.priceRows         || 0,
      metrics.optionsContracts  || 0,
      metrics.technicalRows     || 0,
      metrics.fundamentalRecords|| 0,
      metrics.polygonCalls      || 0,
      metrics.fmpCalls          || 0,
      metrics.yfinanceCalls     || 0,
      metrics.errors            || 0,
      metrics.errors > 0 ? 'complete-with-errors' : 'complete',
    ]
  );
  // Return the completed row so callers can embed metrics in notifications
  const res2 = await query(
    `SELECT id, duration_ms, snapshot_tickers, price_rows, options_contracts, technical_rows,
            fundamental_records, polygon_calls, fmp_calls, yfinance_calls, total_rows, errors, status
     FROM collection_cycles WHERE id = $1`,
    [id]
  ).catch(() => null);
  return res2?.rows[0] ?? null;
}

async function getCycleHistory(limit = 10) {
  const res = await query(
    `SELECT id, started_at, completed_at, duration_ms,
       snapshot_tickers, price_rows, options_contracts, technical_rows, fundamental_records,
       polygon_calls, fmp_calls, yfinance_calls, total_rows, errors, status
     FROM collection_cycles
     ORDER BY started_at DESC LIMIT $1`,
    [limit]
  );
  return res.rows;
}

// ── News ──────────────────────────────────────────────────────────────────────

async function upsertNews(articles) {
  let inserted = 0;
  for (const a of articles) {
    if (!a.primary_ticker || !a.title) continue;
    const res = await query(
      `INSERT INTO market_news (uuid, primary_ticker, title, publisher, url, published_at, summary, related_tickers, related_articles)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
       ON CONFLICT (primary_ticker) DO UPDATE SET
         uuid=EXCLUDED.uuid, title=EXCLUDED.title, publisher=EXCLUDED.publisher,
         url=EXCLUDED.url, published_at=EXCLUDED.published_at, summary=EXCLUDED.summary,
         related_tickers=EXCLUDED.related_tickers, related_articles=EXCLUDED.related_articles,
         fetched_at=NOW()`,
      [a.uuid || '', a.primary_ticker, a.title, a.publisher || null,
       a.url || null, a.published_at || null, a.summary || null,
       a.related_tickers?.length ? `{${a.related_tickers.map(t => `"${t}"`).join(',')}}` : '{}',
       JSON.stringify(a.related_articles || [])]
    ).catch(() => null);
    if (res?.rowCount) inserted++;
  }
  return inserted;
}

// getNews — flexible query for dashboard + research bots
// Options:
//   ticker  {string}   — single ticker: primary OR related_tickers match
//   tickers {string[]} — batch: any of these tickers
//   q       {string}   — full-text keyword search on title+summary (PostgreSQL tsvector)
//   since   {Date|string} — published_at >= since
//   limit   {number}   — max rows (cap 200)
async function getNews({ ticker, tickers, q, limit = 30, since } = {}) {
  const limitN = Math.min(limit, 200);
  const cols = `id, uuid, primary_ticker, title, publisher, url, published_at, summary, related_tickers, related_articles`;
  const where = [];
  const params = [];

  // Ticker filter — single or batch
  if (ticker) {
    params.push(ticker.toUpperCase());
    where.push(`(primary_ticker = $${params.length} OR $${params.length} = ANY(related_tickers))`);
  } else if (tickers && tickers.length) {
    const upper = tickers.map(t => t.toUpperCase());
    params.push(upper);
    where.push(`(primary_ticker = ANY($${params.length}) OR related_tickers && $${params.length})`);
  }

  // Full-text keyword search
  if (q && q.trim()) {
    params.push(q.trim());
    where.push(`search_vec @@ plainto_tsquery('english', $${params.length})`);
  }

  // Date floor
  if (since) {
    params.push(since);
    where.push(`published_at >= $${params.length}`);
  }

  params.push(limitN);
  const sql = `SELECT ${cols} FROM market_news
               ${where.length ? 'WHERE ' + where.join(' AND ') : ''}
               ORDER BY published_at DESC LIMIT $${params.length}`;

  const res = await query(sql, params).catch(() => null);
  return res?.rows || [];
}

module.exports = {
  getConfig, setConfig, getAllConfig,
  getUniverseTickers, getActiveUniverse, addToUniverse,
  upsertUniverse, upsertPrices, getLatestPrice, getPriceHistory,
  upsertSnapshot, getLatestSnapshots,
  upsertOptions, upsertFundamentals,
  getGaps, updateCoverage, getAllCoverage, getGapSummary,
  logRun, getCoverageStats, getTodayApiStats, getDataFreshness,
  startCycle, completeCycle, getCycleHistory,
  upsertNews, getNews,
};
