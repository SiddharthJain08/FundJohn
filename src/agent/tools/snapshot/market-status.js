'use strict';

const { cacheGet, cacheSet } = require('../../../database/redis');

const FMP_BASE = 'https://financialmodelingprep.com/stable';
const TTL = 60;

async function get() {
  const cacheKey = 'market-status';
  const cached = await cacheGet(cacheKey).catch(() => null);
  if (cached) return cached;

  const apiKey = process.env.FMP_API_KEY;
  if (!apiKey) throw new Error('FMP_API_KEY not set');

  const url = `${FMP_BASE}/market-hours?apikey=${apiKey}`;
  const res = await fetch(url);
  if (!res.ok) {
    // Fallback: determine from ET time
    return fallbackMarketStatus();
  }

  const data = await res.json();
  const nyse = Array.isArray(data) ? data.find((m) => m.stockExchange === 'NYSE') : data;
  const result = {
    isTheStockMarketOpen: nyse?.isTheStockMarketOpen ?? false,
    exchange: 'NYSE',
    timestamp: new Date().toISOString(),
  };

  await cacheSet(cacheKey, result, TTL).catch(() => null);
  return result;
}

function fallbackMarketStatus() {
  const now = new Date();
  const et = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
  const day = et.getDay(); // 0=Sun, 6=Sat
  const hour = et.getHours();
  const min = et.getMinutes();
  const timeMin = hour * 60 + min;

  const isWeekday = day >= 1 && day <= 5;
  const isDuringHours = timeMin >= 570 && timeMin < 960; // 9:30-16:00
  return { isTheStockMarketOpen: isWeekday && isDuringHours, exchange: 'NYSE', timestamp: now.toISOString() };
}

module.exports = { get };
