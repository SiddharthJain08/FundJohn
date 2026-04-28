'use strict';

/**
 * data_tier_filter.js — Phase 5 of the Saturday brain.
 *
 * Decides which of three tiers a paperhunter-extracted strategy spec lands in,
 * based on the columns it needs vs. what the data stack actually has on disk
 * (already backfilled), can fetch (provider supports it), or can't reach at all.
 *
 *   Tier A  every required column is in data_coverage with sufficient
 *           backfill depth → eligible for synchronous strategycoder run today.
 *   Tier B  every required column is reachable via at least one provider in
 *           our stack but date-range coverage is incomplete →
 *           pushed to STAGING; the operator's Approve click in the dashboard
 *           triggers the data fetch + post-fetch promotion.
 *   Tier C  at least one required column has NO provider mapping in our
 *           stack → deferred paper note in the vault, surfaces in
 *           data_category_unlock_estimate as a future provider candidate.
 *
 * Provider priority is intentionally hard-coded here in fallback order, with
 * rate-limit-tier breaks (FMP+Polygon are the heavyweights at 300 / 100 RPM,
 * Yahoo is the free fallback, Alpha Vantage is last-resort because of its
 * 5/min, 25/day cap). The fallback_chain in preferences.json provides the
 * per-category routing; this module just consumes it.
 */

const fs   = require('fs');
const path = require('path');

const PREFERENCES_PATH = process.env.OPENCLAW_PREFERENCES_PATH ||
  path.join(__dirname, '..', '..', '..', 'workspaces', 'default', '.agents', 'user', 'preferences.json');

// Order matters: the first provider in this list that can supply a column wins
// when computing fetch routing for Tier-B. Mirrors the operator's stated
// priority (FMP > Massive/Polygon > Alpaca > Yahoo > EDGAR > Tavily).
// AlphaVantage was removed 2026-04-28 — its capabilities are covered by
// Polygon (technical indicators, sector data) and FMP (macro/economic
// calendars). Alpaca takes the broker-data slot above Yahoo.
const PROVIDER_PRIORITY_ORDER = [
  'fmp',
  'massive',
  'polygon',
  'alpaca',
  'yahoo',
  'sec_edgar',
  'tavily',
];

// Synonym map: paperhunter outputs free-form column names from paper text.
// Map them to the canonical column_name that appears in data_columns and the
// data_type that appears in data_coverage. Keep this conservative — entries
// that don't map are treated as Tier-C-eligible (deferred), which is the safer
// default than silently approximating.
const COLUMN_SYNONYMS = {
  // Price data — all roll up to 'prices' / 'data_columns:prices'
  'prices':            { col: 'prices',       data_type: 'prices' },
  'price':             { col: 'prices',       data_type: 'prices' },
  'close':             { col: 'prices',       data_type: 'prices' },
  'closing_prices':    { col: 'prices',       data_type: 'prices' },
  'ohlcv':             { col: 'prices',       data_type: 'prices' },
  'daily_prices':      { col: 'prices',       data_type: 'prices' },
  'returns':           { col: 'log_returns',  data_type: 'prices' },
  'log_returns':       { col: 'log_returns',  data_type: 'prices' },
  'realized_vol':      { col: 'realized_vol', data_type: 'prices' },
  'realized_volatility': { col: 'realized_vol', data_type: 'prices' },
  'volume':            { col: 'prices',       data_type: 'prices' },

  // Options
  'options':           { col: 'options_eod',  data_type: 'options' },
  'options_eod':       { col: 'options_eod',  data_type: 'options' },
  'options_chain':     { col: 'options_eod',  data_type: 'options' },
  'iv':                { col: 'options_eod',  data_type: 'options' },
  'implied_vol':       { col: 'options_eod',  data_type: 'options' },
  'implied_volatility':{ col: 'options_eod',  data_type: 'options' },
  'iv30':              { col: 'options_eod',  data_type: 'options' },
  'iv7':               { col: 'options_eod',  data_type: 'options' },
  'options_greeks':    { col: 'options_eod',  data_type: 'options' },

  // Fundamentals
  'fundamentals':      { col: 'financials',   data_type: 'fundamentals' },
  'financials':        { col: 'financials',   data_type: 'fundamentals' },
  'income_statement':  { col: 'financials',   data_type: 'fundamentals' },
  'balance_sheet':     { col: 'financials',   data_type: 'fundamentals' },
  'cash_flow':         { col: 'financials',   data_type: 'fundamentals' },
  'earnings':          { col: 'earnings',     data_type: 'fundamentals' },
  'eps':               { col: 'earnings',     data_type: 'fundamentals' },
  'earnings_calendar': { col: 'earnings',     data_type: 'fundamentals' },

  // Insider / filings
  'insider':           { col: 'insider',      data_type: 'insider' },
  'insider_transactions': { col: 'insider',   data_type: 'insider' },
  'form4':             { col: 'insider',      data_type: 'insider' },
  'form13f':           { col: 'insider',      data_type: 'insider' },

  // Macro
  'macro':             { col: 'macro',        data_type: 'macro' },
  'macroeconomic':     { col: 'macro',        data_type: 'macro' },
  'cpi':               { col: 'macro',        data_type: 'macro' },
  'fed_funds':         { col: 'macro',        data_type: 'macro' },
  'treasury':          { col: 'macro',        data_type: 'macro' },
};

