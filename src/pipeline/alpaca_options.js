'use strict';

/**
 * alpaca_options.js — Phase 2.1 of alpaca-cli integration.
 *
 * Thin Node wrapper around `alpaca data option chain` so collector.js
 * (and any future caller) can pull broker-canonical option-chain
 * snapshots without going through the yfinance/Polygon HTTP plumbing.
 *
 * Default behavior is GATED OFF: collector.js still routes through
 * runOptionsYFinance / runOptions(Polygon) until the env flag
 *   OPTIONS_DATA_SOURCE=alpaca
 * is set. Reason: the alpha-preview CLI's greeks have been observed to
 * return zero for thinly-traded strikes, and it does not surface an
 * implied-volatility field (our parquet schema requires `iv`). When
 * the quality is validated against yfinance, flip the flag.
 *
 * This module's job is to be the SINGLE place where the Alpaca chain
 * payload is parsed → a flat row array matching the keys our existing
 * options_eod.parquet schema uses.
 */

const { runAlpaca } = require('../channels/api/alpaca_cli');

/** Decode an OCC option-symbol (e.g. "AAPL260429C00185000") into its parts.
 *  Returns { root, expiration:YYYY-MM-DD, optionType:'call'|'put', strike }
 *  or null if the symbol doesn't fit the OCC 21-char format. */
function decodeOccSymbol(occ) {
  if (typeof occ !== 'string' || occ.length < 15) return null;
  // OCC: <root>(YYMMDD)(C|P)(STRIKE×1000 8-digit)
  const m = occ.match(/^(.+?)(\d{6})([CP])(\d{8})$/);
  if (!m) return null;
  const [, root, dateStr, cp, strikeRaw] = m;
  const yy = parseInt(dateStr.slice(0, 2), 10);
  const mm = parseInt(dateStr.slice(2, 4), 10);
  const dd = parseInt(dateStr.slice(4, 6), 10);
  // OCC dates are 20YY (no contracts before 2010 in practice).
  const fullYear = 2000 + yy;
  const expiration = `${fullYear}-${String(mm).padStart(2, '0')}-${String(dd).padStart(2, '0')}`;
  const strike = parseInt(strikeRaw, 10) / 1000.0;
  return { root, expiration, optionType: cp === 'C' ? 'call' : 'put', strike };
}

function flattenSnapshot(occSymbol, snap) {
  const decoded = decodeOccSymbol(occSymbol);
  const lq = snap.latestQuote || {};
  const lt = snap.latestTrade || {};
  const g  = snap.greeks      || {};
  return {
    contract_symbol:  occSymbol,
    underlying:       decoded ? decoded.root       : null,
    expiration:       decoded ? decoded.expiration : null,
    option_type:      decoded ? decoded.optionType : null,
    strike:           decoded ? decoded.strike     : null,
    bid:              typeof lq.bp === 'number' ? lq.bp : null,
    ask:              typeof lq.ap === 'number' ? lq.ap : null,
    bid_size:         typeof lq.bs === 'number' ? lq.bs : null,
    ask_size:         typeof lq.as === 'number' ? lq.as : null,
    last_price:       typeof lt.p  === 'number' ? lt.p  : null,
    last_trade_at:    lt.t || null,
    delta:            typeof g.delta === 'number' ? g.delta : null,
    gamma:            typeof g.gamma === 'number' ? g.gamma : null,
    theta:            typeof g.theta === 'number' ? g.theta : null,
    vega:             typeof g.vega  === 'number' ? g.vega  : null,
    rho:              typeof g.rho   === 'number' ? g.rho   : null,
    // implied_volatility: not surfaced by the alpha CLI as of v0.0.9.
    // Caller must compute it from bid/ask + reference price if needed.
    implied_volatility: null,
    quote_at:         lq.t || null,
  };
}

/**
 * Pull the full option chain for a single underlying. Pages through
 * `next_page_token` until exhausted. Returns a flat array of rows
 * (one per contract).
 *
 * Options:
 *   {
 *     expirationGte / expirationLte:  date filters (YYYY-MM-DD)
 *     strikeGte     / strikeLte:      strike bounds
 *     optionType:                     'call' | 'put'
 *     limit:                          per-page snapshot cap (default 100)
 *     maxPages:                       safety sentinel (default 50)
 *   }
 */
async function runOptionsAlpaca(underlyingSymbol, opts = {}) {
  const {
    expirationGte, expirationLte, strikeGte, strikeLte, optionType,
    limit = 100, maxPages = 50,
  } = opts;

  const rows = [];
  let pageToken = null;
  for (let page = 0; page < maxPages; page++) {
    const args = ['data', 'option', 'chain',
                  '--underlying-symbol', underlyingSymbol,
                  '--limit', String(limit)];
    if (expirationGte) args.push('--expiration-date-gte', expirationGte);
    if (expirationLte) args.push('--expiration-date-lte', expirationLte);
    if (strikeGte)     args.push('--strike-price-gte',    String(strikeGte));
    if (strikeLte)     args.push('--strike-price-lte',    String(strikeLte));
    if (optionType)    args.push('--type',                optionType);
    if (pageToken)     args.push('--page-token',          pageToken);

    const r = await runAlpaca(args);
    if (!r.ok) {
      const e = new Error(r.error?.error || r.stderr || 'alpaca data option chain failed');
      e.cliError = r.error;
      throw e;
    }
    const payload = r.payload || {};
    const snapshots = payload.snapshots || {};
    for (const [occ, snap] of Object.entries(snapshots)) {
      rows.push(flattenSnapshot(occ, snap));
    }
    pageToken = payload.next_page_token || null;
    if (!pageToken) break;
  }
  return rows;
}

module.exports = { runOptionsAlpaca, decodeOccSymbol, flattenSnapshot };
