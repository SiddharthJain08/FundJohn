'use strict';

const { cacheGet, cacheSet } = require('../../../database/redis');

const FMP_BASE = 'https://financialmodelingprep.com/stable';
const TTL = 60; // 60s cache for quotes

async function get(ticker) {
  const cacheKey = `quote:${ticker.toUpperCase()}`;
  const cached = await cacheGet(cacheKey).catch(() => null);
  if (cached) return cached;

  const apiKey = process.env.FMP_API_KEY;
  if (!apiKey) throw new Error('FMP_API_KEY not set');

  const url = `${FMP_BASE}/quote?symbol=${ticker}&apikey=${apiKey}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`FMP quote returned ${res.status}`);

  const data = await res.json();
  const quote = Array.isArray(data) ? data[0] : data;
  if (!quote) return null;

  const result = {
    ticker: quote.symbol,
    price: quote.price,
    change: quote.change,
    changePct: quote.changesPercentage,
    volume: quote.volume,
    avgVolume: quote.avgVolume,
    marketCap: quote.marketCap,
    pe: quote.pe,
    eps: quote.eps,
    sharesOutstanding: quote.sharesOutstanding,
    fiftyTwoWeekHigh: quote.yearHigh,
    fiftyTwoWeekLow: quote.yearLow,
    timestamp: new Date().toISOString(),
  };

  await cacheSet(cacheKey, result, TTL).catch(() => null);
  return result;
}

module.exports = { get };