function _normalizeColumnName(raw) {
  if (!raw) return null;
  // PaperHunter sometimes emits `required` as an array of strings ("prices"),
  // sometimes as an array of objects ({column: "prices", provider: "polygon",
  // already_in_ledger: true, ...}). Accept both shapes.
  let str = raw;
  let providerHint = null;
  let alreadyInLedger = null;
  if (typeof raw === 'object') {
    str = raw.column || raw.name || raw.field || raw.key || '';
    providerHint = raw.provider || raw.fallback || null;
    if (raw.already_in_ledger === true) alreadyInLedger = true;
  }
  const k = String(str).toLowerCase().trim().replace(/[\s-]+/g, '_').replace(/[^a-z0-9_]/g, '');
  if (!k) return null;
  const out = { rawKey: k, match: null, providerHint, alreadyInLedger };
  if (COLUMN_SYNONYMS[k]) out.match = COLUMN_SYNONYMS[k];
  return out;
}

/**
 * Read preferences.json (rate_limits + fallback_chain). Best-effort — falls
 * back to an empty object so we never crash the brain on a missing file.
 */
function _loadPreferences() {
  try {
    return JSON.parse(fs.readFileSync(PREFERENCES_PATH, 'utf8'));
  } catch (_) {
    return { rate_limits: {}, fallback_chain: {} };
  }
}

/**
 * Build the live capability map from data_columns + data_coverage + the
 * fallback_chain in preferences.json. Returned shape:
 *
 *   {
 *     today:   '2026-04-25',
 *     columns: {
 *       <canonical_col>: {
 *         providers:    ['polygon', 'yahoo'],   // ordered by priority
 *         data_type:    'prices',
 *         date_from:    '2016-04-10',
 *         date_to:      '2026-04-24',
 *         row_count:    null,                   // from data_columns when present
 *         ticker_count: 456,
 *         backfilled:   true,                   // has data_coverage rows + fresh
 *       },
 *       ...
 *     },
 *     fetchable_only: {
 *       <canonical_col>: { providers: ['fmp'], data_type: 'fundamentals' },
 *     },
 *     provider_priority: ['fmp','massive','polygon','yahoo',...],
 *   }
 *
 * `columns` are columns we both KNOW about (registered in data_columns) AND
 * have meaningful coverage rows for — these can be Tier-A-eligible.
 *
 * `fetchable_only` are columns we know about (data_columns row exists) but
 * have not backfilled yet — these are Tier-B candidates.
 */
