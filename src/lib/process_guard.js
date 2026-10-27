'use strict';
/**
 * process_guard.js — keep the scheduler process alive across an uncaught
 * exception (2026-10-27).
 *
 * WHY: at 19:00:06Z on 2026-10-27 a dependency `ws` client (the Discord
 * gateway shard nulls its `onerror` on destroy; a late handshake ECONNRESET
 * during an event-loop stall then had no listener) surfaced as an unhandled
 * 'error' event, Node exited 1, systemd restarted johnbot 20 s later and
 * node-cron re-registered AFTER the 15:00 ET tick — the day traded nothing.
 * A socket error must never cost a trading day: log it, alert, keep running.
 * A genuine crash loop (>= maxPerMinute in one minute) still exits so systemd
 * can restart cleanly — the guard trades one lost tick for a bounded number
 * of logged exceptions, never for silent corruption.
 */

function installUncaughtExceptionGuard({ log = console.error, alert = null,
                                         maxPerMinute = 5, exit = process.exit } = {}) {
  const state = { count: 0, windowStart: Date.now() };
  const handler = (err, origin) => {
    const now = Date.now();
    if (now - state.windowStart > 60_000) { state.count = 0; state.windowStart = now; }
    state.count += 1;
    const msg = (err && err.stack) || String(err);
    log(`[process_guard] UNCAUGHT EXCEPTION (${origin}) #${state.count}/min — process kept alive:\n${msg}`);
    if (alert) {
      try {
        const p = alert(`⚠️ uncaught exception (${origin}) — process kept alive: ` +
                        String((err && err.message) || err).slice(0, 300));
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch (_) { /* alerting must never throw inside the handler */ }
    }
    if (state.count >= maxPerMinute) {
      log(`[process_guard] ${state.count} uncaught exceptions within a minute — exiting for a clean restart`);
      exit(1);
    }
  };
  process.on('uncaughtException', handler);
  return { handler, state };
}

module.exports = { installUncaughtExceptionGuard };
