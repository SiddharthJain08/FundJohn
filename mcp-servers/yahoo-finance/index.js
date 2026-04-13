#!/usr/bin/env node
'use strict';

/**
 * Yahoo Finance MCP Server — Tier 2 Fallback Only
 *
 * Provides specialty data that FMP / Alpha Vantage don't cover:
 *   - Options chains (IV, put/call ratio, unusual activity)
 *   - VIX and commodity futures
 *   - Insider transactions (Form 4 detail)
 *   - Short interest (shares short, days to cover, short % float)
 *   - Real-time bid/ask quotes
 *
 * Uses raw Yahoo Finance API endpoints — no authentication required.
 * Rate limit: keep under 30 req/min to avoid throttling.
 *
 * MCP protocol: JSON-RPC 2.0 over stdin/stdout (newline-delimited).
 */

const https = require('https');

const UA = process.env.SEC_USER_AGENT || 'OpenClaw/1.0 (siddharthj1908@gmail.com)';
const YF_BASE = 'https://query1.finance.yahoo.com';

// ── Yahoo Finance crumb/cookie auth ──────────────────────────────────────────
// Yahoo Finance v10+ requires a session crumb and cookies since late 2024.
let yfCrumb   = null;
let yfCookies = '';
let crumbTs   = 0;

function rawGet(url, headers = {}) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, {
      headers: { 'User-Agent': UA, 'Accept': '*/*', ...headers },
    }, (res) => {
      let data = '';
      const setCookie = res.headers['set-cookie'];
      if (setCookie) {
        yfCookies = setCookie.map(c => c.split(';')[0]).join('; ');
      }
      res.on('data', d => { data += d; });
      res.on('end', () => resolve({ status: res.statusCode, body: data, headers: res.headers }));
    });
    req.on('error', reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error('Request timed out')); });
  });
}

async function getYFCrumb() {
  // Refresh crumb if older than 30 minutes
  if (yfCrumb && Date.now() - crumbTs < 30 * 60 * 1000) return yfCrumb;

  // Step 1: get consent/cookie from fc.yahoo.com
  await rawGet('https://fc.yahoo.com');

  // Step 2: get crumb
  const { status, body } = await rawGet(
    'https://query1.finance.yahoo.com/v1/test/getcrumb',
    yfCookies ? { Cookie: yfCookies } : {}
  );

  if (status === 200 && body && !body.includes('Unauthorized') && !body.includes('Too Many')) {
    yfCrumb = body.trim();
    crumbTs = Date.now();
    return yfCrumb;
  }
  // If crumb fetch fails, proceed without it (some endpoints still work)
  return null;
}

