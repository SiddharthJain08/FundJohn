'use strict';

const { cacheGet, cacheSet } = require('../../../database/redis');

const FMP_BASE = 'https://financialmodelingprep.com/stable';
const TTL = 3600; // 1h cache

async function get(ticker) {
  const cacheKey = `earnings-calendar:${ticker.toUpperCase()}`;
  const cached = await cacheGet(cacheKey).catch(() => null);
  if (cached) return cached;

  const apiKey = process.env.FMP_API_KEY;
  if (!apiKey) throw new Error('FMP_API_KEY not set');

  const url = `${FMP_BASE}/earnings-surprises?symbol=${ticker}&apikey=${apiKey}`;
  const res = await fetch(url);
  if (!res.ok) throw new Error(`FMP earnings calendar returned ${res.status}`);

  const data = await res.json();
  if (!data || !data.length) return [];

  // Return upcoming earnings sorted ascending
  const today = new Date().toISOString().slice(0, 10);
  const upcoming = data
    .filter(e => e.date >= today)
    .sort((a, b) => a.date.localeCompare(b.date))
    .slice(0, 5);

  await cacheSet(cacheKey, upcoming, TTL).catch(() => null);
  return upcoming;
}

module.exports = { get };
