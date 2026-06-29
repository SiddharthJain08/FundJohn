// src/channels/api/registry_sync.js
// Sync strategy_registry.status to a lifecycle target, with retries. Throws after
// the final attempt so the caller can GATE on it (registry-first): the manifest
// write only happens after this succeeds, so the engine's trade-gate
// (status='approved') and the dashboard's displayed manifest state cannot silently
// diverge. Idempotent (UPSERT/UPDATE on a stable key) so retries are safe.
async function syncRegistryStatus({ dbQuery, sid, targetStatus, actor, retries = 3, sleepMs = 250, sleep }) {
  const _sleep = sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  let lastErr;
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      if (targetStatus === 'approved') {
        await dbQuery(
          `INSERT INTO strategy_registry
              (id, name, implementation_path, status, approved_by, approved_at, tier, universe, signal_frequency)
           VALUES ($1, $1, $4, $2, $3, NOW(), 2, ARRAY['SP500'], 'daily')
           ON CONFLICT (id) DO UPDATE
              SET status      = $2,
                  approved_by = COALESCE(strategy_registry.approved_by, $3),
                  approved_at = COALESCE(strategy_registry.approved_at, NOW())`,
          [sid, targetStatus, actor, `src/strategies/implementations/${sid}.py`],
        );
      } else {
        await dbQuery(`UPDATE strategy_registry SET status = $2 WHERE id = $1`, [sid, targetStatus]);
      }
      return;
    } catch (e) {
      lastErr = e;
      if (attempt < retries) await _sleep(sleepMs);
    }
  }
  throw new Error(`registry sync failed after ${retries} attempts for ${sid}→${targetStatus}: ${lastErr && lastErr.message}`);
}
module.exports = { syncRegistryStatus };