async function buildCapabilityMap(dbQuery) {
  const prefs = _loadPreferences();
  const fallback = prefs.fallback_chain || {};

  // Columns we've registered (one row per known column → its source provider).
  // ::text casts the dates so downstream string ops don't have to handle JS
  // Date objects (which stringify ugly in reason traces).
  const colsRes = await dbQuery(
    `SELECT column_name, provider,
            min_date::text   AS min_date,
            max_date::text   AS max_date,
            row_count, ticker_count
       FROM data_columns`
  );
  // Aggregated coverage — gives us the backfilled date range per data_type.
  const covRes = await dbQuery(
    `SELECT data_type,
            MIN(date_from)::text AS min_from,
            MAX(date_to)::text   AS max_to,
            COUNT(*)::int        AS n_tickers
       FROM data_coverage
      GROUP BY data_type`
  );
  const coverageByType = {};
  for (const row of covRes.rows) {
    coverageByType[row.data_type] = {
      min_from: row.min_from,
      max_to:   row.max_to,
      n_tickers: row.n_tickers,
    };
  }

  // Resolve provider priority for a (column, data_type) using fallback_chain
  // first (operator-set routing), then the synonym data_type, then a single
  // direct provider mapping from data_columns.
  function _providersFor(canonicalCol, dataType, primaryProvider) {
    const ordered = [];
    const seen = new Set();
    const push = (p) => { if (p && !seen.has(p)) { seen.add(p); ordered.push(p); } };
    // 1) preferences.json fallback_chain entry by data_type.
    const chain = fallback[dataType] || fallback[canonicalCol] || [];
    for (const p of chain) push(p);
    // 2) the provider attached to data_columns row.
    push(primaryProvider);
    // 3) sort to global priority order so the chosen .providers[0] respects
    //    the operator's "FMP first" rule when multiple providers can supply.
    return ordered.sort((a, b) => {
      const ai = PROVIDER_PRIORITY_ORDER.indexOf(a);
      const bi = PROVIDER_PRIORITY_ORDER.indexOf(b);
      return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
    });
  }

  const columns = {};
  const fetchableOnly = {};
  for (const row of colsRes.rows) {
    // Find this column's data_type via the synonym map (lookup by canonical
    // name; if synonyms doesn't know it, fall back to the column name itself).
    const norm = _normalizeColumnName(row.column_name);
    const dataType = norm.match?.match?.data_type || norm.match?.data_type || row.column_name;
    const providers = _providersFor(row.column_name, dataType, row.provider);
    const cov = coverageByType[dataType];
    const entry = {
      providers,
      data_type:    dataType,
      date_from:    row.min_date || cov?.min_from || null,
      date_to:      row.max_date || cov?.max_to   || null,
      row_count:    row.row_count || null,
      ticker_count: row.ticker_count || cov?.n_tickers || null,
      backfilled:   !!cov,
    };
    if (cov) {
      columns[row.column_name] = entry;
    } else {
      // Registered but no coverage yet → reachable but not backfilled.
      fetchableOnly[row.column_name] = entry;
    }
  }
  return {
    today:    new Date().toISOString().slice(0, 10),
    columns,
    fetchable_only: fetchableOnly,
    provider_priority: PROVIDER_PRIORITY_ORDER,
    raw_coverage_by_type: coverageByType,
  };
}

/**
 * Tier a single hunter result. `hunterResult` is the JSON paperhunter writes
 * into research_candidates.hunter_result_json — at minimum we need
 * `data_requirements.required` (array of strings) and optionally
 * `min_lookback_required` (CALENDAR DAYS, per paperhunter contract) + the
 * parent paper's `published_date`.
 *
 * Returns:
 *   {
 *     tier: 'A' | 'B' | 'C',
 *     reasons: [...]                  // human-readable trace
 *     missing_columns: [...]          // names that triggered B or C
 *     provider_route: [               // what to fetch (only for B)
 *       { provider, column, data_type, gap_from, gap_to }
 *     ]
 *     unlock_provider_estimate: '...' // only for C
 *   }
 */
