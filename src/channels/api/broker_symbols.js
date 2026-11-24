'use strict';
// Broker symbol -> engine ticker normalisation for the dashboard positions
// handler. Alpaca reports crypto positions as BTCUSD and orders as BTC/USD;
// the engine (execution_signals.ticker, registry universes) uses BTC-USD.

function engineTicker(symbol, assetClass) {
  if (symbol === null || symbol === undefined) return null;
  const s = String(symbol);
  if (assetClass === 'crypto' || s.includes('/')) {
    const flat = s.replace(/[/-]/g, '').trim().toUpperCase();
    if (flat.endsWith('USD') && flat.length > 3) return `${flat.slice(0, -3)}-USD`;
    return s.trim().toUpperCase();
  }
  return s;
}

// Alpaca `position list` payload -> { engineTicker: {...} }.
function mapBrokerPositions(list, nav) {
  const out = {};
  for (const p of (Array.isArray(list) ? list : [])) {
    if (!p || !p.symbol) continue;
    const qty  = parseFloat(p.qty);
    const mv   = parseFloat(p.market_value);
    const avg  = parseFloat(p.avg_entry_price);
    const cur  = parseFloat(p.current_price);
    const plpc = parseFloat(p.unrealized_plpc);
    const upl  = parseFloat(p.unrealized_pl);
    out[engineTicker(p.symbol, p.asset_class)] = {
      qty:               Number.isFinite(qty)  ? qty  : null,
      market_value:      Number.isFinite(mv)   ? mv   : null,
      avg_entry_price:   Number.isFinite(avg)  ? avg  : null,
      current_price:     Number.isFinite(cur)  ? cur  : null,
      unrealized_plpc:   Number.isFinite(plpc) ? plpc : null,
      unrealized_pl:     Number.isFinite(upl)  ? upl  : null,
      size_pct: (Number.isFinite(mv) && nav && nav > 0) ? Math.abs(mv) / nav : null,
      side: Number.isFinite(qty) ? (qty >= 0 ? 'long' : 'short') : null,
    };
  }
  return out;
}

// Alpaca `order list --nested` payload -> { engineTicker: {stop,target,exiting} }.
function buildBrokerBrackets(orders) {
  const out = {};
  const absorbLeg = (o) => {
    if (!o || !o.symbol || o.status === 'pending_cancel') return;
    const key = engineTicker(o.symbol, o.asset_class);
    const leg = out[key] || (out[key] = { stop: null, target: null, exiting: null });
    const sp = parseFloat(o.stop_price);
    const lp = parseFloat(o.limit_price);
    // emex_ / ahsx_ are liquidations at ~current price, not take-profits.
    const coid = o.client_order_id || '';
    if (coid.startsWith('emex_') || coid.startsWith('ahsx_')) {
      if (Number.isFinite(lp)) leg.exiting = lp;
      return;
    }
    if (Number.isFinite(sp)) leg.stop = sp;
    if (Number.isFinite(lp)) leg.target = lp;
    for (const child of (o.legs || [])) absorbLeg(child);
  };
  for (const o of (Array.isArray(orders) ? orders : [])) absorbLeg(o);
  return out;
}

module.exports = { engineTicker, mapBrokerPositions, buildBrokerBrackets };
