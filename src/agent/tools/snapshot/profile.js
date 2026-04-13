'use strict';

const { cacheGet, cacheSet } = require('../../../database/redis');

const FMP_BASE = 'https://financialmodelingprep.com/stable';
const TTL = 3600; // 1h cache for profile data

async function get(ticker) {
  const cacheKey = `profile:${ticker.toUpperCase()}`;
  const cached = await cacheGet(cacheKey).catch(() => null);
  if (cached) return cached;

  const apiKey = process.env.FMP_API_KEY;
  if (!apiKey) throw new Error('FMP_API_KEY not set');

  const url = `${FMP_BASE}/profile?symbol=${ticker}&apikey=${apiKey}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`FMP profile returned ${res.status}`);

  const data = await res.json();
  const profile = Array.isArray(data) ? data[0] : data;
  if (!profile) return null;

  const result = {
    ticker: profile.symbol,
    companyName: profile.companyName,
    sector: profile.sector,
    industry: profile.industry,
    exchangeShortName: profile.exchangeShortName,
    mktCap: profile.mktCap,
    description: profile.description,
    website: profile.website,
    ceo: profile.ceo,
    employees: profile.fullTimeEmployees,
  };

  await cacheSet(cacheKey, result, TTL).catch(() => null);
  return result;
}

module.exports = { get };