function tierCandidate(hunterResult, capabilityMap, opts = {}) {
  const minBackfillDepth = opts.minBackfillDepthDays ?? 30;
  const stalenessDays    = opts.stalenessDays        ?? 30;
  const today = new Date(capabilityMap.today + 'T00:00:00Z');

  const required = (hunterResult?.data_requirements?.required || [])
    .map(_normalizeColumnName)
    .filter(Boolean);
  if (required.length === 0) {
    // No declared data needs → Tier A by default. PaperHunter normally
    // fills `prices` as the minimum requirement, so this branch is the rare
    // case where an extraction yields no requirements at all.
    return { tier: 'A', reasons: ['no required columns declared'],
             missing_columns: [], provider_route: [] };
  }

  // Resolve required min lookback. Per paperhunter contract this is in
  // CALENDAR DAYS (504 = ~1.4y price-only, 756 = ~2y, 1260 = ~3.5y multi-year
  // fundamental). Default 1825 days (5y) if unspecified.
  const minLookbackDaysRaw = Number(hunterResult?.min_lookback_required);
  const minLookbackDays = Number.isFinite(minLookbackDaysRaw) && minLookbackDaysRaw > 0
    ? minLookbackDaysRaw
    : 1825;  // 5 years default
  const publishedDate = hunterResult?.published_date
    ? new Date(hunterResult.published_date)
    : today;
  const lookbackAnchor = new Date(publishedDate);
  lookbackAnchor.setDate(lookbackAnchor.getDate() - minLookbackDays);

  const reasons = [];
  const missing = [];
  const route   = [];
  let allBackfilled = true;
  let anyUnreachable = false;

  for (const reqRaw of required) {
    const colKey = reqRaw.match?.col;
    const dataType = reqRaw.match?.data_type;

    // Look up by canonical column name first, then the original key.
    const cap = (colKey && capabilityMap.columns[colKey])
              || capabilityMap.columns[reqRaw.rawKey];
    if (cap && cap.backfilled) {
      // Check freshness + depth.
      const dateTo   = cap.date_to   ? new Date(cap.date_to)   : null;
      const dateFrom = cap.date_from ? new Date(cap.date_from) : null;
      const fresh = dateTo && (today - dateTo) / 86400000 <= stalenessDays + minBackfillDepth;
      const deepEnough = dateFrom && dateFrom <= lookbackAnchor;
      if (fresh && deepEnough) {
        reasons.push(`${reqRaw.rawKey}: backfilled (${cap.date_from}→${cap.date_to}, providers ${cap.providers.join('/')})`);
        continue;
      }
      // Backfilled but insufficient → Tier-B (need to extend coverage).
      allBackfilled = false;
      reasons.push(`${reqRaw.rawKey}: backfilled but insufficient (${cap.date_from}→${cap.date_to}; needs ≥ ${lookbackAnchor.toISOString().slice(0,10)})`);
      route.push({
        provider:  cap.providers[0] || null,
        column:    colKey || reqRaw.rawKey,
        data_type: dataType,
        gap_from:  lookbackAnchor.toISOString().slice(0, 10),
        gap_to:    cap.date_from || today.toISOString().slice(0, 10),
      });
      missing.push(reqRaw.rawKey);
      continue;
    }

    // Not backfilled. Is it reachable from a provider in our stack?
    const fetch = (colKey && capabilityMap.fetchable_only[colKey])
               || capabilityMap.fetchable_only[reqRaw.rawKey];
    if (fetch) {
      allBackfilled = false;
      reasons.push(`${reqRaw.rawKey}: reachable but not backfilled (providers ${fetch.providers.join('/')})`);
      route.push({
        provider:  fetch.providers[0] || null,
        column:    colKey || reqRaw.rawKey,
        data_type: dataType,
        gap_from:  lookbackAnchor.toISOString().slice(0, 10),
        gap_to:    today.toISOString().slice(0, 10),
      });
      missing.push(reqRaw.rawKey);
      continue;
    }

    // Unknown column with no synonym match either — Tier C.
    anyUnreachable = true;
    reasons.push(`${reqRaw.rawKey}: NO provider in our stack supplies this`);
    missing.push(reqRaw.rawKey);
  }

  if (anyUnreachable) {
    return {
      tier: 'C',
      reasons,
      missing_columns: missing,
      provider_route: [],
      unlock_provider_estimate: missing.join(', ')
        + ' — no provider mapping in preferences.json/data_columns yet',
    };
  }
  if (allBackfilled) {
    return { tier: 'A', reasons, missing_columns: [], provider_route: [] };
  }
  return {
    tier: 'B',
    reasons,
    missing_columns: missing,
    provider_route: route,
  };
}

module.exports = {
  buildCapabilityMap,
  tierCandidate,
  PROVIDER_PRIORITY_ORDER,
  COLUMN_SYNONYMS,    // exported for tests + saturday_brain inspection
  _normalizeColumnName,
};
