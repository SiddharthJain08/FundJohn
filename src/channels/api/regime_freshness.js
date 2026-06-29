// src/channels/api/regime_freshness.js
// The daily HMM block (date/stress_score/roro_score) in regime_latest.json can
// freeze (e.g. the 2026-06-08 operator resync) while the intraday block keeps
// updating. This flags that staleness so the UI greys the daily values instead
// of presenting frozen numbers as current. Pure; no I/O.
function regimeFreshness(reg, nowMs) {
  const out = { daily_date: null, daily_age_hours: null, daily_stale: false };
  if (!reg || typeof reg !== 'object') return out;
  const d = reg.date || null;
  out.daily_date = d;
  const dailyMs = d ? Date.parse(d + 'T00:00:00Z') : NaN;
  if (Number.isFinite(dailyMs) && Number.isFinite(nowMs)) {
    out.daily_age_hours = (nowMs - dailyMs) / 3600000;
  }
  const intra = reg.intraday_updated_at
    ? Date.parse(String(reg.intraday_updated_at).replace(' ', 'T')) : NaN;
  if (Number.isFinite(dailyMs) && Number.isFinite(intra)) {
    out.daily_stale = (intra - dailyMs) > 24 * 3600000;        // intraday >1d newer than daily
  } else if (Number.isFinite(out.daily_age_hours)) {
    out.daily_stale = out.daily_age_hours > 48;                // fallback: daily block >2d old
  }
  return out;
}
module.exports = { regimeFreshness };
