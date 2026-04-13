'use strict';

/**
 * agents/data-router.js — Tiered Data Routing Layer
 *
 * Agents call fetchData(type, params) instead of calling API sources directly.
 * The router picks the correct tier, handles fallback, caches for 5 minutes,
 * and tracks per-source usage for cost monitoring.
 *
 * Architecture:
 *   Tier 1 — Primary:  FMP (financials), Alpha Vantage (macro + technicals), SEC EDGAR (filings)
 *   Tier 2 — Fallback: Yahoo Finance (options, VIX, insiders, short interest, real-time)
 *
 * Note: This module is used by the orchestrator and pipeline scripts to
 * pre-fetch data before injecting into agent prompts. It is NOT called
 * inside agent processes (which use MCP tools directly).
 */

const https = require('https');
const fs    = require('fs');
const path  = require('path');

// ── Config ───────────────────────────────────────────────────────────────────
const FMP_KEY = process.env.FMP_API_KEY          || '';
const AV_KEY  = process.env.ALPHA_VANTAGE_API_KEY || '';
const SEC_UA  = process.env.SEC_USER_AGENT        || 'OpenClaw/1.0 (siddharthj1908@gmail.com)';

const FMP_BASE   = 'https://financialmodelingprep.com/stable';   // v3 was deprecated Aug 2025
const AV_BASE    = 'https://www.alphavantage.co/query';
const EDGAR_BASE = 'https://efts.sec.gov/LATEST';
const YF_BASE    = 'https://query1.finance.yahoo.com';

// 5-minute in-process cache (keyed by url)
const cache = new Map();
const CACHE_TTL_MS = 5 * 60 * 1000;

// Per-source usage tracking for this run
const usage = { fmp: 0, alpha_vantage: 0, sec_edgar: 0, yahoo_finance: 0, web_scrape: 0 };

