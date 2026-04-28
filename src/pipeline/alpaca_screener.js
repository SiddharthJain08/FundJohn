#!/usr/bin/env node
'use strict';

/**
 * alpaca_screener.js — Phase 2.2 of alpaca-cli integration.
 *
 * Pulls daily top-movers and most-actives from the Alpaca screener and
 * augments universe_config with the resulting symbol set, marking each
 * row source='alpaca_screener'. Idempotent — symbols already in the
 * universe are skipped, not duplicated. New rows land with active=false
 * so they're discoverable in the dashboard but don't immediately enter
 * any strategy's tradable universe (a separate promotion step decides
 * whether to flip active=true).
 *
 * Usage as a function:
 *   const { ingestScreenerCandidates } = require('./alpaca_screener');
 *   const { inserted, skipped } = await ingestScreenerCandidates({ topN: 50 });
 *
 * Usage as a CLI:
 *   node src/pipeline/alpaca_screener.js [--top 50] [--dry-run]
 */

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const { Client } = require('pg');
const { runAlpaca } = require('../channels/api/alpaca_cli');

async function fetchScreenerSymbols({ topN }) {
  const movers  = await runAlpaca(['data', 'screener', 'movers',
                                   '--top', String(topN)]);
  if (!movers.ok) throw new Error(`screener movers failed: ${movers.error?.error}`);

  const actives = await runAlpaca(['data', 'screener', 'most-actives',
                                   '--top', String(topN)]);
  if (!actives.ok) throw new Error(`screener most-actives failed: ${actives.error?.error}`);

  const symbols = new Set();
  // movers payload: {gainers: [{symbol, ...}], losers: [{symbol, ...}]}
  const m = movers.payload || {};
  for (const arr of [m.gainers || [], m.losers || []]) {
    for (const r of arr) if (r.symbol) symbols.add(r.symbol);
  }
  // most-actives payload: {most_actives: [{symbol, ...}]}
  const a = actives.payload || {};
  for (const r of a.most_actives || []) if (r.symbol) symbols.add(r.symbol);

  return Array.from(symbols);
}

async function ingestScreenerCandidates({ topN = 50, dryRun = false } = {}) {
  const symbols = await fetchScreenerSymbols({ topN });
  if (dryRun) {
    return { discovered: symbols.length, inserted: 0, skipped: symbols.length, dryRun: true, symbols };
  }
  if (!symbols.length) return { discovered: 0, inserted: 0, skipped: 0, symbols: [] };

  const client = new Client({ connectionString: process.env.POSTGRES_URI });
  await client.connect();
  let inserted = 0;
  let skipped  = 0;
  try {
    // ON CONFLICT DO NOTHING: symbols already in the universe stay untouched
    // (we never overwrite a manually curated row's metadata).
    for (const sym of symbols) {
      const r = await client.query(
        `INSERT INTO universe_config (ticker, source, active, added_at, category)
         VALUES ($1, 'alpaca_screener', false, NOW(), 'screener_candidate')
         ON CONFLICT (ticker) DO NOTHING
         RETURNING ticker`,
        [sym],
      );
      if (r.rowCount > 0) inserted++; else skipped++;
    }
  } finally {
    await client.end();
  }
  return { discovered: symbols.length, inserted, skipped, symbols };
}

if (require.main === module) {
  (async () => {
    const argv = process.argv.slice(2);
    const top = (() => {
      const i = argv.indexOf('--top');
      return i >= 0 ? parseInt(argv[i + 1], 10) : 50;
    })();
    const dryRun = argv.includes('--dry-run');
    try {
      const r = await ingestScreenerCandidates({ topN: top, dryRun });
      console.log(`[alpaca_screener] discovered=${r.discovered} inserted=${r.inserted} skipped=${r.skipped}`);
      if (dryRun) console.log(`[alpaca_screener] (dry-run) symbols: ${(r.symbols || []).join(', ')}`);
      process.exit(0);
    } catch (err) {
      console.error('[alpaca_screener] ERROR:', err.message);
      process.exit(1);
    }
  })();
}

module.exports = { ingestScreenerCandidates, fetchScreenerSymbols };
