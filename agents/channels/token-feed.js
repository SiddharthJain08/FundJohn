'use strict';

/**
 * token-feed.js — Discord notifications for the Token Monitor and Pipeline Runner.
 *
 * Separate from operator-feed.js and trade-feed.js.
 * Routes to #botjohn-log for status/alerts, #alerts for critical events.
 *
 * Notification types:
 *   SESSION_START    — /run session activated
 *   SESSION_END      — session complete or ended
 *   SCAN_START       — new full-pipeline scan cycle beginning
 *   SCAN_COMPLETE    — scan cycle done
 *   TICKER_SIGNAL    — a GO signal was produced for a ticker
 *   BUDGET_ALERT     — cost threshold crossed (75%, 90%)
 *   BUDGET_HALT      — auto-halt triggered (95%)
 *   OPERATOR_HALT    — manual halt via /token-halt
 *   OPERATOR_RESUME  — manual resume via /token-resume
 *   SPEED_CHANGE     — /token-speed changed
 *   STATUS           — on-demand status report
 */

const RATE_LIMIT_MS = 2000;

const TYPE_CHANNEL_MAP = {
  SESSION_START:   'botjohn-log',
  SESSION_END:     'botjohn-log',
  SCAN_START:      'botjohn-log',
  SCAN_COMPLETE:   'botjohn-log',
  TICKER_SIGNAL:   'alerts',
  BUDGET_ALERT:    'alerts',
  BUDGET_HALT:     'alerts',
  OPERATOR_HALT:   'botjohn-log',
  OPERATOR_RESUME: 'botjohn-log',
  SPEED_CHANGE:    'botjohn-log',
  STATUS:          null,  // direct reply — no channel routing
};

class TokenFeed {
  constructor(channelMap = null, client = null) {
    this._channelMap = channelMap;
    this._client     = client;
    this._queue      = [];
    this._timer      = null;
    this._lastSent   = 0;
  }

  setChannelMap(channelMap, client) {
    this._channelMap = channelMap;
    this._client     = client;
  }

  _resolveChannel(type) {
    if (this._channelMap && this._client) {
      const key = TYPE_CHANNEL_MAP[type];
      if (key) {
        const ch = this._channelMap.getChannel(this._client, key);
        if (ch) return ch;
      }
    }
    return null;
  }

  // ── Notification builders ─────────────────────────────────────────────────

  sessionStart(tickers, durationHours, intervalMin, maxCostUSD) {
    const costStr = maxCostUSD ? ` | Cap: $${maxCostUSD}` : '';
    this._enqueue({
      type:    'SESSION_START',
      content: `🧮 **Pipeline session started** | Tickers: ${tickers.join(', ')} | Duration: ${durationHours}h | Interval: ${intervalMin}m${costStr}`,
    });
  }

  sessionEnd(scans, totalSpawns, totalTokens, estimatedCostUSD) {
    this._enqueue({
      type:    'SESSION_END',
      content: `🧮 **Pipeline session ended** | Scans: ${scans} | Spawns: ${totalSpawns} | Tokens: ${totalTokens.toLocaleString()} | Est. cost: $${estimatedCostUSD}`,
    });
  }

  scanStart(scanNum, tickers, timeRemainingMs) {
    const h = Math.floor(timeRemainingMs / 3_600_000);
    const m = Math.floor((timeRemainingMs % 3_600_000) / 60_000);
    this._enqueue({
      type:    'SCAN_START',
      content: `🔄 **Scan #${scanNum} starting** — ${tickers.join(', ')} | ${h}h ${m}m remaining in session`,
    });
  }

  scanComplete(scanNum, tickers, elapsed) {
    const elapsedStr = elapsed ? `${(elapsed / 1000 / 60).toFixed(1)}m` : '—';
    this._enqueue({
      type:    'SCAN_COMPLETE',
      content: `✅ **Scan #${scanNum} complete** — ${tickers.join(', ')} | Elapsed: ${elapsedStr}`,
    });
  }

  tickerSignal(ticker, signal, scanNum) {
    const icon = signal === 'GO' ? '🎯' : signal === 'BLOCKED' ? '🛑' : '⏳';
    this._enqueue({
      type:    'TICKER_SIGNAL',
      content: `${icon} **Signal: ${signal} — ${ticker}** (Scan #${scanNum}) — check #trade-reports for full report`,
    });
  }

  budgetAlert(threshold, costUSD, maxCostUSD) {
    this._enqueue({
      type:    'BUDGET_ALERT',
      content: `⚠️ **Token budget at ${threshold}%** — $${costUSD.toFixed(2)} of $${maxCostUSD} used — consider \`!john /token-speed slow\` or \`/token-halt\``,
    });
  }

  budgetHalt(reason, costUSD) {
    this._enqueue({
      type:    'BUDGET_HALT',
      content: `🛑 **AUTO-HALT: budget limit reached** — ${reason} | $${costUSD.toFixed(2)} spent | Use \`!john /token-resume\` to continue`,
    });
  }

  operatorHalt(reason) {
    this._enqueue({
      type:    'OPERATOR_HALT',
      content: `🛑 **Pipeline HALTED** by operator${reason ? ` — ${reason}` : ''} | Use \`!john /token-resume\` to continue`,
    });
  }

  operatorResume() {
    this._enqueue({
      type:    'OPERATOR_RESUME',
      content: `▶ **Pipeline RESUMED** — agents will resume spawning`,
    });
  }

  speedChange(newMultiplier, label) {
    this._enqueue({
      type:    'SPEED_CHANGE',
      content: `🧮 **Pipeline speed set to ${label}** (${newMultiplier}x) — inter-spawn throttle adjusted`,
    });
  }

  // ── Queue management ──────────────────────────────────────────────────────

  _enqueue(notification) {
    this._queue.push(notification);
    if (!this._timer) this._scheduleNext();
  }

  _scheduleNext() {
    const now  = Date.now();
    const wait = Math.max(0, this._lastSent + RATE_LIMIT_MS - now);
    this._timer = setTimeout(() => this._flush(), wait);
  }

  async _flush() {
    this._timer = null;
    if (this._queue.length === 0) return;

    const n      = this._queue.shift();
    this._lastSent = Date.now();
    const target = this._resolveChannel(n.type);

    if (target) {
      try {
        await target.send({ content: n.content });
      } catch (err) {
        console.error(`[TokenFeed] Send error (${n.type}):`, err.message);
      }
    } else {
      console.log(`[TokenFeed][${n.type}] ${n.content.replace(/\n/g, ' | ')}`);
    }

    if (this._queue.length > 0) this._scheduleNext();
  }
}

module.exports = TokenFeed;