// ── Routing Table ────────────────────────────────────────────────────────────
const ROUTES = {
  // FMP (Tier 1) — base: /stable (v3 deprecated Aug 2025)
  // Free-tier limits: institutional_holders/company_news/earnings_surprises require paid plan.
  // Use sec_submissions for filing index; Yahoo Finance MCP for insiders.
  company_profile:       { source: 'fmp', path: (p) => `/profile?symbol=${p.ticker}&apikey=${FMP_KEY}` },
  income_statement:      { source: 'fmp', path: (p) => `/income-statement?symbol=${p.ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}` },
  balance_sheet:         { source: 'fmp', path: (p) => `/balance-sheet-statement?symbol=${p.ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}` },
  cash_flow:             { source: 'fmp', path: (p) => `/cash-flow-statement?symbol=${p.ticker}&period=quarterly&limit=4&apikey=${FMP_KEY}` },
  key_metrics:           { source: 'fmp', path: (p) => `/key-metrics?symbol=${p.ticker}&limit=4&apikey=${FMP_KEY}` },
  ratios:                { source: 'fmp', path: (p) => `/ratios?symbol=${p.ticker}&limit=4&apikey=${FMP_KEY}` },
  historical_prices:     { source: 'fmp', path: (p) => `/historical-price-eod/full?symbol=${p.ticker}&apikey=${FMP_KEY}` },
  earnings_calendar:     { source: 'fmp', path: (p) => `/earnings?symbol=${p.ticker}&limit=${p.limit || 4}&apikey=${FMP_KEY}` },
  price_target:          { source: 'fmp', path: (p) => `/price-target-consensus?symbol=${p.ticker}&apikey=${FMP_KEY}` },
  quote:                 { source: 'fmp', path: (p) => `/quote?symbol=${p.ticker}&apikey=${FMP_KEY}` },
  peers:                 { source: 'fmp', path: (p) => `/stock-peers?symbol=${p.ticker}&apikey=${FMP_KEY}` },
  stock_screener:        { source: 'fmp', path: (p) => `/stock-screener?marketCapMoreThan=${p.minCap || 1000000000}&sector=${encodeURIComponent(p.sector || '')}&apikey=${FMP_KEY}` },

  // Alpha Vantage (Tier 1)
  macro_gdp:             { source: 'alpha_vantage', path: () => `?function=REAL_GDP&interval=quarterly&apikey=${AV_KEY}` },
  macro_cpi:             { source: 'alpha_vantage', path: () => `?function=CPI&interval=monthly&apikey=${AV_KEY}` },
  macro_inflation:       { source: 'alpha_vantage', path: () => `?function=INFLATION&apikey=${AV_KEY}` },
  macro_fed_funds:       { source: 'alpha_vantage', path: () => `?function=FEDERAL_FUNDS_RATE&interval=monthly&apikey=${AV_KEY}` },
  macro_unemployment:    { source: 'alpha_vantage', path: () => `?function=UNEMPLOYMENT&apikey=${AV_KEY}` },
  macro_yield_curve:     { source: 'alpha_vantage', path: (p) => `?function=TREASURY_YIELD&interval=monthly&maturity=${p.maturity || '10year'}&apikey=${AV_KEY}` },
  technical_sma:         { source: 'alpha_vantage', path: (p) => `?function=SMA&symbol=${p.ticker}&interval=daily&time_period=${p.period || 50}&series_type=close&apikey=${AV_KEY}` },
  technical_ema:         { source: 'alpha_vantage', path: (p) => `?function=EMA&symbol=${p.ticker}&interval=daily&time_period=${p.period || 20}&series_type=close&apikey=${AV_KEY}` },
  technical_rsi:         { source: 'alpha_vantage', path: (p) => `?function=RSI&symbol=${p.ticker}&interval=daily&time_period=14&series_type=close&apikey=${AV_KEY}` },
  technical_macd:        { source: 'alpha_vantage', path: (p) => `?function=MACD&symbol=${p.ticker}&interval=daily&series_type=close&apikey=${AV_KEY}` },
  technical_bbands:      { source: 'alpha_vantage', path: (p) => `?function=BBANDS&symbol=${p.ticker}&interval=daily&time_period=20&series_type=close&apikey=${AV_KEY}` },
  intraday:              { source: 'alpha_vantage', path: (p) => `?function=TIME_SERIES_INTRADAY&symbol=${p.ticker}&interval=${p.interval || '15min'}&outputsize=compact&apikey=${AV_KEY}`, fallback: 'yf_realtime_quote' },
  sector_performance:    { source: 'alpha_vantage', path: () => `?function=SECTOR&apikey=${AV_KEY}` },

  // SEC EDGAR (Tier 1)
  sec_full_text:         { source: 'sec_edgar', path: (p) => `/search-index?q=${encodeURIComponent(p.query)}&dateRange=custom&startdt=${p.start || ''}&enddt=${p.end || ''}` },
  sec_submissions:       { source: 'sec_edgar', path: (p) => `https://data.sec.gov/submissions/CIK${String(p.cik).padStart(10, '0')}.json` },

  // Yahoo Finance (Tier 2 — specialty only)
  options_chain:         { source: 'yahoo_finance', path: (p) => `${YF_BASE}/v7/finance/options/${p.ticker}` },
  vix:                   { source: 'yahoo_finance', path: () => `${YF_BASE}/v10/finance/quoteSummary/%5EVIX?modules=summaryDetail,price` },
  commodity:             { source: 'yahoo_finance', path: (p) => `${YF_BASE}/v10/finance/quoteSummary/${encodeURIComponent(p.ticker)}?modules=price,summaryDetail` },
  insider_transactions:  { source: 'yahoo_finance', path: (p) => `${YF_BASE}/v10/finance/quoteSummary/${p.ticker}?modules=insiderTransactions`, fallback: 'institutional_holders' },
  short_interest:        { source: 'yahoo_finance', path: (p) => `${YF_BASE}/v10/finance/quoteSummary/${p.ticker}?modules=defaultKeyStatistics,summaryDetail` },
  realtime_quote:        { source: 'yahoo_finance', path: (p) => `${YF_BASE}/v10/finance/quoteSummary/${p.ticker}?modules=price,summaryDetail` },
};

// ── Yahoo Finance crumb auth ─────────────────────────────────────────────────
let yfCrumb   = null;
let yfCookies = '';
let yfCrumbTs = 0;

function rawHttpGet(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { 'User-Agent': SEC_UA, 'Accept': '*/*', ...headers } }, (res) => {
      let data = '';
      const setCookie = res.headers['set-cookie'];
      if (setCookie) yfCookies = setCookie.map(c => c.split(';')[0]).join('; ');
      res.on('data', d => { data += d; });
      res.on('end', () => resolve({ status: res.statusCode, body: data }));
    });
    req.on('error', reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error('timeout')); });
  });
}

