// src/channels/api/strategy_drift.js
// Divergence between manifest trade-INTENT (state ∈ live/monitoring) and registry
// trade-REALITY (strategy_registry.status === 'approved', what the engine trades).
// Pure; no I/O. The dashboard shows manifest intent and flags where reality differs.
const LIVE_STATES = new Set(['live', 'monitoring']);

function classifyDrift(manifestState, registryStatus) {
  const intent  = LIVE_STATES.has(String(manifestState || '').toLowerCase());
  const trading = String(registryStatus || '').toLowerCase() === 'approved';
  if (intent === trading) return 'none';
  return intent ? 'shown_live_not_trading' : 'trading_not_shown';
}

function summarizeDrift(rows) {
  let shown_live_not_trading = 0, trading_not_shown = 0;
  for (const r of rows || []) {
    if (r.drift === 'shown_live_not_trading') shown_live_not_trading++;
    else if (r.drift === 'trading_not_shown') trading_not_shown++;
  }
  return { shown_live_not_trading, trading_not_shown, total: shown_live_not_trading + trading_not_shown };
}

module.exports = { classifyDrift, summarizeDrift };
