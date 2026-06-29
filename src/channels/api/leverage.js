// src/channels/api/leverage.js
// Realized broker leverage from account market values. Pure; no I/O.
//   gross = (|long| + |short|) / equity  — TRUE exposure (the book is long/short)
//   net   = (long + short) / equity
// Returns nulls when equity is not positive (avoid div-by-zero / misleading values).
function realizedLeverage({ long_market_value, short_market_value, equity } = {}) {
  const lmv = Number(long_market_value) || 0;
  const smv = Number(short_market_value) || 0;
  const eq  = Number(equity) || 0;
  if (!(eq > 0)) return { gross: null, net: null };
  return { gross: (Math.abs(lmv) + Math.abs(smv)) / eq, net: (lmv + smv) / eq };
}
module.exports = { realizedLeverage };