async function getYFCrumb() {
  if (yfCrumb && Date.now() - yfCrumbTs < 30 * 60 * 1000) return yfCrumb;
  await rawHttpGet('https://fc.yahoo.com');
  const { status, body } = await rawHttpGet(
    'https://query1.finance.yahoo.com/v1/test/getcrumb',
    yfCookies ? { Cookie: yfCookies } : {}
  );
  if (status === 200 && body && !body.includes('Unauthorized') && !body.includes('Too Many')) {
    yfCrumb = body.trim();
    yfCrumbTs = Date.now();
    return yfCrumb;
  }
  return null;
}

// ── HTTP fetch ────────────────────────────────────────────────────────────────
async function httpGet(url, headers = {}) {
  // Cache check
  const cached = cache.get(url);
  if (cached && Date.now() - cached.ts < CACHE_TTL_MS) {
    return Promise.resolve(cached.data);
  }

  // Yahoo Finance requires crumb auth
  let finalUrl = url;
  let extraHeaders = {};
  if (url.includes('yahoo.com')) {
    const crumb = await getYFCrumb().catch(() => null);
    if (crumb) finalUrl = `${url}${url.includes('?') ? '&' : '?'}crumb=${encodeURIComponent(crumb)}`;
    if (yfCookies) extraHeaders = { Cookie: yfCookies };
  }

  return new Promise((resolve, reject) => {
    const h = https.get(finalUrl, { headers: { 'User-Agent': SEC_UA, ...extraHeaders, ...headers } }, (res) => {
      let raw = '';
      res.on('data', d => { raw += d; });
      res.on('end', () => {
        if (res.statusCode !== 200) {
          reject(new Error(`HTTP ${res.statusCode} — ${url}`));
          return;
        }
        try {
          const data = JSON.parse(raw);
          cache.set(url, { data, ts: Date.now() });
          resolve(data);
        } catch (e) {
          resolve(raw); // return raw string if not JSON
        }
      });
    });
    h.on('error', reject);
    h.setTimeout(20000, () => { h.destroy(); reject(new Error(`Timeout: ${url}`)); });
  });
}

// ── Public API ────────────────────────────────────────────────────────────────

/**
 * Fetch data by type. Automatically picks the correct source and falls back if needed.
 *
 * @param {string} type  — one of the keys in ROUTES
 * @param {object} params — ticker, period, sector, etc. as needed by the route
 * @returns {Promise<any>} — parsed JSON from the source
 */
async function fetchData(type, params = {}) {
  const route = ROUTES[type];
  if (!route) throw new Error(`Unknown data type: "${type}". Available: ${Object.keys(ROUTES).join(', ')}`);

  const source = route.source;
  usage[source] = (usage[source] || 0) + 1;

  let url;
  if (source === 'fmp') {
    url = `${FMP_BASE}${route.path(params)}`;
  } else if (source === 'alpha_vantage') {
    url = `${AV_BASE}${route.path(params)}`;
  } else if (source === 'sec_edgar') {
    const p = route.path(params);
    url = p.startsWith('http') ? p : `${EDGAR_BASE}${p}`;
  } else {
    // yahoo_finance — full URL from path
    url = route.path(params);
  }

  try {
    return await httpGet(url);
  } catch (primaryErr) {
    if (route.fallback) {
      const fallbackType = route.fallback;
      const fallbackRoute = ROUTES[fallbackType];
      if (fallbackRoute) {
        const fallbackSource = fallbackRoute.source;
        usage[fallbackSource] = (usage[fallbackSource] || 0) + 1;
        let fallbackUrl;
        if (fallbackSource === 'fmp') fallbackUrl = `${FMP_BASE}${fallbackRoute.path(params)}`;
        else if (fallbackSource === 'alpha_vantage') fallbackUrl = `${AV_BASE}${fallbackRoute.path(params)}`;
        else if (fallbackSource === 'sec_edgar') {
          const p = fallbackRoute.path(params);
          fallbackUrl = p.startsWith('http') ? p : `${EDGAR_BASE}${p}`;
        } else {
          fallbackUrl = fallbackRoute.path(params);
        }
        return await httpGet(fallbackUrl);
      }
    }
    throw primaryErr;
  }
}

/**
 * Get usage stats for the current run.
 * @returns {{ fmp: number, alpha_vantage: number, sec_edgar: number, yahoo_finance: number }}
 */
function getUsageStats() {
  return { ...usage };
}

/**
 * Clear the in-process cache (call between runs if needed).
 */
function clearCache() {
  cache.clear();
}

module.exports = { fetchData, getUsageStats, clearCache, ROUTES };