// ── HTTP helper ──────────────────────────────────────────────────────────────
async function fetchJSON(url, retryWithCrumb = true) {
  const crumb = await getYFCrumb().catch(() => null);
  const finalUrl = crumb ? `${url}${url.includes('?') ? '&' : '?'}crumb=${encodeURIComponent(crumb)}` : url;

  return new Promise((resolve, reject) => {
    const headers = {
      'User-Agent': UA,
      'Accept': 'application/json',
      ...(yfCookies ? { Cookie: yfCookies } : {}),
    };

    const req = https.get(finalUrl, { headers }, (res) => {
      let data = '';
      res.on('data', d => { data += d; });
      res.on('end', () => {
        if (res.statusCode === 401 || res.statusCode === 403) {
          // Invalidate crumb and reject
          yfCrumb = null;
          reject(new Error(`HTTP ${res.statusCode} — Yahoo Finance auth failed`));
          return;
        }
        if (res.statusCode !== 200) {
          reject(new Error(`HTTP ${res.statusCode} from ${url}`));
          return;
        }
        try { resolve(JSON.parse(data)); }
        catch (e) { reject(new Error(`JSON parse error: ${e.message}`)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(15000, () => { req.destroy(); reject(new Error('Request timed out')); });
  });
}

// ── Tool implementations ─────────────────────────────────────────────────────

async function getRealtimeQuote(ticker) {
  const url = `${YF_BASE}/v10/finance/quoteSummary/${ticker}?modules=price,summaryDetail`;
  const data = await fetchJSON(url);
  const price = data?.quoteSummary?.result?.[0]?.price || {};
  const summary = data?.quoteSummary?.result?.[0]?.summaryDetail || {};
  return {
    ticker,
    price: price.regularMarketPrice?.raw,
    bid: price.bid?.raw,
    ask: price.ask?.raw,
    bidSize: price.bidSize?.raw,
    askSize: price.askSize?.raw,
    dayHigh: price.regularMarketDayHigh?.raw,
    dayLow: price.regularMarketDayLow?.raw,
    volume: price.regularMarketVolume?.raw,
    avgVolume: summary.averageVolume?.raw,
    marketCap: price.marketCap?.raw,
    fiftyTwoWeekHigh: summary.fiftyTwoWeekHigh?.raw,
    fiftyTwoWeekLow: summary.fiftyTwoWeekLow?.raw,
    ts: new Date().toISOString(),
  };
}

async function getVix() {
  const url = `${YF_BASE}/v10/finance/quoteSummary/%5EVIX?modules=summaryDetail,price`;
  const data = await fetchJSON(url);
  const price = data?.quoteSummary?.result?.[0]?.price || {};
  const summary = data?.quoteSummary?.result?.[0]?.summaryDetail || {};
  return {
    symbol: '^VIX',
    current: price.regularMarketPrice?.raw,
    dayOpen: price.regularMarketOpen?.raw,
    dayHigh: price.regularMarketDayHigh?.raw,
    dayLow: price.regularMarketDayLow?.raw,
    fiftyTwoWeekHigh: summary.fiftyTwoWeekHigh?.raw,
    fiftyTwoWeekLow: summary.fiftyTwoWeekLow?.raw,
    interpretation: (() => {
      const v = price.regularMarketPrice?.raw;
      if (!v) return 'unknown';
      if (v < 15) return 'low_fear';
      if (v < 20) return 'moderate';
      if (v < 30) return 'elevated';
      if (v < 40) return 'high_fear';
      return 'extreme_fear';
    })(),
    ts: new Date().toISOString(),
  };
}

async function getCommodity(commodityTicker) {
  const url = `${YF_BASE}/v10/finance/quoteSummary/${encodeURIComponent(commodityTicker)}?modules=price,summaryDetail`;
  const data = await fetchJSON(url);
  const price = data?.quoteSummary?.result?.[0]?.price || {};
  return {
    symbol: commodityTicker,
    price: price.regularMarketPrice?.raw,
    change: price.regularMarketChange?.raw,
    changePct: price.regularMarketChangePercent?.raw,
    dayHigh: price.regularMarketDayHigh?.raw,
    dayLow: price.regularMarketDayLow?.raw,
    currency: price.currency,
    ts: new Date().toISOString(),
  };
}

async function getOptionsChain(ticker) {
  const url = `${YF_BASE}/v7/finance/options/${ticker}`;
  const data = await fetchJSON(url);
  const result = data?.optionChain?.result?.[0] || {};
  const calls = result.options?.[0]?.calls || [];
  const puts  = result.options?.[0]?.puts  || [];

  // Compute put/call ratio by OI
  const totalCallOI = calls.reduce((s, c) => s + (c.openInterest || 0), 0);
  const totalPutOI  = puts.reduce((s, p)  => s + (p.openInterest || 0), 0);
  const pcRatio     = totalCallOI > 0 ? (totalPutOI / totalCallOI).toFixed(3) : null;

  // Find unusually high OI (top 5 by OI)
  const allOptions = [...calls.map(o => ({ ...o, type: 'call' })), ...puts.map(o => ({ ...o, type: 'put' }))];
  const unusual = allOptions
    .sort((a, b) => (b.openInterest || 0) - (a.openInterest || 0))
    .slice(0, 5)
    .map(o => ({
      type: o.type,
      strike: o.strike,
      expiry: o.expiration,
      iv: o.impliedVolatility?.toFixed(4),
      oi: o.openInterest,
      volume: o.volume,
      lastPrice: o.lastPrice,
    }));

  return {
    ticker,
    spotPrice: result.quote?.regularMarketPrice,
    expirationDates: (result.expirationDates || []).slice(0, 5),
    putCallRatio: pcRatio,
    totalCallOI,
    totalPutOI,
    unusualActivity: unusual,
    callCount: calls.length,
    putCount: puts.length,
    ts: new Date().toISOString(),
  };
}

async function getInsiderTransactions(ticker) {
  const url = `${YF_BASE}/v10/finance/quoteSummary/${ticker}?modules=insiderTransactions`;
  const data = await fetchJSON(url);
  const txns = data?.quoteSummary?.result?.[0]?.insiderTransactions?.transactions || [];
  const parsed = txns.map(t => ({
    filer: t.filerName,
    relation: t.filerRelation,
    transactionDesc: t.transactionText,
    shares: t.shares?.raw,
    value: t.value?.raw,
    startDate: t.startDate?.fmt,
    type: (t.value?.raw || 0) > 0 ? 'buy' : 'sell',
  }));

  const buys  = parsed.filter(t => t.type === 'buy');
  const sells = parsed.filter(t => t.type === 'sell');
  const totalBuyValue  = buys.reduce((s, t)  => s + Math.abs(t.value || 0), 0);
  const totalSellValue = sells.reduce((s, t) => s + Math.abs(t.value || 0), 0);

  return {
    ticker,
    transactions: parsed.slice(0, 20),
    summary: {
      buyCount: buys.length,
      sellCount: sells.length,
      totalBuyValue,
      totalSellValue,
      netInsiderActivity: totalBuyValue - totalSellValue,
      sentiment: totalBuyValue > totalSellValue ? 'net_buying' : 'net_selling',
    },
    ts: new Date().toISOString(),
  };
}

async function getShortInterest(ticker) {
  // Short interest is in the defaultKeyStatistics module
  const url = `${YF_BASE}/v10/finance/quoteSummary/${ticker}?modules=defaultKeyStatistics,summaryDetail`;
  const data = await fetchJSON(url);
  const stats = data?.quoteSummary?.result?.[0]?.defaultKeyStatistics || {};
  const summary = data?.quoteSummary?.result?.[0]?.summaryDetail || {};
  return {
    ticker,
    sharesShort: stats.sharesShort?.raw,
    sharesShortPriorMonth: stats.sharesShortPriorMonth?.raw,
    shortRatio: stats.shortRatio?.raw,        // days to cover
    shortPercentOfFloat: stats.shortPercentOfFloat?.raw,
    floatShares: stats.floatShares?.raw,
    sharesOutstanding: stats.sharesOutstanding?.raw,
    avgVolume10d: summary.averageVolume10days?.raw,
    interpretation: (() => {
      const pct = (stats.shortPercentOfFloat?.raw || 0) * 100;
      if (pct < 5) return 'low';
      if (pct < 15) return 'moderate';
      if (pct < 25) return 'elevated';
      return 'high_short_interest';
    })(),
    ts: new Date().toISOString(),
  };
}

// ── Tool definitions ─────────────────────────────────────────────────────────
const TOOLS = [
  {
    name: 'get_realtime_quote',
    description: 'Real-time bid/ask quote for a ticker. Use for entry timing decisions. Tier 2 — do not use for historical prices or fundamentals.',
    inputSchema: {
      type: 'object',
      properties: { ticker: { type: 'string', description: 'Stock ticker symbol (e.g. AAPL)' } },
      required: ['ticker'],
    },
  },
  {
    name: 'get_vix',
    description: 'Current CBOE VIX level and interpretation (low_fear / moderate / elevated / high_fear / extreme_fear). Use for macro stress assessment.',
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'get_commodity',
    description: 'Commodity or index future quote. Use tickers like CL=F (crude), GC=F (gold), ^VIX (VIX), ^TNX (10yr yield).',
    inputSchema: {
      type: 'object',
      properties: { ticker: { type: 'string', description: 'Yahoo Finance commodity ticker (e.g. CL=F, GC=F)' } },
      required: ['ticker'],
    },
  },
  {
    name: 'get_options_chain',
    description: 'Options chain with put/call ratio and top 5 unusual open interest. Use for implied volatility and sentiment signals.',
    inputSchema: {
      type: 'object',
      properties: { ticker: { type: 'string', description: 'Stock ticker symbol' } },
      required: ['ticker'],
    },
  },
  {
    name: 'get_insider_transactions',
    description: 'Form 4 insider transactions — buys, sells, net insider activity. Use when Filing or Bear agent needs granular insider data.',
    inputSchema: {
      type: 'object',
      properties: { ticker: { type: 'string', description: 'Stock ticker symbol' } },
      required: ['ticker'],
    },
  },
  {
    name: 'get_short_interest',
    description: 'Short interest data — shares short, days to cover, short % of float. Use for bear case and squeeze risk assessment.',
    inputSchema: {
      type: 'object',
      properties: { ticker: { type: 'string', description: 'Stock ticker symbol' } },
      required: ['ticker'],
    },
  },
];

// ── MCP JSON-RPC 2.0 server ─────────────────────────────────────────────────
let buf = '';

process.stdin.setEncoding('utf8');
process.stdin.on('data', (chunk) => {
  buf += chunk;
  const lines = buf.split('\n');
  buf = lines.pop(); // keep incomplete line in buffer
  for (const line of lines) {
    if (!line.trim()) continue;
    try {
      handleMessage(JSON.parse(line));
    } catch (e) {
      send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error', data: e.message } });
    }
  }
});

process.stdin.on('end', () => process.exit(0));

function send(obj) {
  process.stdout.write(JSON.stringify(obj) + '\n');
}

async function handleMessage(msg) {
  const { id, method, params } = msg;

  switch (method) {
    case 'initialize':
      send({
        jsonrpc: '2.0', id,
        result: {
          protocolVersion: '2024-11-05',
          capabilities: { tools: {} },
          serverInfo: { name: 'yahoo-finance', version: '1.0.0' },
        },
      });
      break;

    case 'notifications/initialized':
      break; // no-op

    case 'tools/list':
      send({ jsonrpc: '2.0', id, result: { tools: TOOLS } });
      break;

    case 'tools/call': {
      const { name, arguments: args } = params;
      try {
        let result;
        switch (name) {
          case 'get_realtime_quote':        result = await getRealtimeQuote(args.ticker); break;
          case 'get_vix':                   result = await getVix(); break;
          case 'get_commodity':             result = await getCommodity(args.ticker); break;
          case 'get_options_chain':         result = await getOptionsChain(args.ticker); break;
          case 'get_insider_transactions':  result = await getInsiderTransactions(args.ticker); break;
          case 'get_short_interest':        result = await getShortInterest(args.ticker); break;
          default:
            send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Unknown tool: ${name}` } });
            return;
        }
        send({
          jsonrpc: '2.0', id,
          result: { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] },
        });
      } catch (err) {
        send({
          jsonrpc: '2.0', id,
          result: { content: [{ type: 'text', text: `Error: ${err.message}` }], isError: true },
        });
      }
      break;
    }

    default:
      if (id != null) {
        send({ jsonrpc: '2.0', id, error: { code: -32601, message: `Method not found: ${method}` } });
      }
  }
}
